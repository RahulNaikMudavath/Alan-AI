"""Canonical forms used for duplicate detection and request idempotency.

Duplicate detection needs a *canonical* way to write the same fact before the
two can be compared. Normalisation is deliberately explicit and limited to
what can be done without changing meaning:

1. **Unicode** — NFKC, so compatibility forms collapse (full-width ``ＤＡＲＫ``
   becomes ``DARK``, ligatures expand, non-breaking spaces become spaces).
2. **Case** — ``str.casefold()``, the Unicode-aware form of lowercasing
   (``STRAßE`` and ``strasse`` both become ``strasse``).
3. **Punctuation** — every Unicode punctuation character becomes a space, so
   ``"Dark mode!"`` and ``"dark mode"`` match.
4. **Whitespace** — runs of any Unicode whitespace collapse to one space and the
   ends are trimmed.

Two things it deliberately does **not** do:

* It does not remove diacritics. ``café`` and ``cafe`` stay distinct, because
  they are different words in several languages.
* It does not understand meaning. ``vegetarian`` and ``plant-based`` are
  different canonical strings; semantic near-duplicates are out of scope.

Known trade-offs, documented rather than hidden:

* ``C#`` and ``C`` canonicalise to ``c`` and collide, because ``#`` is
  punctuation. ``C++`` does not, because ``+`` is a symbol.
* ``don't`` becomes ``don t`` and does not match ``dont``.
* A value that is only punctuation (``"!!!"``) canonicalises to itself rather
  than to an empty string, so unrelated punctuation-only values never merge.
"""

import hashlib
import json
import unicodedata
from collections.abc import Mapping


def canonical_content(text: str) -> str:
    """Return the canonical form of a memory's content."""
    normalized = unicodedata.normalize("NFKC", text).casefold()

    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("P"):
            characters.append(" ")
        elif category in {"Cc", "Cf"} and not character.isspace():
            # Control and format characters (including zero-width joiners) are
            # removed rather than turned into spaces. Whitespace controls such
            # as tab and newline are *not* removed: they are word separators,
            # and the collapse step below handles them.
            continue
        else:
            characters.append(character)

    canonical = " ".join("".join(characters).split())
    if canonical:
        return canonical
    # Punctuation-only input: keep something stable and distinct.
    return " ".join(normalized.split())


def payload_fingerprint(payload: Mapping[str, object]) -> str:
    """Hash a canonical payload so idempotent retries can be recognised.

    The same logical payload always produces the same digest: keys are sorted,
    separators are fixed, and non-ASCII text is encoded as UTF-8 rather than
    escaped, so nothing depends on dict insertion order or interpreter version.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
