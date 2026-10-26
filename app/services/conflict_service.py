"""Conservative contradiction candidates and their explicit resolution.

Detection is deliberately narrow. It only flags a pair when:

* both memories are active and belong to the same user,
* their significant token sets overlap heavily (containment of the smaller
  set, default 0.8),
* exactly one side contains an explicit negation marker.

That finds "I like coffee" vs "I do not like coffee". It does not find
antonyms ("dark mode" vs "light mode") or different wording ("vegetarian" vs
"eats meat"). It can produce false positives when context differs
("I never drink coffee at night" vs "I drink coffee").

Because of that, detection is advisory. It writes a candidate row and never
changes a memory's status. Resolution is an explicit call, either through this
service or through the direct supersede endpoint. Nothing here produces a
confidence score; the recorded ``token_containment`` is a lexical observation.
"""

import logging
import re
import uuid
from datetime import UTC, datetime

from app.config import Settings
from app.models import (
    ConflictList,
    ConflictRead,
    ConflictResolveRequest,
    ConflictResolveResponse,
    ConflictScanResponse,
    Memory,
)
from app.repositories.conflicts import ConflictRecord, ConflictRepository
from app.repositories.memories import MemoryRepository
from app.repositories.transactions import transaction
from app.services.canonical import canonical_content
from app.services.errors import ConflictNotFoundError, ConflictStateError
from app.services.supersession import supersede_locked

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "i",
        "me",
        "my",
        "mine",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "it",
        "its",
        "they",
        "them",
        "their",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "so",
        "because",
        "while",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "about",
        "from",
        "as",
        "by",
        "that",
        "this",
        "these",
        "those",
        "there",
        "here",
        "very",
        "really",
        "just",
        "also",
        "too",
        "only",
        "now",
        "still",
        "already",
        "yet",
    }
)

_NEGATION_TOKENS = frozenset(
    {
        "not",
        "never",
        "no",
        "none",
        "nothing",
        "nobody",
        "cannot",
        "stopped",
        "quit",
        "avoid",
        "avoids",
        "avoided",
    }
)

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|none|nothing|nobody|cannot|without|stopped|quit|avoid(?:s|ed)?)\b"
    r"|\b(?:can|won|don|doesn|didn|isn|wasn|aren|weren|hasn|haven)['’]t\b",
    re.IGNORECASE,
)

# Contractions are expanded before canonicalisation because the canonical form
# turns apostrophes into spaces, which would hide the negation.
_CONTRACTION_RE = re.compile(r"\b(can|won|shan)['’]t\b", re.IGNORECASE)
_CONTRACTION_MAP = {"can": "can not", "won": "will not", "shan": "shall not"}


def _expand_contractions(text: str) -> str:
    text = _CONTRACTION_RE.sub(lambda match: _CONTRACTION_MAP[match.group(1).lower()], text)
    return re.sub(r"n['’]t\b", " not", text, flags=re.IGNORECASE)


def significant_tokens(text: str) -> set[str]:
    """Content words used for overlap, negation markers removed."""
    tokens = canonical_content(_expand_contractions(text)).split()
    return {
        token
        for token in tokens
        if token not in _STOPWORDS and token not in _NEGATION_TOKENS and len(token) > 1
    }


def has_negation(text: str) -> bool:
    return _NEGATION_RE.search(text) is not None


def token_containment(first: set[str], second: set[str]) -> float:
    """Share of the smaller token set that both sides contain."""
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


