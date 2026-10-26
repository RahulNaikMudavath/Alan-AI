"""Pydantic schemas: the shape of data crossing the API boundary.

These models are deliberately separate from the database rows. They validate
input before it reaches SQL, and they define exactly what the API returns so a
new column cannot leak by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryKind = Literal["fact", "preference", "event", "task"]
MemoryStatus = Literal["active", "superseded"]
MemoryStatusFilter = Literal["active", "superseded", "all"]
SupersedeReason = Literal["user_correction", "outdated", "contradiction", "duplicate", "other"]


def _strip_non_blank(value: str | None) -> str | None:
    """Trim text fields and reject values that are only whitespace."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class MemoryCreate(BaseModel):
    """Request body for storing a memory."""

    # Reject unknown fields instead of silently dropping a typo such as
    # "userid" and storing the memory under the wrong owner.
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128, description="Owner of the memory.")
    agent_id: str | None = Field(
        default=None, max_length=128, description="Optional agent that recorded it."
    )
    content: str = Field(min_length=1, max_length=8000, description="The remembered text.")
    kind: MemoryKind = Field(default="fact", description="Category of the memory.")

    @field_validator("user_id", "agent_id", "content")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        return _strip_non_blank(value)


class MemoryUpdate(BaseModel):
    """Request body for a partial update.

    Both fields are optional, but an empty body is rejected: a PATCH that
    changes nothing is far more likely to be a client bug than an intent.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=8000)
    kind: MemoryKind | None = None

    @field_validator("content")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        return _strip_non_blank(value)

    @model_validator(mode="after")
    def _require_a_change(self) -> MemoryUpdate:
        if not self.model_fields_set:
            raise ValueError("provide at least one of 'content' or 'kind'")
        # An explicit null is a mistake, not "leave this field alone": omit the
        # field to leave it alone.
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"'{name}' must not be null")
        return self


class Memory(BaseModel):
    """A stored memory as returned by the API.

    The lifecycle fields are additive and defaulted so receipts written before
    Milestone 3.3 still deserialize.
    """

    id: str
    user_id: str
    agent_id: str | None
    content: str
    kind: MemoryKind
    created_at: datetime
    updated_at: datetime
    status: MemoryStatus = "active"
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    superseded_reason: SupersedeReason | None = None
    superseded_note: str | None = None


class MemoryList(BaseModel):
    """One page of memories plus enough metadata to request the next page."""

    items: list[Memory]
    total: int = Field(description="Total memories matching the filters, ignoring paging.")
    limit: int
    offset: int


SearchMode = Literal["keyword", "semantic", "hybrid"]
MatchSource = Literal["keyword", "semantic"]

# Query text is embedded as-is, so it may be a whole question rather than a few
# keywords. The cap keeps a request from becoming an unbounded provider call.
MAX_SEARCH_QUERY_LENGTH = 2000


class MemorySearchRequest(BaseModel):
    """Body of ``POST /memories/search``."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    mode: SearchMode = Field(
        default="hybrid",
        description=(
            "keyword: FTS5 only. semantic: cosine similarity only. "
            "hybrid: reciprocal rank fusion of both plus a recency signal."
        ),
    )
    agent_id: str | None = Field(default=None, max_length=128)
    kind: MemoryKind | None = None
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("user_id", "query", "agent_id")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        return _strip_non_blank(value)


class MemorySearchResult(BaseModel):
    """One ranked hit, with enough detail to explain why it matched."""

    memory: Memory
    score: float = Field(
        description="Fused rank score. Relative within this response; not a probability."
    )
    similarity: float | None = Field(
        default=None, description="Cosine similarity when a semantic signal matched."
    )
    matched_by: list[MatchSource] = Field(
        description="Which signals found this memory. Recency only reorders."
    )


class EmbeddingCoverage(BaseModel):
    """How much of the scoped memory set semantic search could actually see."""

    embedded: int = Field(description="Memories with a vector for the active model.")
    considered: int = Field(description="Memories matching the user and filters.")


class MemorySearchResponse(BaseModel):
    mode: SearchMode
    query: str
    degraded: bool = Field(
        description=(
            "True when semantic scoring was requested but unavailable, so results "
            "come from keyword matching only."
        )
    )
    coverage: EmbeddingCoverage
    results: list[MemorySearchResult]


class HealthResponse(BaseModel):
    """Result of the liveness probe."""

    status: Literal["ok", "error"]
    database: Literal["ok", "error"]


# -- conversation ingestion ------------------------------------------------

MessageRole = Literal["user", "assistant", "system", "tool"]

# Absolute ceilings enforced by the schema, before any configuration is
# consulted. They protect the process from a hostile payload; the configurable
# limits in Settings are usually stricter and produce friendlier 422 messages.
MAX_MESSAGE_CHARS = 32000
MAX_MESSAGES = 200
MAX_CONVERSATION_CHARS = 200_000

# The model is asked for at most this many items. More than this is treated as
# a provider contract violation rather than silently truncating.
MAX_RAW_FACTS = 100

# Individual facts may be longer than the configured limit; the service reports
# those as skipped ("too_long") instead of failing the whole request.
MAX_FACT_CONTENT_CHARS = 4000

