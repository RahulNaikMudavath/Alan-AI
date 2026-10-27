"""Fabricated-result tests: metrics, isolation, forbidden, degraded, exit codes."""

import pytest

from app.evaluation.dataset import EvalQuery
from app.evaluation.report import exit_code_for
from app.evaluation.runner import RetrievedItem, _failures, score_query


def _query(
    *,
    relevant: list[str] | None = None,
    forbidden: list[str] | None = None,
    user_id: str = "u1",
    category: str = "relevance",
) -> EvalQuery:
    return EvalQuery(
        id="q1",
        category=category,
        user_id=user_id,
        query="test query",
        relevant=relevant or [],
        must_not_include=forbidden or [],
        rationale="fabricated test case",
    )


def _item(local_id: str, *, user_id: str = "u1", known: bool = True) -> RetrievedItem:
    return RetrievedItem(
        memory_id=f"id-{local_id}",
        local_id=local_id,
        user_id=user_id,
        known=known,
    )


K = [1, 2, 3]


def test_correct_ranking_scores_perfectly() -> None:
    record = score_query(
        query=_query(relevant=["m1", "m2"]),
        retrieved=[_item("m1"), _item("m2"), _item("m3")],
        k_values=K,
        degraded=False,
    )

    assert record["metrics"]["precision@2"] == 1.0
    assert record["metrics"]["recall@2"] == 1.0
    assert record["metrics"]["rr"] == 1.0
    assert record["metrics"]["ndcg@2"] == 1.0


def test_reordered_ranking_changes_metrics_exactly() -> None:
    record = score_query(
        query=_query(relevant=["m1", "m2"]),
        retrieved=[_item("m3"), _item("m1"), _item("m2")],
        k_values=K,
        degraded=False,
    )

    assert record["metrics"]["precision@2"] == 0.5
    assert record["metrics"]["recall@2"] == 0.5
    assert record["metrics"]["rr"] == 0.5
    assert record["metrics"]["ndcg@2"] == pytest.approx(0.3868528072, abs=1e-6)
    assert record["relevance_outcome"] == "hit"
    assert record["missing"] == []


def test_foreign_user_result_is_an_isolation_violation() -> None:
    record = score_query(
        query=_query(relevant=["m1", "m2"]),
        retrieved=[_item("f1", user_id="u2"), _item("m1"), _item("m2")],
        k_values=K,
        degraded=False,
    )

    assert record["foreign_user_hits"] == ["f1"]
    assert record["metrics"]["precision@2"] == 0.5
    assert record["metrics"]["rr"] == 0.5
    assert record["metrics"]["recall@2"] == 0.5


def test_forbidden_result_is_counted_and_lowers_precision() -> None:
    record = score_query(
        query=_query(relevant=["m1", "m2"], forbidden=["x1"]),
        retrieved=[_item("x1"), _item("m1"), _item("m2")],
        k_values=K,
        degraded=False,
    )

    assert record["forbidden_hits"] == ["x1"]
    assert record["metrics"]["precision@2"] == 0.5
    assert record["metrics"]["rr"] == 0.5
    assert "x1" in record["irrelevant"]


def test_foreign_and_forbidden_can_both_apply() -> None:
    record = score_query(
        query=_query(relevant=["m1"], forbidden=["f1"]),
        retrieved=[_item("f1", user_id="u2"), _item("m1")],
        k_values=K,
        degraded=False,
    )

    assert record["foreign_user_hits"] == ["f1"]
    assert record["forbidden_hits"] == ["f1"]
    assert record["metrics"]["precision@1"] == 0.0
    assert record["metrics"]["rr"] == 0.5


def test_degraded_query_has_null_metrics() -> None:
    record = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("m1")],
        k_values=K,
        degraded=True,
    )

    assert record["degraded"] is True
    assert record["metrics"] is None
    assert record["relevance_outcome"] == "hit"


def test_no_answer_abstention_and_restraint_miss() -> None:
    abstained = score_query(
        query=_query(relevant=[], category="no_answer"),
        retrieved=[],
        k_values=K,
        degraded=False,
    )
    returned = score_query(
        query=_query(relevant=[], category="no_answer"),
        retrieved=[_item("m9")],
        k_values=K,
        degraded=False,
    )

    assert abstained["no_answer_abstained"] is True
    assert abstained["metrics"] is None
    assert abstained["relevance_outcome"] == "not_applicable"
    assert returned["no_answer_abstained"] is False
    assert returned["metrics"] is None
    assert returned["foreign_user_hits"] == []
    assert returned["forbidden_hits"] == []


def test_duplicate_ids_are_deduplicated_and_reported() -> None:
    record = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("m1"), _item("m1"), _item("m2")],
        k_values=K,
        degraded=False,
    )

    assert record["retrieved"] == ["m1", "m2"]
    assert record["duplicate_ids"] == ["m1"]
    assert record["metrics"]["precision@2"] == 0.5
    assert record["metrics"]["recall@2"] == 1.0


def test_unknown_result_is_reported() -> None:
    record = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("mystery", known=False), _item("m1")],
        k_values=K,
        degraded=False,
    )

    assert record["unknown_ids"] == ["mystery"]


def test_failure_taxonomy_is_separate() -> None:
    miss = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("m2")],
        k_values=K,
        degraded=False,
    )
    foreign = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("f1", user_id="u2")],
        k_values=K,
        degraded=False,
    )
    forbidden = score_query(
        query=_query(relevant=["m1"], forbidden=["x1"]),
        retrieved=[_item("x1")],
        k_values=K,
        degraded=False,
    )
    degraded = score_query(
        query=_query(relevant=["m1"]),
        retrieved=[_item("m1")],
        k_values=K,
        degraded=True,
    )

    counts = _failures([miss, foreign, forbidden, degraded])

    assert counts["relevance_misses"] == 1
    assert counts["isolation_violations"] == 1
    assert counts["forbidden_hits"] == 1
    assert counts["degraded_queries"] == 1


def test_exit_code_reflects_integrity_only() -> None:
    clean = {
        "modes": {
            "keyword": {
                "failures": {
                    "isolation_violations": 0,
                    "forbidden_hits": 0,
                    "unknown_results": 0,
                    "relevance_misses": 5,
                }
            }
        }
    }
    leaking = {
        "modes": {
            "keyword": {
                "failures": {
                    "isolation_violations": 1,
                    "forbidden_hits": 0,
                    "unknown_results": 0,
                }
            }
        }
    }

    assert exit_code_for(clean) == 0
    assert exit_code_for(leaking) == 3
