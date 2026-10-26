"""Duplicate detection, skip reasons, source indexes, and request limits."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

MakeConversation = Callable[..., dict[str, object]]


def _fact(
    content: str,
    *,
    kind: str = "fact",
    confidence: float = 0.9,
    indexes: tuple[int, ...] = (0,),
) -> dict[str, object]:
    return {
        "content": content,
        "kind": kind,
        "confidence": confidence,
        "source_message_indexes": list(indexes),
    }


def _post(client: TestClient, payload: dict[str, object]) -> dict:
    response = client.post("/conversations", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _reasons(body: dict) -> list[str]:
    return [item["reason"] for item in body["memories_skipped"]]


# -- duplicates -----------------------------------------------------------


def test_duplicate_inside_one_extraction_is_skipped(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(
        extraction_json(
            _fact("Prefers dark mode"),
            _fact("prefers   DARK mode!"),
        )
    )

    body = _post(client, make_conversation())

    assert len(body["memories_created"]) == 1
    assert _reasons(body) == ["duplicate_in_request"]


def test_duplicate_of_an_existing_memory_is_skipped(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    client.post("/memories", json={"user_id": "user-a", "content": "Prefers dark mode."})
    scripted_llm(extraction_json(_fact("prefers  dark MODE")))

    body = _post(client, make_conversation())

    assert body["memories_created"] == []
    assert _reasons(body) == ["duplicate_existing"]
    assert body["memories_skipped"][0]["content"] == "prefers  dark MODE"
    assert client.get("/memories", params={"user_id": "user-a"}).json()["total"] == 1


@pytest.mark.parametrize(
    ("stored", "candidate"),
    [
        ("Prefers dark mode.", "prefers dark mode"),
        ("Café au lait", "café au lait!!!"),
        ("DARK mode", "ＤＡＲＫ　ＭＯＤＥ"),
        ("STRAßE", "strasse"),
        ("Prefers   dark\tmode", "prefers dark mode"),
    ],
)
def test_duplicate_normalisation_variants(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    stored: str,
    candidate: str,
) -> None:
    client.post("/memories", json={"user_id": "user-a", "content": stored})
    scripted_llm(extraction_json(_fact(candidate)))

    body = _post(client, make_conversation())

    assert body["memories_created"] == []
    assert _reasons(body) == ["duplicate_existing"]


def test_diacritics_are_not_folded_for_dedupe(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    """Documented limit: "café" and "cafe" are different canonical strings."""
    client.post("/memories", json={"user_id": "user-a", "content": "café"})
    scripted_llm(extraction_json(_fact("cafe")))

    body = _post(client, make_conversation())

    assert len(body["memories_created"]) == 1
    assert body["memories_skipped"] == []


def test_same_fact_can_exist_for_different_users(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    client.post("/memories", json={"user_id": "user-a", "content": "Prefers dark mode."})
    scripted_llm(extraction_json(_fact("prefers dark mode")))

    body = _post(client, make_conversation(user_id="user-b"))

    assert len(body["memories_created"]) == 1
    assert body["memories_skipped"] == []


# -- quality gates --------------------------------------------------------


def test_low_confidence_facts_are_skipped(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("Uncertain fact", confidence=0.49)))

    body = _post(client, make_conversation())

    assert body["memories_created"] == []
    assert _reasons(body) == ["low_confidence"]
    assert body["memories_skipped"][0]["kind"] == "fact"


def test_overlong_facts_are_skipped_not_rejected(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("x" * 501)))

    body = _post(client, make_conversation())

    assert body["memories_created"] == []
    assert _reasons(body) == ["too_long"]


def test_facts_beyond_the_cap_are_reported(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(*[_fact(f"Fact number {index}") for index in range(12)]))

    body = _post(client, make_conversation())

    assert len(body["memories_created"]) == 10
    assert _reasons(body) == ["limit_exceeded", "limit_exceeded"]


def test_invalid_individual_facts_are_skipped(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(
        extraction_json(
            "not a fact object",
            {"content": "", "kind": "fact"},
            {"content": "ok", "kind": "gossip"},
            {"content": "ok", "kind": "fact", "confidence": 2.0},
            {"content": "x" * 4001, "kind": "fact"},
            _fact("A valid fact"),
        )
    )

    body = _post(client, make_conversation())

    assert [memory["content"] for memory in body["memories_created"]] == ["A valid fact"]
    assert _reasons(body) == ["invalid_fact"] * 5


# -- provenance -----------------------------------------------------------


def test_invalid_source_indexes_are_filtered(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian", indexes=(0, 99, -1))))

    body = _post(
        client,
        make_conversation(
            messages=[
                {"role": "user", "content": "I'm vegetarian."},
                {"role": "assistant", "content": "Noted."},
            ]
        ),
    )

    memory_id = body["memories_created"][0]["id"]
    stored = query_db(
        "SELECT message_indexes FROM memory_sources WHERE memory_id = ?", (memory_id,)
    )
    assert stored == [("[0]",)]


def test_missing_source_indexes_fall_back_to_user_messages(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian", indexes=())))

    body = _post(
        client,
        make_conversation(
            messages=[
                {"role": "user", "content": "I'm vegetarian."},
                {"role": "assistant", "content": "Noted."},
                {"role": "user", "content": "I also like tea."},
            ]
        ),
    )

    memory_id = body["memories_created"][0]["id"]
    stored = query_db(
        "SELECT message_indexes FROM memory_sources WHERE memory_id = ?", (memory_id,)
    )
    assert stored == [("[0, 2]",)]


# -- request size limits --------------------------------------------------


def test_message_count_limit_is_enforced_before_the_provider(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
) -> None:
    provider = scripted_llm()  # no responses: a call would produce a 502

    response = client.post(
        "/conversations",
        json=make_conversation(
            messages=[{"role": "user", "content": f"message {i}"} for i in range(51)]
        ),
    )

    assert response.status_code == 422
    assert "50 messages" in response.json()["detail"]
    assert provider.prompts == []


def test_per_message_limit_is_enforced_before_the_provider(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
) -> None:
    provider = scripted_llm()

    response = client.post(
        "/conversations",
        json=make_conversation(messages=[{"role": "user", "content": "x" * 8001}]),
    )

    assert response.status_code == 422
    assert "exceeds 8000 characters" in response.json()["detail"]
    assert provider.prompts == []


def test_total_conversation_limit_is_enforced_before_the_provider(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
) -> None:
    provider = scripted_llm()

    response = client.post(
        "/conversations",
        json=make_conversation(
            messages=[{"role": "user", "content": "y" * 7000} for _ in range(6)]
        ),
    )

    assert response.status_code == 422
    assert "exceeds 40000 characters" in response.json()["detail"]
    assert provider.prompts == []


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "user-a", "conversation_id": "conv-1", "messages": []},
        {"user_id": "user-a", "messages": [{"role": "user", "content": "hi"}]},
        {
            "user_id": "user-a",
            "conversation_id": "has spaces",
            "messages": [{"role": "user", "content": "hi"}],
        },
        {
            "user_id": "",
            "conversation_id": "conv-1",
            "messages": [{"role": "user", "content": "hi"}],
        },
        {
            "user_id": "user-a",
            "conversation_id": "conv-1",
            "messages": [{"role": "wizard", "content": "hi"}],
        },
        {
            "user_id": "user-a",
            "conversation_id": "conv-1",
            "messages": [{"role": "user", "content": "hi", "extra": True}],
        },
    ],
)
def test_invalid_requests_are_rejected(client: TestClient, payload: dict) -> None:
    assert client.post("/conversations", json=payload).status_code == 422
