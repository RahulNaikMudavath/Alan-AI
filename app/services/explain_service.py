"""Explain one memory: what is observed, and what is only inferred.

The split is intentional. ``observed`` contains rows that exist in the database
(lifecycle columns, provenance, index presence). ``inferred`` contains the
lexical conflict heuristic, labelled with its rule and evidence. Neither section
invents a confidence or relevance score, and raw conversation text is never
returned even when it is stored.
"""

import json

from app.models import (
    ExplainConflictCandidate,
    ExplainIndexes,
    ExplainInferred,
    ExplainLifecycle,
    ExplainObserved,
    ExplainSource,
    ExtractorInfo,
    MemoryExplainResponse,
)
from app.repositories.conflicts import ConflictRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.services.errors import MemoryNotFoundError


class ExplainService:
    def __init__(
        self,
        *,
        memories: MemoryRepository,
        conversations: ConversationRepository,
        embeddings: EmbeddingRepository,
        conflicts: ConflictRepository,
    ) -> None:
        self._memories = memories
        self._conversations = conversations
        self._embeddings = embeddings
        self._conflicts = conflicts

    def explain(self, memory_id: str, *, user_id: str) -> MemoryExplainResponse:
        memory = self._memories.get(memory_id, user_id=user_id)
        if memory is None:
            raise MemoryNotFoundError(memory_id)

        sources = [
            self._source(row)
            for row in self._conversations.sources_for_memory(memory_id=memory_id, user_id=user_id)
        ]
        observed = ExplainObserved(
            origin="conversation" if sources else "direct",
            sources=sources,
            indexes=ExplainIndexes(
                keyword=self._memories.is_keyword_indexed(memory_id),
                embedding_models=self._embeddings.models_for_memory(memory_id),
            ),
            lifecycle=ExplainLifecycle(
                status=memory.status,
                superseded_at=memory.superseded_at,
                superseded_by=memory.superseded_by,
                reason=memory.superseded_reason,
                note=memory.superseded_note,
            ),
        )

        candidates = []
        for record in self._conflicts.list_for_memory(user_id=user_id, memory_id=memory_id):
            other_id = record.memory_b if record.memory_a == memory_id else record.memory_a
            candidates.append(
                ExplainConflictCandidate(
                    conflict_id=record.id,
                    other_memory_id=other_id,
                    rule=record.rule,
                    evidence=record.evidence,
                    status=record.status,
                )
            )

        return MemoryExplainResponse(
            memory=memory,
            observed=observed,
            inferred=ExplainInferred(conflict_candidates=candidates),
        )

    @staticmethod
    def _source(row) -> ExplainSource:
        extractor = None
        raw_extractor = row["extractor"]
        if raw_extractor:
            try:
                extractor = ExtractorInfo.model_validate_json(raw_extractor)
            except ValueError:
                extractor = None
        return ExplainSource(
            conversation_id=row["conversation_id"],
            message_indexes=json.loads(row["message_indexes"]),
            conversation_created_at=row["conversation_created_at"],
            extractor=extractor,
        )
