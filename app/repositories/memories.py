"""Data access for memories.

Every SQL statement in the project lives here. That keeps two promises easy to
enforce: parameters are always bound (never string-formatted into SQL), and
every read is scoped by ``user_id``.
"""

import sqlite3
from collections.abc import Sequence

from app.models import Memory
from app.repositories.search import (
    build_fts_any_expression,
    build_fts_expression,
    build_like_patterns,
)

_COLUMNS = (
    "m.id, m.user_id, m.agent_id, m.content, m.kind, m.created_at, m.updated_at, "
    "m.status, m.superseded_at, m.superseded_by, m.superseded_reason, m.superseded_note"
)

# Newest first. `id` breaks ties so pagination is stable even when two rows
# share a timestamp.
_ORDER = "ORDER BY m.created_at DESC, m.id DESC"


class MemoryRepository:
    """Reads and writes memory rows for one request's connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(
        self,
        *,
        memory_id: str,
        user_id: str,
        agent_id: str | None,
        content: str,
        kind: str,
        created_at: str,
        updated_at: str,
    ) -> Memory:
        """Insert a memory and return it as stored."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO memories
                    (id, user_id, agent_id, content, kind, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (memory_id, user_id, agent_id, content, kind, created_at, updated_at),
            )
        stored = self.get(memory_id, user_id=user_id)
        # The insert above cannot succeed without the row being readable back.
        assert stored is not None
        return stored

    def insert_rows(self, memories: list[Memory]) -> None:
        """Insert several memories without committing.

        Used by conversation ingestion so memory rows, the conversation receipt,
        and provenance are committed as one unit. Must run inside a transaction
        (see :mod:`app.repositories.transactions`).
        """
        if not memories:
            return
        self._connection.executemany(
            """
            INSERT INTO memories
                (id, user_id, agent_id, content, kind, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    memory.id,
                    memory.user_id,
                    memory.agent_id,
                    memory.content,
                    memory.kind,
                    memory.created_at.isoformat(),
                    memory.updated_at.isoformat(),
                )
                for memory in memories
            ],
        )

    def content_by_user(self, user_id: str) -> list[str]:
        """Return every stored content string for a user.

        Duplicate detection canonicalises these in Python: SQLite's ``lower()``
        is ASCII-only and cannot strip punctuation or normalise Unicode. The
        cost is linear in a user's memory count, which is acceptable at this
        scale and revisited when a canonical key column is added.
        """
        rows = self._connection.execute(
            "SELECT content FROM memories WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [row["content"] for row in rows]

    def contents_by_status(self, user_id: str) -> list[tuple[str, str]]:
        """Return ``(content, status)`` for every memory the user owns.

        Duplicate detection canonicalises these in Python: SQLite's ``lower()``
        is ASCII-only and cannot strip punctuation or normalise Unicode. The
        cost is linear in a user's memory count, which is acceptable at this
        scale and revisited when a canonical key column is added.
        """
        rows = self._connection.execute(
            "SELECT content, status FROM memories WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [(row["content"], row["status"]) for row in rows]

    def get(self, memory_id: str, *, user_id: str) -> Memory | None:
        """Return one memory, or None if it does not exist *for this user*.

        ``user_id`` is part of the WHERE clause rather than a check applied
        afterwards, so another user's row is indistinguishable from a row that
        does not exist.
        """
        row = self._connection.execute(
            """
            SELECT id, user_id, agent_id, content, kind, created_at, updated_at,
                   status, superseded_at, superseded_by, superseded_reason, superseded_note
            FROM memories
            WHERE id = ? AND user_id = ?
            """,
            (memory_id, user_id),
        ).fetchone()
        return Memory(**dict(row)) if row is not None else None

    def get_many(self, *, user_id: str, memory_ids: Sequence[str]) -> dict[str, Memory]:
        """Return the requested memories owned by ``user_id``, keyed by id."""
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._connection.execute(
            f"""
            SELECT id, user_id, agent_id, content, kind, created_at, updated_at,
                   status, superseded_at, superseded_by, superseded_reason, superseded_note
            FROM memories
            WHERE user_id = ? AND id IN ({placeholders})
            """,
            [user_id, *memory_ids],
        ).fetchall()
        return {row["id"]: Memory(**dict(row)) for row in rows}

    def list(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        kind: str | None = None,
        status: str = "active",
        query: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[Memory], int]:
        """Return one page of a user's memories and the total match count."""
        filters, params = self._filters(user_id, agent_id, kind, status)

        if query:
            rows, total = self._search(filters, params, query, limit, offset)
        else:
            rows, total = self._page(filters, params, limit, offset)

        return [Memory(**dict(row)) for row in rows], total

    def update(
        self,
        memory_id: str,
        *,
        user_id: str,
        updated_at: str,
        content: str | None = None,
        kind: str | None = None,
    ) -> Memory | None:
        """Apply the supplied changes, or return None if the row is not visible."""
        assignments = ["updated_at = ?"]
        params: list[object] = [updated_at]
        if content is not None:
            assignments.append("content = ?")
            params.append(content)
        if kind is not None:
            assignments.append("kind = ?")
            params.append(kind)
        params.extend((memory_id, user_id))

        with self._connection:
            cursor = self._connection.execute(
                f"UPDATE memories SET {', '.join(assignments)} "
                "WHERE id = ? AND user_id = ? AND status = 'active'",
                params,
            )
        if cursor.rowcount == 0:
            return None
        return self.get(memory_id, user_id=user_id)

    def delete(self, memory_id: str, *, user_id: str) -> bool:
        """Delete one memory. Returns False when it is not visible to ``user_id``."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            )
        return cursor.rowcount > 0

    def count(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        kind: str | None = None,
        status: str = "active",
    ) -> int:
        """Count the user's memories after filters, ignoring paging and search."""
        filters, params = self._filters(user_id, agent_id, kind, status)
        return self._connection.execute(
            f"SELECT COUNT(*) FROM memories m WHERE {' AND '.join(filters)}",
            params,
        ).fetchone()[0]

    def recent_ids(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        kind: str | None = None,
        limit: int,
    ) -> list[str]:
        """Return ids newest first. Feeds the recency signal of hybrid ranking."""
        filters, params = self._filters(user_id, agent_id, kind, "active")
        rows = self._connection.execute(
            f"SELECT m.id FROM memories m WHERE {' AND '.join(filters)} {_ORDER} LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [row["id"] for row in rows]

    def search_ranked(
        self,
        *,
        user_id: str,
        query: str,
        agent_id: str | None = None,
        kind: str | None = None,
        limit: int,
    ) -> list[Memory]:
        """Keyword search ordered by FTS5 relevance (bm25), best match first.

        This is deliberately separate from :meth:`list`, whose newest-first
        ordering is part of the existing ``GET /memories`` contract. Relevance
        ordering is only used by ``POST /memories/search``.
        """
        filters, params = self._filters(user_id, agent_id, kind, "active")
        expression = build_fts_expression(query)
        if expression is not None:
            try:
                rows = self._connection.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM memories m
                    JOIN memories_fts ON memories_fts.memory_id = m.id
                    WHERE {" AND ".join([*filters, "memories_fts MATCH ?"])}
                    ORDER BY bm25(memories_fts) ASC, m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    [*params, expression, limit],
                ).fetchall()
                return [Memory(**dict(row)) for row in rows]
            except sqlite3.OperationalError:
                # No FTS5 index: fall through to LIKE with newest-first order,
                # which is the same degraded behaviour as the list endpoint.
                pass

        patterns = build_like_patterns(query)
        if not patterns:
            return []
        like_filters = [*filters, *(r"m.content LIKE ? ESCAPE '\'" for _ in patterns)]
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS}
            FROM memories m
            WHERE {" AND ".join(like_filters)}
            {_ORDER}
            LIMIT ?
            """,
            [*params, *patterns, limit],
        ).fetchall()
        return [Memory(**dict(row)) for row in rows]

    def search_any(
        self,
        *,
        user_id: str,
        tokens: Sequence[str],
        exclude_id: str | None = None,
        limit: int,
    ) -> list[Memory]:
        """Active memories matching *any* token, best match first.

        Only conflict candidate generation uses this: requiring every word
        (normal search behaviour) would be too strict for finding related
        facts. Never exposed through the public search endpoint.
        """
        filters, params = self._filters(user_id, None, None, "active")
        if exclude_id is not None:
            filters.append("m.id != ?")
            params.append(exclude_id)

        expression = build_fts_any_expression(tokens)
        if expression is not None:
            try:
                rows = self._connection.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM memories m
                    JOIN memories_fts ON memories_fts.memory_id = m.id
                    WHERE {" AND ".join([*filters, "memories_fts MATCH ?"])}
                    ORDER BY bm25(memories_fts) ASC, m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    [*params, expression, limit],
                ).fetchall()
                return [Memory(**dict(row)) for row in rows]
            except sqlite3.OperationalError:
                pass

        patterns = build_like_patterns(" ".join(tokens))
        if not patterns:
            return []
        like_filters = [
            *filters,
            "(" + " OR ".join(r"m.content LIKE ? ESCAPE '\'" for _ in patterns) + ")",
        ]
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS}
            FROM memories m
            WHERE {" AND ".join(like_filters)}
            {_ORDER}
            LIMIT ?
            """,
            [*params, *patterns, limit],
        ).fetchall()
        return [Memory(**dict(row)) for row in rows]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _filters(
        user_id: str,
        agent_id: str | None,
        kind: str | None,
        status: str = "active",
    ) -> tuple[list[str], list[object]]:
        """Build the WHERE fragments that always apply, plus their parameters."""
        filters = ["m.user_id = ?"]
        params: list[object] = [user_id]
        if status != "all":
            filters.append("m.status = ?")
            params.append(status)
        if agent_id is not None:
            filters.append("m.agent_id = ?")
            params.append(agent_id)
        if kind is not None:
            filters.append("m.kind = ?")
            params.append(kind)
        return filters, params

    def supersede(
        self,
        memory_id: str,
        *,
        user_id: str,
        superseded_by: str,
        reason: str,
        note: str | None,
        at: str,
    ) -> Memory | None:
        """Mark a memory superseded, only if it is still active.

        The ``status = 'active'`` predicate makes concurrent supersessions safe:
        exactly one caller can win. Returns None when the row changed.

        Does **not** commit: callers run this inside a transaction so the status
        change and the conflict closure are atomic.
        """
        cursor = self._connection.execute(
            """
            UPDATE memories
            SET status = 'superseded', superseded_at = ?, superseded_by = ?,
                superseded_reason = ?, superseded_note = ?
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (at, superseded_by, reason, note, memory_id, user_id),
        )
        if cursor.rowcount == 0:
            return None
        return self.get(memory_id, user_id=user_id)

    def is_keyword_indexed(self, memory_id: str) -> bool:
        """Whether an FTS row exists for this memory (observed fact, no score)."""
        try:
            row = self._connection.execute(
                "SELECT 1 FROM memories_fts WHERE memory_id = ? LIMIT 1",
                (memory_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _page(
        self,
        filters: list[str],
        params: list[object],
        limit: int,
        offset: int,
        *,
        join: str = "",
    ) -> tuple[list[sqlite3.Row], int]:
        """Run the page query and its matching count query."""
        where = " AND ".join(filters)
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM memories m {join} WHERE {where} {_ORDER} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        total = self._connection.execute(
            f"SELECT COUNT(*) FROM memories m {join} WHERE {where}",
            params,
        ).fetchone()[0]
        return rows, total

    def _search(
        self,
        filters: list[str],
        params: list[object],
        query: str,
        limit: int,
        offset: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Search with FTS5, falling back to LIKE if the index is unavailable."""
        expression = build_fts_expression(query)
        if expression is not None:
            try:
                return self._page(
                    [*filters, "memories_fts MATCH ?"],
                    [*params, expression],
                    limit,
                    offset,
                    join="JOIN memories_fts ON memories_fts.memory_id = m.id",
                )
            except sqlite3.OperationalError:
                # FTS5 is not compiled in, or the index table is missing. The
                # LIKE path below still returns correct, scoped results.
                pass

        patterns = build_like_patterns(query)
        if not patterns:
            return [], 0
        like_filters = [*filters, *(r"m.content LIKE ? ESCAPE '\'" for _ in patterns)]
        return self._page(like_filters, [*params, *patterns], limit, offset)
