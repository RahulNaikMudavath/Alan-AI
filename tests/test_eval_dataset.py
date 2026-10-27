"""Dataset schema, label validation, and the committed dataset."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.evaluation.dataset import DatasetError, EvalDataset, load_dataset

COMMITTED = PROJECT_ROOT / "eval" / "datasets" / "retrieval-v1.json"


def _base() -> dict:
    return {
        "dataset_version": "test-v1",
        "schema_version": 1,
        "users": ["u1", "u2"],
        "memories": [
            {
                "local_id": "m1",
                "user_id": "u1",
                "kind": "preference",
                "content": "Prefers dark mode in the editor.",
            },
            {
                "local_id": "m2",
                "user_id": "u1",
                "kind": "preference",
                "content": "Prefers light mode in the editor.",
                "superseded_by": "m1",
                "superseded_reason": "user_correction",
                "supersession_rationale": "Same scope; the theme changed.",
            },
            {
                "local_id": "b1",
                "user_id": "u2",
                "kind": "fact",
                "content": "API key rotates monthly.",
            },
        ],
        "queries": [
            {
                "id": "q1",
                "category": "relevance",
                "user_id": "u1",
                "query": "editor theme",
                "relevant": ["m1"],
                "must_not_include": ["m2"],
                "rationale": "Asks for the current editor theme.",
            }
        ],
    }


# -- committed dataset ----------------------------------------------------


def test_committed_dataset_loads_and_validates() -> None:
    loaded = load_dataset(COMMITTED)

    assert loaded.dataset.dataset_version == "retrieval-v1"
    assert len(loaded.dataset.memories) == 12
    assert len(loaded.dataset.queries) == 13
    assert len(loaded.sha256) == 64
    assert len(loaded.dataset.users) == 2


def test_committed_dataset_has_required_categories() -> None:
    queries = load_dataset(COMMITTED).dataset.queries

    categories = {query.category for query in queries}
    assert categories == {"relevance", "no_answer", "isolation"}


def test_committed_dataset_has_a_valid_supersession() -> None:
    dataset = load_dataset(COMMITTED).dataset
    by_id = {memory.local_id: memory for memory in dataset.memories}

    old = by_id["a-editor-old"]
    new = by_id["a-editor-new"]
    assert old.superseded_by == "a-editor-new"
    assert old.kind == new.kind == "preference"
    assert old.user_id == new.user_id
    assert old.supersession_rationale

    query = next(query for query in dataset.queries if query.id == "q-editor-theme")
    assert query.relevant == ["a-editor-new"]
    assert query.must_not_include == ["a-editor-old"]


def test_every_committed_query_has_a_rationale() -> None:
    dataset = load_dataset(COMMITTED).dataset

    assert all(query.rationale.strip() for query in dataset.queries)


def test_committed_dataset_supersession_pair_shares_the_subject() -> None:
    dataset = load_dataset(COMMITTED).dataset
    by_id = {memory.local_id: memory for memory in dataset.memories}
    from app.services.conflict_service import significant_tokens

    old = by_id["a-editor-old"]
    new = by_id["a-editor-new"]
    assert significant_tokens(old.content) & significant_tokens(new.content)


# -- loading errors -------------------------------------------------------


def test_missing_dataset_raises(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "missing.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(DatasetError, match="not valid JSON"):
        load_dataset(path)


# -- schema and label validation -----------------------------------------


def test_base_dataset_is_valid() -> None:
    assert EvalDataset.model_validate(_base()).dataset_version == "test-v1"


def test_missing_query_rationale_is_rejected() -> None:
    payload = _base()
    del payload["queries"][0]["rationale"]

    with pytest.raises(ValidationError):
        EvalDataset.model_validate(payload)


def test_duplicate_memory_id_is_rejected() -> None:
    payload = _base()
    payload["memories"].append(copy.deepcopy(payload["memories"][0]))

    with pytest.raises(ValidationError, match="duplicate memory"):
        EvalDataset.model_validate(payload)


def test_duplicate_query_id_is_rejected() -> None:
    payload = _base()
    payload["queries"].append(copy.deepcopy(payload["queries"][0]))

    with pytest.raises(ValidationError, match="duplicate query"):
        EvalDataset.model_validate(payload)


def test_unknown_user_is_rejected() -> None:
    payload = _base()
    payload["memories"][0]["user_id"] = "ghost"

    with pytest.raises(ValidationError, match="unknown user"):
        EvalDataset.model_validate(payload)


def test_unknown_relevant_memory_is_rejected() -> None:
    payload = _base()
    payload["queries"][0]["relevant"] = ["missing"]

    with pytest.raises(ValidationError, match="unknown memory"):
        EvalDataset.model_validate(payload)


def test_superseded_memory_cannot_be_relevant() -> None:
    payload = _base()
    payload["queries"][0]["relevant"] = ["m2"]
    payload["queries"][0]["must_not_include"] = []

    with pytest.raises(ValidationError, match="superseded"):
        EvalDataset.model_validate(payload)


def test_relevant_memory_must_belong_to_the_query_user() -> None:
    payload = _base()
    payload["queries"][0]["relevant"] = ["b1"]
    payload["queries"][0]["must_not_include"] = []

    with pytest.raises(ValidationError, match="another user"):
        EvalDataset.model_validate(payload)


def test_forbidden_memory_must_exist() -> None:
    payload = _base()
    payload["queries"][0]["must_not_include"] = ["ghost"]

    with pytest.raises(ValidationError, match="forbids unknown"):
        EvalDataset.model_validate(payload)


def test_relevant_and_forbidden_must_be_disjoint() -> None:
    payload = _base()
    payload["queries"][0]["must_not_include"] = ["m1"]

    with pytest.raises(ValidationError, match="relevant and forbidden"):
        EvalDataset.model_validate(payload)


def test_relevance_query_needs_a_relevant_memory() -> None:
    payload = _base()
    payload["queries"][0]["relevant"] = []
    payload["queries"][0]["must_not_include"] = []

    with pytest.raises(ValidationError, match="must have relevant"):
        EvalDataset.model_validate(payload)


def test_no_answer_query_must_not_have_relevant_memories() -> None:
    payload = _base()
    payload["queries"][0]["category"] = "no_answer"

    with pytest.raises(ValidationError, match="must not have relevant"):
        EvalDataset.model_validate(payload)


def test_isolation_query_needs_a_cross_user_forbidden_memory() -> None:
    payload = _base()
    payload["queries"][0].update(
        {"category": "isolation", "relevant": [], "must_not_include": ["m1"]}
    )

    with pytest.raises(ValidationError, match="another user"):
        EvalDataset.model_validate(payload)


def test_valid_isolation_query_is_accepted() -> None:
    payload = _base()
    payload["queries"][0].update(
        {"category": "isolation", "relevant": [], "must_not_include": ["b1"]}
    )

    assert EvalDataset.model_validate(payload).queries[0].category == "isolation"


# -- supersession validation ---------------------------------------------


def test_supersession_target_must_exist() -> None:
    payload = _base()
    payload["memories"][1]["superseded_by"] = "ghost"

    with pytest.raises(ValidationError, match="unknown replacement"):
        EvalDataset.model_validate(payload)


def test_supersession_cannot_be_self_referential() -> None:
    payload = _base()
    payload["memories"][1]["superseded_by"] = "m2"

    with pytest.raises(ValidationError, match="cannot supersede itself"):
        EvalDataset.model_validate(payload)


def test_supersession_cannot_cross_users() -> None:
    payload = _base()
    payload["memories"][1]["superseded_by"] = "b1"

    with pytest.raises(ValidationError, match="crosses users"):
        EvalDataset.model_validate(payload)


def test_supersession_cannot_change_kind() -> None:
    payload = _base()
    payload["memories"][0]["kind"] = "fact"

    with pytest.raises(ValidationError, match="changes kind"):
        EvalDataset.model_validate(payload)


def test_supersession_chain_is_rejected() -> None:
    payload = _base()
    payload["memories"].append(
        {
            "local_id": "m3",
            "user_id": "u1",
            "kind": "preference",
            "content": "Prefers light mode in the editor at night.",
        }
    )
    payload["memories"][0].update(
        {
            "superseded_by": "m3",
            "superseded_reason": "user_correction",
            "supersession_rationale": "Later refinement.",
        }
    )
    # m2 -> m1 -> m3 would be a chain.
    with pytest.raises(ValidationError, match="chain"):
        EvalDataset.model_validate(payload)


def test_supersession_must_change_content() -> None:
    payload = _base()
    payload["memories"][1]["content"] = payload["memories"][0]["content"]

    with pytest.raises(ValidationError, match="does not change content"):
        EvalDataset.model_validate(payload)


def test_supersession_must_share_a_significant_token() -> None:
    payload = _base()
    payload["memories"][1]["content"] = "Owns a greyhound named Pixel."

    with pytest.raises(ValidationError, match="shares no significant token"):
        EvalDataset.model_validate(payload)


def test_supersession_requires_a_rationale() -> None:
    payload = _base()
    del payload["memories"][1]["supersession_rationale"]

    with pytest.raises(ValidationError):
        EvalDataset.model_validate(payload)


def test_unsupported_schema_version_is_rejected() -> None:
    payload = _base()
    payload["schema_version"] = 2

    with pytest.raises(ValidationError, match="unsupported schema_version"):
        EvalDataset.model_validate(payload)


def test_dataset_hash_is_over_the_raw_file(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    path.write_text(json.dumps(_base()), encoding="utf-8")

    loaded = load_dataset(path)

    import hashlib

    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
