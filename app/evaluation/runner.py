"""Evaluation orchestration: seed, retrieve, score, aggregate, report."""

import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.evaluation import report as report_module
from app.evaluation.dataset import DatasetError, EvalQuery, LoadedDataset, load_dataset
from app.evaluation.metrics import (
    dedupe_preserving_order,
    hit_at_k,
    median,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.seeding import EvaluationSetupError, seed_dataset
from app.llm.base import EmbeddingProvider
from app.llm.factory import build_embedding_provider
from app.models import MemorySearchRequest, MemorySearchResponse
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.memories import MemoryRepository
from app.services.search_service import SearchService

MODES = ("keyword", "semantic", "hybrid")
DEFAULT_K_VALUES = (1, 3, 5)
MIN_SAMPLES_FOR_P95 = 20
_WARMUP_PASSES = 1


class EvaluationError(RuntimeError):
    """Raised for an invalid evaluation request."""


@dataclass(frozen=True)
class RetrievedItem:
    memory_id: str
    local_id: str
    user_id: str
    known: bool = True


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _mean(values: list[float]) -> float | None:
    return _round(sum(values) / len(values)) if values else None


def score_query(
    *,
    query: EvalQuery,
    retrieved: list[RetrievedItem],
    k_values: list[int],
    degraded: bool,
    stable: bool = True,
) -> dict:
    """Score one query's retrieved items against its single ground truth.

    Pure with respect to storage: all IDs are dataset-local, so this is also
    the entry point for fabricated-result tests.
    """
    unique_ids, duplicate_memory_ids = dedupe_preserving_order(
        [item.memory_id for item in retrieved]
    )
    by_id: dict[str, RetrievedItem] = {}
    for item in retrieved:
        by_id.setdefault(item.memory_id, item)
    unique_items = [by_id[memory_id] for memory_id in unique_ids]
    duplicates = [by_id[memory_id].local_id for memory_id in duplicate_memory_ids]
    local = [item.local_id for item in unique_items]

    relevant = set(query.relevant)
    forbidden = set(query.must_not_include)
    foreign = [item.local_id for item in unique_items if item.user_id != query.user_id]
    forbidden_hits = [local_id for local_id in local if local_id in forbidden]
    unknown = [item.local_id for item in unique_items if not item.known]
    missing = sorted(relevant - set(local))
    irrelevant = [local_id for local_id in local if local_id not in relevant]

    if relevant and not degraded:
        metrics: dict | None = {}
        for k in k_values:
            metrics[f"precision@{k}"] = _round(precision_at_k(local, relevant, k))
            metrics[f"recall@{k}"] = _round(recall_at_k(local, relevant, k))
            metrics[f"ndcg@{k}"] = _round(ndcg_at_k(local, relevant, k))
            metrics[f"hit@{k}"] = _round(hit_at_k(local, relevant, k))
        metrics["rr"] = _round(reciprocal_rank(local, relevant))
    else:
        metrics = None

    if not relevant:
        relevance_outcome = "not_applicable"
    elif any(local_id in relevant for local_id in local):
        relevance_outcome = "hit"
    else:
        relevance_outcome = "miss"

    return {
        "query_id": query.id,
        "category": query.category,
        "query": query.query,
        "rationale": query.rationale,
        "expected": list(query.relevant),
        "retrieved": local,
        "missing": missing,
        "irrelevant": irrelevant,
        "foreign_user_hits": foreign,
        "forbidden_hits": forbidden_hits,
        "duplicate_ids": duplicates,
        "unknown_ids": unknown,
        "degraded": degraded,
        "stable": stable,
        "relevance_outcome": relevance_outcome,
        "no_answer_abstained": (len(local) == 0) if not relevant else None,
        "metrics": metrics,
    }


def _retrieved_items(
    response: MemorySearchResponse, id_to_local: dict[str, str]
) -> list[RetrievedItem]:
    items = []
    for result in response.results:
        local = id_to_local.get(result.memory.id)
        items.append(
            RetrievedItem(
                memory_id=result.memory.id,
                local_id=local or result.memory.id,
                user_id=result.memory.user_id,
                known=local is not None,
            )
        )
    return items


def _signature(response: MemorySearchResponse) -> tuple[tuple[str, ...], bool]:
    return tuple(result.memory.id for result in response.results), response.degraded


def _aggregate(records: list[dict], k_values: list[int]) -> dict:
    scorable = [record for record in records if record["metrics"] is not None]
    aggregate: dict[str, float | int | None] = {}

    for key in ("precision", "recall", "ndcg", "hit"):
        for k in k_values:
            metric = f"{key}@{k}"
            values = [
                record["metrics"][metric]
                for record in scorable
                if record["metrics"].get(metric) is not None
            ]
            aggregate[metric] = _mean(values)

    mrr_values = [
        record["metrics"]["rr"] for record in scorable if record["metrics"].get("rr") is not None
    ]
    aggregate["mrr"] = _mean(mrr_values)
    aggregate["queries_scored"] = len(scorable)
    return aggregate


def _failures(records: list[dict]) -> dict:
    return {
        # Buckets are disjoint: an integrity failure is counted as
        # isolation/forbidden, not also as a relevance miss.
        "relevance_misses": sum(
            1
            for record in records
            if record["relevance_outcome"] == "miss"
            and not record["degraded"]
            and not record["foreign_user_hits"]
            and not record["forbidden_hits"]
        ),
        "isolation_violations": sum(len(record["foreign_user_hits"]) for record in records),
        "forbidden_hits": sum(len(record["forbidden_hits"]) for record in records),
        "duplicate_results": sum(len(record["duplicate_ids"]) for record in records),
        "unknown_results": sum(len(record["unknown_ids"]) for record in records),
        "degraded_queries": sum(1 for record in records if record["degraded"]),
        "unstable_queries": sum(1 for record in records if not record["stable"]),
    }


def _no_answer(records: list[dict]) -> dict:
    no_answer = [record for record in records if record["category"] == "no_answer"]
    passed = sum(1 for record in no_answer if record["no_answer_abstained"])
    return {
        "queries": len(no_answer),
        "passed": passed,
        "pass_rate": _round(passed / len(no_answer)) if no_answer else None,
    }


def _latency(records: list[dict]) -> dict:
    values = [record["latency_ms"] for record in records]
    if not values:
        return {
            "samples": 0,
            "min": None,
            "median": None,
            "p95": None,
            "p95_status": "insufficient_samples",
            "max": None,
        }
    reliable = len(values) >= MIN_SAMPLES_FOR_P95
    return {
        "samples": len(values),
        "min": round(min(values), 3),
        "median": round(median(values) or 0.0, 3),
        "p95": round(percentile(values, 95) or 0.0, 3) if reliable else None,
        "p95_status": "ok" if reliable else "insufficient_samples",
        "max": round(max(values), 3),
    }


def _run_mode(
    *,
    mode: str,
    queries: list[EvalQuery],
    search: SearchService,
    id_to_local: dict[str, str],
    k_values: list[int],
    repeat: int,
    dataset_path: Path,
) -> dict:
    limit = max(k_values)
    records: list[dict] = []

    for query in queries:
        request = MemorySearchRequest(
            user_id=query.user_id,
            query=query.query,
            mode=mode,
            limit=limit,
        )
        for _ in range(_WARMUP_PASSES):
            search.search(request)

        latencies: list[float] = []
        responses: list[MemorySearchResponse] = []
        for _ in range(repeat):
            started = time.perf_counter()
            response = search.search(request)
            latencies.append((time.perf_counter() - started) * 1000.0)
            responses.append(response)

        first = responses[0]
        signature = _signature(first)
        stable = all(_signature(other) == signature for other in responses[1:])
        record = score_query(
            query=query,
            retrieved=_retrieved_items(first, id_to_local),
            k_values=k_values,
            degraded=first.degraded,
            stable=stable,
        )
        record["latency_ms"] = round(median(latencies) or 0.0, 3)
        record["coverage"] = first.coverage.model_dump()
        record["reproduce"] = {
            "request": {
                "user_id": query.user_id,
                "query": query.query,
                "mode": mode,
                "limit": limit,
            },
            "command": (
                f"python -m app.cli evaluate --dataset {dataset_path.as_posix()} "
                f"--only {query.id} --modes {mode} "
                f"--k {','.join(str(k) for k in k_values)}"
            ),
        }
        records.append(record)

    return {
        "aggregate": _aggregate(records, k_values),
        "failures": _failures(records),
        "no_answer": _no_answer(records),
        "latency_ms": _latency(records),
        "per_query": records,
    }


def run_evaluation(
    *,
    loaded: LoadedDataset,
    provider: EmbeddingProvider,
    settings: Settings,
    modes: list[str],
    k_values: list[int],
    repeat: int,
    only: str | None = None,
    database_path: Path | None = None,
    external: bool = False,
    search_provider: EmbeddingProvider | None = None,
) -> dict:
    """Run the selected modes and return a complete report."""
    if not modes or any(mode not in MODES for mode in modes):
        raise EvaluationError(f"invalid modes: {modes}")
    if not k_values or min(k_values) < 1 or max(k_values) > 50:
        raise EvaluationError("K values must be between 1 and 50")
    if repeat < 1:
        raise EvaluationError("repeat must be positive")

    queries = [query for query in loaded.dataset.queries if only is None or query.id == only]
    if not queries:
        raise EvaluationError(f"query not found in dataset: {only}")
    if only is not None and len({query.id for query in queries}) != 1:
        raise EvaluationError(f"query id is not unique: {only}")

    started = time.perf_counter()
    temporary_directory = None
    if database_path is None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="recallkit-eval-")
        resolved_path = Path(temporary_directory.name) / "eval.db"
    else:
        resolved_path = Path(database_path)

    connection = None
    try:
        seeded = seed_dataset(loaded.dataset, provider, database_path=resolved_path)
        database = Database(resolved_path)
        connection = database.connect()
        service = SearchService(
            memories=MemoryRepository(connection),
            embeddings=EmbeddingRepository(connection),
            # ``search_provider`` exists for outage tests: seeds use ``provider``
            # while query embedding uses this one. Production callers omit it.
            provider=search_provider or provider,
            settings=settings,
        )
        modes_results = {
            mode: _run_mode(
                mode=mode,
                queries=queries,
                search=service,
                id_to_local=seeded.id_to_local,
                k_values=k_values,
                repeat=repeat,
                dataset_path=loaded.path,
            )
            for mode in modes
        }
    finally:
        if connection is not None:
            connection.close()
        if temporary_directory is not None:
            temporary_directory.cleanup()

    duration_ms = (time.perf_counter() - started) * 1000.0
    configuration = {
        "provider": {
            "name": settings.embedding_provider,
            "model": provider.model,
            "dimensions": provider.dimensions,
            "external": external,
        },
        "settings": {
            "semantic_min_similarity": settings.semantic_min_similarity,
            "semantic_max_candidates": settings.semantic_max_candidates,
            "hybrid_candidate_pool": settings.hybrid_candidate_pool,
            "hybrid_keyword_weight": settings.hybrid_keyword_weight,
            "hybrid_semantic_weight": settings.hybrid_semantic_weight,
            "hybrid_recency_weight": settings.hybrid_recency_weight,
            "rrf_k": settings.rrf_k,
        },
        "modes": list(modes),
        "k_values": list(k_values),
        "retrieval_limit": max(k_values),
    }
    run_metadata = {
        "mode": "external" if external else "offline",
        "generated_at": datetime.now(UTC).isoformat(),
        "k_values": list(k_values),
        "retrieval_limit": max(k_values),
        "repeat": repeat,
        "warmup_passes": _WARMUP_PASSES,
        "duration_ms": round(duration_ms, 3),
    }
    fingerprint = report_module.compute_fingerprint(
        loaded=loaded, configuration=configuration, modes_results=modes_results
    )
    return report_module.build_report(
        loaded=loaded,
        modes_results=modes_results,
        configuration=configuration,
        run_metadata=run_metadata,
        fingerprint=fingerprint,
    )


