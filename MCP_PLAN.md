# Phase 14 — Serveur MCP

> Objectif : exposer l'API à des agents IA via le Model Context Protocol, de façon
> qu'un agent choisisse **le bon outil**, ne **sature pas son contexte**, ne **brûle
> pas le budget**, et survive aux **opérations de plusieurs minutes**.
>
> État actuel : aucun code MCP dans le dépôt. Cette phase se pose sur les 13 phases
> livrées (auth labellisée, rate limit pondéré, plafonds de coût, job store SQLite,
> `/metrics`) — elle les **réutilise** plutôt que de les réimplémenter.

---

## 0. Décision d'architecture

**Le serveur MCP est monté dans le même process ASGI que l'API, sur `/mcp`.**

Pas un process séparé qui rappellerait l'API en HTTP. Raisons, dans l'ordre :

| | In-process (retenu) | Process séparé |
|---|---|---|
| Pool navigateur | un seul, borné par `CRAWL_MAX_CONCURRENCY` | deux pools → la borne globale ne borne plus rien |
| Cache SearXNG + single-flight | partagé | dupliqué, taux de hit divisé |
| Ledger de coût SQLite | un seul fichier, plafond journalier exact | deux écrivains sur la même base, ou plafond contourné |
| `JobManager` | les jobs MCP **sont** les jobs HTTP, streamables aux deux | jobs invisibles depuis l'autre côté |
| Latence | zéro saut | un aller-retour HTTP + resérialisation par appel |
| Déploiement | le conteneur unique du §0 d'`IMPROVEMENT_PLAN.md` reste unique | un service de plus |

Conséquence : les outils MCP appellent **les services** (`AnswerService`, `ExtractService`,
`DeepResearchEngine`, `CrawlerService`, `SearxngClient`) directement, jamais les routes.
Ce que les routes portent en plus des services — auth, plafond de coût, comptabilisation,
pondération du rate limit — doit être **réappliqué explicitement** côté MCP (§6). C'est
le principal risque de cette phase, et il est traité en premier.

### Dépendance

`mcp>=2.0` (dernière version publiée : `2.0.0`). Vérifié :

- pas présent dans `uv.lock` aujourd'hui, aucune dépendance transitive ne le tire ;
- `pydantic>=2.12` ✅ (2.13.4 verrouillé), `sse-starlette>=3.0` ✅ (3.4.4),
  `starlette>=0.27` ✅ (1.3.1), `python-multipart` ✅, `opentelemetry-api` ✅ ;
- ajoute `jsonschema`, `pyjwt[crypto]`, et **`httpx2`** — qui est httpx 2.x republié sous
  un nom de distribution distinct. Son module de premier niveau s'appelle `httpx2`, donc
  **aucun conflit** avec le `httpx==0.28.1` qu'utilisent `SearxngClient` et litellm.
  Coût réel : ~2 Mo d'image et une pile HTTP dormante. Acceptable ; à noter dans le
  Dockerfile pour que personne ne s'en étonne.

API v2 : `from mcp.server import MCPServer` (ex-`FastMCP` en 1.x), `mcp.streamable_http_app()`,
`Context` depuis `mcp.server.mcpserver`. Épingler `mcp>=2.0,<3` dans `pyproject.toml`.

---

## 1. Le problème, formulé

Un wrapper 1:1 des 9 endpoints donnerait un serveur MCP **médiocre**, pour quatre raisons
qui n'ont rien à voir avec le protocole :

1. **Confusion de sélection.** `search`, `answer`, `research` répondent tous à « trouve-moi
   des infos sur X », avec 3 ordres de grandeur de coût entre eux. Sans description qui
   dit *quand ne pas* les utiliser, un agent prend `research` pour « quelle heure est-il
   à Tokyo » — 100 points de rate limit et quelques dollars.
