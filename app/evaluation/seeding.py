"""Deterministic seeding for evaluation runs.

A temporary database is created through the real migration path and populated
with deterministic IDs and timestamps, so rankings and tie-breaks are repeatable.
Embeddings are produced by the selected provider; supersessions are applied
through the same helper the API uses.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database
from app.evaluation.dataset import EvalDataset
from app.llm.base import EmbeddingProvider
from app.repositories.conflicts import ConflictRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.repositories.transactions import transaction
from app.services.embedding_index import EmbeddingIndex
from app.services.supersession import supersede_locked

_BASE_TIME = datetime(2025, 1, 1, tzinfo=UTC)
_SUPERSEDED_AT = datetime(2025, 1, 2, tzinfo=UTC)
_ID_NAMESPACE = uuid.UUID("6f1c9d2e-5a2b-4f0e-9b7a-1c3d5e7f9a0b")


class EvaluationSetupError(RuntimeError):
    """Raised when the evaluation database cannot be prepared."""


@dataclass(frozen=True)
class SeededDataset:
    path: Path
    local_to_id: dict[str, str]
    id_to_local: dict[str, str]


def memory_id_for(dataset_version: str, local_id: str) -> str:
    """Deterministic UUID for a dataset-local memory ID."""
    return uuid.uuid5(_ID_NAMESPACE, f"{dataset_version}:{local_id}").hex


def seed_dataset(
    dataset: EvalDataset,
    provider: EmbeddingProvider,
    *,
    database_path: Path,
) -> SeededDataset:
    """Create and populate a temporary database. The file is left in place."""
    database = Database(database_path)
    database.initialize()

    connection = database.connect()
    try:
        memories = MemoryRepository(connection)
        embeddings = EmbeddingRepository(connection)
        conflicts = ConflictRepository(connection)
        index = EmbeddingIndex(repository=embeddings, provider=provider)

        local_to_id: dict[str, str] = {}
        id_to_local: dict[str, str] = {}
        for position, entry in enumerate(dataset.memories):
            memory_id = memory_id_for(dataset.dataset_version, entry.local_id)
            stamp = (_BASE_TIME + timedelta(seconds=position)).isoformat()
            stored = memories.add(
                memory_id=memory_id,
                user_id=entry.user_id,
                agent_id=entry.agent_id,
                content=entry.content,
                kind=entry.kind,
                created_at=stamp,
                updated_at=stamp,
            )
            if not index.index(stored):
                # Never run a benchmark with a silently missing vector.
                raise EvaluationSetupError(
                    f"failed to embed memory {entry.local_id}; check the provider"
                )
            local_to_id[entry.local_id] = memory_id
            id_to_local[memory_id] = entry.local_id

        superseding = [entry for entry in dataset.memories if entry.superseded_by is not None]
        if superseding:
            with transaction(connection):
                for entry in superseding:
                    supersede_locked(
                        memories=memories,
                        conflicts=conflicts,
                        user_id=entry.user_id,
                        memory_id=local_to_id[entry.local_id],
                        superseded_by=local_to_id[entry.superseded_by],
                        reason=entry.superseded_reason or "other",
                        note=None,
                        at=_SUPERSEDED_AT.isoformat(),
                    )

        return SeededDataset(
            path=database_path,
            local_to_id=local_to_id,
            id_to_local=id_to_local,
        )
    finally:
        connection.close()
