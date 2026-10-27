"""Maintenance commands.

Usage (PowerShell, from the project root):

    .\\.venv\\Scripts\\python.exe -m app.cli init
    .\\.venv\\Scripts\\python.exe -m app.cli migrate --check
    .\\.venv\\Scripts\\python.exe -m app.cli migrate
    .\\.venv\\Scripts\\python.exe -m app.cli backfill-embeddings
    .\\.venv\\Scripts\\python.exe -m app.cli seed-demo --user-id demo-user
    .\\.venv\\Scripts\\python.exe -m app.cli evaluate --dataset eval/datasets/retrieval-v1.json

Schema changes are never applied by application startup. ``init`` creates a
database from nothing; ``migrate`` upgrades an existing one. Both are safe to
rerun and report what they did.

``seed-demo`` writes demo memories for the local dashboard. It never runs
automatically, it refuses to touch a database whose schema is out of date, and
it does nothing when the chosen user already has memories.

Backfill only adds vectors for the *configured* model where none exist. It never
overwrites an existing vector, never touches vectors belonging to another model,
and is therefore safe to rerun as often as you like.
"""

import argparse
import sys
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.evaluation.runner import DEFAULT_K_VALUES, MODES, evaluate_command
from app.llm.base import EmbeddingProvider
from app.llm.factory import build_embedding_provider, build_llm_provider
from app.migrations.runner import SchemaNotReadyError, latest_version
from app.models import (
    ConversationIngestRequest,
    MemoryCreate,
    SupersedeRequest,
)
from app.repositories.conflicts import ConflictRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.retrieval.vectors import pack_vector
from app.services.conflict_service import ConflictService
from app.services.conversation_service import ConversationService
from app.services.embedding_index import EmbeddingIndex
from app.services.memory_service import MemoryService

# Demo content for the local dashboard. It is synthetic and intentionally
# includes one same-scope supersession and one explicit-negation pair so the
# lifecycle and conflict views have something to show.
_DEMO_MEMORIES = (
    ("Prefers dark mode in the editor.", "preference"),
    ("Prefers light mode in the editor.", "preference"),
    ("Works as a backend engineer on payments.", "fact"),
    ("Drinks oat milk flat whites in the morning.", "preference"),
    ("Deploys on Fridays at 3pm.", "event"),
    ("Owns a greyhound named Pixel.", "fact"),
    ("Prefers editorial-style documentation with examples.", "preference"),
    ("I like coffee.", "preference"),
    ("I do not like coffee.", "preference"),
)

_DEMO_CONVERSATION = "demo-conversation-1"

_BACKUP_REMINDER = (
    "Back up the database file and any -wal/-shm files before migrating.\n"
    "The server must be stopped while you copy them."
)


def init_database(settings: Settings) -> int:
    """Create a database from nothing, or report that it already exists."""
    database = Database(settings.database_path)
    state = database.state()

    if state.exists and state.initialized:
        print(f"Database already initialized: {database.path} (version {state.version}).")
        print("Run 'python -m app.cli migrate' to apply pending migrations.")
        return 1
    if state.version > state.latest:
        print(
            f"Database reports version {state.version}, but this build supports "
            f"{state.latest}. Use the matching build."
        )
        return 1

    database.initialize()
    print(f"Database initialized: {database.path} (version {latest_version()}).")
    return 0


def migrate_database(settings: Settings, *, check: bool = False) -> int:
    """Apply pending migrations, or report them without writing when checking."""
    database = Database(settings.database_path)
    state = database.state()
    print(f"Database: {database.path}")

    if not state.exists or not state.initialized:
        print("No schema found. For a brand-new database run:")
        print("  python -m app.cli init")
        return 1
    if state.version > state.latest:
        print(
            f"Schema version {state.version} is newer than this build supports "
            f"({state.latest}). Use the matching build or restore a backup."
        )
        return 1
    if state.version == state.latest:
        print(f"Schema version {state.version} is current. Nothing to do.")
        return 0

    if check:
        print(f"Schema version {state.version} is out of date; expected {state.latest}.")
        print("Back up the database, then run: python -m app.cli migrate")
        return 1

    print(_BACKUP_REMINDER)
    applied = database.migrate()
    print(f"Applied {len(applied)} migration(s): {', '.join(str(v) for v in applied)}")
    print(f"Schema version: {state.version} -> {latest_version()}")
    return 0