2. **Saturation de contexte.** `POST /v1/crawl` accepte 50 URLs et rend du Markdown
   complet : plusieurs Mo possibles, injectés tels quels dans la fenêtre du modèle. Les
   formats `screenshot` et `pdf` rendent du base64 — catastrophique. Un résultat d'outil
   MCP n'est pas une réponse HTTP : **il est facturé au token, dans le contexte de l'agent.**
3. **Boutons inutilisables.** `prune_threshold`, `word_count_threshold`, `content_filter`,
   `include_raw_markdown`, `exclude_external_links` : un agent ne sait pas les régler et
   les remplira au hasard. Ce sont des réglages d'instance, pas des paramètres d'outil.
4. **Opérations longues.** Un `/v1/research` prend des minutes. Un appel d'outil MCP
   synchrone qui bloque 8 minutes est un appel qui *timeout* chez la plupart des clients.

Le plan traite ces quatre points comme les livrables principaux ; le branchement du
protocole lui-même est la partie facile.

---

## 2. Surface d'outils

**9 outils**, groupés par coût. Le coût est écrit **dans la description**, en toutes
lettres, parce que c'est le seul endroit que l'agent lit avant de choisir.

### Palier 0 — gratuit, instantané

#### `web_search`
```
q: str
max_results: int = 8          (plafond MCP : 20, vs 100 en HTTP)
time_range: "day"|"week"|"month"|"year" | None
include_domains / exclude_domains: list[str] | None
language: str | None
```
> « Recherche web. Rend des titres, URLs et extraits — **pas** le contenu des pages.
> Gratuit et instantané. Utilise-le pour découvrir *quelles* pages lire, puis `fetch_url`
> pour les lire. N'utilise pas `answer` ou `deep_research` si tu veux juste une liste
> de liens. »

Annotations : `readOnlyHint: true`, `idempotentHint: true`, `openWorldHint: true`.

#### `map_site`
```
url: str
search: str | None            (filtre sous-chaîne)
limit: int = 100              (plafond MCP : 300)
```
> « Liste les URLs d'un site (sitemap.xml, robots.txt, ou les liens de la page).
> Gratuit, une seule requête HTTP. À faire **avant** `crawl_site` pour savoir ce
> que tu vas déclencher. »

### Palier 1 — coûte du temps navigateur, pas de LLM

#### `fetch_url`
```
urls: list[str]               (1..10 — plafond MCP, vs 50 en HTTP)
max_chars_per_page: int = 8000   (§3)
include_links: bool = False
```
> « Récupère le contenu de pages web en Markdown propre. Quelques secondes par page.
> Tronqué à `max_chars_per_page` ; le texte complet reste lisible via la ressource
> renvoyée. Pas d'appel LLM. »

Les réglages de nettoyage (`content_filter`, `prune_threshold`, `word_count_threshold`,
`include_raw_markdown`, `exclude_external_links`) **ne sont pas exposés** : ils prennent
les valeurs de configuration de l'instance. `formats` non plus — voir §3 pour
screenshot/pdf.

#### `crawl_site` — **opération longue** (§4)
```
url: str
max_pages: int = 25           (plafond MCP : 100, vs 500 en HTTP)
max_depth: int = 2
url_patterns: list[str] | None
```
> « Explore un site entier en suivant ses liens. **Minutes**, pas secondes. Lance un
> job : tu recevras un identifiant, pas le résultat. Pour une poignée d'URLs connues,
> `fetch_url` est bien plus rapide. »

Annotations : `readOnlyHint: true`, `openWorldHint: true`, `taskSupport: "optional"`.

### Palier 2 — dépense des crédits LLM

#### `answer`
```
query: str
max_sources: int = 5          (plafond MCP : 10)
language: str | None
```
> « Réponse sourcée en une passe : recherche + lecture des pages + **un** appel LLM.
> Quelques secondes, quelques centimes. Rend la réponse et ses sources numérotées.
> Utilise-le pour une question factuelle qui tient en un paragraphe. Pour un rapport
> approfondi, `deep_research`. »

`model` **non exposé** : laisser un agent choisir le modèle, c'est laisser un agent
choisir la facture. Réglage d'instance (`LLM_MODEL`).

