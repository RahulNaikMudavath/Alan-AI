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
from app.repositories.memories import MemoryRepository
from app.services.memory_service import MemoryService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_connection(
    database: Annotated[Database, Depends(get_database)],
) -> Iterator[sqlite3.Connection]:
    """Give the request its own connection and always close it afterwards."""
    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


def get_memory_service(
    connection: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> MemoryService:
    return MemoryService(MemoryRepository(connection))


SettingsDep = Annotated[Settings, Depends(get_settings)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
