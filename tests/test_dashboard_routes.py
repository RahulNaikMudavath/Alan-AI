"""Dashboard page, assets, security headers, and the disabled-dashboard flag."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app


@pytest.fixture
def dashboard_settings(tmp_path: Path) -> Settings:
    resolved = Settings(
        _env_file=None,
        database_path=tmp_path / "dash.db",
        eval_reports_path=tmp_path / "reports",
        environment="test",
    )
    Database(resolved.database_path).initialize()
    (tmp_path / "reports").mkdir(exist_ok=True)
    return resolved


@pytest.fixture
def dashboard_client(dashboard_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(dashboard_settings)) as client:
        yield client


def test_dashboard_root_redirects_to_the_page(dashboard_client: TestClient) -> None:
    response = dashboard_client.get("/dashboard", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/dashboard/"


def test_dashboard_page_renders(dashboard_client: TestClient) -> None:
    response = dashboard_client.get("/dashboard/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "RecallKit" in response.text
    assert "Local developer tool" in response.text


def test_dashboard_page_sends_security_headers(dashboard_client: TestClient) -> None:
    response = dashboard_client.get("/dashboard/")

    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_assets_are_served(dashboard_client: TestClient) -> None:
    script = dashboard_client.get("/dashboard/assets/app.js")
    style = dashboard_client.get("/dashboard/assets/styles.css")

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert style.status_code == 200
    assert "text/css" in style.headers["content-type"]
    assert script.headers["cache-control"] == "no-store"


def test_unknown_asset_is_404(dashboard_client: TestClient) -> None:
    assert dashboard_client.get("/dashboard/assets/missing.js").status_code == 404


def test_existing_api_still_works(dashboard_client: TestClient) -> None:
    assert dashboard_client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_root_redirect_to_docs_is_preserved(dashboard_client: TestClient) -> None:
    response = dashboard_client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"


def test_disabled_dashboard_is_not_mounted(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "off.db",
        eval_reports_path=tmp_path / "reports",
        dashboard_enabled=False,
        environment="test",
    )
    Database(settings.database_path).initialize()

    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard/").status_code == 404
        assert client.get("/dashboard/api/runtime").status_code == 404
        assert client.get("/dashboard/assets/app.js").status_code == 404
        # The product API is unaffected.
        assert client.get("/health").status_code == 200
        assert client.get("/memories", params={"user_id": "user-a"}).status_code == 200