SkipReason = Literal[
    "duplicate_existing",
    "duplicate_in_request",
    "low_confidence",
    "too_long",
    "invalid_fact",
    "limit_exceeded",
    "duplicate_superseded",
]


class ConversationMessage(BaseModel):
    """One message in the submitted conversation. Treated as untrusted data."""

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        return _strip_non_blank(value) or ""


class ConversationIngestRequest(BaseModel):
    """Body of ``POST /conversations``."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    # Client-supplied so retries can be idempotent. Scoped by user_id, so two
    # users may use the same value independently.
    conversation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Client-owned idempotency key, unique within a user.",
    )
    agent_id: str | None = Field(default=None, max_length=128)
    messages: list[ConversationMessage] = Field(min_length=1, max_length=MAX_MESSAGES)

    @field_validator("user_id", "agent_id", "conversation_id")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        return _strip_non_blank(value)

    @model_validator(mode="after")
    def _within_absolute_size(self) -> ConversationIngestRequest:
        total = sum(len(message.content) for message in self.messages)
        if total > MAX_CONVERSATION_CHARS:
            raise ValueError(
                f"conversation exceeds the absolute limit of {MAX_CONVERSATION_CHARS} characters"
            )
        return self


class ExtractedFact(BaseModel):
    """One fact as claimed by the extraction model.

    Strict on purpose: the model's output is untrusted, and this is the only
    shape allowed to become a memory row.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_FACT_CONTENT_CHARS)
    kind: MemoryKind
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_message_indexes: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("content")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        return _strip_non_blank(value) or ""


class RawExtraction(BaseModel):
    """Top-level model output.

    Items are validated individually by the service so one bad fact becomes a
    skip instead of failing the whole conversation.
    """

    model_config = ConfigDict(extra="forbid")

    memories: list[Any] = Field(max_length=MAX_RAW_FACTS)


class SkippedMemory(BaseModel):
    """A candidate that was understood but deliberately not stored."""

    content: str
    kind: str | None = None
    reason: SkipReason
    source_message_indexes: list[int] = Field(default_factory=list)


class ExtractorInfo(BaseModel):
    provider: str
    model: str
    prompt_version: str


class ConversationIngestResponse(BaseModel):
    conversation_id: str
    replayed: bool
    messages_received: int
    memories_created: list[Memory]
    memories_skipped: list[SkippedMemory]
    extractor: ExtractorInfo


# -- lifecycle and explainability ------------------------------------------


class SupersedeRequest(BaseModel):
    """Body of ``POST /memories/{id}/supersede``."""

    model_config = ConfigDict(extra="forbid")

    superseded_by: str = Field(min_length=1, max_length=64)
    reason: SupersedeReason
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SupersedeResponse(BaseModel):
    superseded: Memory
    replacement: Memory


ConflictStatus = Literal["open", "resolved"]
ConflictResolution = Literal["kept_a", "kept_b", "kept_both", "superseded"]
ConflictStatusFilter = Literal["open", "resolved", "all"]


class ConflictRead(BaseModel):
    """A candidate contradiction, with both memories and the evidence."""

    id: str
    status: ConflictStatus
    rule: str
    evidence: dict[str, Any]
    resolution: ConflictResolution | None
    detected_at: datetime
    resolved_at: datetime | None
    memory_a: Memory
    memory_b: Memory


class ConflictList(BaseModel):
    items: list[ConflictRead]
    total: int
    limit: int
    offset: int


class ConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    resolution: Literal["keep_a", "keep_b", "keep_both"]
    note: str | None = Field(default=None, max_length=500)

    @field_validator("user_id")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        return _strip_non_blank(value) or ""

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ConflictResolveResponse(BaseModel):
    conflict: ConflictRead
    superseded: Memory | None = None
    replacement: Memory | None = None


class ConflictScanResponse(BaseModel):
    scanned: int
    conflicts_found: int
    conflicts_existing: int


class ExplainSource(BaseModel):
    conversation_id: str
    message_indexes: list[int]
    conversation_created_at: datetime
    extractor: ExtractorInfo | None


class ExplainIndexes(BaseModel):
    keyword: bool
    embedding_models: list[str]


class ExplainLifecycle(BaseModel):
    status: MemoryStatus
    superseded_at: datetime | None
    superseded_by: str | None
    reason: SupersedeReason | None
    note: str | None


class ExplainObserved(BaseModel):
    """Facts read directly from stored rows."""

    origin: Literal["direct", "conversation"]
    sources: list[ExplainSource]
    indexes: ExplainIndexes
    lifecycle: ExplainLifecycle


class ExplainConflictCandidate(BaseModel):
    """A heuristic candidate. Evidence only; never a confidence score."""

    conflict_id: str
    other_memory_id: str
    rule: str
    evidence: dict[str, Any]
    status: ConflictStatus


class ExplainInferred(BaseModel):
    conflict_candidates: list[ExplainConflictCandidate]


class MemoryExplainResponse(BaseModel):
    memory: Memory
    observed: ExplainObserved
    inferred: ExplainInferred
