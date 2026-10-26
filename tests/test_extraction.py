"""Prompt construction and parsing of untrusted extraction output."""

import json

import pytest

from app.llm.fake import DeterministicLLMProvider
from app.models import ConversationMessage
from app.services.extraction import (
    ExtractionOutputError,
    build_prompt,
    parse_extraction,
    strip_code_fences,
)


def _messages() -> list[ConversationMessage]:
    return [
        ConversationMessage(role="user", content="I'm vegetarian."),
        ConversationMessage(role="assistant", content="Noted."),
        ConversationMessage(role="user", content="I deploy on Fridays."),
    ]


# -- prompt ---------------------------------------------------------------


def test_prompt_contains_every_message_with_its_index() -> None:
    prompt = build_prompt(_messages())

    assert "I'm vegetarian." in prompt
    assert "I deploy on Fridays." in prompt
    assert '"index": 0' in prompt
    assert '"index": 1' in prompt
    assert '"index": 2' in prompt
    assert prompt.count("MESSAGES_JSON:") == 1


def test_prompt_tells_the_model_the_conversation_is_data() -> None:
    prompt = build_prompt(_messages())

    assert "not instructions" in prompt.lower()
    assert '"memories"' in prompt


def test_prompt_preserves_non_ascii_content() -> None:
    prompt = build_prompt([ConversationMessage(role="user", content="J'aime le café 🌙")])

    assert "café 🌙" in prompt


def test_prompt_escapes_nothing_that_breaks_the_marker_line() -> None:
    prompt = build_prompt(
        [ConversationMessage(role="user", content="line one\nline two")],
    )

    # json.dumps escapes the newline, so the payload stays on one line and the
    # deterministic provider can read it back.
    assert prompt.split("MESSAGES_JSON:", 1)[1].strip().splitlines()[0]
    assert "line one\\nline two" in prompt


def test_deterministic_provider_reads_the_prompt_it_is_given() -> None:
    provider = DeterministicLLMProvider()

    raw = provider.complete(build_prompt(_messages()))
    payload = json.loads(raw)

    assert isinstance(payload["memories"], list)
    assert any("vegetarian" in fact["content"] for fact in payload["memories"])


# -- fences ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"memories": []}',
        '```json\n{"memories": []}\n```',
        '```\n{"memories": []}\n```',
        '```JSON\n{"memories": []}\n```',
    ],
)
def test_fenced_and_unfenced_json_is_accepted(raw: str) -> None:
    assert parse_extraction(raw).memories == []


def test_strip_code_fences_leaves_plain_text_alone() -> None:
    assert strip_code_fences('  {"memories": []}  ') == '{"memories": []}'


# -- malformed output -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("not json at all", "not valid JSON"),
        ('{"memories": [}', "not valid JSON"),
        ("[]", "JSON object"),
        ('"a string"', "JSON object"),
        ('{"facts": []}', "schema"),
        ('{"memories": [], "extra": 1}', "schema"),
        ('{"memories": "nope"}', "schema"),
    ],
)
def test_malformed_output_raises(raw: str, reason: str) -> None:
    with pytest.raises(ExtractionOutputError, match=reason):
        parse_extraction(raw)


def test_too_many_raw_items_is_a_contract_violation() -> None:
    raw = json.dumps({"memories": [{"content": "x"}] * 101})

    with pytest.raises(ExtractionOutputError, match="schema"):
        parse_extraction(raw)


def test_individual_items_are_not_validated_at_parse_time() -> None:
    # Per-item problems must become skips, not a failed conversation.
    raw = json.dumps({"memories": ["not an object", {"content": ""}, 42]})

    parsed = parse_extraction(raw)

    assert len(parsed.memories) == 3


def test_error_messages_never_echo_the_output() -> None:
    secret = "sk-should-never-appear"

    with pytest.raises(ExtractionOutputError) as excinfo:
        parse_extraction(f"not json {secret}")

    assert secret not in str(excinfo.value)


def test_deterministic_provider_labels_fact_kinds() -> None:
    provider = DeterministicLLMProvider()
    prompt = build_prompt(
        [
            ConversationMessage(
                role="user",
                content="I prefer tea. I moved to Berlin on Friday. I own a bike.",
            )
        ]
    )

    facts = json.loads(provider.complete(prompt))["memories"]
    kinds = {fact["content"]: fact["kind"] for fact in facts}

    assert kinds["I prefer tea"] == "preference"
    assert kinds["I moved to Berlin on Friday"] == "event"
    assert kinds["I own a bike"] == "fact"
