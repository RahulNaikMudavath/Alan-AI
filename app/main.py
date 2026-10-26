"""Application factory and the health endpoint.

Run locally with ``uvicorn app.main:app --reload``. The database schema is
applied during application startup, not at import time, so importing this
module never touches the disk.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.responses import RedirectResponse

from app.api import conversations, memories, search
from app.config import Settings
from app.db import Database
from app.llm.factory import build_embedding_provider, build_llm_provider
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
    llm_provider = build_llm_provider(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Never creates or upgrades schema. A stale database stops the server
        # with instructions to back up and run `python -m app.cli migrate`.
        database.require_current()
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
        if llm_provider is None:
            logger.info("fact extraction disabled; POST /conversations will return 503")
        else:
            logger.info(
                "extraction ready provider=%s model=%s",
                llm_provider.name,
                llm_provider.model,
            )
        try:
            yield
        finally:
            if embedding_provider is not None:
                embedding_provider.close()
            if llm_provider is not None:
                llm_provider.close()

    app = FastAPI(
        title=resolved.app_name,
        version="0.3.0",
        description=(
            "Persistent memory for AI agents: store facts per user, extract them "
            "from conversations, then recall them by keyword, meaning, or both."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.database = database
    app.state.embedding_provider = embedding_provider
    app.state.llm_provider = llm_provider
    app.include_router(memories.router)
    app.include_router(search.router)
    app.include_router(conversations.router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

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
