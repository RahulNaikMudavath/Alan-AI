"""Shared FastAPI dependencies.

Dependencies are the wiring of the app: a route declares what it needs, and
these functions build it from the current request. Tests can therefore swap
the database by constructing the app with different settings, without patching
module globals.
"""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.db import Database
from app.llm.base import EmbeddingProvider, LLMProvider
from app.repositories.conversations import ConversationRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.services.conversation_service import ConversationService
from app.services.embedding_index import EmbeddingIndex
from app.services.memory_service import MemoryService
from app.services.search_service import SearchService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_embedding_provider(request: Request) -> EmbeddingProvider | None:
    """The app-wide provider, or None when embeddings are disabled."""
    return request.app.state.embedding_provider


def get_llm_provider(request: Request) -> LLMProvider | None:
    """The app-wide extraction provider, or None when extraction is disabled."""
    return request.app.state.llm_provider


def get_connection(
    database: Annotated[Database, Depends(get_database)],
) -> Iterator[sqlite3.Connection]:
    """Give the request its own connection and always close it afterwards."""
    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


def get_memory_repository(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> MemoryRepository:
    return MemoryRepository(connection)


def get_embedding_repository(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> EmbeddingRepository:
    return EmbeddingRepository(connection)


def get_conversation_repository(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> ConversationRepository:
    return ConversationRepository(connection)


def get_embedding_index(
    repository: Annotated[EmbeddingRepository, Depends(get_embedding_repository)],
    provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
) -> EmbeddingIndex:
    return EmbeddingIndex(repository=repository, provider=provider)


def get_memory_service(
    repository: Annotated[MemoryRepository, Depends(get_memory_repository)],
    embeddings: Annotated[EmbeddingIndex, Depends(get_embedding_index)],
) -> MemoryService:
    return MemoryService(repository, embeddings)


def get_search_service(
    memories: Annotated[MemoryRepository, Depends(get_memory_repository)],
    embeddings: Annotated[EmbeddingRepository, Depends(get_embedding_repository)],
    provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchService:
    return SearchService(
        memories=memories,
        embeddings=embeddings,
        provider=provider,
        settings=settings,
    )


def get_conversation_service(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
    conversations: Annotated[ConversationRepository, Depends(get_conversation_repository)],
    memories: Annotated[MemoryRepository, Depends(get_memory_repository)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    provider: Annotated[LLMProvider | None, Depends(get_llm_provider)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationService:
    return ConversationService(
        connection=connection,
        conversations=conversations,
        memories=memories,
        memory_service=memory_service,
        provider=provider,
        settings=settings,
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
