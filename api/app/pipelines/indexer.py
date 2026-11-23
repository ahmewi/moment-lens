"""Search index schema definitions + create/update helpers."""
from __future__ import annotations

import logging
import threading
from typing import Iterable

from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    ScoringProfile,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    TextWeights,
    VectorSearch,
    VectorSearchAlgorithmKind,
    VectorSearchProfile,
)

from app.azure_clients import search_client, search_index_client
from app.config import settings
from app.models import SceneCard

log = logging.getLogger(__name__)

# text-embedding-3-large dimensions
EMBEDDING_DIMS = 3072
VECTOR_PROFILE = "moments-vector-profile"
HNSW_CONFIG = "moments-hnsw"
SEMANTIC_CONFIG = "moments-semantic"
SCORING_PROFILE = "moments-relevance"


def _build_index(name: str) -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="video_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="filename", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="start_seconds", type=SearchFieldDataType.Double, filterable=True, sortable=True),
        SimpleField(name="end_seconds", type=SearchFieldDataType.Double, filterable=True, sortable=True),
        SimpleField(name="duration_seconds", type=SearchFieldDataType.Double, filterable=True, sortable=True),
        SearchableField(name="summary", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="transcript_excerpt", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="labels",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
            filterable=True,
            facetable=True,
        ),
        SearchField(
            name="entities",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
            filterable=True,
            facetable=True,
        ),
        SearchableField(name="ocr_text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="speakers",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
            filterable=True,
            facetable=True,
        ),
        SimpleField(name="thumbnail_url", type=SearchFieldDataType.String),
        SimpleField(name="blob_url", type=SearchFieldDataType.String),
        SimpleField(name="content_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
        # Advanced #6: separate vector for entity-only text. Lets the search
        # router fan out a query against both vectors and merge by RRF, which
        # measurably boosts entity-name precision ("rivera scoring" stays on
        # Rivera even when transcript is sparse).
        SearchField(
            name="entity_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=HNSW_CONFIG,
                kind=VectorSearchAlgorithmKind.HNSW,
                parameters=HnswParameters(m=4, ef_construction=400, ef_search=500, metric="cosine"),
            )
        ],
        profiles=[
            VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name=HNSW_CONFIG),
        ],
    )

    semantic = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="summary"),
                    content_fields=[
                        SemanticField(field_name="transcript_excerpt"),
                        SemanticField(field_name="ocr_text"),
                    ],
                    keywords_fields=[
                        SemanticField(field_name="labels"),
                        SemanticField(field_name="entities"),
                    ],
                ),
            )
        ]
    )

    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic,
        scoring_profiles=[
            ScoringProfile(
                name=SCORING_PROFILE,
                # Field weights bias BM25 toward high-precision metadata fields
                # so that "rivera scoring" is dominated by entity/label/summary
                # matches rather than incidental commentary in transcript_excerpt.
                text_weights=TextWeights(weights={
                    "entities": 8.0,
                    "labels": 4.0,
                    "summary": 3.0,
                    "ocr_text": 2.0,
                    "transcript_excerpt": 1.0,
                    "filename": 0.5,
                }),
            )
        ],
        default_scoring_profile=SCORING_PROFILE,
    )


_ensure_lock = threading.Lock()
_ensured: bool = False


def ensure_indexes() -> None:
    """Create or update the VI index (idempotent, once per process).

    Azure AI Search rejects field type changes on an existing index, so if a
    pre-existing index has incompatible schema (e.g. `labels` as Edm.String
    instead of Collection(Edm.String)) we drop and recreate it. Demo-grade —
    deletes any existing data in those indexes.
    """
    global _ensured
    with _ensure_lock:
        if _ensured:
            return
        client = search_index_client()
        desired_collection_fields = {"labels", "entities", "speakers"}
        required_fields = {"entity_vector", "content_type"}  # added in v2
        for name in (settings.search_index,):
            log.info("Ensuring search index '%s'", name)
            try:
                existing = client.get_index(name)
                mismatch = False
                existing_field_names = {f.name for f in existing.fields}
                for f in existing.fields:
                    if f.name in desired_collection_fields and "Collection" not in str(f.type):
                        mismatch = True
                        break
                # Force recreate if newer required fields are missing on this index
                if not mismatch and not required_fields.issubset(existing_field_names):
                    log.warning("Index '%s' missing fields %s; recreating.",
                                name, required_fields - existing_field_names)
                    mismatch = True
                if mismatch:
                    log.warning("Index '%s' has incompatible schema; deleting and recreating.", name)
                    try:
                        client.delete_index(name)
                    except Exception as de:  # noqa: BLE001
                        log.warning("delete_index('%s') ignored: %s", name, de)
                    client.create_index(_build_index(name))
                    continue
            except Exception as e:  # noqa: BLE001
                log.info("Index '%s' not found or unreadable (%s); creating.", name, e)
            try:
                client.create_or_update_index(_build_index(name))
            except Exception as e:  # noqa: BLE001
                log.warning("create_or_update_index('%s') failed: %s", name, e)
        _ensured = True


def upsert_scene_cards(
    index_name: str,
    cards: Iterable[SceneCard],
    embeddings: dict[str, list[float]],
    entity_embeddings: dict[str, list[float]] | None = None,
) -> None:
    """Upload scene cards (with their embeddings) to the named index.

    `entity_embeddings` is optional — when provided populates `entity_vector`
    for the multi-vector search path. Falls back to the same content_vector
    when missing so old code paths keep working.
    """
    client = search_client(index_name)
    docs = []
    entity_embeddings = entity_embeddings or {}
    for c in cards:
        d = c.model_dump()
        d["content_vector"] = embeddings.get(c.id, [])
        d["entity_vector"]  = entity_embeddings.get(c.id) or d["content_vector"]
        docs.append(d)
    if not docs:
        return
    for offset in range(0, len(docs), 100):
        batch = docs[offset:offset + 100]
        result = client.upload_documents(documents=batch)
        if len(result) != len(batch) or any(item.succeeded is not True for item in result):
            raise RuntimeError("Search did not accept all scene cards")
    log.info("Uploaded %d docs to %s", len(docs), index_name)