def backfill_embeddings(
    settings: Settings,
    *,
    user_id: str | None = None,
    batch_size: int = 50,
    provider: EmbeddingProvider | None = None,
) -> int:
    """Embed memories that have no vector for the active model.

    Returns a process exit code: 0 when nothing failed, 1 when at least one
    memory could not be embedded or the schema is not ready, 2 when embeddings
    are disabled.
    """
    provider = provider if provider is not None else build_embedding_provider(settings)
    if provider is None:
        print("Embeddings are disabled (RECALLKIT_EMBEDDING_PROVIDER=none). Nothing to do.")
        return 2

    database = Database(settings.database_path)
    try:
        database.require_current()
    except SchemaNotReadyError as exc:
        print(exc)
        return 1

    embedded = 0
    already_present = 0
    failed = 0
    cursor = 0

    connection = database.connect()
    try:
        repository = EmbeddingRepository(connection)
        while True:
            batch = repository.missing(
                model=provider.model,
                user_id=user_id,
                after_rowid=cursor,
                limit=batch_size,
            )
            if not batch:
                break
            # Advance regardless of outcome so a permanently failing row cannot
            # stall the scan.
            cursor = batch[-1][0]

            contents = [memory.content for _, memory in batch]
            try:
                vectors: list[list[float] | None] = list(provider.embed(contents))
            except Exception as exc:  # noqa: BLE001 - isolate one bad item from the batch
                print(
                    f"A batch of {len(batch)} failed ({type(exc).__name__}); "
                    "retrying one at a time."
                )
                vectors = []
                for content in contents:
                    try:
                        vectors.append(provider.embed([content])[0])
                    except Exception:  # noqa: BLE001 - counted as a failed row
                        vectors.append(None)

            created_at = datetime.now(UTC).isoformat()
            for (_, memory), vector in zip(batch, vectors, strict=True):
                if vector is None or len(vector) != provider.dimensions:
                    failed += 1
                    continue
                wrote = repository.insert_if_absent(
                    memory_id=memory.id,
                    model=provider.model,
                    dimensions=len(vector),
                    vector=pack_vector(vector),
                    created_at=created_at,
                )
                if wrote:
                    embedded += 1
                else:
                    already_present += 1
    finally:
        connection.close()
        provider.close()

    print(
        f"model={provider.model} embedded={embedded} "
        f"already_present={already_present} failed={failed}"
    )
    return 0 if failed == 0 else 1


