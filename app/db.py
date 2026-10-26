"""SQLite connection management, migration checks, and explicit setup.

RecallKit talks to SQLite through the standard library, so there is no ORM to
learn. One :class:`Database` instance lives on the FastAPI app; each request
opens its own short-lived connection, which keeps requests independent of each
other and avoids sharing a connection across threads.

Schema policy: the application never creates or upgrades a database during
startup. :meth:`Database.initialize` and :meth:`Database.migrate` are called
explicitly by the CLI (or by tests), and :meth:`Database.require_current` is the
read-only gate used by the lifespan.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT
from app.migrations.runner import (
    SchemaNotReadyError,
    current_version,
    has_baseline,
    latest_version,
    not_ready_message,
)
from app.migrations.runner import (
    apply as apply_migrations,
)

SCHEMA_PATH = PROJECT_ROOT / "schema.sql"


@dataclass(frozen=True)
class DatabaseState:
    """A read-only snapshot of whether the schema is ready to serve."""

    path: Path
    exists: bool
    initialized: bool
    version: int
    latest: int

    @property
    def pending(self) -> bool:
        return self.initialized and self.version < self.latest


class Database:
    """Owns the SQLite file and hands out connections to it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        """Open a new connection with row access by column name.

        Foreign keys are off by default in SQLite and must be enabled on every
        connection. RecallKit relies on cascades from ``memories`` to
        embeddings, provenance, and conflicts, so this is not optional.
        """
        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Create the database and bring it fully up to date.

        Explicit operation: used by ``python -m app.cli init`` and by tests.
        Never called from application startup.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._upgrade()

    def migrate(self) -> list[int]:
        """Bring an existing database to the current schema.

        Ensures the baseline tables exist (idempotent) so databases created by
        older milestones gain tables added since, then applies pending
        migrations. Returns the applied versions.

        Raises :class:`SchemaNotReadyError` when the database file is missing;
        creating a database from nothing is ``init``'s job.
        """
        if not self.path.exists():
            state = self.state()
            raise SchemaNotReadyError(
                not_ready_message(
                    path=self.path,
                    version=state.version,
                    latest=state.latest,
                    reason="missing",
                )
            )
        return self._upgrade()

    def _upgrade(self) -> list[int]:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with closing(self.connect()) as connection:
            # WAL lets readers continue while a write is in flight. It is a
            # persistent property of the file, so setting it once is enough.
            connection.execute("PRAGMA journal_mode = WAL")
            # The baseline is idempotent DDL. Running it inside a transaction
            # means a partially created schema cannot be left behind.
            try:
                connection.executescript(f"BEGIN IMMEDIATE;\n{schema}\nCOMMIT;")
            except sqlite3.Error:
                connection.rollback()
                raise
            return apply_migrations(connection)

    def state(self) -> DatabaseState:
        """Inspect the database without writing to it."""
        latest = latest_version()
        if not self.path.exists():
            return DatabaseState(
                path=self.path, exists=False, initialized=False, version=0, latest=latest
            )
        with closing(self.connect()) as connection:
            return DatabaseState(
                path=self.path,
                exists=True,
                initialized=has_baseline(connection),
                version=current_version(connection),
                latest=latest,
            )

    def require_current(self) -> None:
        """Refuse to serve unless the schema is exactly current.

        Read-only. Raises :class:`SchemaNotReadyError` with operator
        instructions; an unopenable path still raises ``sqlite3.OperationalError``.
        """
        state = self.state()
        reason = None
        if not state.exists or not state.initialized:
            reason = "missing"
        elif state.version < state.latest:
            reason = "pending"
        elif state.version > state.latest:
            reason = "newer"
        if reason is not None:
            raise SchemaNotReadyError(
                not_ready_message(
                    path=self.path,
                    version=state.version,
                    latest=state.latest,
                    reason=reason,
                )
            )

    def check(self) -> bool:
        """Return True when the database is reachable and the schema is applied."""
        if not self.path.exists():
            return False
        try:
            with closing(self.connect()) as connection:
                connection.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
