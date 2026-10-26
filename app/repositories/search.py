"""Turn user search text into safe SQLite predicates.

Two strategies exist, chosen by the repository:

* **FTS5** is the primary path. It handles word boundaries, prefixes and
  diacritics (``cafe`` finds ``Café``).
* **LIKE** is the fallback, used when the FTS5 index is unavailable.

Both builders are pure functions, so the tricky part — neutralising user text —
can be unit-tested without a database.

The important rule: user text is never concatenated into SQL or into an FTS5
expression. FTS tokens are individually double-quoted (which makes FTS5 read
them as literal strings, so operators such as ``*``, ``-``, ``:``, ``NEAR(``
and ``OR`` lose their meaning), and LIKE patterns are escaped then passed as
bound parameters.
"""

import re
from collections.abc import Sequence

# Split on anything that is not a word character. This drops every FTS5
# operator character and gives the LIKE fallback the same notion of a "word"
# that the FTS path uses.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# A deliberately small ceiling: no legitimate keyword query needs more, and it
# bounds the size of the generated SQL and FTS expression.
MAX_TOKENS = 16


def tokenize(query: str) -> list[str]:
    """Split user text into at most :data:`MAX_TOKENS` word tokens."""
    return _TOKEN_RE.findall(query)[:MAX_TOKENS]


def build_fts_expression(query: str) -> str | None:
    """Build an FTS5 MATCH expression, or None if the text has no words.

    Each token becomes a quoted prefix query, so ``"depl"`` matches ``deploy``.
    Tokens are ANDed by whitespace, which is FTS5's implicit operator: every
    word must appear somewhere in the memory.
    """
    tokens = tokenize(query)
    if not tokens:
        return None
    # Doubling an embedded double quote is how FTS5 escapes one inside a
    # quoted string. tokenize() already strips quotes, so this is defence in
    # depth rather than the primary protection.
    return " ".join('"{}"*'.format(token.replace('"', '""')) for token in tokens)


def _escape_like(text: str) -> str:
    """Escape the LIKE wildcards, using backslash as the escape character."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_like_patterns(query: str) -> list[str]:
    """Build ``%...%`` patterns for the LIKE fallback.

    Word tokens are used when present so that multi-word queries behave like
    the FTS path (all words must appear). Punctuation-only queries fall back to
    the literal text, which lets a caller search for something like ``"!!!"``.
    """
    tokens = tokenize(query)
    if not tokens:
        literal = query.strip()
        tokens = [literal] if literal else []
    return [f"%{_escape_like(token)}%" for token in tokens]


def build_fts_any_expression(tokens: Sequence[str]) -> str | None:
    """Build an OR-ed FTS5 expression from already-tokenised words.

    Used only by conflict candidate generation, where requiring every word
    (the normal AND behaviour) would be too strict. Every token is quoted, so
    operators cannot be injected.
    """
    cleaned = [token.replace('"', '""') for token in tokens if token]
    if not cleaned:
        return None
    return " OR ".join(f'"{token}"*' for token in cleaned)
