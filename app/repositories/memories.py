"""Data access for memories.

Every SQL statement in the project lives here. That keeps two promises easy to
enforce: parameters are always bound (never string-formatted into SQL), and
every read is scoped by ``user_id``.
"""

import sqlite3

from app.models import Memory
from app.repositories.search import build_fts_expression, build_like_patterns

_COLUMNS = "m.id, m.user_id, m.agent_id, m.content, m.kind, m.created_at, m.updated_at"

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

    def get(self, memory_id: str, *, user_id: str) -> Memory | None:
        """Return one memory, or None if it does not exist *for this user*.

        ``user_id`` is part of the WHERE clause rather than a check applied
        afterwards, so another user's row is indistinguishable from a row that
        does not exist.
        """
        row = self._connection.execute(
            """
            SELECT id, user_id, agent_id, content, kind, created_at, updated_at
            FROM memories
            WHERE id = ? AND user_id = ?
            """,
            (memory_id, user_id),
        ).fetchone()
        return Memory(**dict(row)) if row is not None else None

    def list(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[Memory], int]:
        """Return one page of a user's memories and the total match count."""
        filters, params = self._filters(user_id, agent_id, kind)

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
                f"UPDATE memories SET {', '.join(assignments)} WHERE id = ? AND user_id = ?",
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

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _filters(
        user_id: str, agent_id: str | None, kind: str | None
    ) -> tuple[list[str], list[object]]:
        """Build the WHERE fragments that always apply, plus their parameters."""
        filters = ["m.user_id = ?"]
        params: list[object] = [user_id]
        if agent_id is not None:
            filters.append("m.agent_id = ?")
            params.append(agent_id)
        if kind is not None:
            filters.append("m.kind = ?")
            params.append(kind)
        return filters, params

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
