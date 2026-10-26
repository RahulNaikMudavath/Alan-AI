"""Conversation receipts, provenance, and optional raw message storage.

The write methods here do **not** commit. They are only used inside
:func:`app.repositories.transactions.transaction`, so the conversation receipt,
the memories it produced, and the provenance rows are committed together.

Read methods are always scoped by ``user_id``: a conversation id is only unique
within a user.
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationRecord:
    """A previously ingested conversation, as needed to serve a replay."""

    conversation_id: str
    payload_hash: str
    result_json: str


class ConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, *, user_id: str, conversation_id: str) -> ConversationRecord | None:
        """Return this user's receipt for ``conversation_id``, if it exists."""
        row = self._connection.execute(
            """
            SELECT conversation_id, payload_hash, result_json
            FROM conversations
            WHERE user_id = ? AND conversation_id = ?
            """,
            (user_id, conversation_id),
        ).fetchone()
        if row is None:
            return None
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            payload_hash=row["payload_hash"],
            result_json=row["result_json"],
        )

    def insert(
        self,
        *,
        user_id: str,
        conversation_id: str,
        agent_id: str | None,
        message_count: int,
        memories_created: int,
        memories_skipped: int,
        extractor: str,
        payload_hash: str,
        result_json: str,
        created_at: str,
    ) -> None:
        """Insert a conversation receipt. Must run inside a transaction."""
        self._connection.execute(
            """
            INSERT INTO conversations (
                user_id, conversation_id, agent_id, message_count,
                memories_created, memories_skipped, extractor,
                payload_hash, result_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                conversation_id,
                agent_id,
                message_count,
                memories_created,
                memories_skipped,
                extractor,
                payload_hash,
                result_json,
                created_at,
            ),
        )

    def add_sources(
        self,
        rows: Sequence[tuple[str, str, str, str, str]],
    ) -> None:
        """Record provenance rows ``(memory_id, user_id, conversation_id, indexes, created_at)``.

        Must run inside a transaction.
        """
        if not rows:
            return
        self._connection.executemany(
            """
            INSERT INTO memory_sources
                (memory_id, user_id, conversation_id, message_indexes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def add_messages(
        self,
        rows: Sequence[tuple[str, str, int, str, str]],
    ) -> None:
        """Store raw messages ``(user_id, conversation_id, position, role, content)``.

        Only called when ``RECALLKIT_STORE_MESSAGES`` is enabled. Must run
        inside a transaction.
        """
        if not rows:
            return
        self._connection.executemany(
            """
            INSERT INTO conversation_messages
                (user_id, conversation_id, position, role, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def sources_for_memory(self, *, memory_id: str, user_id: str) -> list[sqlite3.Row]:
        """Return provenance joined with its conversation metadata."""
        return self._connection.execute(
            """
            SELECT s.conversation_id, s.message_indexes,
                   c.created_at AS conversation_created_at, c.extractor
            FROM memory_sources s
            JOIN conversations c
              ON c.user_id = s.user_id AND c.conversation_id = s.conversation_id
            WHERE s.memory_id = ? AND s.user_id = ?
            ORDER BY c.created_at DESC, s.conversation_id
            """,
            (memory_id, user_id),
        ).fetchall()

    def source_indexes(self, memory_id: str) -> list[int] | None:
        """Return stored provenance indexes for one memory, if any."""
        row = self._connection.execute(
            "SELECT message_indexes FROM memory_sources WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["message_indexes"])
