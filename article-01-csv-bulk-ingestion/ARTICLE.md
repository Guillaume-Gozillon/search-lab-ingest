# Ingérer 1,4 million de produits Amazon dans Elasticsearch en moins de 90 secondes avec Python et pandas

*Un guide pratique pour construire un pipeline d'ingestion CSV robuste et performant*

---

Quand on commence à travailler sérieusement avec Elasticsearch, la question de l'ingestion de données se pose rapidement. Les tutoriels montrent souvent quelques dizaines de documents. Mais en production — ou même pour des expérimentations réalistes — on parle de centaines de milliers, voire de millions d'enregistrements.

Dans cet article, je vais vous montrer comment j'ai ingéré **1,4 million de produits Amazon** dans Elasticsearch 9.x en **moins de 90 secondes**, avec Python, pandas et quelques optimisations bien choisies. Le code est propre, modulaire, et réutilisable pour n'importe quel dataset CSV.

---

## Contexte : pourquoi ce projet ?

Ce pipeline fait partie d'un écosystème que j'appelle **search-lab** — un ensemble de repos compagnons pour explorer les capacités d'Elasticsearch à travers des articles Medium. L'idée : travailler avec des données réalistes, pas des exemples jouets.

Le dataset choisi est l'**Amazon Products Dataset 2023** disponible sur Kaggle : 1,4 million de produits, environ 300 MB en CSV. Il contient exactement ce qu'on veut pour tester un moteur de recherche — des titres textuels, des prix, des notes, des catégories.

Pourquoi Elasticsearch plutôt qu'une base SQL ? Parce que la recherche full-text, le scoring par pertinence, et les agrégations analytiques sont des cas où ES excelle nativement. Un `SELECT WHERE title LIKE '%coffee maker%'` ne vous donnera jamais la même qualité de résultats qu'une recherche ES avec l'analyseur `english`.

---

## 1. Le setup Docker : simple et reproductible

Première décision : faire tourner ES en local sans friction. Docker est la réponse évidente. Voici le `docker-compose.yml` que j'utilise :

```yaml
services:
  elasticsearch:
    image: elasticsearch:9.0.1
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - xpack.security.http.ssl.enabled=false
      - ES_JAVA_OPTS=-Xms2g -Xmx2g
    ports:
      - "9200:9200"
    mem_limit: 4g
    healthcheck:
      test: curl -sf http://localhost:9200/_cluster/health | grep -q '"status":"green"\|"status":"yellow"'
      interval: 10s
      retries: 10

  kibana:
    image: kibana:9.0.1
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports:
      - "5601:5601"
    depends_on:
      elasticsearch:
        condition: service_healthy
    mem_limit: 1g
```

Quelques choix délibérés :

- **`xpack.security.enabled=false`** : En développement local, la sécurité ajoute de la complexité sans valeur. On l'active en production, pas pour des expérimentations.
- **`ES_JAVA_OPTS=-Xms2g -Xmx2g`** : On fixe le heap JVM à 2 GB. La règle empirique ES : ne jamais dépasser 50% de la RAM disponible pour le heap, et rester sous 32 GB.
- **`healthcheck` avec condition** : Kibana ne démarre qu'une fois ES réellement prêt. Ça évite les race conditions au boot.

```bash
docker compose -f docker/docker-compose.yml up -d
```

---

## 2. Le mapping : définir le contrat de données

Avant d'ingérer quoi que ce soit, il faut définir le **mapping** — c'est-à-dire le schéma de l'index. C'est l'une des décisions les plus importantes, et elle est difficile à changer après coup.

```json
{
  "settings": {
    "number_of_shards": 2,
    "number_of_replicas": 0
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "asin":        { "type": "keyword" },
      "title":       { "type": "text", "analyzer": "english" },
      "imgUrl":      { "type": "keyword", "index": false },
      "productURL":  { "type": "keyword", "index": false },
      "stars":       { "type": "float" },
      "price":       { "type": "float" },
      "isBestSeller":{ "type": "boolean" },
      "ingested_at": { "type": "date" }
    }
  }
}
```

