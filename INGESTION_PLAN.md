# Phase 15 — Ingestion documentaire (anydoc)

> Objectif : que l'API lise correctement les documents qu'elle rencontre déjà — et
> celles qu'elle laisse tomber en silence aujourd'hui. Quatorze formats bureautiques
> au lieu d'un seul, et des tableaux qui restent des tableaux.
>
> Bibliothèque : [`firecrawl/anydoc`](https://github.com/firecrawl/anydoc) — Rust,
> liaisons Python (`firecrawl-anydoc`), MIT. Word, PowerPoint, Excel, OpenDocument,
> RTF, EPUB, CSV, PDF → Markdown GFM.
>
> Indépendante de la Phase 14 (MCP) : les deux peuvent avancer en parallèle.

---

## 1. Le problème, mesuré

### 1.1 La voie PDF actuelle détruit les tableaux

`_pdf_to_markdown` ([crawler.py:520](app/core/crawl/crawler.py:520)) appelle
`page.extract_text()` de `pypdf` et concatène le résultat sous des titres `## Page n`.
Sur un PDF contenant un tableau, voici les deux sorties — même fichier, mesuré :

**anydoc**
```markdown
## Rapport trimestriel
## 1. Résultats

Le chiffre d'affaires progresse de 12 %.

|Région|T1|T2|
|---|---|---|
|EMEA|1 200|1 450|
|APAC|890|1 010|
```

**`pypdf` — ce que l'API produit aujourd'hui**
```
## Page 1

Rapport trimestriel
1. Résultats
Le chiffre d'affaires progresse de 12 %.
Région
T1
T2
EMEA
1 200
1 450
APAC
890
1 010
```

Le tableau devient une colonne de valeurs sans relation entre elles. Rien dans ce texte
ne dit que `1 450` est le T2 d'EMEA : un modèle qui lit ça **devine**, et il devine
souvent bien, ce qui est pire qu'une erreur franche.

Ce texte est exactement ce qui alimente `/v1/answer`, `/v1/research` et `/v1/extract`.
Et les tableaux sont exactement l'endroit où sont les chiffres. **Ce n'est pas un défaut
de confort, c'est une source de réponses fausses non détectables.**

### 1.2 Treize formats tombent en silence

`_looks_like_pdf_url` filtre sur le suffixe `.pdf` ou le segment `/pdf/`. Tout le reste
part vers Chromium :

| URL rencontrée | Aujourd'hui |
|---|---|
| `rapport-annuel.pdf` | ✅ texte plat, tableaux perdus (§1.1) |
| `rapport-annuel.docx` | ❌ le navigateur télécharge, la page est vide |
| `resultats-q3.xlsx` | ❌ idem |
| `presentation.pptx` | ❌ idem |
| `norme.odt`, `livre.epub`, `export.csv` | ❌ idem |

L'échec est **silencieux** : `success: true`, `markdown: ""`, `word_count: 0`. Un run de
recherche approfondie qui tombe sur trois rapports publics en `.docx` paie trois sessions
navigateur pour zéro contenu, et n'a aucun moyen de le signaler.

### 1.3 La détection par URL est fragile par construction

Le pré-filtre est un choix assumé et raisonnable (pas de HEAD sur chaque URL, cf. le
commentaire en place). Mais il rate les URLs sans extension — `/download?id=4021`,
`/api/documents/8812` — qui sont la norme sur les portails documentaires. Le format
n'est connaissable qu'après récupération.

---

## 2. Ce qu'apporte anydoc — et ce qu'il n'apporte pas

**Apporte :**

- **Un modèle de document partagé** pour les 14 formats, sérialisé par un seul rendu
  Markdown : titres avec ancres, gras/italique, listes imbriquées avec leur numérotation
  d'origine, tableaux avec cellules fusionnées, notes de bas de page, notes du
  présentateur (pptx). La sortie a la même forme quel que soit le format d'entrée.
- **Détection depuis les octets** (`format_from_bytes`) : en-tête PDF, groupe ouvrant RTF,
  noms de flux OLE, mimetype du paquet ZIP. Corrige §1.3.
- **Erreurs typées** : `UnsupportedError`, `MalformedError`, `EncryptedError`,
  `ResourceLimitError`, `MissingPartError`, toutes sous `ConvertError`.
- **Le GIL est relâché** pendant la conversion. Le `asyncio.to_thread` déjà en place
  autour de `pypdf` donne alors du **vrai** parallélisme au lieu de threads en contention
  — ce qui compte directement pour le KPI « blocage de l'event loop p99 < 50 ms » du
  mono-worker.
- **Pas de modèle ML, pas de service externe.** Cohérent avec le §0 d'`IMPROVEMENT_PLAN.md` :
  tout tourne dans le conteneur.

**N'apporte pas :**

- **Pas d'OCR.** Un PDF scanné (image sans couche texte) lève `UnsupportedError` et reste
  illisible. L'offre hostée Firecrawl Parse ajoute l'OCR ; la bibliothèque non. À
  documenter comme limite connue, pas à contourner.
- **Pas de gain de vitesse sur les petits fichiers.** Mesuré sur un PDF d'une page :
  anydoc 7,7 ms contre pypdf 1,3 ms. L'avantage annoncé porte sur les documents
  volumineux et complexes. **C'est la qualité qui justifie ce lot, pas la vitesse** — et
  l'écart en valeur absolue est négligeable devant une session navigateur.

---

## 3. Vérifications effectuées

Parseur en version 0.1.x nourri d'octets non fiables venus d'Internet : les affirmations
du README ont été testées plutôt que crues.

| Vérification | Résultat |
|---|---|
| **Bombe de décompression** — xlsx structurellement valide, 503 Ko compressés, ~500 Mo une fois gonflé | **0,25 s, RSS 44 Mo, contenu correct rendu.** Les limites internes tiennent ; la mémoire n'explose pas |
| Octets aléatoires, PDF tronqué, fichier vide | `UnsupportedError` / `MalformedError` propres, **aucun panic Rust traversant le FFI** |
| Détection de format depuis les octets (xlsx) | ✅ |
| CSV (sans signature) | nécessite de nommer le format — conforme à la doc |
| Roues disponibles | `manylinux` + `musllinux` × `aarch64` + `x86_64`, abi3-cp310 → **l'image Docker multi-arch est couverte**, pas de compilation Rust au build |
| Dépendances Python transitives | **aucune** |

Le point sur la bombe de décompression est le plus important : c'est l'attaque la plus
évidente contre un service qui télécharge des ZIP arbitraires (un `.xlsx`, un `.docx` et
un `.pptx` sont des ZIP). `CRAWL_PDF_MAX_MB` plafonne le **téléchargement**, pas la
**décompression** — c'est anydoc qui couvre ce second plafond, et il le couvre.

---

## 4. Intégration dans le crawler

Le cœur du lot. Trois évolutions dans `app/core/crawl/crawler.py`, dans l'ordre.

### 4.1 Généraliser la voie PDF en voie « document »

| Aujourd'hui | Après |
|---|---|
| `_looks_like_pdf_url` | `_looks_like_document_url` — liste de suffixes des 14 formats, `/pdf/` conservé |
| `_NotAPdf` | `_NotADocument` |
| `_PdfTooLarge` | `_DocumentTooLarge` |
| `_crawl_pdf` | `_crawl_document` |
| `_fetch_pdf` | `_fetch_document` |
| `_pdf_to_markdown` | `_document_to_markdown` |
| `enable_pdf` / `pdf_max_mb` | `enable_documents` / `doc_max_mb` |

La garde SSRF, le suivi manuel des redirections (chaque saut repasse par
`self._guard.check`), le plafond de taille appliqué **pendant** le streaming, et la prise
du sémaphore `self._sessions` — tout cela est correct et reste **strictement inchangé**.
Le lot change ce qui est accepté et comment c'est converti, pas la façon dont c'est
récupéré.

### 4.2 Accepter par content-type, confirmer par octets

`_fetch_pdf` rejette aujourd'hui tout `content-type != "application/pdf"`
([crawler.py:476](app/core/crawl/crawler.py:476)). Remplacé par :

1. **Liste d'acceptation de content-types** — les MIME OOXML, OpenDocument,
   `application/rtf`, `application/epub+zip`, `text/csv`, `application/pdf`, plus les
   génériques `application/octet-stream` et `application/x-download` que servent beaucoup
   de portails.
2. **Confirmation sur les octets** après récupération : `anydoc.format_from_bytes(data)`.
   Si elle rend `None` **et** que le content-type ne nommait pas un format connu, on lève
   `_NotADocument` et le navigateur reprend la main — exactement la sémantique de repli
   actuelle.

Le content-type devient une **indication** ; les octets font foi. C'est ce qui corrige
§1.3 sans ajouter le moindre aller-retour réseau.

### 4.3 La seconde chance

Ce qui ferme le cas des URLs sans extension, gratuitement : quand le crawl navigateur
d'une URL rend une page **vide** (`success: true`, `word_count == 0`) et que la voie
document n'a pas été essayée, retenter une fois en voie document.

Ne se déclenche que sur un échec avéré, donc coût nul sur le chemin nominal. Couvre
`/download?id=4021` et les portails qui servent un `.docx` sans extension.

### 4.4 Convertir hors de l'event loop

`_document_to_markdown` reste appelé via `asyncio.to_thread`, comme `pypdf` aujourd'hui.
Le commentaire en place — « pypdf is CPU-bound and this process has a single event loop »
— reste vrai pour anydoc, à ceci près que le GIL relâché fait que le thread travaille
réellement en parallèle.

### 4.5 Tracer la provenance

Champ optionnel `source_format: str | None` sur `CrawledPage` (`"html"`, `"pdf"`,
`"docx"`, …). Additif, donc non cassant. Deux usages concrets :

- un agent MCP sait s'il lit une page web ou un tableur, ce qui change la confiance qu'il
  accorde à la structure ;
- `metrics.record_crawled_pages` peut ventiler par format et rendre visible la part de
  documents bureautiques dans les runs de recherche — donnée qu'on n'a pas aujourd'hui.

---

## 5. Nouvel endpoint `POST /v1/convert`

Envoi direct d'un fichier → Markdown. `python-multipart` est déjà une dépendance ; il n'y
a rien à ajouter.

```
POST /v1/convert          (multipart/form-data)
  file: UploadFile
  format: str | None      (nomme le format ; requis pour le CSV, sans signature)
→ { "markdown": str, "format": str, "title": str | None, "word_count": int }
```

- Plafond de taille : `CONVERT_MAX_MB`, appliqué **avant** de lire le corps en mémoire.
- Poids de rate limit : **2** (pas d'appel LLM, pas de session navigateur ; comparable à
  `/v1/map`).
- Pas de garde SSRF : rien n'est récupéré côté serveur, l'octet vient du client. C'est le
  seul endpoint dans ce cas, à signaler explicitement en revue.
- Les `ConvertError` deviennent des **400** avec le nom du type d'erreur en `detail`
  (`EncryptedError` → « document protégé par mot de passe »), pas des 500 : un fichier
  illisible est une erreur du client, pas du serveur.

---

## 6. Effets sur le reste de l'API

| Surface | Effet | Code à écrire |
|---|---|---|
| `/v1/crawl` | lit 14 formats, tableaux préservés | §4 |
| `/v1/crawl/site` | idem, un site de documentation qui mêle HTML et PDF devient exploitable | aucun |
| `/v1/extract` | extraction structurée depuis un `.xlsx` ou un `.docx` | **aucun** — passe par `CrawlerService` |
| `/v1/answer` | moins de sources muettes, tableaux lisibles | aucun |
| `/v1/research` | le gain le plus fort : rapports publics, dépôts réglementaires, décks | aucun |
| **MCP (Phase 14)** | `fetch_url` gère les documents de façon transparente ; un outil `convert_document` s'ajoute au palier 1 | ~30 lignes |

Sur le MCP, une précision qui évite un contresens : anydoc se distribue **aussi** comme
Agent Skill (`npx skills add firecrawl/anydoc`), mais celle-ci ne convertit que les
fichiers **locaux** de l'agent, via la CLI. La valeur côté serveur est de lire les
documents **distants** que l'agent ne peut pas atteindre — derrière la garde SSRF, avec
les plafonds de coût et le pool navigateur borné. Les deux ne se recouvrent pas.

---

## 7. Configuration

```bash
# ---- Ingestion documentaire ----
# Word, PowerPoint, Excel, OpenDocument, RTF, EPUB, CSV et PDF sont convertis en
# Markdown avant d'atteindre le navigateur. Coupe l'ensemble de la voie document :
# les URLs concernées repartent vers Chromium (qui n'en tirera rien).
CRAWL_ENABLE_DOCUMENTS=true
# Remplace CRAWL_PDF_MAX_MB, qui reste accepté comme alias pendant une version.
# Plafonne le TÉLÉCHARGEMENT. La décompression a ses propres limites, internes à
# anydoc (une bombe zip de 500 Mo gonflés est refusée sans exploser la mémoire).
CRAWL_DOC_MAX_MB=25
# Repli sur pypdf quand anydoc échoue sur un PDF. À laisser actif tant qu'anydoc
# est en 0.1.x — voir §8.
CRAWL_PDF_FALLBACK=true
# Taille max d'un fichier envoyé à POST /v1/convert.
CONVERT_MAX_MB=25
```

`CRAWL_ENABLE_PDF` / `CRAWL_PDF_MAX_MB` continuent de fonctionner en alias pendant une
version, avec un avertissement au démarrage. Ils ne sont pas supprimés dans ce lot :
c'est de la configuration déjà déployée.

---

## 8. Risques et stratégie de repli

| Risque | Gravité | Traitement |
|---|---|---|
| **Le dépôt a 4 jours** (créé le 3 août 2026), version `0.1.7`, sept publications en quatre jours. Les 9 800 étoiles mesurent la hype, pas le vécu en production | **élevée** | version **exacte** épinglée dans `pyproject.toml` (pas de plage) ; Dependabot proposera les montées, elles passeront en revue comme le reste |
| Régression de qualité ou plantage sur un PDF qui marchait | moyenne | **`pypdf` conservé en repli** sur `ConvertError` pour les PDF (`CRAWL_PDF_FALLBACK`). La voie actuelle reste joignable en une variable d'environnement |
| Format bureautique qui fait boucler ou exploser le parseur | moyenne | `CRAWL_ENABLE_DOCUMENTS=false` coupe tout sans redéploiement ; conversion dans un thread, jamais sur l'event loop ; limites de décompression vérifiées §3 |
| Panic Rust traversant le FFI | faible | non reproduit sur entrées hostiles (§3) ; `except Exception` autour de la conversion, comme aujourd'hui autour de `pypdf` |
| **Pas d'OCR** : les PDF scannés restent illisibles | faible | limite documentée dans `README.md` et `API_REFERENCE.md`. Ne pas la contourner : un OCR local ferait entrer un modèle ML dans le conteneur, ce que le §0 exclut |
| `httpx2` de la Phase 14 + anydoc : deux extensions natives de plus | faible | anydoc n'a **aucune** dépendance Python transitive ; roues préconstruites, pas de toolchain Rust au build |

**Séquence de sortie de risque**, à trancher plus tard et pas dans ce lot : quand anydoc
aura passé quelques mois et une 0.2, retirer `pypdf` et `CRAWL_PDF_FALLBACK`. Tant que ce
n'est pas le cas, la dépendance supplémentaire est le prix de la réversibilité.

---

## 9. Tests

Le dépôt tient `mypy --strict` et une barrière de couverture à 80 %.

| Test | Ce qu'il protège |
|---|---|
| Conversion d'un `.xlsx`, `.docx`, `.pptx`, `.csv` de référence (fixtures générées, pas de corpus binaire au dépôt) | le câblage des 14 formats |
| **PDF avec tableau → le Markdown contient bien `\|---\|`** | la régression du §1.1 — c'est *le* test qui justifie le lot |
| URL sans extension servant un `.docx` | la seconde chance (§4.3) |
| Content-type menteur (`application/octet-stream` sur un vrai docx) | la confirmation par octets (§4.2) |
| Fichier chiffré → `EncryptedError` → `CrawledPage.success == false` avec un message clair | pas de 500, pas d'échec silencieux |
| **Bombe de décompression** (xlsx ~500 Mo gonflés) | mémoire bornée, pas de blocage — le test du §3, figé en régression |
| Document de 30 Mo avec `CRAWL_DOC_MAX_MB=25` | le plafond coupe pendant le streaming, pas après |
| Redirection `.pdf` → hôte privé | **la garde SSRF tient toujours** sur la voie généralisée |
| `CRAWL_ENABLE_DOCUMENTS=false` | repli navigateur, aucune conversion tentée |
| anydoc lève sur un PDF, `CRAWL_PDF_FALLBACK=true` | `pypdf` prend le relais |
| `POST /v1/convert` : nominal, trop gros, chiffré, format inconnu | l'endpoint du §5 |

---

## 10. Découpage

| Lot | Contenu | Effort |
|---|---|---|
| **15.1 — Voie document** | dépendance épinglée ; §4.1 à §4.4 ; repli `pypdf` ; configuration §7 ; tests de conversion, de garde SSRF, de plafond et de bombe | ~1 j |
| **15.2 — Robustesse & visibilité** | seconde chance (§4.3) ; `source_format` + ventilation des métriques (§4.5) ; tests de content-type menteur et d'URL sans extension | ~0,5 j |
| **15.3 — `/v1/convert` & docs** | endpoint (§5) ; `API_REFERENCE.md` ; `README.md` (formats supportés, limite OCR, roadmap Phase 15) ; outil MCP `convert_document` si la Phase 14 est livrée | ~0,5 j |

**Total ≈ 2 j.** 15.1 se suffit à lui-même et livre l'essentiel de la valeur : les
lots suivants peuvent glisser.

---

## 11. Fichiers touchés

```
app/core/crawl/crawler.py    modifié   §4 — voie document, détection, seconde chance
app/models/crawl.py          modifié   CrawledPage.source_format (additif)
app/models/convert.py        nouveau   schémas de POST /v1/convert
app/api/routes/convert.py    nouveau   endpoint §5
app/api/middleware.py        modifié   poids 2 pour /v1/convert
app/config.py                modifié   CRAWL_ENABLE_DOCUMENTS, CRAWL_DOC_MAX_MB,
                                       CRAWL_PDF_FALLBACK, CONVERT_MAX_MB + alias
app/main.py                  modifié   include_router(convert)
pyproject.toml               modifié   firecrawl-anydoc épinglé ; pypdf conservé
.env.example                 modifié   section §7
README.md                    modifié   formats, limite OCR, roadmap
API_REFERENCE.md             modifié   /v1/convert, source_format, configuration
tests/test_documents.py      nouveau   §9
tests/test_convert.py        nouveau   §9
```

Aucun fichier de `app/services/`, `app/core/research/` ni `app/core/llm/` n'est modifié :
la qualité de `/v1/answer`, `/v1/research` et `/v1/extract` s'améliore parce que le
contenu qu'ils reçoivent s'améliore, pas parce qu'ils changent.
