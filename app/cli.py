"""Maintenance commands.

Usage (PowerShell, from the project root):

    .\\.venv\\Scripts\\python.exe -m app.cli backfill-embeddings
    .\\.venv\\Scripts\\python.exe -m app.cli backfill-embeddings --user-id user-a --batch-size 25

Backfill only adds vectors for the *configured* model where none exist. It never
overwrites an existing vector, never touches vectors belonging to another model,
and is therefore safe to rerun as often as you like.
"""

import argparse
import sys
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.llm.base import EmbeddingProvider
from app.llm.factory import build_embedding_provider
from app.repositories.embeddings import EmbeddingRepository
from app.retrieval.vectors import pack_vector


def backfill_embeddings(
    settings: Settings,
    *,
    user_id: str | None = None,
    batch_size: int = 50,
    provider: EmbeddingProvider | None = None,
) -> int:
    """Embed memories that have no vector for the active model.

    Returns a process exit code: 0 when nothing failed, 1 when at least one
    memory could not be embedded, 2 when embeddings are disabled.
    """
    provider = provider if provider is not None else build_embedding_provider(settings)
    if provider is None:
        print("Embeddings are disabled (RECALLKIT_EMBEDDING_PROVIDER=none). Nothing to do.")
        return 2

    database = Database(settings.database_path)
    database.initialize()

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


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    backfill = subcommands.add_parser(
        "backfill-embeddings",
        help="Embed memories that have no vector for the configured model.",
    )
    backfill.add_argument("--user-id", help="Only backfill one user's memories.")
    backfill.add_argument("--batch-size", type=int, default=50)

    args = parser.parse_args(argv)

    if args.command == "backfill-embeddings":
        if args.batch_size < 1:
            parser.error("--batch-size must be positive")
        return backfill_embeddings(
            settings or Settings(),
            user_id=args.user_id,
            batch_size=args.batch_size,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
