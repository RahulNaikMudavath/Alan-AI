"""Evaluation dataset schema, validation, and hashing.

The dataset is synthetic, versioned, and human-reviewed. Every query carries a
``rationale`` and every supersession carries a ``supersession_rationale`` so a
label cannot be added without a stated justification.

Validation is intentionally strict: ground truth is defined once per query and
is reused unchanged by keyword, semantic, and hybrid evaluation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.models import MAX_SEARCH_QUERY_LENGTH, MemoryKind, SupersedeReason
from app.services.conflict_service import significant_tokens

SUPPORTED_SCHEMA_VERSION = 1

_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"

QueryCategory = Literal["relevance", "no_answer", "isolation"]


class DatasetError(ValueError):
    """Raised when a dataset cannot be loaded or is internally inconsistent."""


class EvalMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_id: str = Field(pattern=_ID_PATTERN)
    user_id: str = Field(min_length=1, max_length=128)
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=8000)
    agent_id: str | None = Field(default=None, max_length=128)
    superseded_by: str | None = Field(default=None, pattern=_ID_PATTERN)
    superseded_reason: SupersedeReason | None = None
    supersession_rationale: str | None = Field(default=None, max_length=1000)

    @field_validator("content", "user_id", "agent_id", "supersession_rationale")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _supersession_fields_are_consistent(self) -> EvalMemory:
        if self.superseded_by is None:
            if self.superseded_reason is not None or self.supersession_rationale is not None:
                raise ValueError(
                    "superseded_reason and supersession_rationale require superseded_by"
                )
            return self
        if self.superseded_reason is None:
            raise ValueError("superseded_by requires superseded_reason")
        if not self.supersession_rationale:
            raise ValueError("superseded_by requires a supersession_rationale")
        return self


class EvalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    category: QueryCategory
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    relevant: list[str] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=1000)

    @field_validator("query", "rationale", "user_id")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("relevant", "must_not_include")
    @classmethod
    def _no_duplicate_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate IDs")
        for item in value:
            if not re.match(_ID_PATTERN, item):
                raise ValueError(f"invalid memory ID: {item}")
        return value


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(pattern=_VERSION_PATTERN)
    schema_version: int
    users: list[str] = Field(min_length=1)
    memories: list[EvalMemory] = Field(min_length=1)
    queries: list[EvalQuery] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_consistency(self) -> EvalDataset:
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version}; "
                f"expected {SUPPORTED_SCHEMA_VERSION}"
            )
        if len(set(self.users)) != len(self.users):
            raise ValueError("users must be unique")

        users = set(self.users)
        memories: dict[str, EvalMemory] = {}
        for memory in self.memories:
            if memory.local_id in memories:
                raise ValueError(f"duplicate memory local_id: {memory.local_id}")
            if memory.user_id not in users:
                raise ValueError(
                    f"memory {memory.local_id} references unknown user {memory.user_id}"
                )
            memories[memory.local_id] = memory

        for memory in self.memories:
            if memory.superseded_by is None:
                continue
            if memory.superseded_by == memory.local_id:
                raise ValueError(f"memory {memory.local_id} cannot supersede itself")
            replacement = memories.get(memory.superseded_by)
            if replacement is None:
                raise ValueError(
                    f"memory {memory.local_id} references unknown replacement "
                    f"{memory.superseded_by}"
                )
            if replacement.superseded_by is not None:
                raise ValueError(f"supersession chain detected at {memory.superseded_by}")
            if replacement.user_id != memory.user_id:
                raise ValueError(
                    f"supersession {memory.local_id} -> {replacement.local_id} crosses users"
                )
            if replacement.kind != memory.kind:
                raise ValueError(
                    f"supersession {memory.local_id} -> {replacement.local_id} changes kind"
                )
            if replacement.content == memory.content:
                raise ValueError(
                    f"supersession {memory.local_id} -> {replacement.local_id} "
                    "does not change content"
                )
            shared = significant_tokens(memory.content) & significant_tokens(replacement.content)
            if not shared:
                raise ValueError(
                    f"supersession {memory.local_id} -> {replacement.local_id} "
                    "shares no significant token; are these the same fact?"
                )

        query_ids: set[str] = set()
        for query in self.queries:
            if query.id in query_ids:
                raise ValueError(f"duplicate query id: {query.id}")
            query_ids.add(query.id)
            if query.user_id not in users:
                raise ValueError(f"query {query.id} references unknown user {query.user_id}")

            relevant = set(query.relevant)
            for local_id in relevant:
                memory = memories.get(local_id)
                if memory is None:
                    raise ValueError(f"query {query.id} references unknown memory {local_id}")
                if memory.user_id != query.user_id:
                    raise ValueError(
                        f"query {query.id} relevant memory {local_id} belongs to another user"
                    )
                if memory.superseded_by is not None:
                    raise ValueError(f"query {query.id} relevant memory {local_id} is superseded")

            for local_id in query.must_not_include:
                if local_id not in memories:
                    raise ValueError(f"query {query.id} forbids unknown memory {local_id}")
            if relevant & set(query.must_not_include):
                raise ValueError(
                    f"query {query.id} lists the same memory as relevant and forbidden"
                )

            if query.category == "relevance" and not relevant:
                raise ValueError(f"query {query.id} must have relevant memories")
            if query.category == "no_answer" and relevant:
                raise ValueError(f"query {query.id} must not have relevant memories")
            if query.category == "isolation":
                cross_user = any(
                    memories[local_id].user_id != query.user_id
                    for local_id in query.must_not_include
                )
                if not cross_user:
                    raise ValueError(
                        f"query {query.id} needs a forbidden memory owned by another user"
                    )
        return self


@dataclass(frozen=True)
class LoadedDataset:
    dataset: EvalDataset
    sha256: str
    path: Path


def load_dataset(path: Path) -> LoadedDataset:
    """Read, hash, parse, and validate a dataset file."""
    if not path.exists():
        raise DatasetError(f"dataset not found: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DatasetError(f"{path.name}: not valid JSON") from exc
    try:
        dataset = EvalDataset.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise DatasetError(f"{path.name}: {location}: {first['msg']}") from exc
    return LoadedDataset(
        dataset=dataset,
        sha256=hashlib.sha256(raw).hexdigest(),
        path=path,
    )