def _format_metric(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


def default_report_path(loaded: LoadedDataset, external: bool) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = "external" if external else "offline"
    return Path("eval/reports") / f"{loaded.dataset.dataset_version}-{mode}-{stamp}.json"


def evaluate_command(
    *,
    dataset_path: str,
    modes: list[str],
    k_values: list[int],
    repeat: int,
    only: str | None,
    output: str | None,
    database: str | None,
    provider_mode: str,
    allow_external: bool,
    settings: Settings | None = None,
) -> int:
    """CLI entry point. Returns a process exit code."""
    try:
        loaded = load_dataset(Path(dataset_path))
    except (DatasetError, OSError) as exc:
        print(exc)
        return 1

    base = settings or Settings()
    if provider_mode == "offline":
        effective = base.model_copy(
            update={
                "embedding_provider": "fake",
                "embedding_model": "",
                "embedding_dimensions": 256,
            }
        )
        external = False
    else:
        if not allow_external:
            print("Refusing external provider evaluation without --allow-external-provider.")
            return 1
        effective = base
        external = effective.embedding_provider != "fake"

    try:
        provider = build_embedding_provider(effective)
    except ValueError as exc:
        print(str(exc))
        return 1
    if provider is None:
        print("No embedding provider is configured; refusing to evaluate.")
        return 1

    try:
        report = run_evaluation(
            loaded=loaded,
            provider=provider,
            settings=effective,
            modes=modes,
            k_values=k_values,
            repeat=repeat,
            only=only,
            database_path=Path(database) if database else None,
            external=external,
        )
    except (EvaluationError, EvaluationSetupError) as exc:
        print(exc)
        return 1
    finally:
        provider.close()

    destination = Path(output) if output else default_report_path(loaded, external)
    report_module.write_report(destination, report)

    print(
        f"dataset={loaded.dataset.dataset_version} "
        f"memories={len(loaded.dataset.memories)} "
        f"queries={len(loaded.dataset.queries)} "
        f"mode={'external' if external else 'offline'} "
        f"provider={report['search_configuration']['provider']['name']}:"
        f"{report['search_configuration']['provider']['model']}"
    )
    for mode in modes:
        aggregate = report["modes"][mode]["aggregate"]
        print(
            f"{mode}: P@1={_format_metric(aggregate.get('precision@1'))} "
            f"R@3={_format_metric(aggregate.get('recall@3'))} "
            f"MRR={_format_metric(aggregate.get('mrr'))} "
            f"nDCG@3={_format_metric(aggregate.get('ndcg@3'))} "
            f"scored={aggregate.get('queries_scored')}"
        )
    print(f"fingerprint={report['deterministic_fingerprint']}")
    print(f"report={destination}")

    return report_module.exit_code_for(report)