#### `extract_structured`
```
urls: list[str]               (1..5 — plafond MCP, vs 25 en HTTP)
schema: dict                  (JSON Schema)
```
> « Extrait des champs typés depuis des pages, selon un JSON Schema. **Un appel LLM
> par URL.** Si tu veux juste le texte, `fetch_url` est gratuit. »

Le mode `css` n'est pas exposé — il demande un schéma crawl4ai qu'un agent n'a aucun
moyen d'écrire correctement à l'aveugle. Le mode `llm` est le seul générique.

### Palier 3 — dépense sérieusement

#### `deep_research_estimate`
```
query: str
breadth: int = 4 / depth: int = 2
```
> « Chiffre un `deep_research` **sans rien lancer** : nombre de pages, d'appels LLM,
> coût projeté en dollars. Gratuit et instantané. Appelle-le avant `deep_research`
> si la requête est large ou si tu n'es pas sûr. »

Outil séparé, et non un booléen `estimate_only` sur `deep_research`. Un drapeau qui
change la *forme* de la réponse (`status: "estimated"` sans rapport) est exactement le
genre de polymorphisme sur lequel un agent se trompe.

#### `deep_research` — **opération longue** (§4)
```
query: str
breadth: int = 4              (1..6 — plafond MCP, vs 10)
depth: int = 2                (1..3 — plafond MCP, vs 5)
answers: list[str] | None     (réponses aux questions de clarification, §5)
time_range / include_domains / exclude_domains
```
> « Rapport de recherche approfondie : recherches récursives, dizaines de pages lues,
> rapport cité. **Plusieurs minutes et plusieurs dollars par appel.** Lance un job.
> Peut d'abord demander des précisions. Chiffre-le avec `deep_research_estimate` si
> tu hésites. N'appelle pas cet outil pour une question à réponse courte — utilise
> `answer`. »

Annotations : `readOnlyHint: true`, `openWorldHint: true`, `taskSupport: "required"`.

#### `job_status`
```
job_id: str
include_result: bool = True
```
> « État et résultat d'un job lancé par `deep_research` ou `crawl_site`. »

Chemin de repli pour les clients qui n'implémentent pas les *tasks* MCP (§4).

### Ce qui n'est **pas** exposé, et pourquoi

| Écarté | Raison |
|---|---|
| `GET /v1/search` | même chose que `POST`, l'ETag n'a pas de sens en MCP |
| `formats: screenshot/pdf` | base64 dans le contexte — voir §3 pour le traitement |
| `model` sur tous les outils | l'agent choisirait la facture |
| `mode: "css"` sur extract | schéma crawl4ai non devinable |
| `webhook_url` | l'agent n'a pas d'endpoint à rappeler ; c'est une primitive serveur-à-serveur |
| `stream: true` sur answer | remplacé par les notifications de progression MCP (§4) |
| `DELETE /v1/jobs/{id}` | annulation exposée via `tasks/cancel`, pas comme outil |
| `/metrics`, `/health` | ressources MCP (§7), pas des outils |

---

## 3. Budget de contexte — le cœur du sujet

**Règle : aucun résultat d'outil ne dépasse `MCP_MAX_RESULT_TOKENS` (défaut 4000).**

C'est ce qui sépare « j'ai wrappé mon API » de « serveur MCP correct ». Un `fetch_url`
sur 10 pages de documentation, c'est 400 ko de Markdown ; injectés bruts, ils consomment
plus de contexte que tout le reste de la conversation.

### Mécanique

1. **Chaque outil produit son payload complet** (inchangé, via les services existants).
2. **Le payload complet est stocké** dans un `ArtifactStore` sur `app.state` et reçoit
   un identifiant.
