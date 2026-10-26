"""Pydantic schemas: the shape of data crossing the API boundary.

These models are deliberately separate from the database rows. They validate
input before it reaches SQL, and they define exactly what the API returns so a
new column cannot leak by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryKind = Literal["fact", "preference", "event", "task"]


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
    """A stored memory as returned by the API."""

    id: str
    user_id: str
    agent_id: str | None
    content: str
    kind: MemoryKind
    created_at: datetime
    updated_at: datetime


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
