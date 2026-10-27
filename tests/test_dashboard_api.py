"""Read-only dashboard endpoints: runtime identity and report files."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dashboard
from app.config import Settings
from app.db import Database
from app.main import create_app


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    path = tmp_path / "reports"
    path.mkdir()
    return path


@pytest.fixture
def client(tmp_path: Path, reports_dir: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "dash.db",
        eval_reports_path=reports_dir,
        environment="test",
    )
    Database(settings.database_path).initialize()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _write_report(path: Path, payload: dict, name: str = "report.json") -> Path:
    target = path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# -- runtime --------------------------------------------------------------


def test_runtime_reports_provider_identity(client: TestClient) -> None:
    response = client.get("/dashboard/api/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["app"]["name"] == "RecallKit"
    assert body["embedding"]["provider"] == "fake"
    assert body["embedding"]["model"] == "recallkit-fake-hash-v1-256"
    assert body["embedding"]["dimensions"] == 256
    assert body["embedding"]["external"] is False
    assert body["llm"]["provider"] == "fake"
    assert body["features"]["store_messages"] is False
    assert "No authentication" in body["notice"]


def test_runtime_never_leaks_paths_or_secrets(client: TestClient, tmp_path: Path) -> None:
    text = client.get("/dashboard/api/runtime").text

    assert str(tmp_path) not in text
    assert "database_path" not in text
    assert "api_key" not in text
    assert "sk-" not in text
    assert "RECALLKIT_" not in text


def test_runtime_handles_disabled_embeddings(tmp_path: Path, reports_dir: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "none.db",
        eval_reports_path=reports_dir,
        embedding_provider="none",
        environment="test",
    )
    Database(settings.database_path).initialize()

    with TestClient(create_app(settings)) as client:
        body = client.get("/dashboard/api/runtime").json()

    assert body["embedding"]["model"] is None
    assert body["embedding"]["dimensions"] is None
    assert body["embedding"]["external"] is False


# -- report listing -------------------------------------------------------


def test_report_listing_is_empty_without_files(client: TestClient) -> None:
    assert client.get("/dashboard/api/reports").json() == {"items": []}


def test_report_listing_only_returns_json_files(client: TestClient, reports_dir: Path) -> None:
    _write_report(reports_dir, {"report_version": 1})
    (reports_dir / "notes.txt").write_text("not a report", encoding="utf-8")
    (reports_dir / "broken.json").write_text("{not json", encoding="utf-8")

    items = client.get("/dashboard/api/reports").json()["items"]

    assert {item["name"] for item in items} == {"report.json", "broken.json"}
    assert all(item["size"] > 0 for item in items)
    assert "modified_at" in items[0]


def test_report_listing_is_newest_first(client: TestClient, reports_dir: Path) -> None:
    older = _write_report(reports_dir, {"report_version": 1, "tag": "older"}, "older.json")
    newer = _write_report(reports_dir, {"report_version": 1, "tag": "newer"}, "newer.json")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    names = [item["name"] for item in client.get("/dashboard/api/reports").json()["items"]]

    assert names == ["newer.json", "older.json"]


# -- report reading -------------------------------------------------------


def test_report_is_returned_as_json(client: TestClient, reports_dir: Path) -> None:
    _write_report(reports_dir, {"report_version": 1, "modes": {"keyword": {}}})

    response = client.get("/dashboard/api/reports/report.json")

    assert response.status_code == 200
    assert response.json()["report_version"] == 1


def test_missing_report_is_404(client: TestClient) -> None:
    assert client.get("/dashboard/api/reports/missing.json").status_code == 404


def test_invalid_json_report_is_422(client: TestClient, reports_dir: Path) -> None:
    (reports_dir / "broken.json").write_text("{not json", encoding="utf-8")

    response = client.get("/dashboard/api/reports/broken.json")

    assert response.status_code == 422
    assert response.json()["detail"] == "report is not valid JSON"


def test_oversize_report_is_rejected(
    client: TestClient, reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_report(reports_dir, {"report_version": 1, "padding": "x" * 500})
    monkeypatch.setattr(dashboard, "MAX_REPORT_BYTES", 100)

    response = client.get("/dashboard/api/reports/report.json")

    assert response.status_code == 413
    assert response.json()["detail"] == "report is too large"


@pytest.mark.parametrize(
    "name",
    [
        "../secrets.json",
        "..%2Fsecrets.json",
        "%2e%2e%2f%2e%2e%2fsecrets.json",
        "subdir/report.json",
        "not-json",
        "report.json/../../x.json",
    ],
)
def test_path_traversal_is_rejected(client: TestClient, name: str) -> None:
    response = client.get(f"/dashboard/api/reports/{name}")

    assert response.status_code in (404, 400, 422)


def test_traversal_cannot_read_a_file_outside_the_reports_directory(
    client: TestClient, tmp_path: Path, reports_dir: Path
) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"secret": True}), encoding="utf-8")

    response = client.get("/dashboard/api/reports/..%2Fsecret.json")

    assert response.status_code == 404
    assert "secret" not in response.text


def test_symlink_out_of_the_reports_directory_is_rejected(
    client: TestClient, tmp_path: Path, reports_dir: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"outside": True}), encoding="utf-8")
    link = reports_dir / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this platform")

    response = client.get("/dashboard/api/reports/link.json")

    assert response.status_code == 404


def test_reports_endpoint_does_not_write_anything(client: TestClient, reports_dir: Path) -> None:
    before = sorted(path.name for path in reports_dir.iterdir())

    client.get("/dashboard/api/reports")
    client.get("/dashboard/api/reports/missing.json")

    assert sorted(path.name for path in reports_dir.iterdir()) == before


def test_report_listing_marks_unreadable_files(
    client: TestClient, reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_report(reports_dir, {"report_version": 1})
    _write_report(reports_dir, {"report_version": 1, "padding": "x" * 500}, "big.json")
    monkeypatch.setattr(dashboard, "MAX_REPORT_BYTES", 100)

    items = {item["name"]: item for item in client.get("/dashboard/api/reports").json()["items"]}

    assert items["report.json"]["readable"] is True
    assert items["big.json"]["readable"] is False
    assert items["big.json"]["unreadable_reason"] == "too_large"


def test_report_listing_summarises_reports(client: TestClient, reports_dir: Path) -> None:
    _write_report(
        reports_dir,
        {
            "report_version": 1,
            "dataset": {
                "version": "retrieval-v1",
                "sha256": "a" * 64,
                "queries": 13,
            },
            "run": {"mode": "offline", "k_values": [1, 3, 5]},
            "modes": {
                "keyword": {"per_query": [{"query_id": "q1"}] * 13},
                "semantic": {"per_query": [{"query_id": "q1"}] * 13},
                "hybrid": {"per_query": [{"query_id": "q1"}] * 13},
            },
            "deterministic_fingerprint": "sha256:abc",
        },
    )

    [item] = client.get("/dashboard/api/reports").json()["items"]

    assert item["readable"] is True
    assert item["dataset_version"] == "retrieval-v1"
    assert item["dataset_queries"] == 13
    assert item["evaluated_queries"] == 13
    assert item["modes"] == ["hybrid", "keyword", "semantic"]
    assert item["k_values"] == [1, 3, 5]
    assert item["run_mode"] == "offline"
    assert item["fingerprint"] == "sha256:abc"


def test_report_listing_distinguishes_a_partial_run(client: TestClient, reports_dir: Path) -> None:
    _write_report(
        reports_dir,
        {
            "report_version": 1,
            "dataset": {"version": "retrieval-v1", "sha256": "b" * 64, "queries": 13},
            "run": {"mode": "offline", "k_values": [1, 3, 5]},
            "modes": {
                "keyword": {"per_query": [{"query_id": "q1"}]},
                "semantic": {"per_query": [{"query_id": "q1"}]},
                "hybrid": {"per_query": [{"query_id": "q1"}]},
            },
        },
    )

    [item] = client.get("/dashboard/api/reports").json()["items"]

    assert item["dataset_queries"] == 13
    assert item["evaluated_queries"] == 1


def test_report_listing_marks_invalid_json_unreadable(
    client: TestClient, reports_dir: Path
) -> None:
    (reports_dir / "broken.json").write_text("{not json", encoding="utf-8")

    [item] = client.get("/dashboard/api/reports").json()["items"]

    assert item["readable"] is False
    assert item["unreadable_reason"] == "invalid_json"
    assert "evaluated_queries" not in item


def test_report_listing_tolerates_an_unexpected_shape(
    client: TestClient, reports_dir: Path
) -> None:
    _write_report(reports_dir, {"hello": "world"})

    [item] = client.get("/dashboard/api/reports").json()["items"]

    assert item["readable"] is True
    assert item.get("evaluated_queries") is None
    assert item.get("modes") == []


def test_report_listing_detects_modes_with_divergent_counts(
    client: TestClient, reports_dir: Path
) -> None:
    _write_report(
        reports_dir,
        {
            "report_version": 1,
            "dataset": {"version": "retrieval-v1", "sha256": "c" * 64, "queries": 13},
            "run": {"mode": "offline", "k_values": [1, 3, 5]},
            "modes": {
                "keyword": {"per_query": [{"query_id": "q1"}] * 13},
                "semantic": {"per_query": [{"query_id": "q1"}]},
                "hybrid": {"per_query": [{"query_id": "q1"}] * 13},
            },
        },
    )

    [item] = client.get("/dashboard/api/reports").json()["items"]

    assert item["dataset_queries"] == 13
    assert item["evaluated_queries"] == 1
    assert item["all_modes_full"] is False
    assert item["is_offline"] is True
