# Plan d'amélioration — Unified Search API

> Audit du code à `d45065e` (v0.1.0). Chaque point renvoie au fichier:ligne concerné.
> Les estimations de gain sont des ordres de grandeur, à confirmer par benchmark (cf. §7).

## 0. Contexte de déploiement (décidé)

Ces trois choix conditionnent tout le plan :

| Choix | Conséquence |
|---|---|
| **Un seul conteneur, un seul worker** | Pas de Redis, pas de multi-worker. L'état en mémoire (cache, rate limit, pub/sub SSE) est **correct** dans ce mode — c'est une simplification, pas une dette. La persistance se fait en SQLite (§6.1). |
| **Auth par clé API, activée** | Le mécanisme existe déjà ([config.py:29](app/config.py:29)) mais est désactivé par défaut. À activer et à enrichir : clés labellisées, quota et plafond de coût par clé (§6.2–6.4). |
| **Rerank : les deux options** | `lexical` (gratuit) et `llm` (meilleur) implémentés derrière la même interface, `llm` par défaut, bascule automatique sur `lexical` en cas d'échec (§4.2). |

Corollaire important : avec **un seul event loop**, tout ce qui le bloque gèle l'API entière.
Cela fait monter la priorité de la Phase 8 (§3.1, §3.4) — c'est le cœur de la performance ici,
puisqu'on ne peut pas compenser en ajoutant des workers.

---

## 1. Synthèse

L'architecture est saine : injection de dépendances propre, clients partagés sur `app.state`,
moteur de recherche testable sans réseau, séparation `core / services / api` cohérente.
Les problèmes sont concentrés sur **quatre axes** :

| Axe | Constat | Priorité |
|---|---|---|
| **Sécurité** | SSRF non mitigé sur `/v1/crawl`, **auth désactivée par défaut**, conteneur en root, SearXNG exposé | 🔴 P0 |
| **Correction** | 9 bugs réels (concurrence, fuites mémoire, races) | 🔴 P0 |
| **Performance** | Boucle d'événements bloquée, pas de compression, pas de dédup, pas de retry LLM | 🟠 P1 |
| **Fonctionnalités** | Manque `/extract`, `/map`, citations en recherche, persistance, observabilité | 🟡 P2 |

**Top 6 à faire en premier** (impact/effort maximal) :
1. Garde SSRF sur toutes les URLs crawlées (§2.1) — faille exploitable en l'état
2. **Auth activée par défaut + comparaison constante** (§2.13) — sans elle, n'importe qui dépense ta facture LLM
3. Sémaphore global dans le moteur de recherche (§2.2) — la concurrence explose en `c^depth`
4. Retry/timeout/fallback LLM (§4.1) — un seul 429 tue un job de 10 minutes
5. Middlewares ASGI purs + GZip + ORJSON (§3.1–3.3) — gain latence/bande passante immédiat
6. Dédup d'URLs + rerank avant crawl (§4.2) — la plus grosse économie de coût

---

## 2. Bugs et failles (P0)

### 2.1 🔴 SSRF sur `/v1/crawl`, `/v1/crawl/site` — **faille exploitable**

`app/models/crawl.py:13` accepte n'importe quelle `AnyHttpUrl`. Aucune validation en aval.
Un client peut donc faire lire au serveur :

```
POST /v1/crawl {"urls": ["http://169.254.169.254/latest/meta-data/iam/security-credentials/"]}
POST /v1/crawl {"urls": ["http://searxng:8080/config", "http://localhost:8000/v1/jobs/"]}
POST /v1/crawl {"urls": ["http://10.0.0.0/admin"]}
```

Le markdown de la page interne est renvoyé tel quel dans la réponse HTTP.

**Correctif** — nouveau module `app/core/net/guard.py` :
- Résoudre le DNS **avant** connexion, refuser toute IP dans les plages privées, loopback,
  link-local (`169.254/16`, `fd00::/8`, `::1`, `127/8`, `10/8`, `172.16/12`, `192.168/16`,
  `100.64/10` CGNAT, `0.0.0.0/8`)