Expliquons chaque choix :

**`dynamic: strict`** — Tout champ non déclaré dans le mapping provoque une erreur d'indexation. En mode `dynamic: true` (le défaut), ES infère les types automatiquement, ce qui peut mener à des surprises — un champ numérique inféré comme `long` alors qu'on voulait `float`. Avec `strict`, on garde le contrôle total.

**`keyword` vs `text`** — Le champ `title` est en `text` avec l'analyseur `english` : il sera tokenisé, les stop words retirés, les mots ramenés à leur racine (*stemming*). Parfait pour la recherche full-text. L'`asin` en revanche est en `keyword` : on veut des correspondances exactes, pas de tokenisation.

**`index: false` sur les URLs** — `imgUrl` et `productURL` ne seront jamais utilisés pour filtrer ou rechercher. On les stocke pour les récupérer dans les résultats, mais on dit explicitement à ES de ne pas les indexer. Ça économise de l'espace disque et de la mémoire.

---

## 3. La transformation des données : gérer la réalité du CSV

Un CSV de 300 MB, c'est rarement propre. Pandas charge les cellules vides comme `NaN` (un float en Python), les nombres peuvent être stockés comme strings, et certaines lignes sont incomplètes. La fonction de transformation est le seul endroit où on traite tout ça.

```python
import math

def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value

def transform(row: dict) -> dict | None:
    asin = _clean(row.get("asin"))
    if not asin:
        return None  # ligne invalide, on skip

    return {
        "_id": str(asin),
        "asin": str(asin),
        "title": _clean(row.get("title")),
        "stars": _parse_float(row.get("stars")),
        "price": _parse_float(row.get("price")),
        "isBestSeller": bool(_clean(row.get("isBestSeller")) or False),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
```

Points importants :

- **`_clean` gère le NaN pandas** : `math.isnan()` ne fonctionne que sur les floats, d'où le `isinstance` guard.
- **`transform` retourne `None`** pour les lignes sans ASIN — le pipeline les ignore silencieusement.
- **`_id` = ASIN** : On utilise l'identifiant métier comme `_id` ES. Ça rend les ré-ingestions idempotentes — un même document réindexé écrase l'ancien au lieu de créer un doublon.
- **Fonctions pures sans effets de bord** : `transform` ne touche à rien d'externe. Ça facilite les tests unitaires et le debug.

---

## 4. Le pipeline : streamer sans exploser la mémoire

300 MB en CSV, c'est faisable en RAM. Mais 3 GB ? 30 GB ? Le principe de base : **ne jamais charger le fichier entier en mémoire**.

Pandas supporte la lecture par chunks avec `read_csv(..., chunksize=10_000)`. Combiné avec un générateur Python, on obtient un stream de documents qui ne chargera jamais plus de 10 000 lignes à la fois :

```python
def document_stream(csv_path, limit=None):
    for chunk in pd.read_csv(csv_path, chunksize=10_000, low_memory=False):
        for row in chunk.to_dict("records"):
            if limit is not None and emitted >= limit:
                return
            doc = transform(row)
            if doc is None:
                continue
            doc["_index"] = config.ES_INDEX
            yield doc
```

Le paramètre `--limit` est précieux en développement : tester avec 1 000 documents avant de lancer l'ingestion complète évite bien des mauvaises surprises.

---

## 5. Les optimisations bulk : là où le gain de performance se joue

C'est la partie la plus intéressante. Sans optimisation, ES indexe chaque document en temps réel : il écrit le segment, le refresh rend le doc visible, les réplicas se synchronisent. Multiplié par 1,4 million de documents, c'est catastrophiquement lent.

Avant de lancer l'ingestion :

```python
def optimize_for_import(client, index):
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "-1", "number_of_replicas": 0}},
    )
```

