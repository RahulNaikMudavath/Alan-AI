"""Health endpoint and test-environment guarantees."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_startup_fails_when_database_cannot_be_opened(tmp_path: Path) -> None:
    # A directory where the database file should be makes SQLite unusable,
    # which stands in for a missing volume or bad permissions. Failing at
    # startup is better than serving requests against no storage.
    broken_path = tmp_path / "recallkit.db"
    broken_path.mkdir()
    settings = Settings(_env_file=None, database_path=broken_path)

    with pytest.raises(sqlite3.OperationalError):
        with TestClient(create_app(settings)):
            pass


def test_health_reports_503_when_database_check_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state.database, "check", lambda: False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "error"}


def test_app_uses_the_configured_temporary_database(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings)

    assert app.state.database.path == settings.database_path
    assert app.state.database.path.is_relative_to(tmp_path)