- Refuser les schémas ≠ `http(s)` et les ports non standard (allow-list `80,443,8080` configurable)
- Rejouer la vérification sur chaque redirection (anti DNS-rebinding)
- Config : `CRAWL_ALLOW_PRIVATE_NETWORKS=false`, `CRAWL_DOMAIN_ALLOWLIST=`, `CRAWL_DOMAIN_DENYLIST=`
- Appliquer aussi au deep-crawl (les liens découverts ne sont pas validés aujourd'hui)

### 2.2 🔴 Sémaphore recréé à chaque niveau de récursion

`app/core/research/engine.py:158` — `asyncio.Semaphore(self._concurrency)` est instancié
**dans** `_deep_research`, donc chaque appel récursif obtient un budget neuf.

Concurrence réelle = `concurrency ^ depth`, pas `concurrency`.
Avec `concurrency=2, depth=3` → 8 branches simultanées, chacune lançant jusqu'à 5 crawls.

**Correctif** : un seul `asyncio.Semaphore` porté par `_RunState`, créé dans `run()`.

### 2.3 🔴 `crawl_max_concurrency` est une limite *par requête*, pas globale

`app/core/crawl/crawler.py:80` — un nouveau `MemoryAdaptiveDispatcher(max_session_permit=5)`
est créé à **chaque** appel de `crawl()`. Sous N requêtes concurrentes → N×5 sessions navigateur.
Combiné à §2.2, un seul job de recherche peut ouvrir des dizaines d'onglets Chromium.

**Correctif** : un `asyncio.Semaphore(max_concurrency)` au niveau du `CrawlerService`,
acquis autour de tout `arun`/`arun_many`. Dispatcher réutilisé plutôt que recréé.

### 2.4 🔴 Race sur `CrawlerService.start()`

`app/core/crawl/crawler.py:35-38` — le check `if not self._started` n'est pas atomique.
Deux premières requêtes simultanées démarrent deux pools de navigateurs.

**Correctif** : `asyncio.Lock` + double-check, ou démarrage explicite au `lifespan`.

### 2.5 🟠 Fuite mémoire du rate limiter

`app/api/middleware.py:23` — `_HITS: dict[str, deque]` est un `defaultdict` global jamais purgé.
Une IP vue une fois garde son entrée pour la durée du process. Sous trafic distribué (ou IPv6),
croissance non bornée.

**Correctif** : purge périodique des deques vides, ou réutiliser le `TTLCache` borné existant
(`app/core/cache.py`). En mono-worker, garder ces compteurs en mémoire est correct (§6.1) — il
manque seulement la borne.

### 2.6 🟠 `_evict()` n'est appelé qu'à la création d'un job

`app/jobs/manager.py:88` — si plus aucun job n'est créé, les résultats terminés
(rapports complets, jusqu'à 500 pages de markdown pour un site-crawl) restent en mémoire
indéfiniment malgré `JOB_RETENTION_SECONDS`.

**Correctif** : tâche de fond périodique lancée au `lifespan`.

### 2.7 🟠 File SSE non bornée + événements finaux perdus

`app/jobs/manager.py:158-170` :
- `asyncio.Queue()` sans `maxsize` → un client SSE lent fait grossir la file sans limite
- après le replay, `if job.terminal: return` peut couper la lecture alors que des événements
  publiés *pendant* le replay attendent dans la file → le client rate les derniers messages
- `job.progress` grossit sans borne (1 événement par page sur un site-crawl de 500 pages),
  et est rejoué intégralement à chaque nouvel abonné

**Correctif** : `asyncio.Queue(maxsize=N)` avec politique drop-oldest, `deque(maxlen=N)` pour
`progress`, et drainer la file avant de sortir sur `terminal`.

### 2.8 🟡 Comparaison de clé API non constante en temps

`app/api/deps.py:27` — `provided not in settings.api_key_set` fuit de l'information par timing.

**Correctif** : `hmac.compare_digest` sur chaque clé (`any(compare_digest(provided, k) for k in keys)`).

### 2.9 🟡 `zip(urls, results, strict=False)`

`app/core/crawl/crawler.py:92` — si crawl4ai renvoie moins de résultats ou dans un autre ordre,
l'appariement est silencieusement faux. Le `getattr(r, "url", url)` masque partiellement le
problème mais les URLs en échec peuvent être mal étiquetées.

**Correctif** : indexer les résultats par URL normalisée, et produire une entrée `success=False`
explicite pour toute URL demandée sans résultat.

### 2.10 🟡 Rapport final non borné en tokens

`app/core/research/engine.py:243` — `final_report_prompt(query, learnings)` inclut **tous** les
learnings sans `trim_to_tokens`. Sur un run `breadth=8, depth=3`, le prompt peut dépasser la
fenêtre de contexte → échec du run entier après 10 minutes de travail.

**Correctif** : budget de tokens sur les learnings (+ résumé hiérarchique si dépassement).

### 2.11 🟡 Le rapport passe par une sortie structurée

`app/core/research/schemas.py:41` — `ReportDraft.report_markdown` force un rapport de 3+ pages
à transiter dans un champ string JSON : surcoût de tokens (échappement), risque d'échec de
parsing, et **streaming impossible**. Par ailleurs `LLM_MAX_TOKENS=4096` plafonne le rapport à
~3 000 mots alors que le prompt demande « 3 pages ou plus » (`prompts.py:67`).

**Correctif** : utiliser `complete()`/`stream_complete()` pour le rapport, avec
`LLM_REPORT_MAX_TOKENS` dédié (défaut 8192).

### 2.12 🟡 Conteneur en root & SearXNG exposé

- `Dockerfile:9` — `USER root` sans retour à un utilisateur non privilégié → runtime en root
- `Dockerfile:2` — `FROM unclecode/crawl4ai:latest` non pinné → builds non reproductibles
- `Dockerfile:19` — `uv pip install .` ignore `uv.lock` → dépendances non figées
- `docker-compose.yml:16` — SearXNG publie `8080:8080` alors que l'API est son unique client
- `docker-compose.yml:12` — `depends_on` sans `condition: service_healthy`

**Correctif** : pin par digest, `--locked`, `USER appuser` final, retirer le mapping de ports
SearXNG, healthcheck sur SearXNG + `depends_on.condition`.

### 2.13 🔴 Auth et rate limiting désactivés par défaut

`app/config.py:29,34` — `auth_enabled=False` et `rate_limit_enabled=False` par défaut,
repris tels quels dans [.env.example](.env.example). Une instance déployée sans relecture
attentive du `.env` est **ouverte à tous**. Or `/v1/research` déclenche des dizaines d'appels
LLM facturés sur *tes* clés ([main.py:42](app/main.py:42)) : le spam ne coûte pas seulement de
la CPU, il coûte de l'argent réel.

**Correctif (Phase 7 — protection minimale)** :
- `AUTH_ENABLED=true` et `RATE_LIMIT_ENABLED=true` par défaut dans `.env.example`
- au démarrage, si `auth_enabled` est vrai et `api_key_set` est vide → **refuser de démarrer**
  avec un message clair (plutôt que d'accepter silencieusement toutes les requêtes)
- si `auth_enabled` est faux et `environment != "development"` → log `WARNING` visible
- comparaison en temps constant (§2.8)
- ne jamais logger la clé reçue, même tronquée

**Correctif (Phase 11 — protection fine)** : clés labellisées, quota par clé, plafond de coût,
rate limiting pondéré. Voir §6.2–6.4.

---

## 3. Performance — chemin chaud HTTP

### 3.1 Middlewares : `BaseHTTPMiddleware` → ASGI pur

`app/api/middleware.py:32,50` — les deux middlewares héritent de `BaseHTTPMiddleware`, qui
enveloppe chaque requête dans un task group anyio + memory stream. Coût par requête notable, et
interférence avec le backpressure des réponses **SSE** (`/v1/answer?stream`, `/v1/jobs/*/stream`).

**Correctif** : réécrire `RequestContextMiddleware` et `RateLimitMiddleware` en middlewares ASGI
purs (`async def __call__(self, scope, receive, send)`), en manipulant les headers dans le
`send` wrapper. *Gain estimé : latence p50 en baisse sensible sur les endpoints légers,
streaming plus fluide.*

### 3.2 Compression des réponses

Aucune compression aujourd'hui. Les payloads sont du markdown volumineux
(`CrawlResponse` de 50 pages, rapport de recherche, résultat de site-crawl).

**Correctif** : `GZipMiddleware(minimum_size=1024)` + exclusion des routes SSE (le `Content-Type`
`text/event-stream` doit rester non compressé pour préserver le flux incrémental).
*Gain estimé : 5–10× sur la bande passante des grosses réponses.*

### 3.3 Sérialisation JSON

FastAPI utilise `json` par défaut. Sur un `CrawlResponse` de plusieurs Mo, la sérialisation
domine le temps de réponse.

**Correctif** : `default_response_class=ORJSONResponse`, ajouter `orjson` aux dépendances.

### 3.4 tiktoken bloque la boucle d'événements

`app/core/llm/tokens.py:33` — `enc.encode(text)` est CPU-bound et synchrone. Sur une page de
200 kB de markdown, cela bloque l'event loop plusieurs dizaines de ms, × 5 pages × N branches.
Toutes les autres requêtes de l'API sont gelées pendant ce temps.

**Correctif** en deux temps :
1. Pré-tronquer par caractères avant d'encoder (`text[: max_tokens * 6]`) — élimine ~90 % du
   travail dans le cas nominal, sans changer le résultat
2. Passer le reste dans `asyncio.to_thread` (tiktoken libère le GIL en Rust)

### 3.5 Cache de recherche : pas de single-flight

`app/core/search/searxng.py:67` — N requêtes identiques concurrentes (cas classique du moteur de
recherche qui fanout) manquent toutes le cache et frappent toutes SearXNG.

**Correctif** : registre `dict[str, asyncio.Future]` des requêtes en vol ; les appelants suivants
attendent le même future. *Gain : effondrement des round-trips redondants pendant un run.*

Complément : le cache renvoie l'objet `SearchResponse` partagé — ajouter un `model_copy(deep=True)`
en lecture pour éviter toute pollution du cache par un appelant qui muterait un résultat.

### 3.6 Mono-worker assumé — ce que ça implique

`Dockerfile:29` — un seul worker uvicorn. **C'est le mode retenu** (§0), donc pas de
`--workers` à ajouter. Deux conséquences à garder en tête :

- **Tout ce qui bloque l'event loop gèle l'API entière.** Il n'y a pas de second worker pour
  absorber. C'est pourquoi §3.1 (middlewares ASGI) et §3.4 (tiktoken hors boucle) ne sont pas
  des micro-optimisations ici mais le cœur du sujet.
- **En contrepartie, l'état en mémoire est correct** : le cache, les compteurs de rate limit et
  le pub/sub SSE n'ont aucun problème de cohérence. Pas de Redis, pas de script Lua, pas de
  sérialisation inter-process. C'est une vraie économie de complexité.

Le seul vrai parallélisme utile reste celui de Chromium (plusieurs onglets, §2.3) et des appels
réseau — tous deux déjà asynchrones.

*Si un jour le besoin de scale apparaît* : monter les `CRAWL_MAX_CONCURRENCY` /
`RESEARCH_CONCURRENCY` avant d'envisager le multi-process, puis relire §6 — le passage
multi-worker demanderait de remplacer SQLite+mémoire par Redis. Hors périmètre aujourd'hui.

---

## 4. Performance & qualité — moteur de recherche

### 4.1 🔴 Aucun retry / timeout / fallback LLM

`app/core/llm/client.py:46,62` — `acompletion()` est appelé sans `num_retries`, sans `timeout`,
sans `fallbacks`. Conséquences :
- un 429 provider fait échouer un job de recherche entier
- une connexion qui pend bloque un job d'arrière-plan **indéfiniment**
  (`RESEARCH_SYNC_TIMEOUT` ne couvre que le chemin synchrone)

**Correctif** : passer par `litellm.Router`, qui expose exactement ce qu'il faut :

```python
router = litellm.Router(
    model_list=[...],                 # modèle principal + fallbacks
    num_retries=3,
    timeout=120, stream_timeout=30,
    retry_policy=RetryPolicy(RateLimitErrorRetries=3, TimeoutErrorRetries=2,
                             AuthenticationErrorRetries=0, BadRequestErrorRetries=1),
    allowed_fails_policy=AllowedFailsPolicy(RateLimitErrorAllowedFails=100),
)
```

Config associée : `LLM_FALLBACK_MODELS`, `LLM_TIMEOUT`, `LLM_MAX_RETRIES`.
Ajouter aussi un sémaphore global de concurrence LLM (`LLM_MAX_CONCURRENCY`) pour ne pas
saturer le quota provider.

### 4.2 🔴 Ni dédup d'URL ni sélection avant crawl

`app/core/research/engine.py:206` — `_gather_sources` est appelé par branche, sans ensemble
`visited` partagé. Les SERP de branches voisines se recoupent fortement → **les mêmes URLs sont
crawlées plusieurs fois par run**. Et les 5 résultats sont crawlés aveuglément, sans filtre de
pertinence : crawler une page coûte un onglet Chromium + ~2 000 tokens de contexte LLM, qu'elle
soit utile ou non.

#### a) Dédup d'URL par run

`visited: set[str]` porté par `_RunState`, sur URL **normalisée** : minuscules sur le host,
retrait du fragment, du slash final et des paramètres de tracking (`utm_*`, `fbclid`, `gclid`,
`ref`). Une URL déjà crawlée dans une autre branche est réutilisée, pas re-crawlée.

#### b) Rerank avant crawl — les deux implémentations

**Principe** : SearXNG renvoie déjà titre + snippet + domaine + date **gratuitement**, sans
crawl. On note les résultats sur cette base et on ne crawle que le top-k.

Effet de bord vertueux : puisqu'on filtre, on peut **élargir** le SERP sans surcoût de crawl.
On demande plus de résultats et on en crawle moins — meilleur rappel *et* coût inférieur.

```
RESEARCH_RESULTS_PER_QUERY=10   # était 5, en dur dans engine.py:38
RESEARCH_CRAWL_TOP_K=3          # on ne crawle que 3 pages
RESEARCH_RERANK=llm             # off | lexical | llm
RESEARCH_RERANK_MODEL=          # vide → LLM_MODEL ; mettre un modèle rapide/pas cher
```

Interface commune, dans `app/core/research/rerank.py` :

```python
class Reranker(Protocol):
    async def rank(self, goal: str, results: list[SearchResultItem],
                   top_k: int) -> list[SearchResultItem]: ...
```

Trois implémentations : `NoopReranker`, `LexicalReranker`, `LLMReranker`. Le moteur ne connaît
que le protocole — l'implémentation est choisie une fois au `lifespan` d'après la config.

**Option 1 — `LexicalReranker` (BM25)**

Score de recouvrement lexical entre l'objectif de recherche et `titre + snippet`, pondéré par
la rareté des mots (un mot rare pèse plus qu'un mot banal). Corpus = les résultats de cette
requête uniquement.

- ~30 lignes en pur Python, ou la dépendance `rank-bm25` — au choix
- Coût nul, latence en microsecondes, aucun appel réseau
- Signaux complémentaires à ajouter au score : malus sur les domaines agrégateurs/réseaux
  sociaux, bonus de fraîcheur quand `time_range` est demandé, malus sur les snippets très courts
- **Limite** : ne comprend pas le sens. « voiture électrique » vs « véhicule à batterie » = zéro
  mot commun, donc mauvaise note pour une page pourtant idéale.

**Option 2 — `LLMReranker`** *(défaut retenu)*

Un appel structuré par requête SERP, sur les titres + snippets uniquement.

```python
class RankedResults(BaseModel):
    keep: list[int] = Field(..., description="Indices des résultats à crawler, du plus "
                                             "pertinent au moins pertinent.")
    dropped_reason: dict[int, str] = Field(default_factory=dict)  # traçabilité / debug
```

- Comprend les synonymes, l'intention, et repère les pages parasites (listes de liens, pages
  produit déguisées, contenu daté)
- **Le coût s'autofinance** : l'appel voit ~10 titres+snippets (~600 tokens) ; éliminer 7 pages
  sur 10 économise ~14 000 tokens de contenu. Ce qu'il coûte réellement, c'est **la latence** —
  un aller-retour supplémentaire par requête SERP.
- Utiliser un modèle rapide et bon marché via `RESEARCH_RERANK_MODEL` : c'est une tâche de tri,
  pas de rédaction. Ne pas y mettre le modèle du rapport.
- **Dégradation gracieuse obligatoire** : timeout, erreur provider ou sortie invalide →
  bascule silencieuse sur `LexicalReranker`, jamais d'échec du run. C'est la raison principale
  pour laquelle on implémente quand même l'option lexicale.
- Le coût de ces appels doit être compté dans la comptabilité de §6.5.

**Mémoïsation** : mettre en cache la décision de rerank sur la clé
`hash(goal) + hash(urls triées)`. Le fanout du moteur produit des SERP qui se recoupent — inutile
de repayer le même arbitrage.

#### c) Dédup de contenu inter-sources

Deux sources qui republient la même dépêche = deux fois le même texte envoyé au LLM. Hash sur
shingles (n-grammes) pour détecter le quasi-doublon et n'en garder qu'un.

*Gain estimé : réduction significative du nombre de pages crawlées et de tokens consommés par
run, à qualité au moins égale — à confirmer sur le jeu de référence (§9, Phase 9).*

### 4.3 🟠 Le `RateLimiter` du dispatcher ajoute 1–3 s par page

`app/core/crawl/crawler.py:82` — `RateLimiter(base_delay=(1.0, 3.0))` s'applique même quand les
URLs sont sur des domaines **différents** (cas majoritaire en recherche).

**Correctif** : rendre le délai configurable, et n'appliquer un délai qu'en cas de collision de
domaine dans le lot. Config `CRAWL_RATE_LIMIT_BASE_DELAY`.

### 4.4 🟠 Étape de clarification = un aller-retour LLM sur chaque requête

`app/api/routes/research.py:56` — même en `interactive="auto"`, un appel LLM complet précède
tout travail utile.

**Correctif** : fusionner clarification et génération des SERP queries en un seul appel structuré
(`{needs_clarification, questions, queries}`), ou court-circuiter en `auto` selon une heuristique
de longueur/spécificité de la requête.

### 4.5 🟡 Duplication entre `answer()` et `stream_answer()`

`app/services/answer.py:43-155` — ~40 lignes dupliquées (recherche, crawl, construction du
contexte). Toute correction doit être faite deux fois.

**Correctif** : extraire `_prepare_context(req) -> tuple[list[Source], str]`, les deux méthodes
n'en gardent que la partie LLM.

### 4.6 🟡 Constantes de tuning non configurables

`engine.py:38-42` — `RESULTS_PER_QUERY`, `PER_PAGE_TOKEN_BUDGET`,
`PROCESS_CONTEXT_TOKEN_BUDGET`, `NUM_LEARNINGS`, `NUM_FOLLOWUPS` sont figés dans le code.

**Correctif** : les remonter dans `Settings` + surchargeables par requête.

---

## 5. Nouvelles fonctionnalités

### 5.1 `POST /v1/extract` — extraction structurée

Le manque le plus visible face aux alternatives (Firecrawl `/extract`, Exa).

```jsonc
// requête
{ "urls": ["https://..."],
  "schema": { "type": "object", "properties": { "price": {"type":"number"},
                                                "title": {"type":"string"} } },
  "mode": "llm" }        // "css" = JsonCssExtractionStrategy (gratuit, déterministe)
                         // "llm" = extraction par modèle (générique)
// réponse
{ "results": [ { "url": "...", "data": {...}, "success": true } ] }
```

crawl4ai fournit déjà `JsonCssExtractionStrategy` (mode `css`, sans coût LLM) ;
le mode `llm` réutilise `LLMClient.structured` avec un modèle Pydantic construit dynamiquement
depuis le JSON Schema fourni.

### 5.2 `POST /v1/map` — découverte d'URLs sans crawl complet

Réponse en ~1 s au lieu de plusieurs minutes : `sitemap.xml` + `robots.txt` + extraction des
liens d'une seule page. Sert de pré-étape à `/v1/crawl/site` (le client choisit ce qu'il crawle).

```jsonc
{ "url": "https://exemple.com", "search": "pricing", "limit": 200 }
→ { "links": ["https://exemple.com/pricing", ...], "count": 42, "source": "sitemap" }
```

### 5.3 Enrichir la sortie de crawl

crawl4ai calcule déjà des champs que le code **jette** (`crawler.py:163`) :

| Champ crawl4ai | Utilité | Statut |
|---|---|---|
| `markdown_with_citations` + `references_markdown` | markdown avec liens en notes de bas de page | ignoré |
| `screenshot` (base64) | aperçu visuel | non exposé |
| `pdf` (bytes) | archivage / rendu | non exposé |
| `cleaned_html` | pipelines aval | non exposé |
| `media` / tables | extraction de données | non exposé |
| `ssl_certificate`, `response_headers` | audit | non exposé |

**Correctif** : champ `formats: ["markdown", "html", "screenshot", "pdf", "links", "tables"]`
dans `CrawlRequest`, et n'inclure dans la réponse que ce qui est demandé.

### 5.4 Ingestion PDF

Une part importante des sources de recherche sérieuses sont des PDF (arXiv, rapports, normes).
Aujourd'hui crawl4ai sur une URL `.pdf` ne renvoie rien d'exploitable.

**Correctif** : détecter `Content-Type: application/pdf`, router vers `pypdf`/`pymupdf` →
markdown. Config `CRAWL_ENABLE_PDF=true`, `CRAWL_PDF_MAX_MB=25`.

### 5.5 Recherche : citations en ligne dans le rapport

`/v1/answer` produit des `[n]` cités ; `/v1/research` ne produit qu'une liste de sources en
annexe (`engine.py:248`) — impossible de vérifier une affirmation.

**Correctif** : porter l'index de source sur chaque learning (`Learning{text, source_urls}`),
et l'exiger dans le prompt de rapport final. Champ `citations` dans la réponse.

### 5.6 Recherche : streaming du rapport + filtres

- `stream=true` sur `/v1/research` : émettre les tokens du rapport final via SSE
  (débloqué par §2.11)
- `time_range` / `freshness` : `SearchRequest` le supporte déjà mais le moteur ne l'utilise
  jamais (`engine.py:207`)
- `include_domains` / `exclude_domains`

### 5.7 Cache et déduplication sur `/v1/answer`

Aucun cache aujourd'hui : deux fois la même question = deux fois le pipeline complet.
**Correctif** : cache TTL clé = hash(query, model, max_sources, langue), `ANSWER_CACHE_TTL=600`.

### 5.8 Webhooks de fin de job

`POST /v1/research {"background": true, "webhook_url": "https://..."}` → callback signé (HMAC)
à la complétion. Évite le polling. La cible du webhook passe par la même garde SSRF (§2.1).

### 5.9 `GET /health/ready`

`/health` renvoie `ok` même si SearXNG est injoignable et le navigateur mort.
**Correctif** : sonde de readiness qui ping SearXNG, vérifie l'état du pool navigateur et la
présence d'au moins une clé provider. `/health` reste la sonde de liveness.

### 5.10 `GET /v1/search`

L'endpoint est POST-only. Une variante GET permet le test au navigateur/curl, et surtout le
cache HTTP (`Cache-Control`, `ETag`) côté reverse proxy/CDN.

---

## 6. Persistance & protection de la facture (mono-conteneur)

> Redis est **hors périmètre** (§0). Il ne servirait qu'à partager l'état entre plusieurs
> process ; avec un seul worker, l'état en mémoire est déjà correct. Le seul manque réel est la
> **durabilité au redémarrage**, que SQLite couvre sans conteneur supplémentaire.

### 6.1 Persistance des jobs en SQLite

Aujourd'hui, redémarrer le conteneur perd tous les jobs, y compris les rapports terminés
(`app/jobs/manager.py:68`). Un rapport qui a coûté 10 minutes et quelques dizaines de centimes
disparaît, et le client qui interroge `/v1/jobs/{id}` reçoit un 404.

**Correctif** : `aiosqlite`, un fichier sur un volume Docker. Ce qui est persisté et ce qui ne
l'est pas :

| État | Décision | Pourquoi |
|---|---|---|
| Jobs + résultats | **SQLite** | Coûteux à recalculer, doit survivre au redémarrage |
| Événements de progression | **SQLite** (borné, cf. §2.7) | Permet le replay SSE après reprise |
| Pub/sub SSE temps réel | **mémoire** | Mono-process : les abonnés sont dans le même process |
| Cache de recherche | **mémoire** | TTL de 300 s — aucun intérêt à persister |
| Compteurs de rate limit | **mémoire** | Fenêtre de 60 s ; repartir de zéro au boot est acceptable |
| Compteurs de coût | **SQLite** | Un plafond journalier remis à zéro à chaque redémarrage ne protège rien |

Schéma minimal : `jobs(id, type, status, created_at, updated_at, result_json, error)` et
`job_events(job_id, seq, payload_json)`. Config `JOB_BACKEND=sqlite|memory` (défaut `sqlite`),
`JOB_DB_PATH=/srv/api/data/jobs.db`, volume `./data:/srv/api/data` dans `docker-compose.yml`.
La rétention `JOB_RETENTION_SECONDS` s'applique inchangée, avec un `VACUUM` périodique.

L'interface `JobStore` est déjà quasi en place — les docstrings de `manager.py` et `cache.py`
prévoyaient explicitement cette bascule.

### 6.2 Clés API labellisées

Format actuel : `API_KEYS=key1,key2` (`app/config.py:31`) — impossible de savoir *qui* consomme
quoi, ni de révoquer une clé sans les toucher toutes.

**Correctif** — format `label:clé`, rétrocompatible (une entrée sans `:` prend un label auto) :

```
API_KEYS=app-mobile:sk_a1b2...,script-perso:sk_c3d4...,test:sk_e5f6...
```

- Chaque requête authentifiée porte son label dans `request.state.api_key_label`
- Les logs, métriques et compteurs de coût sont **attribués par label** — jamais par clé
- La clé n'apparaît nulle part dans les logs, ni tronquée ni hachée
- Révocation = retirer la ligne du `.env` + redémarrage (ou rechargement via
  `get_settings.cache_clear()` sur un endpoint admin protégé)
- Génération : documenter `openssl rand -hex 32` dans le README, comme pour `SEARXNG_SECRET`

### 6.3 Rate limiting pondéré par coût

Un `/v1/research` coûte ~1 000× un `/v1/search` mais compte pour 1 hit
(`app/api/middleware.py:72`). Une limite de 120 req/min autorise donc 120 recherches profondes
par minute — largement de quoi vider un budget.

**Correctif** : token bucket pondéré par endpoint, en mémoire (correct en mono-worker).

| Endpoint | Poids |
|---|---|
| `/v1/search` | 1 |
| `/v1/map` | 2 |
| `/v1/crawl` | 5 |
| `/v1/answer`, `/v1/extract` | 10 |
| `/v1/crawl/site` | 25 |
| `/v1/research` | 100 |

- Clé du bucket = **label de clé API** quand l'auth est active, IP sinon
- Budget par clé configurable : `RATE_LIMIT_RPM` global, surchargeable par label
- Headers standards `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` + `Retry-After`

### 6.4 Disjoncteur de coût

Le rate limiting borne le *nombre* d'appels, pas la *dépense*. Une seule recherche
`breadth=10, depth=5` peut coûter plus que mille recherches simples.

**Correctif**, trois niveaux :
1. `RESEARCH_MAX_COST_USD` — plafond **par run**. Le moteur suit le coût cumulé et s'arrête
   proprement en rédigeant le rapport avec ce qu'il a déjà, plutôt que d'échouer
2. `DAILY_COST_CAP_USD` — plafond **par jour et par clé**, compteur en SQLite (§6.1).
   Dépassement → `429` avec un message explicite
3. `estimate_only: true` sur `/v1/research` — renvoie une estimation de coût et du nombre de
   pages **sans rien lancer**. Permet au client de confirmer avant de s'engager

### 6.5 Comptabilité tokens & coût

Prérequis de §6.4 : sans mesure, pas de plafond. litellm expose `usage` et le calcul de coût sur
chaque réponse — il suffit de les agréger le long d'un run.

```jsonc
{ "usage": { "prompt_tokens": 48210, "completion_tokens": 3120,
             "total_cost_usd": 0.021, "searches": 12, "pages_crawled": 27,
             "pages_skipped_by_rerank": 43 } }
```

C'est autant une **fonctionnalité produit** qu'une métrique : sans elle, impossible de savoir ce
que coûte un run avant de le lancer. À exposer dans les réponses `/v1/answer` et `/v1/research`,
et à agréger par label de clé pour §6.4.

---

## 7. Observabilité

### 7.1 Logs structurés

`app/main.py:24` — `logging.basicConfig(level=...)` : format texte par défaut, pas de
`request_id` dans les logs alors qu'il est calculé (`middleware.py:36`), pas de latence.

**Correctif** : formatter JSON, `contextvars` pour propager le `request_id` dans tout le call
tree, log d'accès avec méthode/route/status/durée. Config `LOG_FORMAT=json|text`.

### 7.2 Métriques Prometheus

`GET /metrics` : latence par route (histogramme), taux d'erreur, taux de hit du cache,
succès/échecs de crawl, jobs en cours, tokens et coût LLM **par label de clé** (§6.2),
pages écartées par le rerank, sessions navigateur actives.

Endpoint à protéger par l'auth ou à restreindre au réseau interne — il expose l'usage par client.

> La comptabilité tokens/coût elle-même est traitée en **§6.5** : elle conditionne le disjoncteur
> de §6.4 et l'arbitrage du rerank, donc elle est rattachée à la Phase 11 mais **livrée en
> avance, juste après la Phase 8** (cf. §12).

### 7.3 Tracing (optionnel)

OpenTelemetry sur FastAPI + httpx + litellm : la cascade search → crawl → LLM d'un run de
recherche devient lisible.

---

## 8. Tests, CI, DX

- **Couverture** : ajouter `pytest-cov`, seuil 80 %, `--cov-fail-under` en CI
- **mypy** est une dépendance dev mais **n'est jamais exécuté** en CI
  (`.github/workflows/ci.yml`) → l'ajouter en mode `strict` sur `app/`
- **Tests manquants** : SSRF (doit rejeter), limites de concurrence, races du job manager,
  single-flight du cache, budgets de tokens
- **Tests de charge** : `locust`/`k6` sur `/v1/search` et `/v1/answer` — indispensable pour
  chiffrer les gains des §3
- **Benchmarks de régression** : un job CI qui mesure p50/p95 sur un scénario figé
- **Dépendances** : figer via `uv.lock` dans le Dockerfile (`--locked`), Dependabot/Renovate
- **SDK client** : générer un client Python/TS depuis l'OpenAPI (`openapi-generator`) —
  gros gain d'adoption
- **Exemples OpenAPI** : `model_config = {"json_schema_extra": {"examples": [...]}}` sur les
  modèles de requête, pour un `/docs` directement utilisable

---

## 9. Roadmap par phases

Chaque phase est indépendamment livrable et testable.

### Phase 7 — Correction & sécurité (P0) · ~1–2 j — ✅ **livrée**
> Rien d'autre ne doit passer avant.

- [x] Garde SSRF + tests (§2.1) — `app/core/net/guard.py`, appliquée à `/v1/crawl`,
      `/v1/crawl/site`, aux liens découverts en deep-crawl et à l'URL d'atterrissage
      après redirection
- [x] Sémaphore global moteur (§2.2) + sémaphore global crawler (§2.3) — le permis
      moteur est relâché **avant** la récursion (sinon un sémaphore unique interbloque)
- [x] Lock sur `CrawlerService.start()` (§2.4)
- [x] Purge du rate limiter (§2.5), éviction périodique des jobs (§2.6)
- [x] Files SSE bornées + drain sur terminal (§2.7)
- [x] `hmac.compare_digest` (§2.8), appariement crawl par URL (§2.9) — un `arun` par
      URL remplace `arun_many`, l'appariement devient exact par construction
- [x] Budget de tokens sur le rapport (§2.10)
- [x] Durcissement Docker : pin par digest, `--locked`, user non-root, SearXNG non exposé (§2.12)
- [x] **Auth + rate limit activés par défaut, refus de démarrer si aucune clé** (§2.13)

Livré en plus, comme conséquence directe : **§4.3** (délai de crawl conditionné au
domaine). Abandonner `arun_many` retire aussi son `RateLimiter`, qui imposait 1–3 s à
chaque page ; `CRAWL_PER_HOST_DELAY` ne temporise plus que les collisions de domaine.
À retirer de la Phase 9.

**Critères d'acceptation** :
- un test prouve que `POST /v1/crawl` sur `169.254.169.254` renvoie 400
- un run `depth=3` n'ouvre jamais plus de `CRAWL_MAX_CONCURRENCY` sessions navigateur
- l'app refuse de démarrer avec `AUTH_ENABLED=true` et `API_KEYS=` vide
- une requête sans clé sur `/v1/research` renvoie 401

### Phase 8 — Performance du chemin chaud (P1) · ~2 j
- [ ] Middlewares ASGI purs (§3.1)
- [ ] GZip + ORJSON (§3.2, §3.3)
- [ ] tiktoken non bloquant (§3.4)
- [ ] Single-flight sur le cache de recherche (§3.5)
- [ ] Retry/timeout/fallback LLM via `litellm.Router` (§4.1)

**Critère d'acceptation** : benchmark avant/après sur `/v1/search` et `/v1/crawl` (p50, p95),
chiffres consignés dans le PR.

### Phase 9 — Qualité & coût de la recherche (P1) · ~3 j
- [ ] Dédup d'URL par run + normalisation (§4.2a)
- [ ] Interface `Reranker` + `NoopReranker` + `LexicalReranker` BM25 (§4.2b)
- [ ] `LLMReranker` avec bascule automatique sur lexical en cas d'échec (§4.2b)
- [ ] SERP élargi (`RESULTS_PER_QUERY=10`) + `CRAWL_TOP_K=3` (§4.2b)
- [ ] Mémoïsation des décisions de rerank (§4.2b)
- [ ] Dédup de contenu inter-sources (§4.2c)
- [ ] Rapport en texte libre + `LLM_REPORT_MAX_TOKENS` (§2.11)
- [ ] Citations en ligne dans le rapport (§5.5)
- [x] ~~Délai de crawl conditionné au domaine (§4.3)~~ — livré en Phase 7
- [ ] Fusion clarification/génération de queries (§4.4)
- [ ] Refactor `AnswerService` (§4.5), constantes en config (§4.6)

**Critère d'acceptation** : sur un jeu de 10 requêtes de référence, comparer les trois modes
`RESEARCH_RERANK=off|lexical|llm` sur trois axes — pages crawlées, coût USD, latence — à qualité
de rapport égale ou supérieure. Les chiffres décident du défaut à livrer.

> ⚠️ Cette phase a besoin de la comptabilité de coût (§6.5) pour être évaluée sérieusement.
> Livrer §6.5 seul en amont (c'est petit) plutôt que d'estimer à l'aveugle.

### Phase 10 — Nouveaux endpoints (P2) · ~3 j
- [ ] `POST /v1/extract` (§5.1)
- [ ] `POST /v1/map` (§5.2)
- [ ] Formats de sortie de crawl (§5.3)
- [ ] Ingestion PDF (§5.4)
- [ ] Streaming + filtres de recherche (§5.6)
- [ ] Cache `/v1/answer` (§5.7)
- [ ] `GET /health/ready` (§5.9), `GET /v1/search` (§5.10)

### Phase 11 — Persistance & protection de la facture (P2) · ~2 j
> Remplace l'ancienne phase « scale-out ». Redis retiré du périmètre (§0).

- [ ] Comptabilité tokens/coût par run et par clé (§6.5) — *à avancer avant la Phase 9*
- [ ] Job store SQLite + volume Docker (§6.1)
- [ ] Clés API labellisées (§6.2)
- [ ] Rate limiting pondéré par coût d'endpoint (§6.3)
- [ ] Disjoncteurs : plafond par run, plafond journalier, `estimate_only` (§6.4)
- [ ] Webhooks de fin de job (§5.8)

**Critères d'acceptation** : un redémarrage du conteneur préserve un rapport terminé ; une clé
qui atteint son plafond journalier reçoit un 429 explicite pendant qu'une autre continue de
fonctionner.

### Phase 12 — Observabilité (P2) · ~1–2 j
- [ ] Logs JSON avec `request_id` et label de clé (§7.1)
- [ ] `/metrics` Prometheus, protégé (§7.2)
- [ ] OpenTelemetry optionnel (§7.3)

### Phase 13 — DX & industrialisation · ~1–2 j
- [ ] mypy strict + couverture en CI (§8)
- [ ] Tests de charge et benchmarks de régression (§8)
- [ ] Exemples OpenAPI, SDK généré (§8)
- [ ] `API_REFERENCE.md` régénéré, README à jour

---

## 10. Architecture cible

Un seul conteneur, un seul worker. Le seul service externe reste SearXNG.

```
                    ┌────────────────────────────────────────────────┐
  client ─HTTP/SSE▶ │ ASGI: Auth(clé labellisée) → RateLimit(pondéré │
                    │   par coût) → Ctx/Headers → GZip → CORS        │
                    └──────────────────────┬─────────────────────────┘
                                           │
      ┌────────────┬────────────┬──────────┼──────────┬───────────┬──────────┐
   /search      /crawl       /extract    /map      /answer    /research    /jobs
      │            │            │          │          │           │          │
      ▼            ▼            ▼          ▼          ▼           ▼          ▼
┌──────────┐ ┌───────────────────────────────┐  ┌─────────────────────┐ ┌──────────┐
│ Searxng  │ │  CrawlerService               │  │  LLM Router         │ │ JobStore │
│ + single │ │  · garde SSRF                 │  │  · retry/fallback   │ │  SQLite  │
│   flight │ │  · sémaphore GLOBAL           │  │  · timeout          │ │ + pub/sub│
│ + cache  │ │  · PDF / screenshot / formats │  │  · disjoncteur coût │ │  mémoire │
└──────────┘ └───────────────────────────────┘  └─────────────────────┘ └──────────┘
      └────────────┴──────────────┬──────────────────────┘                    │
                                  ▼                                           │
                  ┌───────────────────────────────────┐                       │
                  │  DeepResearchEngine               │                       │
                  │  · sémaphore unique par run       │                       │
                  │  · set `visited` partagé          │                       │
                  │  · Reranker: llm → lexical (repli)│                       │
                  │  · learnings sourcés → citations  │                       │
                  │  · budgets de tokens partout      │                       │
                  └───────────────────────────────────┘                       │
                                  │                                           │
                                  ▼                                           ▼
              logs JSON · /metrics · coût par clé ─────────▶ compteurs SQLite
                                                             (plafond journalier)
```

**Mémoire vs disque** — la règle : ce qui est cher à recalculer ou doit résister au redémarrage
va en SQLite ; le reste (cache 300 s, fenêtres de rate limit, abonnés SSE) reste en mémoire, ce
qui est correct en mono-process. Détail en §6.1.

---

## 11. Indicateurs de succès

À mesurer avant la Phase 8 pour disposer d'une ligne de base :

| Indicateur | Comment | Objectif |
|---|---|---|
| p50/p95 `/v1/search` (cache chaud) | k6, 50 VU | ↓ après §3.1–3.3 |
| Débit `/v1/search` (req/s) | k6 | ↑ après middlewares ASGI |
| Octets transférés sur `/v1/crawl` | k6 | ↓ 5–10× (GZip) |
| **Blocage de l'event loop (p99)** | `asyncio` debug / py-spy | **< 50 ms** — critique en mono-worker |
| Pages crawlées par run de recherche | §6.5 | ↓ nettement (dédup + rerank) |
| Coût USD par run de recherche | §6.5 | mesuré, puis plafonné |
| Latence ajoutée par le rerank LLM | §6.5 | à arbitrer contre le coût économisé |
| Écart de qualité `llm` vs `lexical` | jeu de 10 requêtes de référence | décide du défaut livré |
| Sessions navigateur max simultanées | `/metrics` | ≤ `CRAWL_MAX_CONCURRENCY` |
| Taux d'échec des jobs de recherche | `/metrics` | < 1 % (retry LLM) |
| Jobs survivant à un redémarrage | test d'intégration | 100 % des jobs terminés |

---

## 12. Ordre d'exécution recommandé

```
Phase 7 (P0 : SSRF, auth, bugs de concurrence)
   └─▶ Phase 8 (perf HTTP + retry/timeout LLM)
          └─▶ §6.5 comptabilité de coût  ◀── petit lot, sorti de la Phase 11
                 ├─▶ Phase 9  (rerank, dédup, citations)   ─┐
                 └─▶ Phase 10 (extract, map, PDF, formats) ─┤
                        └─▶ Phase 11 (SQLite, quotas, plafonds) ─┴─▶ Phase 12 (observabilité)
                                                                        └─▶ Phase 13 (DX, docs)
```

Deux ajustements par rapport à la version précédente :

1. **§6.5 (comptabilité de coût) est sorti de la Phase 11 et remonté juste après la Phase 8.**
   C'est un petit lot, et sans lui la Phase 9 s'évalue à l'aveugle : impossible de comparer
   `lexical` et `llm` sans mesurer le coût et la latence de chacun.
2. **La protection est livrée en deux temps.** Le minimum vital — auth obligatoire + rate limit
   activé — est en Phase 7, tout de suite. Le raffinement — quotas par clé, pondération par
   coût, plafond journalier — attend la Phase 11. Tu n'es donc pas exposé entre les deux.