**`refresh_interval: -1`** — Par défaut, ES refresh l'index toutes les secondes pour rendre les nouveaux documents visibles aux recherches. C'est coûteux : ça force la création de nouveaux segments Lucene. En désactivant complètement le refresh pendant l'ingestion, ES peut écrire en mémoire et flusher sur disque bien plus efficacement.

**`number_of_replicas: 0`** — Chaque réplica reçoit une copie de chaque document indexé. En bulk import, les réplicas sont du travail supplémentaire sans bénéfice immédiat. On les remet à 1 après.

Après l'ingestion, on restaure les settings normaux :

```python
def restore_after_import(client, index):
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "1s", "number_of_replicas": 1}},
    )
    client.indices.refresh(index=index)
```

---

## 6. `streaming_bulk` : pourquoi pas le bulk classique ?

La librairie `elasticsearch-py` propose deux approches pour l'indexation en masse :

- **`bulk`** : prend une liste complète de documents, la sérialise, l'envoie en une requête.
- **`streaming_bulk`** : consomme un itérateur, envoie des batches au fil de l'eau.

Avec 1,4 million de documents, `bulk` classique signifierait construire la liste entière en mémoire avant d'envoyer quoi que ce soit. `streaming_bulk` consomme le générateur chunk par chunk (`chunk_size=2000`) : la mémoire reste stable quelle que soit la taille du dataset.

```python
for ok, result in streaming_bulk(
    client, actions,
    chunk_size=2_000,
    raise_on_error=False
):
    if not ok:
        errors.append(result)
```

`raise_on_error=False` permet de collecter les erreurs sans interrompre le pipeline. On les logue à la fin plutôt que de tout arrêter pour un document mal formé.

---

## 7. Le forcemerge : l'étape qu'on oublie souvent

Après une ingestion en masse, l'index Lucene contient potentiellement des centaines de segments — chaque flush pendant l'ingestion en crée de nouveaux. Trop de segments = recherches plus lentes.

```python
client.indices.forcemerge(index=index, max_num_segments=1, request_timeout=300)
```

`max_num_segments=1` fusionne tous les segments en un seul par shard. C'est l'optimum pour un index en lecture seule : les recherches deviennent maximalement efficaces. **Attention** : c'est une opération coûteuse en I/O, à faire une seule fois après l'ingestion, jamais en continu.

---

## 8. Résultats

```
Bulk complete — 1 426 337 indexed, 0 errors in 84.9s
Total: 1 426 337 documents
Segments avant merge : 312 → après merge : 2
```

**~16 800 documents/seconde**, zéro erreur. Une vérification rapide via les agrégations : prix moyen **$43.37**, note moyenne **4.0/5**, **8 520 best sellers**. Et la recherche full-text fonctionne — `"coffee makers"` remonte aussi "coffee maker" et "making coffee" grâce au stemming de l'analyseur `english`.

---

## Conclusion : les points clés

1. **`dynamic: strict` d'abord** — Définir explicitement son mapping évite les surprises en production.
2. **`keyword` vs `text`, c'est fondamental** — Se tromper ici, c'est des recherches qui ne fonctionnent pas ou de l'espace disque gaspillé.
3. **Les deux optimisations qui changent tout** — `refresh_interval: -1` et `number_of_replicas: 0` pendant l'ingestion. Sans elles, le même pipeline tournerait 5 à 10 fois plus lentement.
4. **`streaming_bulk` + générateur** — La combinaison qui permet d'ingérer des datasets de n'importe quelle taille avec une empreinte mémoire constante.
5. **Le forcemerge, pas optionnel** — Si votre index est principalement en lecture, c'est la dernière étape de l'optimisation. Ne la sautez pas.
6. **Testez avec `--limit`** — Valider le pipeline sur 1 000 documents avant de lancer sur 1,4 million économise du temps et de la frustration.

*Le code complet est disponible sur GitHub. Le prochain article de la série couvrira les requêtes full-text avancées, les agrégations, et le scoring par pertinence — avec ce même dataset comme terrain de jeu.*
