"""Exact-duplicate canonicalisation and idempotency fingerprints."""

import pytest

from app.services.canonical import canonical_content, payload_fingerprint


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Whitespace, including Unicode whitespace.
        ("  Prefers   dark   mode  ", "prefers dark mode"),
        ("Prefers\tdark\nmode", "prefers dark mode"),
        ("Prefers\u00a0dark mode", "prefers dark mode"),
        ("Prefers\u3000dark mode", "prefers dark mode"),
        # Case, including Unicode case folding.
        ("PREFERS DARK MODE", "prefers dark mode"),
        ("STRAßE", "strasse"),
        ("ΟΔΟΣ", "οδος"),
        # Punctuation.
        ("Prefers dark mode!!!", "prefers dark mode"),
        ("Prefers, dark mode.", "prefers dark mode"),
        ("I don't like it", "i don t like it"),
        # Unicode compatibility forms.
        ("ＤＡＲＫ　ＭＯＤＥ", "dark mode"),
        ("ﬁle", "file"),
        # Format characters are removed, not turned into spaces.
        ("dark\u200bmode", "darkmode"),
        ("dark\u200b mode", "dark mode"),
    ],
)
def test_equivalent_forms_share_a_canonical_key(left: str, right: str) -> None:
    assert canonical_content(left) == canonical_content(right)


def test_canonical_form_is_readable() -> None:
    assert canonical_content("  Prefers   DARK mode!  ") == "prefers dark mode"


def test_canonical_form_is_idempotent() -> None:
    for text in ["Prefers dark mode", "!!!", "Café au lait", "🌙 mode", "  "]:
        once = canonical_content(text)
        assert canonical_content(once) == once


def test_blank_text_canonicalises_to_empty() -> None:
    assert canonical_content("   \t\n ") == ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Accents are meaning, not formatting, and are preserved.
        ("café", "cafe"),
        # A contraction and the missing apostrophe are different words.
        ("don't", "dont"),
    ],
)
def test_documentedly_distinct_forms_stay_distinct(left: str, right: str) -> None:
    assert canonical_content(left) != canonical_content(right)


def test_punctuation_only_values_do_not_collapse() -> None:
    assert canonical_content("!!!") == "!!!"
    assert canonical_content("!!!") != canonical_content("???")


def test_documented_collision_of_punctuated_symbol() -> None:
    """``#`` is punctuation, so "C#" collides with "C". Pinned on purpose."""
    assert canonical_content("C#") == canonical_content("C") == "c"


def test_math_symbols_are_preserved() -> None:
    assert canonical_content("C++") == "c++"
    assert canonical_content("C++") != canonical_content("C")


# -- payload fingerprints -------------------------------------------------


def test_fingerprint_is_stable_across_key_order() -> None:
    assert payload_fingerprint({"a": 1, "b": [1, 2]}) == payload_fingerprint({"b": [1, 2], "a": 1})


def test_fingerprint_changes_with_content() -> None:
    first = payload_fingerprint({"messages": [{"content": "I like tea"}]})
    second = payload_fingerprint({"messages": [{"content": "I like coffee"}]})

    assert first != second


def test_fingerprint_is_order_sensitive_for_lists() -> None:
    first = payload_fingerprint({"messages": ["one", "two"]})
    second = payload_fingerprint({"messages": ["two", "one"]})

    assert first != second


def test_fingerprint_handles_non_ascii_deterministically() -> None:
    payload = {"content": "Café 🌙"}

    assert payload_fingerprint(payload) == payload_fingerprint(dict(payload))
    assert len(payload_fingerprint(payload)) == 64