3. **Le résultat renvoyé est budgété** :
   - `structuredContent` : les métadonnées complètes (URLs, statuts, compteurs, `usage`)
     — petites et toujours utiles ;
   - `content` : le texte tronqué, avec un marqueur **explicite** —
     `[tronqué : 8 000 des 47 231 caractères. Suite : search-api://artifact/a1b2c3/page/2]` ;
   - un bloc `resource_link` par artefact, pour que l'agent puisse aller chercher la
     suite **s'il en a besoin** au lieu de la recevoir d'office.
4. **La troncature est en tête**, jamais au milieu : le début d'une page contient le
   titre, le chapô et la structure — ce qui permet à l'agent de décider s'il veut lire
   le reste.

### Répartition entre pages

Un `fetch_url` sur 10 URLs ne donne pas 8 000 caractères × 10. Le budget est **global**
et réparti : `min(max_chars_per_page, budget_total / nombre_de_pages_réussies)`. Une
page qui échoue ne consomme pas sa part — elle est redistribuée.

### Screenshot et PDF

Jamais en ligne. Deux modes, selon `MCP_IMAGE_MODE` :

- `off` (défaut) — le format n'est pas proposé ;
- `resource` — l'image est un artefact, l'outil rend un `resource_link` que le client
  charge à la demande ;
- `inline` — bloc `ImageContent` pour les clients à vision, à activer en connaissance
  de cause.

### `ArtifactStore`

Petite structure sur `app.state`, dans l'esprit du reste du dépôt (borné, balayé,
jamais illimité) :

- TTL `MCP_ARTIFACT_TTL` (défaut 1800 s) et taille max `MCP_ARTIFACT_MAX_ITEMS`,
- réutilise `app.core.cache.TTLCache` si sa sémantique suffit, sinon un module jumeau,
- **en mémoire, pas en SQLite** : un artefact est cher à *transporter*, pas à recalculer,
  et il n'a pas à survivre à un redémarrage — la règle mémoire/disque du §6.1 d'`IMPROVEMENT_PLAN.md`
  le range du côté mémoire.

---

## 4. Opérations longues — tasks MCP sur le `JobManager`

`deep_research` et `crawl_site` durent des minutes. La spec MCP 2025-11-25 a exactement
la primitive qu'il faut : les **tasks**.

### Le pont

Le `JobManager` fait déjà tout le travail : création, exécution en tâche de fond,
progression via `emit`, statut terminal, annulation, persistance SQLite, réconciliation
au démarrage. La couche MCP n'ajoute pas de moteur de jobs — elle **traduit** :

| MCP | Existant |
|---|---|
| `tools/call` avec `task: {}` → `CreateTaskResult` | `jobs.create()` + `jobs.submit()` |
| `tasks/get` | `jobs.get(id).public()` |
| `tasks/cancel` | `jobs.cancel(id)` |
| `taskId` | l'`id` du job — **le même**, donc un job MCP reste inspectable via `GET /v1/jobs/{id}` et son SSE |
| `notifications/progress` | le callback `on_progress` de `DeepResearchEngine`, déjà branché sur `emit` |

Capacités déclarées :

```json
{"capabilities": {"tasks": {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}}}}
```

### Le repli, non négociable

Tous les clients n'implémentent pas les tasks. Si le client ne les a pas négociées,
`deep_research` :

1. crée le job de la même façon,
2. rend **immédiatement** `{job_id, status: "running", poll_with: "job_status"}` avec
   une description qui dit explicitement à l'agent de rappeler `job_status` et à quel
   rythme,
3. le résultat final arrive par `job_status`.

Un seul chemin de code sous les deux façades : c'est le même job, la même progression,
le seul écart est la manière dont le client apprend qu'il a fini.

### Progression

`ctx.report_progress(done, total, message)` alimenté par les `ResearchProgress` que le
moteur émet déjà (`stage`, `current_depth/total_depth`, `completed_queries/total_queries`).
Les jetons du rapport en streaming (`stage: "report_token"`) ne sont **pas** relayés : un
agent n'a pas besoin de voir le rapport se construire, il a besoin du rapport.

---

## 5. Clarification → elicitation

