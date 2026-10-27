"""Offline runner, determinism, CLI, and the committed evaluation dataset."""

import json
from pathlib import Path

import pytest

from app.cli import main
from app.config import PROJECT_ROOT, Settings
from app.evaluation.dataset import load_dataset
from app.evaluation.runner import run_evaluation
from app.llm.base import EmbeddingError
from app.llm.fake import DeterministicEmbeddingProvider

COMMITTED = PROJECT_ROOT / "eval" / "datasets" / "retrieval-v1.json"
MODES = ["keyword", "semantic", "hybrid"]


class ExplodingProvider:
    model = "exploding-v1"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("provider is down")

    def close(self) -> None:
        """Nothing to release."""


@pytest.fixture
def eval_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "unused.db",
        embedding_provider="fake",
        embedding_dimensions=256,
        embedding_model="",
    )


def _provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(dimensions=256)


def _run(settings: Settings, *, modes=None, repeat=1, only=None, search_provider=None):
    return run_evaluation(
        loaded=load_dataset(COMMITTED),
        provider=_provider(),
        settings=settings,
        modes=modes or MODES,
        k_values=[1, 3, 5],
        repeat=repeat,
        only=only,
        search_provider=search_provider,
    )


def test_offline_run_produces_a_complete_report(eval_settings: Settings) -> None:
    report = _run(eval_settings)

    assert report["report_version"] == 1
    assert report["dataset"]["version"] == "retrieval-v1"
    assert report["dataset"]["sha256"] == load_dataset(COMMITTED).sha256
    assert report["run"]["mode"] == "offline"
    assert report["search_configuration"]["provider"] == {
        "name": "fake",
        "model": "recallkit-fake-hash-v1-256",
        "dimensions": 256,
        "external": False,
    }
    assert "precision_at_k" in report["metric_definitions"]
    assert any("lexical hash embedding" in caveat for caveat in report["caveats"])

    for mode in MODES:
        result = report["modes"][mode]
        assert result["aggregate"]["queries_scored"] == 9
        assert len(result["per_query"]) == 13
        assert result["latency_ms"]["samples"] == 13
        # Fewer than 20 scored queries: p95 is deliberately withheld.
        assert result["latency_ms"]["p95"] is None
        assert result["latency_ms"]["p95_status"] == "insufficient_samples"
        for record in result["per_query"]:
            assert set(record) >= {
                "query_id",
                "expected",
                "retrieved",
                "missing",
                "irrelevant",
                "foreign_user_hits",
                "forbidden_hits",
                "degraded",
                "stable",
                "metrics",
                "latency_ms",
                "reproduce",
                "coverage",
                "rationale",
            }


def test_two_runs_share_a_fingerprint_and_retrievals(eval_settings: Settings) -> None:
    first = _run(eval_settings)
    second = _run(eval_settings)

    assert first["deterministic_fingerprint"] == second["deterministic_fingerprint"]
    for mode in MODES:
        first_records = first["modes"][mode]["per_query"]
        second_records = second["modes"][mode]["per_query"]
        assert [record["retrieved"] for record in first_records] == [
            record["retrieved"] for record in second_records
        ]
        assert [record["metrics"] for record in first_records] == [
            record["metrics"] for record in second_records
        ]


def test_fingerprint_ignores_generated_at_and_latency(eval_settings: Settings) -> None:
    first = _run(eval_settings)
    second = _run(eval_settings)

    assert first["run"]["generated_at"] != second["run"]["generated_at"]
    assert first["deterministic_fingerprint"] == second["deterministic_fingerprint"]


def test_only_filters_to_one_query(eval_settings: Settings) -> None:
    report = _run(eval_settings, only="q-coffee")

    for mode in MODES:
        records = report["modes"][mode]["per_query"]
        assert [record["query_id"] for record in records] == ["q-coffee"]


def test_superseded_memory_never_appears(eval_settings: Settings) -> None:
    report = _run(eval_settings)

    for mode in MODES:
        query = next(
            record
            for record in report["modes"][mode]["per_query"]
            if record["query_id"] == "q-editor-theme"
        )
        assert "a-editor-old" not in query["retrieved"]
        assert query["expected"] == ["a-editor-new"]
        assert report["modes"][mode]["failures"]["forbidden_hits"] == 0


def test_real_run_has_no_isolation_violations(eval_settings: Settings) -> None:
    report = _run(eval_settings)

    for mode in MODES:
        assert report["modes"][mode]["failures"]["isolation_violations"] == 0
        for record in report["modes"][mode]["per_query"]:
            for local_id in record["retrieved"]:
                if local_id.startswith("b-"):
                    assert record["query_id"].startswith("q-postgres")


def test_no_answer_queries_are_reported_separately(eval_settings: Settings) -> None:
    report = _run(eval_settings)

    for mode in MODES:
        no_answer = report["modes"][mode]["no_answer"]
        assert no_answer["queries"] == 2
        assert 0.0 <= no_answer["pass_rate"] <= 1.0
        for record in report["modes"][mode]["per_query"]:
            if record["category"] == "no_answer":
                assert isinstance(record["no_answer_abstained"], bool)
                assert record["metrics"] is None


def test_degraded_queries_are_excluded_and_counted(eval_settings: Settings) -> None:
    report = _run(
        eval_settings,
        modes=["semantic"],
        search_provider=ExplodingProvider(),
    )

    result = report["modes"]["semantic"]
    assert result["failures"]["degraded_queries"] == 13
    assert result["aggregate"]["queries_scored"] == 0
    assert result["aggregate"]["precision@1"] is None
    for record in result["per_query"]:
        assert record["degraded"] is True
        assert record["metrics"] is None


def test_cli_writes_the_offline_report(eval_settings: Settings, tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    database = tmp_path / "eval.db"

    exit_code = main(
        [
            "evaluate",
            "--dataset",
            str(COMMITTED),
            "--modes",
            "keyword",
            "--k",
            "1,3",
            "--repeat",
            "1",
            "--output",
            str(output),
            "--database",
            str(database),
        ],
        settings=eval_settings,
    )

    assert exit_code == 0
    assert output.exists()
    assert database.exists()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["run"]["mode"] == "offline"
    assert report["deterministic_fingerprint"].startswith("sha256:")
    assert list(report["modes"]) == ["keyword"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--k", "0"], 1),
        (["--k", "51"], 1),
        (["--modes", "fuzzy"], 1),
        (["--k", "not-numbers"], 1),
        (["--repeat", "0"], 1),
    ],
)
def test_cli_rejects_invalid_options(
    eval_settings: Settings, tmp_path: Path, argv: list[str], expected: int
) -> None:
    exit_code = main(
        [
            "evaluate",
            "--dataset",
            str(COMMITTED),
            "--output",
            str(tmp_path / "r.json"),
            *argv,
        ],
        settings=eval_settings,
    )

    assert exit_code == expected


def test_cli_rejects_missing_dataset(eval_settings: Settings, tmp_path: Path) -> None:
    exit_code = main(
        ["evaluate", "--dataset", str(tmp_path / "missing.json")],
        settings=eval_settings,
    )

    assert exit_code == 1


def test_cli_requires_explicit_external_provider_flag(
    eval_settings: Settings, tmp_path: Path
) -> None:
    exit_code = main(
        [
            "evaluate",
            "--dataset",
            str(COMMITTED),
            "--provider",
            "configured",
            "--output",
            str(tmp_path / "r.json"),
        ],
        settings=eval_settings,
    )

    assert exit_code == 1
    assert not (tmp_path / "r.json").exists()
