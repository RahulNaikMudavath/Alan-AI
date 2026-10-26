"""Keyword, semantic and hybrid retrieval.

Scores are **fused rank scores**: each signal contributes
``weight / (rrf_k + rank)``. That is Reciprocal Rank Fusion, chosen because a
cosine similarity (bounded, -1..1) and a bm25 score (unbounded, negative) cannot
be added without inventing calibration constants. Only relative order inside one
response is meaningful; the raw cosine is exposed separately as ``similarity``.

Every candidate query filters by ``user_id`` in SQL, and the optional
``agent_id``/``kind`` filters are applied in the same WHERE clause. Not one of
them is enforced by Python after the fact.
"""

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.llm.base import EmbeddingProvider
from app.models import (
    EmbeddingCoverage,
    Memory,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchResult,
)
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.retrieval.vectors import cosine_similarity, unpack_vector

logger = logging.getLogger(__name__)


@dataclass
class _Candidate:
    memory: Memory
    score: float = 0.0
    similarity: float | None = None
    matched_by: set[str] = field(default_factory=set)


class SearchService:
    def __init__(
        self,
        *,
        memories: MemoryRepository,
        embeddings: EmbeddingRepository,
        provider: EmbeddingProvider | None,
        settings: Settings,
    ) -> None:
        self._memories = memories
        self._embeddings = embeddings
        self._provider = provider
        self._settings = settings

    def search(self, request: MemorySearchRequest) -> MemorySearchResponse:
        scope = {
            "user_id": request.user_id,
            "agent_id": request.agent_id,
            "kind": request.kind,
        }
        pool = max(self._settings.hybrid_candidate_pool, request.limit)

        considered = self._memories.count(**scope)
        embedded = (
            self._embeddings.count(model=self._provider.model, **scope)
            if self._provider is not None
            else 0
        )

        semantic_hits: list[tuple[Memory, float]] = []
        degraded = False
        if request.mode in ("semantic", "hybrid"):
            semantic_hits, degraded = self._semantic_hits(request, pool)

        keyword_hits: list[Memory] = []
        if request.mode in ("keyword", "hybrid") or degraded:
            keyword_hits = self._memories.search_ranked(query=request.query, limit=pool, **scope)

        recent_ids: list[str] = []
        if request.mode == "hybrid":
            recent_ids = self._memories.recent_ids(limit=pool, **scope)

        candidates = self._fuse(keyword_hits, semantic_hits, recent_ids)
        return MemorySearchResponse(
            mode=request.mode,
            query=request.query,
            degraded=degraded,
            coverage=EmbeddingCoverage(embedded=embedded, considered=considered),
            results=[self._to_result(candidate) for candidate in candidates[: request.limit]],
        )

    # -- signals -----------------------------------------------------------

    def _semantic_hits(
        self, request: MemorySearchRequest, pool: int
    ) -> tuple[list[tuple[Memory, float]], bool]:
        """Return ``(ranked hits, degraded)`` for the query's embedding."""
        if self._provider is None:
            return [], True
        try:
            query_vectors = self._provider.embed([request.query])
            query_vector = query_vectors[0]
        except Exception as exc:  # noqa: BLE001 - retrieval degrades, it does not 500
            logger.warning("query_embedding_failed error=%s", type(exc).__name__)
            return [], True
        if len(query_vector) != self._provider.dimensions:
            logger.warning(
                "query_embedding_dimension_mismatch expected=%s actual=%s",
                self._provider.dimensions,
                len(query_vector),
            )
            return [], True

        rows = self._embeddings.candidates(
            user_id=request.user_id,
            model=self._provider.model,
            agent_id=request.agent_id,
            kind=request.kind,
            limit=self._settings.semantic_max_candidates,
        )

        scored: list[tuple[Memory, float]] = []
        skipped = 0
        for memory, dimensions, blob in rows:
            try:
                vector = unpack_vector(blob)
            except ValueError:
                skipped += 1
                continue
            if dimensions != len(vector) or len(vector) != len(query_vector):
                skipped += 1
                continue
            similarity = cosine_similarity(query_vector, vector)
            if similarity <= self._settings.semantic_min_similarity:
                continue
            scored.append((memory, similarity))

        if skipped:
            # A corrupt row must never fail a search; it is simply invisible.
            logger.warning("skipped_invalid_embeddings count=%s", skipped)

        # Newest wins ties, mirroring the ordering used everywhere else.
        scored.sort(
            key=lambda item: (round(item[1], 12), item[0].created_at, item[0].id), reverse=True
        )
        return scored[:pool], False

    # -- fusion ------------------------------------------------------------

    def _fuse(
        self,
        keyword_hits: list[Memory],
        semantic_hits: list[tuple[Memory, float]],
        recent_ids: list[str],
    ) -> list[_Candidate]:
        k = self._settings.rrf_k
        candidates: dict[str, _Candidate] = {}

        def entry(memory: Memory) -> _Candidate:
            candidate = candidates.get(memory.id)
            if candidate is None:
                candidate = _Candidate(memory=memory)
                candidates[memory.id] = candidate
            return candidate

        for rank, memory in enumerate(keyword_hits):
            candidate = entry(memory)
            candidate.score += self._settings.hybrid_keyword_weight / (k + rank)
            candidate.matched_by.add("keyword")

        for rank, (memory, similarity) in enumerate(semantic_hits):
            candidate = entry(memory)
            candidate.score += self._settings.hybrid_semantic_weight / (k + rank)
            candidate.matched_by.add("semantic")
            candidate.similarity = (
                similarity
                if candidate.similarity is None
                else max(candidate.similarity, similarity)
            )

        # Recency reorders hits that already matched something; it never
        # introduces a memory that no query signal found.
        if recent_ids and self._settings.hybrid_recency_weight:
            positions = {memory_id: rank for rank, memory_id in enumerate(recent_ids)}
            for memory_id, candidate in candidates.items():
                position = positions.get(memory_id)
                if position is not None:
                    candidate.score += self._settings.hybrid_recency_weight / (k + position)

        return sorted(
            candidates.values(),
            key=lambda candidate: (
                round(candidate.score, 12),
                candidate.memory.created_at,
                candidate.memory.id,
            ),
            reverse=True,
        )

    @staticmethod
    def _to_result(candidate: _Candidate) -> MemorySearchResult:
        return MemorySearchResult(
            memory=candidate.memory,
            score=round(candidate.score, 6),
            similarity=None if candidate.similarity is None else round(candidate.similarity, 6),
            matched_by=[
                source for source in ("keyword", "semantic") if source in candidate.matched_by
            ],
        )
