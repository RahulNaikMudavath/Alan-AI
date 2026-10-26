"""Keyword search: FTS5 behaviour, safety, and the LIKE fallback."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

MakeMemory = Callable[..., dict[str, object]]


def _search(client: TestClient, q: str, **params: object) -> dict[str, object]:
    response = client.get("/memories", params={"user_id": "user-a", "q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _drop_full_text_index(settings: Settings) -> None:
    """Simulate a database without FTS5 to exercise the LIKE fallback."""
    with closing(sqlite3.connect(settings.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER IF EXISTS memories_ai")
        connection.execute("DROP TRIGGER IF EXISTS memories_au")
        connection.execute("DROP TRIGGER IF EXISTS memories_ad")
        connection.execute("DROP TABLE IF EXISTS memories_fts")
    # Guard against a helper that silently does nothing, which would leave the
    # "fallback" tests quietly exercising the FTS path instead.
    with closing(sqlite3.connect(settings.database_path)) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'memories_fts'"
        ).fetchone()[0]
    assert remaining == 0


def test_search_matches_content(client: TestClient, make_memory: MakeMemory) -> None:
    match = make_memory(content="Prefers dark mode.")
    make_memory(content="Deploys happen on Fridays.")

    body = _search(client, "dark")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [match["id"]]


def test_search_is_case_insensitive(client: TestClient, make_memory: MakeMemory) -> None:
    match = make_memory(content="Prefers dark mode.")

    assert [item["id"] for item in _search(client, "DARK")["items"]] == [match["id"]]


def test_search_requires_every_word(client: TestClient, make_memory: MakeMemory) -> None:
    both = make_memory(content="Prefers dark mode.")
    make_memory(content="Dark chocolate is a snack.")

    body = _search(client, "dark mode")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [both["id"]]


def test_search_matches_word_prefixes(client: TestClient, make_memory: MakeMemory) -> None:
    match = make_memory(content="Deploys happen on Fridays.")

    assert [item["id"] for item in _search(client, "depl")["items"]] == [match["id"]]


def test_search_ignores_diacritics(client: TestClient, make_memory: MakeMemory) -> None:
    match = make_memory(content="Drinks Café au lait.")

    assert [item["id"] for item in _search(client, "cafe")["items"]] == [match["id"]]


def test_search_is_scoped_to_the_user(client: TestClient, make_memory: MakeMemory) -> None:
    mine = make_memory(user_id="user-a", content="Dark mode for user A.")
    make_memory(user_id="user-b", content="Dark mode for user B.")

    body = _search(client, "dark")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [mine["id"]]


def test_search_combines_with_filters(client: TestClient, make_memory: MakeMemory) -> None:
    wanted = make_memory(content="Dark mode task.", kind="task", agent_id="agent-1")
    make_memory(content="Dark mode fact.", kind="fact", agent_id="agent-1")
    make_memory(content="Dark mode other agent.", kind="task", agent_id="agent-2")

    body = _search(client, "dark", kind="task", agent_id="agent-1")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [wanted["id"]]


def test_search_without_matches_returns_an_empty_page(
    client: TestClient, make_memory: MakeMemory
) -> None:
    make_memory(content="Prefers dark mode.")

    body = _search(client, "quantum")

    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_search_paginates(client: TestClient, make_memory: MakeMemory) -> None:
    created = [make_memory(content=f"Dark mode note {index}.") for index in range(3)]

    first = _search(client, "dark", limit=2)
    second = _search(client, "dark", limit=2, offset=2)

    assert first["total"] == second["total"] == 3
    assert [item["id"] for item in first["items"]] == [created[2]["id"], created[1]["id"]]
    assert [item["id"] for item in second["items"]] == [created[0]["id"]]


def test_blank_search_is_treated_as_no_search(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="First.")
    make_memory(content="Second.")

    assert _search(client, "   ")["total"] == 2


def test_punctuation_only_search_is_safe(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="No punctuation here.")

    assert _search(client, "!!!")["total"] == 0


@pytest.mark.parametrize(
    "query",
    [
        '"',
        "*",
        "%",
        "_",
        "^dark",
        "-dark",
        "dark*",
        "content:dark",
        "NEAR(dark mode)",
        "dark OR mode",
        "dark AND NOT mode",
        "' OR 1=1 --",
        '") OR ("',
        "dark; DROP TABLE memories; --",
    ],
)
def test_search_input_cannot_escape_its_scope(
    client: TestClient, make_memory: MakeMemory, query: str
) -> None:
    mine = make_memory(user_id="user-a", content="Dark mode notes.")
    make_memory(user_id="user-b", content="Dark mode secret.")

    body = _search(client, query)

    # Whatever the input means, only this user's rows may come back.
    assert {item["user_id"] for item in body["items"]} <= {"user-a"}
    # The table is still intact after the injection attempts, and ordinary
    # search still finds this user's row (so the assertion above is not
    # passing merely because every search returns nothing).
    assert client.get(f"/memories/{mine['id']}", params={"user_id": "user-a"}).status_code == 200
    assert _search(client, "dark")["total"] == 1


def test_search_falls_back_to_like_without_fts(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    match = make_memory(content="Prefers dark mode.")
    make_memory(content="Deploys happen on Fridays.")
    _drop_full_text_index(settings)

    body = _search(client, "dark")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [match["id"]]


def test_like_fallback_is_scoped_to_the_user(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    mine = make_memory(user_id="user-a", content="Dark mode for A.")
    make_memory(user_id="user-b", content="Dark mode for B.")
    _drop_full_text_index(settings)

    body = _search(client, "dark")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [mine["id"]]


def test_like_fallback_escapes_wildcards(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    underscored = make_memory(content="snake_case identifier")
    make_memory(content="plain text with no wildcards")
    _drop_full_text_index(settings)

    # An unescaped `_` would match any single character and return both rows.
    underscore_results = _search(client, "_")
    assert [item["id"] for item in underscore_results["items"]] == [underscored["id"]]

    # An unescaped `%` would match everything.
    assert _search(client, "%")["total"] == 0


def test_like_fallback_requires_every_word(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    both = make_memory(content="Prefers dark mode.")
    make_memory(content="Dark chocolate is a snack.")
    _drop_full_text_index(settings)

    body = _search(client, "dark mode")

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [both["id"]]


def test_index_is_backfilled_when_rows_are_missing(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Backfilled note about kubernetes.")
    with closing(sqlite3.connect(settings.database_path)) as connection, connection:
        connection.execute("DELETE FROM memories_fts")

    # The index is empty, so a normal search finds nothing...
    assert _search(client, "kubernetes")["total"] == 0

    # ...until startup re-applies the schema, which backfills missing rows.
    client.app.state.database.initialize()

    body = _search(client, "kubernetes")
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [memory["id"]]