class ConflictService:
    def __init__(
        self,
        *,
        connection,
        memories: MemoryRepository,
        conflicts: ConflictRepository,
        settings: Settings,
    ) -> None:
        self._connection = connection
        self._memories = memories
        self._conflicts = conflicts
        self._settings = settings

    # -- detection ---------------------------------------------------------

    def detect_for(self, memory: Memory) -> list[str]:
        """Create candidates for one memory. Best effort; never raises."""
        if not self._settings.conflict_detection_enabled or memory.status != "active":
            return []
        try:
            pairs = self._pairs(memory)
        except Exception as exc:  # noqa: BLE001 - detection must not fail a write
            logger.warning("conflict_detection_failed error=%s", type(exc).__name__)
            return []
        if not pairs:
            return []

        now = datetime.now(UTC).isoformat()
        created: list[str] = []
        try:
            with transaction(self._connection):
                created = self._insert_pairs(memory.user_id, pairs, now)
        except Exception as exc:  # noqa: BLE001 - a candidate is advisory
            logger.warning("conflict_detection_write_failed error=%s", type(exc).__name__)
            return []
        return created

    def detect_for_many(self, memories: list[Memory]) -> list[str]:
        created: list[str] = []
        for memory in memories:
            created.extend(self.detect_for(memory))
        return created

    def scan(self, *, user_id: str) -> ConflictScanResponse:
        """Re-scan a user's active memories. Safe to run repeatedly."""
        now = datetime.now(UTC).isoformat()
        scanned = 0
        found: list[str] = []
        existing = 0
        with transaction(self._connection):
            memories, _ = self._memories.list(
                user_id=user_id,
                status="active",
                limit=self._settings.conflict_scan_limit,
                offset=0,
            )
            for memory in memories:
                scanned += 1
                pairs = self._pairs(memory)
                for conflict_id, inserted in self._insert_pairs_with_status(user_id, pairs, now):
                    if inserted:
                        found.append(conflict_id)
                    else:
                        existing += 1
        return ConflictScanResponse(
            scanned=scanned, conflicts_found=len(found), conflicts_existing=existing
        )

    def _pairs(self, memory: Memory) -> list[tuple[str, str, dict]]:
        """Return canonical ``(memory_a, memory_b, evidence)`` triples."""
        tokens = sorted(significant_tokens(memory.content))
        if not tokens:
            return []
        candidates = self._memories.search_any(
            user_id=memory.user_id,
            tokens=tokens[: self._settings.conflict_candidate_limit],
            exclude_id=memory.id,
            limit=self._settings.conflict_candidate_limit,
        )

        pairs: list[tuple[str, str, dict]] = []
        for candidate in candidates:
            if candidate.status != "active":
                continue
            first, second = (memory, candidate) if memory.id < candidate.id else (candidate, memory)
            evidence = self._evidence(first, second)
            if evidence is not None:
                pairs.append((first.id, second.id, evidence))
        return pairs

    def _evidence(self, first: Memory, second: Memory) -> dict | None:
        first_tokens = significant_tokens(first.content)
        second_tokens = significant_tokens(second.content)
        if not first_tokens or not second_tokens:
            return None

        shared = first_tokens & second_tokens
        if not shared:
            return None
        containment = token_containment(first_tokens, second_tokens)
        if containment < self._settings.conflict_overlap_threshold:
            return None

        first_negated = has_negation(first.content)
        second_negated = has_negation(second.content)
        if first_negated == second_negated:
            return None

        return {
            "rule": "explicit_negation",
            "shared_terms": sorted(shared),
            "negated": {"a": first_negated, "b": second_negated},
            "token_containment": round(containment, 3),
        }

    def _insert_pairs(
        self, user_id: str, pairs: list[tuple[str, str, dict]], now: str
    ) -> list[str]:
        return [
            conflict_id
            for conflict_id, inserted in self._insert_pairs_with_status(user_id, pairs, now)
            if inserted
        ]

    def _insert_pairs_with_status(
        self, user_id: str, pairs: list[tuple[str, str, dict]], now: str
    ) -> list[tuple[str, bool]]:
        results: list[tuple[str, bool]] = []
        for memory_a, memory_b, evidence in pairs:
            conflict_id = uuid.uuid4().hex
            inserted = self._conflicts.insert_candidate(
                conflict_id=conflict_id,
                user_id=user_id,
                memory_a=memory_a,
                memory_b=memory_b,
                rule="explicit_negation",
                evidence=evidence,
                detected_at=now,
            )
            results.append((conflict_id, inserted))
        return results

    # -- reading -----------------------------------------------------------

    def list_conflicts(
        self,
        *,
        user_id: str,
        status: str,
        limit: int,
        offset: int,
    ) -> ConflictList:
        records, total = self._conflicts.list(
            user_id=user_id, status=status, limit=limit, offset=offset
        )
        by_id = self._memories.get_many(
            user_id=user_id,
            memory_ids=[mid for record in records for mid in (record.memory_a, record.memory_b)],
        )
        items = []
        for record in records:
            first = by_id.get(record.memory_a)
            second = by_id.get(record.memory_b)
            if first is None or second is None:
                # A cascade removed a memory between the two reads.
                continue
            items.append(self._read(record, first, second))
        return ConflictList(items=items, total=total, limit=limit, offset=offset)

    @staticmethod
    def _read(record: ConflictRecord, first: Memory, second: Memory) -> ConflictRead:
        return ConflictRead(
            id=record.id,
            status=record.status,
            rule=record.rule,
            evidence=record.evidence,
            resolution=record.resolution,
            detected_at=record.detected_at,
            resolved_at=record.resolved_at,
            memory_a=first,
            memory_b=second,
        )

    # -- resolution --------------------------------------------------------

    def resolve(self, conflict_id: str, request: ConflictResolveRequest) -> ConflictResolveResponse:
        """Resolve one candidate. Everything happens in one transaction."""
        now = datetime.now(UTC).isoformat()
        user_id = request.user_id

        with transaction(self._connection):
            record = self._conflicts.get(conflict_id, user_id=user_id)
            if record is None:
                raise ConflictNotFoundError(conflict_id)
            if record.status != "open":
                raise ConflictStateError("conflict is already resolved")

            pair = self._memories.get_many(
                user_id=user_id, memory_ids=[record.memory_a, record.memory_b]
            )
            first = pair.get(record.memory_a)
            second = pair.get(record.memory_b)
            if first is None or second is None:
                raise ConflictNotFoundError(conflict_id)
            if first.status != "active" or second.status != "active":
                raise ConflictStateError("a memory in this conflict is no longer active")

            if request.resolution == "keep_a":
                resolution = "kept_a"
                loser, winner = second, first
            elif request.resolution == "keep_b":
                resolution = "kept_b"
                loser, winner = first, second
            else:
                resolution = "kept_both"
                loser = winner = None

            # Resolve this row first; supersession then closes only *other*
            # open candidates involving the retired memory.
            if not self._conflicts.mark_resolved(
                conflict_id, user_id=user_id, resolution=resolution, resolved_at=now
            ):
                raise ConflictStateError("conflict was resolved concurrently")

            if loser is not None and winner is not None:
                supersede_locked(
                    memories=self._memories,
                    conflicts=self._conflicts,
                    user_id=user_id,
                    memory_id=loser.id,
                    superseded_by=winner.id,
                    reason="contradiction",
                    note=request.note,
                    at=now,
                )

        refreshed = self._conflicts.get(conflict_id, user_id=user_id)
        pair = self._memories.get_many(
            user_id=user_id, memory_ids=[record.memory_a, record.memory_b]
        )
        first = pair.get(record.memory_a)
        second = pair.get(record.memory_b)
        if refreshed is None or first is None or second is None:  # pragma: no cover
            raise ConflictNotFoundError(conflict_id)

        superseded = replacement = None
        if request.resolution == "keep_a":
            superseded, replacement = second, first
        elif request.resolution == "keep_b":
            superseded, replacement = first, second
        return ConflictResolveResponse(
            conflict=self._read(refreshed, first, second),
            superseded=superseded,
            replacement=replacement,
        )
