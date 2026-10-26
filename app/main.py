"""Application factory and the health endpoint.

Run locally with ``uvicorn app.main:app --reload``. The database schema is
applied during application startup, not at import time, so importing this
module never touches the disk.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from app.api import memories
from app.config import Settings
from app.db import Database
from app.models import HealthResponse


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Passing ``settings`` lets tests point the app at a temporary database. The
    default path comes from the environment.
    """
    resolved = settings or Settings()
    database = Database(resolved.database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        yield

    app = FastAPI(
        title=resolved.app_name,
        version="0.1.0",
        description="Persistent memory for AI agents: store and recall facts per user.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.database = database
    app.include_router(memories.router)

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
        summary="Liveness and database readiness",
        responses={503: {"model": HealthResponse, "description": "Database unavailable"}},
    )
    def health(response: Response) -> HealthResponse:
        if not database.check():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="error", database="error")
        return HealthResponse(status="ok", database="ok")

    return app


app = create_app()
