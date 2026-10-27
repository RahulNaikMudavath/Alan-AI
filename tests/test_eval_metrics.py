"""Hand-computed metric contracts and edge cases."""

import math

import pytest

from app.evaluation.metrics import (
    dedupe_preserving_order,
    hit_at_k,
    median,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_precision_matches_hand_calculation() -> None:
    retrieved = ["m3", "m1", "m4", "m2"]
    relevant = {"m1", "m2"}

    assert precision_at_k(retrieved, relevant, 1) == 0.0
    assert precision_at_k(retrieved, relevant, 3) == pytest.approx(1 / 3)
    assert precision_at_k(retrieved, relevant, 4) == pytest.approx(0.5)


def test_recall_matches_hand_calculation() -> None:
    retrieved = ["m3", "m1", "m4", "m2"]
    relevant = {"m1", "m2"}

    assert recall_at_k(retrieved, relevant, 1) == 0.0
    assert recall_at_k(retrieved, relevant, 3) == pytest.approx(0.5)
    assert recall_at_k(retrieved, relevant, 4) == 1.0


def test_reciprocal_rank_matches_hand_calculation() -> None:
    assert reciprocal_rank(["m3", "m1", "m2"], {"m1", "m2"}) == pytest.approx(0.5)
    assert reciprocal_rank(["m1", "m2"], {"m1", "m2"}) == 1.0
    assert reciprocal_rank(["m3", "m4"], {"m1"}) == 0.0


def test_ndcg_matches_hand_calculation() -> None:
    retrieved = ["m3", "m1", "m4", "m2"]
    relevant = {"m1", "m2"}

    assert ndcg_at_k(retrieved, relevant, 1) == 0.0
    expected = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
    assert ndcg_at_k(retrieved, relevant, 3) == pytest.approx(expected)
    # At K=4 the two relevant items are not in the ideal positions 1 and 2.
    expected_at_4 = (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
    assert ndcg_at_k(retrieved, relevant, 4) == pytest.approx(expected_at_4)


def test_hit_at_k() -> None:
    retrieved = ["m3", "m1"]

    assert hit_at_k(retrieved, {"m1"}, 1) == 0.0
    assert hit_at_k(retrieved, {"m1"}, 2) == 1.0
    assert hit_at_k(retrieved, {"m9"}, 2) == 0.0


def test_fewer_results_than_k() -> None:
    assert precision_at_k(["m1"], {"m1"}, 3) == pytest.approx(1 / 3)
    assert recall_at_k(["m1"], {"m1"}, 3) == 1.0
    assert ndcg_at_k(["m1"], {"m1"}, 3) == 1.0
    assert hit_at_k(["m1"], {"m1"}, 3) == 1.0


def test_empty_relevance_is_null_not_zero() -> None:
    assert precision_at_k(["m1"], set(), 3) is None
    assert recall_at_k(["m1"], set(), 3) is None
    assert reciprocal_rank(["m1"], set()) is None
    assert ndcg_at_k(["m1"], set(), 3) is None
    assert hit_at_k(["m1"], set(), 3) is None


def test_empty_results_with_relevance_is_zero() -> None:
    assert precision_at_k([], {"m1"}, 3) == 0.0
    assert recall_at_k([], {"m1"}, 3) == 0.0
    assert reciprocal_rank([], {"m1"}) == 0.0
    assert ndcg_at_k([], {"m1"}, 3) == 0.0
    assert hit_at_k([], {"m1"}, 3) == 0.0


def test_dedupe_preserves_first_occurrence() -> None:
    unique, duplicates = dedupe_preserving_order(["m1", "m1", "m2", "m1"])

    assert unique == ["m1", "m2"]
    assert duplicates == ["m1", "m1"]


def test_duplicates_do_not_change_ideal_metrics() -> None:
    unique, duplicates = dedupe_preserving_order(["m1", "m1", "m2"])

    assert duplicates == ["m1"]
    assert precision_at_k(unique, {"m1"}, 3) == pytest.approx(1 / 3)
    assert recall_at_k(unique, {"m1"}, 3) == 1.0
    assert ndcg_at_k(unique, {"m1"}, 3) == 1.0


def test_percentile_nearest_rank() -> None:
    values = list(range(1, 21))

    assert percentile(values, 95) == 19
    assert percentile(values, 50) == 10
    assert percentile([], 95) is None


def test_median_handles_even_and_odd_counts() -> None:
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
    assert median([]) is None
