"""Conflict candidates and their explicit resolutions.

A row here is a *candidate*, not a verdict: detection is a narrow lexical
heuristic and never changes memory state. Rows are written with
``ON CONFLICT DO NOTHING`` so concurrent scans cannot create duplicates.

Write methods do not commit; callers use
:func:`app.repositories.transactions.transaction`.
"""

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ConflictRecord:
    id: str
    user_id: str
    memory_a: str
    memory_b: str
    status: str
    rule: str
    evidence: dict
    resolution: str | None
    detected_at: str
    resolved_at: str | None


def _record(row: sqlite3.Row) -> ConflictRecord:
    return ConflictRecord(
        id=row["id"],
        user_id=row["user_id"],
        memory_a=row["memory_a"],
        memory_b=row["memory_b"],
        status=row["status"],
        rule=row["rule"],
        evidence=json.loads(row["evidence"]),
        resolution=row["resolution"],
        detected_at=row["detected_at"],
        resolved_at=row["resolved_at"],
    )


_COLUMNS = (
    "id, user_id, memory_a, memory_b, status, rule, evidence, resolution, detected_at, resolved_at"
)


class ConflictRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_candidate(
        self,
        *,
        conflict_id: str,
        user_id: str,
        memory_a: str,
        memory_b: str,
        rule: str,
        evidence: dict,
        detected_at: str,
    ) -> bool:
        """Insert a pair once. Returns True when this call created the row."""
        cursor = self._connection.execute(
            """
            INSERT INTO memory_conflicts
                (id, user_id, memory_a, memory_b, status, rule, evidence, detected_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
            ON CONFLICT (user_id, memory_a, memory_b) DO NOTHING
            """,
            (
                conflict_id,
                user_id,
                memory_a,
                memory_b,
                rule,
                json.dumps(evidence, sort_keys=True),
                detected_at,
            ),
        )
        return cursor.rowcount == 1

    def get(self, conflict_id: str, *, user_id: str) -> ConflictRecord | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM memory_conflicts WHERE id = ? AND user_id = ?",
            (conflict_id, user_id),
        ).fetchone()
        return _record(row) if row is not None else None

    def list(
        self,
        *,
        user_id: str,
        status: str = "open",
        limit: int,
        offset: int,
    ) -> tuple[list[ConflictRecord], int]:
        filters = ["user_id = ?"]
        params: list[object] = [user_id]
        if status != "all":
            filters.append("status = ?")
            params.append(status)
        where = " AND ".join(filters)
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS} FROM memory_conflicts
            WHERE {where}
            ORDER BY detected_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        total = self._connection.execute(
            f"SELECT COUNT(*) FROM memory_conflicts WHERE {where}", params
        ).fetchone()[0]
        return [_record(row) for row in rows], total

    def list_for_memory(self, *, user_id: str, memory_id: str) -> list[ConflictRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS} FROM memory_conflicts
            WHERE user_id = ? AND (memory_a = ? OR memory_b = ?)
            ORDER BY detected_at DESC, id DESC
            """,
            (user_id, memory_id, memory_id),
        ).fetchall()
        return [_record(row) for row in rows]

    def mark_resolved(
        self,
        conflict_id: str,
        *,
        user_id: str,
        resolution: str,
        resolved_at: str,
    ) -> bool:
        """Resolve an open conflict. False means it was already resolved."""
        cursor = self._connection.execute(
            """
            UPDATE memory_conflicts
            SET status = 'resolved', resolution = ?, resolved_at = ?
            WHERE id = ? AND user_id = ? AND status = 'open'
            """,
            (resolution, resolved_at, conflict_id, user_id),
        )
        return cursor.rowcount == 1

    def close_open_for_memory(self, *, user_id: str, memory_id: str, resolved_at: str) -> int:
        """Close every open candidate involving a memory that just changed state."""
        cursor = self._connection.execute(
            """
            UPDATE memory_conflicts
            SET status = 'resolved', resolution = 'superseded', resolved_at = ?
            WHERE user_id = ? AND status = 'open'
              AND (memory_a = ? OR memory_b = ?)
            """,
            (resolved_at, user_id, memory_id, memory_id),
        )
        return cursor.rowcount
