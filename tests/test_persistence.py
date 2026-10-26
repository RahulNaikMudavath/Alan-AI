"""Data survives an application restart.

Each app instance gets its own connection pool and its own SQLite handles, so
reading the memory back from a *new* app proves the row is on disk rather than
in process memory.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_memory_survives_a_new_app_and_connection(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first_client:
        created = first_client.post(
            "/memories",
            json={"user_id": "user-a", "content": "Deploys happen on Fridays at 3pm."},
        ).json()

    # Second app, second set of connections, same database file.
    restarted_settings = Settings(_env_file=None, database_path=settings.database_path)
    with TestClient(create_app(restarted_settings)) as second_client:
        response = second_client.get(f"/memories/{created['id']}", params={"user_id": "user-a"})

    assert response.status_code == 200
    assert response.json() == created
