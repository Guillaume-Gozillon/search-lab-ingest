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

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_EMBED_DIMS: int = int(os.getenv("OLLAMA_EMBED_DIMS", "768"))

# URL de text-embeddings-inference. Le modèle n'est pas un paramètre côté client : il est
# fixé par le `--model-id` du conteneur, un serveur TEI ne sert qu'un modèle.
TEI_URL: str = os.getenv("TEI_URL", "http://localhost:8080")

# Moteur d'embedding : `tei` (défaut) ou `ollama`. Les deux servent nomic-embed-text-v1.5
# et ne produisent PAS les mêmes vecteurs — cosinus moyen mesuré à 0,51 sur 1000 titres.
# Un index construit avec l'un ne s'interroge pas avec l'autre : voir le README.
EMBED_BACKEND: str = os.getenv("EMBED_BACKEND", "tei").strip().lower()

# Requêtes d'embedding en vol. Le pipeline est un seul thread qui lit le CSV, embed, puis
# bulk : sans concurrence, le GPU attend pendant le bulk et le bulk attend pendant le GPU.
# Ne sert que si le serveur accepte de paralléliser — Ollama plafonne à
# OLLAMA_NUM_PARALLEL, TEI batche dynamiquement sans limite déclarée.
EMBED_WORKERS: int = int(os.getenv("EMBED_WORKERS", "4"))

# Préfixes de tâche nomic-embed-text-v1.5, entraîné avec `search_document: ` à
# l'indexation et `search_query: ` à la recherche. VIDES PAR DÉFAUT : les activer change
# les vecteurs produits (cos mesuré entre titre brut et titre préfixé : 0,684), donc
# impose de reconstruire l'index ET d'aligner le côté requête. L'un sans l'autre dégrade
# la pertinence en silence, sans jamais lever d'erreur.
EMBED_DOC_PREFIX: str = os.getenv("EMBED_DOC_PREFIX", "")
EMBED_QUERY_PREFIX: str = os.getenv("EMBED_QUERY_PREFIX", "")
