"""SQLite connection management and schema initialization.

RecallKit talks to SQLite through the standard library, so there is no ORM to
learn. One :class:`Database` instance lives on the FastAPI app; each request
opens its own short-lived connection, which keeps requests independent of each
other and avoids sharing a connection across threads.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import PROJECT_ROOT

SCHEMA_PATH = PROJECT_ROOT / "schema.sql"


class Database:
    """Owns the SQLite file and hands out connections to it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        """Open a new connection with row access by column name."""
        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Create the database file and apply ``schema.sql``.

        Safe to run on every startup: every statement in the schema is
        idempotent.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with closing(self.connect()) as connection, connection:
            # WAL lets readers continue while a write is in flight. It is a
            # persistent property of the file, so setting it once is enough.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(schema)

    def check(self) -> bool:
        """Return True when the database is reachable and the schema is applied.

        Reading from ``memories`` (rather than ``SELECT 1``) proves the tables
        exist, which is what callers actually depend on.
        """
        if not self.path.exists():
            return False
        try:
            with closing(self.connect()) as connection:
                connection.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
