"""Pure retrieval metrics.

The contract is deliberately small and explicit; the report embeds the same
definitions so a stored benchmark is self-describing.

* ``R`` is the set of relevant local IDs.
* ``L`` is the retrieved list **after de-duplication, preserving first
  occurrence**. Duplicates are reported separately by the runner.
* ``K`` is a positive cutoff.

Every relevance metric returns ``None`` when ``R`` is empty. No-answer queries
are scored by abstention instead; a missing value never silently becomes zero.
"""

import math
from collections.abc import Iterable, Sequence


def dedupe_preserving_order(ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return ``(unique_ids, duplicate_ids)`` preserving first occurrence."""
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    for item in ids:
        if item in seen:
            duplicates.append(item)
        else:
            seen.add(item)
            unique.append(item)
    return unique, duplicates


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float | None:
    """``|L@K ∩ R| / K``. The denominator is K even if fewer were returned."""
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    hits = sum(1 for item in retrieved[:k] if item in relevant_set)
    return hits / k


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float | None:
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    hits = sum(1 for item in retrieved[:k] if item in relevant_set)
    return hits / len(relevant_set)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float | None:
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float | None:
    """Binary-gain nDCG@K, with IDCG over ``min(|R|, K)``."""
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(retrieved[:k], start=1)
        if item in relevant_set
    )
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant_set), k) + 1))
    return dcg / ideal if ideal else None


def hit_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float | None:
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    return 1.0 if any(item in relevant_set for item in retrieved[:k]) else 0.0


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile. ``p`` is in (0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(p / 100.0 * len(ordered)) - 1
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
