"""Report construction, deterministic fingerprint, and JSON output."""

import hashlib
import json
from pathlib import Path

from app.evaluation.dataset import LoadedDataset

REPORT_VERSION = 1

METRIC_DEFINITIONS = {
    "precision_at_k": (
        "count(i in 1..min(K,|L|) where L[i] in R) / K. Denominator is K even "
        "when fewer than K results were returned. Null when R is empty."
    ),
    "recall_at_k": ("count(i in 1..min(K,|L|) where L[i] in R) / |R|. Null when R is empty."),
    "reciprocal_rank": (
        "1 / rank of the first relevant result, or 0 when none is retrieved. Null when R is empty."
    ),
    "mrr": "Mean of reciprocal ranks over scored queries. Null when none are scored.",
    "ndcg_at_k": (
        "Binary-gain DCG@K divided by IDCG@K, where IDCG is summed over "
        "min(|R|, K) ideal ranks. Null when R is empty."
    ),
    "hit_at_k": (
        "1 when at least one relevant result appears in L@K, else 0. Null when R is empty."
    ),
    "no_answer_abstained": (
        "1 when no results were returned for a query with an empty relevant set, "
        "else 0. Measures restraint, not correctness."
    ),
    "duplicate_handling": (
        "Retrieved IDs are de-duplicated preserving first occurrence before "
        "metrics; duplicates are reported and counted separately."
    ),
    "degraded_handling": (
        "Queries whose semantic signal degraded have null metrics and are "
        "excluded from mode averages; they are counted separately."
    ),
    "failure_buckets": (
        "Relevance misses, isolation violations, forbidden results, duplicate "
        "results, and degraded queries are counted in disjoint buckets. A "
        "foreign or forbidden result is reported under integrity, not also as "
        "a relevance miss."
    ),
    "latency": (
        "In-process wall time around SearchService.search only, in milliseconds. "
        "Seeding and report writing are excluded."
    ),
}

CAVEATS = [
    "Offline evaluation uses a deterministic lexical hash embedding; it measures "
    "plumbing and ranking behaviour, not semantic understanding.",
    "Relevance labels are synthetic and editorially authored, not judgments from real users.",
    "Latency is an in-process microbenchmark: no HTTP, no concurrency, and p95 is "
    "withheld below 20 scored queries.",
    "Identical fingerprints establish repeatability for this dataset, "
    "configuration, and provider. They do not establish retrieval accuracy.",
    "External-provider runs are not guaranteed to be deterministic across model "
    "versions or provider changes.",
]


def build_report(
    *,
    loaded: LoadedDataset,
    modes_results: dict,
    configuration: dict,
    run_metadata: dict,
    fingerprint: str,
) -> dict:
    dataset = loaded.dataset
    return {
        "report_version": REPORT_VERSION,
        "dataset": {
            "version": dataset.dataset_version,
            "schema_version": dataset.schema_version,
            "sha256": loaded.sha256,
            "memories": len(dataset.memories),
            "queries": len(dataset.queries),
            "users": len(dataset.users),
        },
        "run": run_metadata,
        "search_configuration": configuration,
        "metric_definitions": METRIC_DEFINITIONS,
        "modes": modes_results,
        "deterministic_fingerprint": fingerprint,
        "caveats": CAVEATS,
    }


def compute_fingerprint(
    *,
    loaded: LoadedDataset,
    configuration: dict,
    modes_results: dict,
) -> str:
    """Hash the repeatable parts of a run.

    Includes dataset hash, retrieval configuration, provider identity, and every
    per-query relevance outcome. Excludes timestamps and latency values.
    """
    outcomes: dict[str, dict] = {}
    for mode, result in modes_results.items():
        outcomes[mode] = {}
        for record in result["per_query"]:
            outcomes[mode][record["query_id"]] = {
                "expected": record["expected"],
                "retrieved": record["retrieved"],
                "metrics": record["metrics"],
                "degraded": record["degraded"],
                "stable": record["stable"],
                "relevance_outcome": record["relevance_outcome"],
                "no_answer_abstained": record["no_answer_abstained"],
                "foreign_user_hits": record["foreign_user_hits"],
                "forbidden_hits": record["forbidden_hits"],
                "duplicate_ids": record["duplicate_ids"],
                "unknown_ids": record["unknown_ids"],
            }

    payload = {
        "dataset": {
            "version": loaded.dataset.dataset_version,
            "sha256": loaded.sha256,
        },
        "configuration": configuration,
        "outcomes": outcomes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def integrity_failures(report: dict) -> int:
    """Count isolation, forbidden, and unknown-result violations."""
    total = 0
    for mode in report.get("modes", {}).values():
        failures = mode.get("failures", {})
        total += int(failures.get("isolation_violations", 0))
        total += int(failures.get("forbidden_hits", 0))
        total += int(failures.get("unknown_results", 0))
    return total


def exit_code_for(report: dict) -> int:
    """0 for a completed run, 3 for an integrity violation."""
    return 3 if integrity_failures(report) > 0 else 0


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
