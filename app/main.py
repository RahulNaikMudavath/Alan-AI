"""Application factory and the health endpoint.

Run locally with ``uvicorn app.main:app --reload``. The database schema is
applied during application startup, not at import time, so importing this
module never touches the disk.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from app.api import memories, search
from app.config import Settings
from app.db import Database
from app.llm.factory import build_embedding_provider
from app.models import HealthResponse

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Passing ``settings`` lets tests point the app at a temporary database. The
    default path comes from the environment. A misconfigured provider raises
    here, before the server accepts a single request.
    """
    resolved = settings or Settings()
    database = Database(resolved.database_path)
    embedding_provider = build_embedding_provider(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        if embedding_provider is None:
            logger.info("embeddings disabled; semantic search will report degraded")
        else:
            # Provider name and model only. The API key must never be logged.
            logger.info(
                "embeddings ready provider=%s model=%s dimensions=%s",
                resolved.embedding_provider,
                embedding_provider.model,
                embedding_provider.dimensions,
            )
        try:
            yield
        finally:
            if embedding_provider is not None:
                embedding_provider.close()

    app = FastAPI(
        title=resolved.app_name,
        version="0.2.0",
        description=(
            "Persistent memory for AI agents: store facts per user, then recall "
            "them by keyword, meaning, or both."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.database = database
    app.state.embedding_provider = embedding_provider
    app.include_router(memories.router)
    app.include_router(search.router)

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
