"""Central configuration — all env vars loaded once here, imported everywhere else."""

import os

from dotenv import load_dotenv

load_dotenv()

ES_URL: str = os.getenv("ES_URL", "http://localhost:9200")
ES_INDEX: str = os.getenv("ES_INDEX", "amazon_products")
ES_ALIAS: str = os.getenv("ES_ALIAS", "products")

# Alias distinct de ES_ALIAS : l'index vectoriel a son propre cycle de vie.
ES_EMBEDDINGS_ALIAS: str = os.getenv("ES_EMBEDDINGS_ALIAS", "products_embeddings")

# Réplicas rétablis après un import. Le lab tourne sur un nœud unique : demander un
# réplica le laisserait non assigné et l'index en yellow indéfiniment. À monter à 1 sur
# un vrai cluster.
ES_REPLICAS: int = int(os.getenv("ES_REPLICAS", "0"))

# URL de text-embeddings-inference. Le modèle n'est pas un paramètre côté client : il est
# fixé par le `--model-id` du conteneur, un serveur TEI ne sert qu'un modèle.
TEI_URL: str = os.getenv("TEI_URL", "http://localhost:8080")

# Moteur d'embedding. `tei` est le seul aujourd'hui. Changer de moteur n'est pas une
# opération neutre : un index est lié à celui qui l'a construit, et rien ne signale un
# mélange. Voir le README.
EMBED_BACKEND: str = os.getenv("EMBED_BACKEND", "tei").strip().lower()

# Requêtes d'embedding en vol. Le pipeline est un seul thread qui lit le CSV, embed, puis
# bulk : sans concurrence, le GPU attend pendant le bulk et le bulk attend pendant le GPU.
# TEI batche dynamiquement, mais son --max-concurrent-requests compte un permis par input :
# il doit couvrir EMBED_WORKERS × 2 × la taille de lot, sinon 429.
EMBED_WORKERS: int = int(os.getenv("EMBED_WORKERS", "4"))

# Préfixes de tâche nomic-embed-text-v1.5, entraîné avec `search_document: ` à
# l'indexation et `search_query: ` à la recherche. VIDES PAR DÉFAUT : les activer change
# les vecteurs produits (cos mesuré entre titre brut et titre préfixé : 0,684), donc
# impose de reconstruire l'index ET d'aligner le côté requête. L'un sans l'autre dégrade
# la pertinence en silence, sans jamais lever d'erreur.
EMBED_DOC_PREFIX: str = os.getenv("EMBED_DOC_PREFIX", "")
EMBED_QUERY_PREFIX: str = os.getenv("EMBED_QUERY_PREFIX", "")
