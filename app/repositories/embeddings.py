"""Vector storage for semantic search.

Vectors live in their own table keyed by ``(memory_id, model)`` so that changing
embedding models never mixes incompatible vectors and never touches the
``memories`` table. Every query that reads a vector joins ``memories`` and
filters by ``user_id`` in SQL, exactly like the rest of the repository layer.
"""

import sqlite3

from app.models import Memory

_MEMORY_COLUMNS = "m.id, m.user_id, m.agent_id, m.content, m.kind, m.created_at, m.updated_at"


def _memory_from_row(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        content=row["content"],
        kind=row["kind"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class EmbeddingRepository:
    """Reads and writes vectors for one request's connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert(
        self,
        *,
        memory_id: str,
        model: str,
        dimensions: int,
        vector: bytes,
        created_at: str,
    ) -> None:
        """Write a vector, replacing an existing one for the same model."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO memory_embeddings (memory_id, model, dimensions, vector, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (memory_id, model) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    vector = excluded.vector,
                    created_at = excluded.created_at
                """,
                (memory_id, model, dimensions, vector, created_at),
            )

    def insert_if_absent(
        self,
        *,
        memory_id: str,
        model: str,
        dimensions: int,
        vector: bytes,
        created_at: str,
    ) -> bool:
        """Insert only when no vector exists for this (memory, model) pair.

        Returns True when a row was written. The backfill command uses this so
        rerunning it can never overwrite vectors, and never disturbs rows that
        belong to a different model.
        """
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO memory_embeddings
                    (memory_id, model, dimensions, vector, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, model, dimensions, vector, created_at),
            )
        return cursor.rowcount > 0

    def delete_for_memory(self, memory_id: str, *, model: str | None = None) -> int:
        """Delete one memory's vectors, optionally only for a single model."""
        if model is None:
            sql = "DELETE FROM memory_embeddings WHERE memory_id = ?"
            params: tuple[object, ...] = (memory_id,)
        else:
            sql = "DELETE FROM memory_embeddings WHERE memory_id = ? AND model = ?"
            params = (memory_id, model)
        with self._connection:
            cursor = self._connection.execute(sql, params)
        return cursor.rowcount

    def get(self, memory_id: str, *, model: str) -> tuple[int, bytes] | None:
        """Return ``(dimensions, vector_blob)`` for one memory and model."""
        row = self._connection.execute(
            "SELECT dimensions, vector FROM memory_embeddings WHERE memory_id = ? AND model = ?",
            (memory_id, model),
        ).fetchone()
        return (row["dimensions"], row["vector"]) if row is not None else None

    def count(
        self,
        *,
        user_id: str,
        model: str,
        agent_id: str | None = None,
        kind: str | None = None,
    ) -> int:
        """Count vectors available for a scoped retrieval."""
        filters, params = self._filters(user_id, model, agent_id, kind)
        return self._connection.execute(
            f"""
            SELECT COUNT(*)
            FROM memories m
            JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE {" AND ".join(filters)}
            """,
            params,
        ).fetchone()[0]

    def candidates(
        self,
        *,
        user_id: str,
        model: str,
        agent_id: str | None = None,
        kind: str | None = None,
        limit: int,
    ) -> list[tuple[Memory, int, bytes]]:
        """Return scoped ``(memory, dimensions, vector)`` rows, newest first.

        The limit bounds the brute-force scan. Ordering by recency means that if
        a user ever exceeds the cap, recent memories win — a documented
        trade-off until an approximate index is justified.
        """
        filters, params = self._filters(user_id, model, agent_id, kind)
        rows = self._connection.execute(
            f"""
            SELECT
                {_MEMORY_COLUMNS},
                e.dimensions AS embedding_dimensions,
                e.vector AS embedding_vector
            FROM memories m
            JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE {" AND ".join(filters)}
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [
            (_memory_from_row(row), row["embedding_dimensions"], row["embedding_vector"])
            for row in rows
        ]

    def missing(
        self,
        *,
        model: str,
        user_id: str | None = None,
        after_rowid: int = 0,
        limit: int = 100,
    ) -> list[tuple[int, Memory]]:
        """Return ``(rowid, memory)`` rows that have no vector for ``model``.

        Scans by ``rowid`` cursor rather than ``OFFSET`` so the backfill can
        advance even when individual rows keep failing to embed. Vectors for a
        different model are left alone and simply do not count as coverage.
        """
        filters = [
            "m.rowid > ?",
            "NOT EXISTS (SELECT 1 FROM memory_embeddings e "
            "WHERE e.memory_id = m.id AND e.model = ?)",
        ]
        params: list[object] = [after_rowid, model]
        if user_id is not None:
            filters.append("m.user_id = ?")
            params.append(user_id)
        params.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT m.rowid AS rowid, {_MEMORY_COLUMNS}
            FROM memories m
            WHERE {" AND ".join(filters)}
            ORDER BY m.rowid ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [(row["rowid"], _memory_from_row(row)) for row in rows]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _filters(
        user_id: str, model: str, agent_id: str | None, kind: str | None
    ) -> tuple[list[str], list[object]]:
        filters = ["m.user_id = ?", "e.model = ?"]
        params: list[object] = [user_id, model]
        if agent_id is not None:
            filters.append("m.agent_id = ?")
            params.append(agent_id)
        if kind is not None:
            filters.append("m.kind = ?")
            params.append(kind)
        return filters, params