def seed_demo(settings: Settings, *, user_id: str = "demo-user") -> int:
    """Create demo memories so the local dashboard has something to show.

    Explicit command only. Refuses an out-of-date schema, does nothing when the
    user already has memories, and skips the conversation sample when a real
    (paid) extraction provider is configured.
    """
    database = Database(settings.database_path)
    try:
        database.require_current()
    except SchemaNotReadyError as exc:
        print(exc)
        return 1

    connection = database.connect()
    provider = None
    llm_provider = None
    try:
        if connection.execute(
            "SELECT 1 FROM memories WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone():
            print(f"User {user_id} already has memories. Nothing to seed.")
            return 0

        provider = build_embedding_provider(settings)
        memories = MemoryRepository(connection)
        embeddings = EmbeddingRepository(connection)
        conflicts = ConflictRepository(connection)
        index = EmbeddingIndex(repository=embeddings, provider=provider)
        memory_service = MemoryService(memories, index, conflicts=conflicts, connection=connection)
        conflict_service = ConflictService(
            connection=connection, memories=memories, conflicts=conflicts, settings=settings
        )

        created = [
            memory_service.remember(
                MemoryCreate(
                    user_id=user_id,
                    content=content,
                    kind=kind,
                    agent_id="demo-agent",
                )
            )
            for content, kind in _DEMO_MEMORIES
        ]

        # Same-scope supersession: the editor theme preference changed.
        memory_service.supersede(
            created[0].id,
            user_id=user_id,
            payload=SupersedeRequest(
                superseded_by=created[1].id,
                reason="user_correction",
                note="Demo: the editor theme preference changed.",
            ),
        )

        conversation_memories = 0
        if settings.llm_provider == "fake":
            llm_provider = build_llm_provider(settings)
            if llm_provider is not None:
                conversation_service = ConversationService(
                    connection=connection,
                    conversations=ConversationRepository(connection),
                    memories=memories,
                    memory_service=memory_service,
                    provider=llm_provider,
                    settings=settings,
                    conflicts=conflict_service,
                )
                response, _ = conversation_service.ingest(
                    ConversationIngestRequest(
                        user_id=user_id,
                        conversation_id=_DEMO_CONVERSATION,
                        agent_id="demo-agent",
                        messages=[{"role": "user", "content": "I'm vegetarian and I prefer tea."}],
                    )
                )
                conversation_memories = len(response.memories_created)
        else:
            print(
                "Skipping the conversation sample: the configured extraction "
                "provider is not the offline fake."
            )

        conflict_service.detect_for_many(created)
        open_conflicts = connection.execute(
            "SELECT COUNT(*) FROM memory_conflicts WHERE user_id = ? AND status = 'open'",
            (user_id,),
        ).fetchone()[0]

        print(
            f"Seeded {len(created) + conversation_memories} memories for {user_id} "
            f"(1 superseded, {open_conflicts} open conflict candidate, "
            f"{conversation_memories} conversation-sourced)."
        )
        return 0
    finally:
        connection.close()
        if provider is not None:
            provider.close()
        if llm_provider is not None:
            llm_provider.close()


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "init",
        help="Create a database and apply all migrations. Refuses an existing database.",
    )

    migrate = subcommands.add_parser(
        "migrate",
        help="Apply pending schema migrations to an existing database.",
    )
    migrate.add_argument(
        "--check",
        action="store_true",
        help="Report whether migrations are pending, without writing.",
    )

    backfill = subcommands.add_parser(
        "backfill-embeddings",
        help="Embed memories that have no vector for the configured model.",
    )
    backfill.add_argument("--user-id", help="Only backfill one user's memories.")
    backfill.add_argument("--batch-size", type=int, default=50)

    seed = subcommands.add_parser(
        "seed-demo",
        help="Create demo memories for the local dashboard. Never automatic.",
    )
    seed.add_argument("--user-id", default="demo-user")

    evaluate = subcommands.add_parser(
        "evaluate",
        help="Run the retrieval evaluation offline and write a JSON report.",
    )
    evaluate.add_argument("--dataset", default="eval/datasets/retrieval-v1.json")
    evaluate.add_argument("--modes", default=",".join(MODES))
    evaluate.add_argument("--k", default=",".join(str(k) for k in DEFAULT_K_VALUES))
    evaluate.add_argument("--repeat", type=int, default=3)
    evaluate.add_argument("--only", help="Evaluate a single query ID.")
    evaluate.add_argument("--output", help="Report path; defaults under eval/reports/.")
    evaluate.add_argument("--database", help="Keep the seeded database at this path.")
    evaluate.add_argument(
        "--provider",
        choices=["offline", "configured"],
        default="offline",
        help="offline forces the deterministic fake embedding provider.",
    )
    evaluate.add_argument(
        "--allow-external-provider",
        action="store_true",
        help="Required to use the provider configured in the environment.",
    )

    args = parser.parse_args(argv)

    if args.command == "evaluate":
        mode_list = [part.strip() for part in args.modes.split(",") if part.strip()]
        if not mode_list or any(mode not in MODES for mode in mode_list):
            print(f"invalid --modes; choose from {', '.join(MODES)}")
            return 1
        mode_list = list(dict.fromkeys(mode_list))
        try:
            k_values = sorted({int(part) for part in args.k.split(",") if part.strip()})
        except ValueError:
            print("invalid --k; expected comma-separated integers")
            return 1
        if not k_values or min(k_values) < 1 or max(k_values) > 50:
            print("--k values must be between 1 and 50")
            return 1
        if args.repeat < 1:
            print("--repeat must be positive")
            return 1
        if args.provider == "configured" and not args.allow_external_provider:
            print("Refusing external provider evaluation without --allow-external-provider.")
            return 1
        return evaluate_command(
            dataset_path=args.dataset,
            modes=mode_list,
            k_values=k_values,
            repeat=args.repeat,
            only=args.only,
            output=args.output,
            database=args.database,
            provider_mode=args.provider,
            allow_external=args.allow_external_provider,
            settings=settings,
        )

    if args.command == "init":
        return init_database(settings or Settings())
    if args.command == "migrate":
        return migrate_database(settings or Settings(), check=args.check)
    if args.command == "backfill-embeddings":
        if args.batch_size < 1:
            parser.error("--batch-size must be positive")
        return backfill_embeddings(
            settings or Settings(),
            user_id=args.user_id,
            batch_size=args.batch_size,
        )
    if args.command == "seed-demo":
        if not args.user_id.strip():
            parser.error("--user-id must not be blank")
        return seed_demo(settings or Settings(), user_id=args.user_id.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
