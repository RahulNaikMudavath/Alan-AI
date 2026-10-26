"""Explicit transactions for multi-statement writes.

SQLite's Python driver commits at the end of every ``with connection:`` block.
That is fine for single-row writes, but ingestion must commit a conversation,
its memories, and its provenance together or not at all. Repositories used
inside this helper therefore expose non-committing insert methods.

``BEGIN IMMEDIATE`` takes the write lock up front, so two concurrent ingestions
serialise instead of failing late on a write conflict.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run a block in one write transaction, rolling back on any exception."""
    if connection.in_transaction:
        raise RuntimeError("nested transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
