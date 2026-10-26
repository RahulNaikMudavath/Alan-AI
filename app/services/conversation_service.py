"""Conversation ingestion: extraction, deduplication, and atomic persistence.

The order of operations is the design:

1. Enforce request-size limits **before** calling a provider, so an oversized
   payload cannot cost money.
2. Check idempotency **before** calling a provider, so a retry is free.
3. Call the provider outside any database transaction.
4. Validate each fact individually (a bad fact is a skip, not a failure).
5. Open one ``BEGIN IMMEDIATE`` transaction and write the conversation receipt,
   the memories, and the provenance together.
6. Embed the new memories after commit, best effort.

Because raw messages are only stored when explicitly enabled, the default
on-disk result of ingestion is extracted facts plus provenance indexes, not the
conversation itself.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from app.config import Settings
from app.llm.base import LLMProvider
from app.models import (
    MAX_FACT_CONTENT_CHARS,
    ConversationIngestRequest,
    ConversationIngestResponse,
    ExtractedFact,
    ExtractorInfo,
    Memory,
    RawExtraction,
    SkippedMemory,
    SkipReason,
)
from app.repositories.conversations import ConversationRecord, ConversationRepository
from app.repositories.memories import MemoryRepository
from app.repositories.transactions import transaction
from app.services.canonical import canonical_content, payload_fingerprint
from app.services.conflict_service import ConflictService
from app.services.extraction import PROMPT_VERSION, build_prompt, parse_extraction
from app.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Bump when the fingerprint payload changes shape, so stored receipts are not
# compared against a different definition.
_FINGERPRINT_VERSION = 1


class ConversationConflictError(Exception):
    """Same user and conversation id, but a different payload."""


class ExtractionDisabledError(Exception):
    """No extraction provider is configured."""


class ConversationLimitError(Exception):
    """The request exceeds a configured size limit."""


class ConversationService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        conversations: ConversationRepository,
        memories: MemoryRepository,
        memory_service: MemoryService,
        provider: LLMProvider | None,
        settings: Settings,
        conflicts: ConflictService | None = None,
    ) -> None:
        self._connection = connection
        self._conversations = conversations
        self._memories = memories
        self._memory_service = memory_service
        self._provider = provider
        self._settings = settings
        self._conflicts = conflicts

    def ingest(self, request: ConversationIngestRequest) -> tuple[ConversationIngestResponse, bool]:
        """Ingest one conversation. Returns ``(receipt, replayed)``."""
        self._check_limits(request)

        fingerprint = self._fingerprint(request)
        existing = self._conversations.get(
            user_id=request.user_id, conversation_id=request.conversation_id
        )
        if existing is not None:
            return self._replay(existing, fingerprint)

        if self._provider is None:
            raise ExtractionDisabledError("fact extraction is disabled")

        # Provider call and parsing happen with no transaction open: a slow
        # model must never hold the SQLite write lock.
        prompt = build_prompt(request.messages)
        raw = self._provider.complete(prompt)
        parsed = parse_extraction(raw)

        try:
            return self._persist(request, parsed, fingerprint), False
        except sqlite3.IntegrityError:
            # Another request won the race for this (user_id, conversation_id).
            # Its transaction committed; ours rolled back completely.
            existing = self._conversations.get(
                user_id=request.user_id, conversation_id=request.conversation_id
            )
            if existing is None:
                raise
            return self._replay(existing, fingerprint)

    # -- persistence -------------------------------------------------------

    def _persist(
        self,
        request: ConversationIngestRequest,
        parsed: RawExtraction,
        fingerprint: str,
    ) -> ConversationIngestResponse:
        now = datetime.now(UTC).isoformat()
        extractor = ExtractorInfo(
            provider=self._provider.name if self._provider else "none",
            model=self._provider.model if self._provider else "none",
            prompt_version=PROMPT_VERSION,
        )

        with transaction(self._connection):
            # Read inside the write lock so concurrent ingestions cannot both
            # miss each other's memories.
            existing_active: set[str] = set()
            existing_superseded: set[str] = set()
            for content, memory_status in self._memories.contents_by_status(request.user_id):
                if memory_status == "active":
                    existing_active.add(canonical_content(content))
                else:
                    existing_superseded.add(canonical_content(content))
            batch_keys: set[str] = set()
            created: list[tuple[Memory, list[int]]] = []
            skipped: list[SkippedMemory] = []

            for item in parsed.memories:
                fact = self._validate_fact(item)
                if fact is None:
                    skipped.append(self._invalid_skip(item, len(request.messages)))
                    continue

                indexes = self._source_indexes(fact, request)
                skip = self._quality_skip(fact, len(created))
                if skip is not None:
                    skipped.append(
                        SkippedMemory(
                            content=fact.content,
                            kind=fact.kind,
                            reason=skip,
                            source_message_indexes=indexes,
                        )
                    )
                    continue

                key = canonical_content(fact.content)
                if key in batch_keys:
                    skipped.append(
                        SkippedMemory(
                            content=fact.content,
                            kind=fact.kind,
                            reason="duplicate_in_request",
                            source_message_indexes=indexes,
                        )
                    )
                    continue
                if key in existing_active:
                    skipped.append(
                        SkippedMemory(
                            content=fact.content,
                            kind=fact.kind,
                            reason="duplicate_existing",
                            source_message_indexes=indexes,
                        )
                    )
                    continue
                if key in existing_superseded:
                    skipped.append(
                        SkippedMemory(
                            content=fact.content,
                            kind=fact.kind,
                            reason="duplicate_superseded",
                            source_message_indexes=indexes,
                        )
                    )
                    continue

                batch_keys.add(key)
                created.append(
                    (
                        self._memory_service.prepare(
                            user_id=request.user_id,
                            agent_id=request.agent_id,
                            content=fact.content,
                            kind=fact.kind,
                            created_at=now,
                        ),
                        indexes,
                    )
                )

            response = ConversationIngestResponse(
                conversation_id=request.conversation_id,
                replayed=False,
                messages_received=len(request.messages),
                memories_created=[memory for memory, _ in created],
                memories_skipped=skipped,
                extractor=extractor,
            )

            # Receipt first: its primary key is the idempotency backstop for a
            # concurrent duplicate, and a conflict rolls back everything below.
            self._conversations.insert(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                agent_id=request.agent_id,
                message_count=len(request.messages),
                memories_created=len(created),
                memories_skipped=len(skipped),
                extractor=extractor.model_dump_json(),
                payload_hash=fingerprint,
                result_json=response.model_dump_json(),
                created_at=now,
            )
            self._memories.insert_rows([memory for memory, _ in created])
            self._conversations.add_sources(
                [
                    (
                        memory.id,
                        request.user_id,
                        request.conversation_id,
                        json.dumps(indexes),
                        now,
                    )
                    for memory, indexes in created
                ]
            )
            if self._settings.store_messages:
                self._conversations.add_messages(
                    [
                        (
                            request.user_id,
                            request.conversation_id,
                            position,
                            message.role,
                            message.content,
                        )
                        for position, message in enumerate(request.messages)
                    ]
                )

        # Committed. Embedding and conflict detection are derived data: both
        # are best effort and neither can roll back a stored memory.
        stored = [memory for memory, _ in created]
        self._memory_service.index_many(stored)
        if self._conflicts is not None:
            self._conflicts.detect_for_many(stored)
        return response

    # -- decisions ---------------------------------------------------------

    def _check_limits(self, request: ConversationIngestRequest) -> None:
        settings = self._settings
        if len(request.messages) > settings.max_messages_per_conversation:
            raise ConversationLimitError(
                f"at most {settings.max_messages_per_conversation} messages are allowed"
            )
        for position, message in enumerate(request.messages):
            if len(message.content) > settings.max_message_chars:
                raise ConversationLimitError(
                    f"message {position} exceeds {settings.max_message_chars} characters"
                )
        total = sum(len(message.content) for message in request.messages)
        if total > settings.max_conversation_chars:
            raise ConversationLimitError(
                f"conversation exceeds {settings.max_conversation_chars} characters"
            )

    def _quality_skip(self, fact: ExtractedFact, accepted: int) -> SkipReason | None:
        """Return a skip reason for quality/cap problems, or None to accept."""
        if len(fact.content) > self._settings.max_fact_chars:
            return "too_long"
        if fact.confidence < self._settings.min_fact_confidence:
            return "low_confidence"
        if accepted >= self._settings.max_facts_per_conversation:
            return "limit_exceeded"
        return None

    @staticmethod
    def _validate_fact(item: object) -> ExtractedFact | None:
        if not isinstance(item, dict):
            return None
        try:
            return ExtractedFact.model_validate(item)
        except ValidationError:
            return None

    def _invalid_skip(self, item: object, message_count: int) -> SkippedMemory:
        """Describe a fact that could not be validated, defensively."""
        if not isinstance(item, dict):
            return SkippedMemory(content="", reason="invalid_fact")
        content = item.get("content")
        kind = item.get("kind")
        indexes = item.get("source_message_indexes")
        return SkippedMemory(
            content=content[:MAX_FACT_CONTENT_CHARS] if isinstance(content, str) else "",
            kind=kind if isinstance(kind, str) else None,
            reason="invalid_fact",
            source_message_indexes=self._clean_indexes(indexes, message_count),
        )

    @staticmethod
    def _clean_indexes(raw: object, message_count: int) -> list[int]:
        if not isinstance(raw, list):
            return []
        return sorted(
            {
                index
                for index in raw
                if isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < message_count
            }
        )

    def _source_indexes(self, fact: ExtractedFact, request: ConversationIngestRequest) -> list[int]:
        """Keep valid citations; fall back to every user message."""
        indexes = self._clean_indexes(fact.source_message_indexes, len(request.messages))
        if indexes:
            return indexes
        user_indexes = [
            position for position, message in enumerate(request.messages) if message.role == "user"
        ]
        if user_indexes:
            return user_indexes
        return list(range(len(request.messages)))

    # -- idempotency -------------------------------------------------------

    @staticmethod
    def _fingerprint(request: ConversationIngestRequest) -> str:
        """Hash the parts of the request that change what would be extracted.

        ``user_id`` and ``conversation_id`` are the lookup key, so they are not
        part of the payload hash.
        """
        return payload_fingerprint(
            {
                "version": _FINGERPRINT_VERSION,
                "agent_id": request.agent_id,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
            }
        )

    def _replay(
        self, record: ConversationRecord, fingerprint: str
    ) -> tuple[ConversationIngestResponse, bool]:
        if record.payload_hash != fingerprint:
            raise ConversationConflictError(
                "conversation_id already exists with a different payload"
            )
        stored = ConversationIngestResponse.model_validate_json(record.result_json)
        return stored.model_copy(update={"replayed": True}), True
