"""Dashboard contracts: local assets only, no innerHTML, and API paths exist."""

import re
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.db import Database
from app.main import create_app

DASHBOARD = PROJECT_ROOT / "dashboard"
ASSET_REFERENCE = re.compile(r'(?:src|href)="(/dashboard/assets/[^"]+)"')
STRING_PATH = re.compile(r"`(/[^`]*)`|\"(/[^\"]*)\"|'(/[^']*)'")
QUERY_SEGMENT = re.compile(r"\$\{query\((?:[^{}]|\{[^{}]*\})*\)\}")
INTERPOLATION = re.compile(r"\$\{[^}]*\}")

EXPECTED_PATHS = {
    "/health",
    "/dashboard/api/runtime",
    "/dashboard/api/reports",
    "/dashboard/api/reports/{p}",
    "/memories",
    "/memories/{p}",
    "/memories/{p}/explain",
    "/memories/{p}/supersede",
    "/memories/search",
    "/memory-conflicts",
    "/memory-conflicts/{p}/resolve",
}


def _files() -> list[Path]:
    return sorted(
        path
        for path in DASHBOARD.rglob("*")
        if path.is_file() and path.suffix in {".html", ".js", ".css"}
    )


def _extract_paths(source: str) -> set[str]:
    paths = set()
    for match in STRING_PATH.finditer(source):
        literal = next(group for group in match.groups() if group is not None)
        literal = QUERY_SEGMENT.sub("", literal)
        literal = INTERPOLATION.sub("{p}", literal)
        if "?" in literal:
            literal = literal.split("?", 1)[0]
        paths.add(literal)
    return paths


def test_dashboard_has_no_external_assets_or_telemetry() -> None:
    for path in _files():
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, f"external URL in {path}"
        assert "https://" not in text, f"external URL in {path}"


def test_dashboard_never_uses_innerhtml() -> None:
    for path in DASHBOARD.rglob("*.js"):
        assert "innerHTML" not in path.read_text(encoding="utf-8"), path


def test_dashboard_html_has_no_inline_scripts_or_styles() -> None:
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")

    assert "<script>" not in html
    assert " style=" not in html
    assert "<style" not in html


def test_every_referenced_asset_exists() -> None:
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    references = ASSET_REFERENCE.findall(html)

    assert references, "the page should reference its local assets"
    for reference in references:
        relative = reference.removeprefix("/dashboard/assets/")
        assert (DASHBOARD / "assets" / relative).is_file(), reference


def test_api_client_paths_match_the_backend(tmp_path: Path) -> None:
    source = (DASHBOARD / "assets" / "api.js").read_text(encoding="utf-8")
    extracted = _extract_paths(source)

    assert extracted == EXPECTED_PATHS

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "contracts.db",
        eval_reports_path=tmp_path / "reports",
        environment="test",
    )
    Database(settings.database_path).initialize()
    app = create_app(settings)
    documented = {
        path.replace("{memory_id}", "{p}").replace("{conflict_id}", "{p}")
        for path in app.openapi()["paths"]
    }
    for path in extracted:
        if path.startswith("/dashboard/"):
            continue
        assert path in documented, f"dashboard calls an undocumented path: {path}"


def test_dashboard_routes_are_not_in_the_product_openapi(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "openapi.db",
        eval_reports_path=tmp_path / "reports",
        environment="test",
    )
    Database(settings.database_path).initialize()
    app = create_app(settings)

    assert all(not path.startswith("/dashboard") for path in app.openapi()["paths"])


def test_dashboard_assets_have_no_network_calls(tmp_path: Path) -> None:
    """Guard against accidental telemetry: fetch should only be used in api.js."""
    for path in DASHBOARD.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        if path.name == "api.js":
            continue
        assert "fetch(" not in text, f"unexpected fetch in {path}"
        assert "XMLHttpRequest" not in text, path
        assert "sendBeacon" not in text, path