`/v1/research` peut répondre `status: "needs_clarification"` avec des questions. En HTTP,
le client rappelle avec `answers`. En MCP, il existe mieux : `elicitation/create` en mode
`form`, où le serveur demande au *client* de solliciter l'utilisateur.

- Si le client déclare la capacité `elicitation` : les questions deviennent un
  `requestedSchema` (un champ string requis par question, la question en `description`),
  et la réponse relance le run dans le **même appel d'outil**. L'agent ne voit qu'un
  `deep_research` qui a réussi.
- Sinon : les questions sont rendues comme résultat d'outil, avec pour consigne de
  rappeler `deep_research` avec `answers`. C'est le comportement HTTP actuel.
- `action: "decline"` / `"cancel"` de l'utilisateur → le run part sans clarification
  (comportement `interactive: false`), il ne reste pas bloqué.

L'heuristique `query_looks_specific` reste en amont : une requête déjà précise ne
déclenche ni appel LLM de clarification, ni elicitation.

---

## 6. Auth, rate limit, coût — les trous à boucher

Le montage ASGI fait passer `/mcp` par les middlewares de l'app (CORS → RequestContext →
RateLimit → Compression). Il **ne fait pas** passer par les dépendances de *routeur*.
Quatre écarts réels, tous à traiter dans le premier lot.

### 6.1 Authentification — `require_api_key` ne s'applique pas

C'est une dépendance de routeur (`APIRouter(dependencies=[Depends(require_api_key)])`).
Une app montée ne l'exécute jamais. **Sans correctif, `/mcp` est ouvert.**

Correctif : un `TokenVerifier` qui réutilise `matched_label` de `app/api/deps.py` —
comparaison en temps constant, même `API_KEYS`, mêmes labels :

```python
class ApiKeyVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        label = matched_label(token, get_settings().api_key_labels)
        if label is None:
            return None
        return AccessToken(token=token, client_id=label, scopes=["search-api"])
```

`client_id` porte le **label**, jamais la clé — comme partout ailleurs. Dans un outil,
`get_access_token().client_id` rend le label, c'est-à-dire exactement ce que
`get_api_key_label` rend côté HTTP. `AUTH_ENABLED=false` → label `anonymous`, cohérent
avec le reste.

Les clients MCP envoient `Authorization: Bearer <clé>` : les clés existantes fonctionnent
sans rien changer côté utilisateur.

### 6.2 Rate limit — mauvaise pondération

`path_weight` fait correspondre des **préfixes de chemin** (`/v1/research` → 100). Le
chemin est `/mcp` pour tous les outils → poids 1. Un `deep_research` via MCP coûterait
1 point au lieu de 100 : **le garde-fou budgétaire tombe.**

Le limiteur tourne avant le routage, donc avant de savoir quel outil est appelé.
Correctif en deux temps :

1. le middleware débite le poids de base (1) à l'entrée, comme aujourd'hui ;
2. la couche MCP, **avant d'exécuter l'outil**, débite le complément via un helper
   `charge(label, extra_weight)` exporté depuis `app/api/middleware.py` (même deque,
   même fenêtre glissante) ;
3. si le complément fait dépasser le budget, l'appel est refusé par un résultat d'outil
   en erreur qui **dit** le budget et le délai de reset — un agent peut agir là-dessus,
   un 429 opaque non.

Table de poids par outil, alignée sur `_WEIGHTS` : `web_search` 1, `map_site` 2,
`fetch_url` 5, `answer` 10, `extract_structured` 10, `crawl_site` 25, `deep_research` 100,
`deep_research_estimate` 1, `job_status` 1.

### 6.3 Plafond de coût journalier

`enforce_daily_cost_cap` est aussi une dépendance de route. Les trois outils dépensiers
(`answer`, `extract_structured`, `deep_research`) doivent l'appeler explicitement avec le
label du token, et clôturer par `record_usage(ledger, label, usage)` — y compris sur
échec, comme le font les routes.

