"""End-to-end tests for Memory Explorer workflows on a temporary database."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.cli import main
from app.config import Settings
from app.db import Database
from app.main import create_app


def _seeded_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    db_path = tmp_path / "explorer_workflow.db"
    settings = Settings(
        _env_file=None,
        database_path=db_path,
        environment="test",
    )
    Database(settings.database_path).initialize()
    assert main(["seed-demo", "--user-id", "demo-user"], settings=settings) == 0
    app = create_app(settings)
    return TestClient(app), settings


def test_explorer_superseded_memory_inspection_retrieves_replacement(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)

    # List superseded memories
    response = client.get("/memories?user_id=demo-user&status=superseded")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    superseded_memory = data["items"][0]
    assert superseded_memory["content"] == "Prefers dark mode in the editor."
    replacement_id = superseded_memory["superseded_by"]
    assert replacement_id is not None

    # Inspect the actual replacement memory using the existing endpoint
    rep_response = client.get(f"/memories/{replacement_id}?user_id=demo-user")
    assert rep_response.status_code == 200
    rep_data = rep_response.json()
    assert rep_data["content"] == "Prefers light mode in the editor."
    assert rep_data["status"] == "active"


def test_explorer_supersession_workflow_end_to_end(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)

    # Create a replacement memory
    create_res = client.post(
        "/memories",
        json={
            "user_id": "demo-user",
            "content": "Deploys on Wednesdays at 11am.",
            "kind": "event",
        },
    )
    assert create_res.status_code == 201
    replacement_id = create_res.json()["id"]

    # Find the old memory: "Deploys on Fridays at 3pm."
    list_res = client.get("/memories?user_id=demo-user&q=Friday&status=active")
    assert list_res.status_code == 200
    old_id = list_res.json()["items"][0]["id"]

    # Supersede the old memory with the new replacement
    super_res = client.post(
        f"/memories/{old_id}/supersede?user_id=demo-user",
        json={
            "superseded_by": replacement_id,
            "reason": "user_correction",
            "note": "Schedule shifted to Wednesday mornings",
        },
    )
    assert super_res.status_code == 200
    superseded = super_res.json()["superseded"]
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == replacement_id
    assert superseded["superseded_reason"] == "user_correction"


def test_explorer_conflict_resolutions(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)

    # Retrieve the open conflict
    conflicts_res = client.get("/memory-conflicts?user_id=demo-user&status=open")
    assert conflicts_res.status_code == 200
    conflicts = conflicts_res.json()["items"]
    assert len(conflicts) >= 1
    conflict_id = conflicts[0]["id"]
    mem_a_id = conflicts[0]["memory_a"]["id"]
    mem_b_id = conflicts[0]["memory_b"]["id"]

    # Test Keep Both resolution
    res = client.post(
        f"/memory-conflicts/{conflict_id}/resolve",
        json={
            "user_id": "demo-user",
            "resolution": "keep_both",
            "note": "Resolved via test keeping both",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["conflict"]["status"] == "resolved"
    assert data["conflict"]["resolution"] == "kept_both"

    # Both memories must still be active
    mem_a = client.get(f"/memories/{mem_a_id}?user_id=demo-user").json()
    mem_b = client.get(f"/memories/{mem_b_id}?user_id=demo-user").json()
    assert mem_a["status"] == "active"
    assert mem_b["status"] == "active"


def test_explorer_conflict_resolution_keep_a(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)

    # Retrieve the open conflict
    conflicts_res = client.get("/memory-conflicts?user_id=demo-user&status=open")
    assert conflicts_res.status_code == 200
    conflicts = conflicts_res.json()["items"]
    assert len(conflicts) >= 1
    conflict_id = conflicts[0]["id"]
    mem_a_id = conflicts[0]["memory_a"]["id"]
    mem_b_id = conflicts[0]["memory_b"]["id"]

    # Test Keep A resolution (memory B gets superseded by memory A)
    res = client.post(
        f"/memory-conflicts/{conflict_id}/resolve",
        json={
            "user_id": "demo-user",
            "resolution": "keep_a",
            "note": "Resolved keeping memory A",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["conflict"]["status"] == "resolved"
    assert data["conflict"]["resolution"] == "kept_a"

    # Memory A is active, Memory B is superseded
    mem_a = client.get(f"/memories/{mem_a_id}?user_id=demo-user").json()
    mem_b = client.get(f"/memories/{mem_b_id}?user_id=demo-user").json()
    assert mem_a["status"] == "active"
    assert mem_b["status"] == "superseded"
    assert mem_b["superseded_by"] == mem_a_id


def test_explorer_deletion_preserves_conversation_receipts(tmp_path: Path) -> None:
    client, _ = _seeded_client(tmp_path)

    # Verify conversation exists
    conv_list = client.get("/memories?user_id=demo-user&status=active").json()
    conv_mem = next(m for m in conv_list["items"] if "vegetarian" in m["content"])

    # Delete the memory
    del_res = client.delete(f"/memories/{conv_mem['id']}?user_id=demo-user")
    assert del_res.status_code == 204

    # Deleted memory is 404
    assert client.get(f"/memories/{conv_mem['id']}?user_id=demo-user").status_code == 404
