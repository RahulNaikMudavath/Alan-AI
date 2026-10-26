"""Shared fixtures.

Every test gets a brand-new SQLite file inside pytest's ``tmp_path``. The
fixture passes explicit settings to ``create_app``, so no test can reach the
development database at ``data/recallkit.db``.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # ignore a developer's .env during tests
        database_path=tmp_path / "recallkit-test.db",
        environment="test",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    # The context manager runs startup/shutdown, which creates the schema.
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def memory_payload() -> dict[str, object]:
    return {
        "user_id": "user-a",
        "content": "Prefers dark mode and concise answers.",
        "kind": "preference",
    }


@pytest.fixture
def make_memory(client: TestClient) -> Callable[..., dict[str, object]]:
    """Create a memory through the API and return the stored representation.

    A failed create fails the test immediately, so tests that use this fixture
    only have to assert the behaviour they are actually about.
    """

    def _make(
        *,
        user_id: str = "user-a",
        content: str = "A memory.",
        kind: str = "fact",
        agent_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"user_id": user_id, "content": content, "kind": kind}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        response = client.post("/memories", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make