**Point d'attention propre aux agents :** un humain lance un `deep_research` et attend.
Un agent en boucle peut en lancer vingt. Deux garde-fous supplémentaires :

- le coût projeté est affiché dans le résultat de `deep_research_estimate` **et** dans
  celui de `deep_research` (champ `usage`), pour que l'agent voie ce qu'il dépense ;
- `MCP_RESEARCH_MAX_CONCURRENT` (défaut 2) borne les runs simultanés **par label**, avec
  un refus explicite au-delà. Le plafond journalier est une limite de fin de course ;
  celle-ci empêche d'y arriver en trente secondes.

### 6.4 Sécurité de transport

`streamable_http_app()` ne répond par défaut qu'à `localhost`. En conteneur, l'hôte de
la requête n'est pas `localhost` → **toutes les requêtes seraient refusées**. Il faut
configurer `transport_security` avec une liste d'hôtes autorisés, exposée en
`MCP_ALLOWED_HOSTS` (défaut : la valeur de `SEARXNG_BASE_URL`-like côté API, sinon `*`
en développement uniquement).

Ne pas confondre avec la garde SSRF : elle continue de s'appliquer telle quelle à toute
URL crawlée depuis un outil MCP, sans une ligne de code en plus, puisque les outils
passent par `CrawlerService`.

### 6.5 CORS

`allow_headers=["*"]` couvre déjà les en-têtes `Mcp-*` en requête. Il manque
`expose_headers=["Mcp-Session-Id"]` : sans lui, un client MCP **navigateur** ne peut pas
lire son identifiant de session et ne peut pas maintenir de session. Une ligne dans
`app/main.py`.

### 6.6 Compression

Rien à faire, et c'est heureux : `CompressionMiddleware` décide sur le `Content-Type`
sortant et laisse `text/event-stream` intact. Le transport streamable HTTP bascule entre
JSON et SSE selon la réponse ; les deux sont déjà traités correctement.

---

## 7. Ressources et prompts

### Ressources

| URI | Contenu |
|---|---|
| `search-api://artifact/{id}` | payload complet d'un appel d'outil (§3) |
| `search-api://artifact/{id}/page/{n}` | une page précise d'un crawl multi-URL |
| `search-api://job/{id}` | statut + résultat d'un job |
| `search-api://capabilities` | limites effectives de l'instance |

`search-api://capabilities` mérite un mot : il rend les plafonds réels (URLs par appel,
budget de tokens, modèle par défaut, plafond journalier restant, breadth/depth max). Un
agent qui peut lire les contraintes de l'instance arrête de deviner — et arrête de
retenter des appels que le serveur refusera toujours.

### Prompts

Deux, pas plus, et seulement s'ils gagnent leur place :

- `research_brief` — transforme une demande floue en requête de recherche bien posée
  (breadth/depth suggérés, domaines à exclure). Évite le `deep_research` à 4 $ sur une
  requête d'une ligne.
- `verify_claim` — vérifie une affirmation contre des sources via `web_search` + `fetch_url`,
  sans passer par `answer`.

---

## 8. Configuration

Nouvelle section dans `.env.example` et `API_REFERENCE.md`, format identique au reste :

```bash
# ---- MCP ----
MCP_ENABLED=true
MCP_PATH=/mcp
# Hôtes acceptés par le transport streamable HTTP. En conteneur, l'hôte n'est pas
# localhost : sans cette liste, toutes les requêtes MCP sont refusées.
MCP_ALLOWED_HOSTS=
# Plafond de tokens d'UN résultat d'outil. C'est du contexte d'agent, pas de la
# bande passante : le dépassement se paie en tokens sur chaque tour suivant.
MCP_MAX_RESULT_TOKENS=4000
MCP_MAX_URLS_PER_CALL=10
MCP_ARTIFACT_TTL=1800
MCP_ARTIFACT_MAX_ITEMS=256
# off | resource | inline — les captures et PDF ne sont jamais mis en ligne par défaut.
MCP_IMAGE_MODE=off
MCP_TASKS_ENABLED=true
# Runs de recherche simultanés par clé. Le plafond journalier est une limite de fin
# de course ; celle-ci empêche une boucle d'agent d'y arriver en trente secondes.
MCP_RESEARCH_MAX_CONCURRENT=2
```

### Distribution

- **Streamable HTTP** (déploiement) : `https://<hôte>/mcp`, en-tête
  `Authorization: Bearer <clé>`.
- **stdio** (clients de bureau) : `python -m app.mcp` — même `MCPServer`, transport
  différent, sans serveur HTTP. Nécessaire parce qu'une partie des clients ne parle
  encore que stdio. Attention : en stdio le process porte son propre pool navigateur ;
  à documenter comme un mode « poste de travail », pas un mode serveur.
- Extraits `mcp.json` prêts à coller pour Claude Code et Claude Desktop, dans
  `MCP_REFERENCE.md`.

---

## 9. Tests et évaluation

Le dépôt tient un `mypy --strict` et une barrière de couverture à 80 % — la couche MCP
s'y plie.

| Test | Ce qu'il protège |
|---|---|
| Appel de chaque outil via une session client MCP en mémoire | le câblage |
| **Snapshot de `tools/list`** | un changement de schéma d'outil casse les agents en production silencieusement — ce test le rend visible en revue |
| Budget : crawl d'une page de 500 ko | `MCP_MAX_RESULT_TOKENS` jamais dépassé, marqueur de troncature présent, `resource_link` valide |
| Auth : sans clé / clé invalide | refus, et **aucune fuite** de la clé dans les logs |
| Pondération : `deep_research` × 2 avec `RATE_LIMIT_RPM=150` | le second est refusé (100 + 100 > 150) |
| Plafond de coût : label au-dessus du plafond | les trois outils dépensiers refusent |
| Tasks : `deep_research` → `tasks/get` → résultat | et la variante repli sans capacité tasks |
| Elicitation : run avec clarification | avec et sans capacité `elicitation` |
| Annulation : `tasks/cancel` | le job passe bien en terminal |

### L'éval de sélection d'outil

C'est ce qui distingue « correct » de « parfait », et ça ne se teste pas en unitaire.
Un jeu de ~15 requêtes avec l'outil attendu :

| Requête | Attendu | Piège |
|---|---|---|
| « quelle heure est-il à Tokyo » | `web_search` | `answer` / `deep_research` |
| « résume cet article <url> » | `fetch_url` | `answer` |
| « quelles sont les pages du site X » | `map_site` | `crawl_site` |
| « prix et stock de ces 3 produits » | `extract_structured` | `fetch_url` + interprétation |
| « HNSW vs IVFFlat, compromis ? » | `answer` | `deep_research` |
| « état du RAG en production en 2026 » | `deep_research` (après `estimate`) | `answer` |

À faire tourner à la main lors des changements de description d'outil. Un échec ne
signale pas un bug de code : il signale une description à réécrire. C'est le seul moyen
de vérifier que le §2 fait ce qu'il prétend.

### Observabilité

Le §7.2 existant s'étend naturellement : `mcp_tool_calls_total{tool,label,outcome}`,
`mcp_tool_duration_seconds{tool}`, `mcp_result_tokens{tool}` (pour vérifier que le budget
tient dans la vraie vie), `mcp_artifacts_stored`. Les traces OTel suivent, `/mcp` étant
dans la même app instrumentée.

---

## 10. Découpage

| Lot | Contenu | Effort |
|---|---|---|
| **14.1 — Socle & sécurité** | dépendance `mcp>=2.0` ; `app/mcp/server.py` monté sur `/mcp` ; `session_manager.run()` dans le lifespan ; `ApiKeyVerifier` (§6.1) ; `transport_security` (§6.4) ; `expose_headers` (§6.5) ; `charge()` + poids par outil (§6.2) ; `ArtifactStore` + budget (§3) ; les 3 outils du palier 0–1 (`web_search`, `map_site`, `fetch_url`) ; tests d'auth, de pondération et de budget | ~2 j |
| **14.2 — Outils dépensiers** | `answer`, `extract_structured`, `deep_research_estimate`, `deep_research` en synchrone ; plafond journalier + `record_usage` (§6.3) ; borne de concurrence par label | ~1,5 j |
| **14.3 — Opérations longues** | tasks MCP sur le `JobManager` (§4) ; `crawl_site` ; `job_status` ; progression ; annulation ; repli sans capacité tasks | ~2 j |
| **14.4 — Ergonomie agent** | elicitation (§5) ; ressources + `search-api://capabilities` (§7) ; prompts ; passe complète sur les descriptions et annotations d'outils | ~1,5 j |
| **14.5 — Distribution & éval** | transport stdio ; `MCP_REFERENCE.md` + `mcp.json` ; section `.env.example` ; métriques MCP ; jeu d'éval de sélection d'outil ; note Dockerfile sur `httpx2` | ~1,5 j |

**Total ≈ 8,5 j.** 14.1 est un préalable strict — livrer les outils avant l'auth
exposerait `/mcp` sans clé. Le reste peut glisser sans casser ce qui précède.

---

## 11. Risques

| Risque | Traitement |
|---|---|
| **`/mcp` ouvert sans authentification** — `require_api_key` est une dépendance de routeur, elle ne s'applique pas à une app montée | §6.1, premier lot, avec un test qui échoue si la protection saute |
| **Garde-fou budgétaire contourné** — `/mcp` reçoit le poids par défaut 1 au lieu de 100 | §6.2, même lot |
| **`mcp` 2.0 est récent** — API en mouvement, migration 1.x→2.x fraîche (`FastMCP` → `MCPServer`) | épingler `>=2.0,<3` ; la couche MCP est isolée dans `app/mcp/`, les services n'en savent rien ; toute la surface est testée |
| **Un client ne supporte ni tasks ni elicitation** | chemins de repli explicites en §4 et §5, testés tous les deux |
| **`httpx2` en plus de `httpx`** | namespaces distincts, vérifié ; ~2 Mo d'image |
| **Le mode stdio double le pool navigateur** | documenté comme mode poste de travail ; `CRAWL_MAX_CONCURRENCY` reste process-wide dans chaque process |
| **Descriptions d'outils qui dérivent** | snapshot de `tools/list` + jeu d'éval (§9) |

---

## 12. Fichiers touchés

```
app/mcp/__init__.py          nouveau   MCPServer, capacités, montage
app/mcp/server.py            nouveau   construction + auth + transport security
app/mcp/tools.py             nouveau   les 9 outils
app/mcp/budget.py            nouveau   troncature, marqueurs, resource_links (§3)
app/mcp/artifacts.py         nouveau   ArtifactStore borné + TTL
app/mcp/tasks.py             nouveau   pont tasks MCP ↔ JobManager (§4)
app/mcp/resources.py         nouveau   ressources + prompts (§7)
app/mcp/__main__.py          nouveau   transport stdio
app/main.py                  modifié   Mount, lifespan, expose_headers
app/api/middleware.py        modifié   charge() exporté (§6.2)
app/config.py                modifié   réglages MCP_*
pyproject.toml               modifié   mcp>=2.0,<3
.env.example                 modifié   section MCP
README.md                    modifié   endpoint + roadmap Phase 14
API_REFERENCE.md             modifié   renvoi vers MCP_REFERENCE.md
MCP_REFERENCE.md             nouveau   catalogue d'outils, mcp.json, limites
tests/test_mcp_*.py          nouveau   voir §9
evals/tool_selection.md      nouveau   jeu d'éval (§9)
```

Aucun fichier de `app/services/`, `app/core/` ni `app/models/` n'est modifié : la couche
MCP consomme les services tels quels. C'est le test de la décision du §0 — si un service
doit changer pour MCP, c'est que l'abstraction est au mauvais endroit.
