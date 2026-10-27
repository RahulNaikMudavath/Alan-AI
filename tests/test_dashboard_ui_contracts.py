"""Static contracts for the M3.6 dashboard polish.

These are fast, deterministic checks over the shipped assets. They do not
execute a browser; the opt-in suite in ``test_dashboard_browser.py`` does that.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.db import Database
from app.main import create_app

ASSETS = PROJECT_ROOT / "dashboard" / "assets"


def _read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _ui_imports(source: str) -> list[str]:
    match = re.search(r"import\s*\{([^}]*)\}\s*from\s*\"\./ui\.js\";", source)
    if not match:
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


@pytest.mark.parametrize(
    "asset", ["app.js", "overview.js", "explorer.js", "debugger.js", "evaluation.js"]
)
def test_no_unused_ui_imports(asset: str) -> None:
    source = _read(asset)
    match = re.search(r"import\s*\{([^}]*)\}\s*from\s*\"\./ui\.js\";", source)
    assert match is not None, f"{asset} should import from ui.js"
    body = source[match.end() :]
    unused = [
        name for name in _ui_imports(source) if not re.search(r"\b" + re.escape(name) + r"\b", body)
    ]
    assert unused == [], f"{asset} imports unused helpers: {unused}"


def test_overview_defines_prefilled_demo_queries() -> None:
    source = _read("overview.js")

    assert "export const DEMO_QUERIES" in source
    for query in [
        "editor theme preference",
        "oat milk flat white",
        "Friday deploy",
        "greyhound Pixel",
        "payments backend",
    ]:
        assert query in source, f"missing demo query: {query}"


def test_overview_hands_queries_to_the_debugger() -> None:
    source = _read("overview.js")

    assert 'ctx.navigate("debugger", { q: item.query })' in source


def test_overview_four_step_journey_links_all_views() -> None:
    source = _read("overview.js")

    assert 'ctx.navigate("overview")' in source
    assert 'ctx.navigate("explorer")' in source
    assert 'ctx.navigate("debugger")' in source
    assert 'ctx.navigate("evaluation")' in source


def test_overview_explains_demo_core_pillars() -> None:
    source = _read("overview.js")

    assert "What this demo demonstrates" in source
    assert "Memory lifecycle" in source
    assert "Provenance" in source
    assert "Retrieval comparison" in source
    assert "Evaluation" in source


def test_debugger_prefills_from_the_route() -> None:
    assert 'ctx.params.get("q")' in _read("debugger.js")
    assert "DEFAULT_QUERY" in _read("debugger.js")


@pytest.mark.parametrize("label", ["expected", "unexpected", "forbidden", "foreign user"])
def test_debugger_classifies_each_result(label: str) -> None:
    source = _read("debugger.js")

    assert f'"{label}"' in source


def test_debugger_does_not_judge_without_expectations() -> None:
    """Without user-supplied IDs, a result is only "retrieved"."""
    source = _read("debugger.js")

    assert 'return "retrieved";' in source
    assert "state.expected.length || state.forbidden.length" in source


def test_debugger_states_its_checks_are_browser_side() -> None:
    source = _read("debugger.js")

    assert "not a security boundary" in source
    assert "never estimates relevance" not in source  # that wording lives on Overview
    assert "It never estimates relevance" in _read("overview.js")


def test_debugger_explains_each_mode_without_inventing_scores() -> None:
    source = _read("debugger.js")

    for phrase in ["FTS5", "Cosine similarity", "Reciprocal-rank fusion"]:
        assert phrase in source
    assert "confidence score" in source


def test_debugger_derives_and_classifies_missing_results() -> None:
    source = _read("debugger.js")

    assert '"missing"' in source
    assert "state.expected.filter((id) => !retrievedIds.includes(id))" in source
    assert "missingCard" in source
    assert "Missing expected" in source


def test_debugger_labels_scores_as_ranking_not_confidence() -> None:
    source = _read("debugger.js")

    assert "ranking score ${formatMetric(result.score)}" in source
    assert "Never a confidence score or probability" in source
    assert "not a confidence score or probability" in source


def test_debugger_checks_superseded_history_read_only() -> None:
    source = _read("debugger.js")

    assert "Check superseded history" in source
    assert 'status: "superseded"' in source
    assert "searches active memories only" in source
    assert (
        "excluded from normal search" in source or "excluded from all active search modes" in source
    )


def test_debugger_separates_foreign_user_from_relevance() -> None:
    source = _read("debugger.js")

    assert '"foreign_user"' in source
    assert "Foreign user memory" in source
    assert "server isolation required" in source
    assert "server-side user isolation remains essential" in source


def test_debugger_explains_three_modes_accurately() -> None:
    source = _read("debugger.js")

    assert "SQLite FTS5 full-text search" in source
    assert "deterministic lexical hashes measuring shared words, not semantic meaning" in source
    assert "hybrid_recency_weight" in source


def test_debugger_provides_actionable_zero_result_guidance() -> None:
    source = _read("debugger.js")

    assert "function emptyModePanel(mode)" in source
    assert "Useful next steps to try:" in source
    assert "No active memories matched this retrieval mode" in source


def test_debugger_handles_malformed_ids() -> None:
    source = _read("debugger.js")

    assert "Ignored malformed ID(s)" in source
    assert "ID_PATTERN" in source


def test_evaluation_prefers_full_runs() -> None:
    source = _read("evaluation.js")

    assert "function isFullRun" in source
    assert "rankReports" in source
    assert "summary.evaluated_queries === summary.dataset_queries" in source


def test_evaluation_gates_comparisons_on_compatibility() -> None:
    source = _read("evaluation.js")

    for fragment in ["sameDataset", "sameK", "sameModes", "No deltas are shown"]:
        assert fragment in source


def test_evaluation_separates_measured_from_context() -> None:
    source = _read("evaluation.js")

    assert '"Measured"' in source
    assert '"Context"' in source
    assert "never runs an evaluation" in source


def test_evaluation_validates_report_shape() -> None:
    source = _read("evaluation.js")

    assert "function validateReport" in source
    assert "not a compatible evaluation report" in source


def test_evaluation_ranks_full_readable_offline_newer() -> None:
    source = _read("evaluation.js")

    assert "fullDelta" in source
    assert "readableDelta" in source
    assert "offlineDelta" in source
    assert "isOfflineReport" in source


def test_evaluation_labels_partial_reports_with_query_counts() -> None:
    source = _read("evaluation.js")

    assert "partial: ${item.evaluated_queries} of ${item.dataset_queries} queries" in source
    assert "partial: ${evaluated} of ${datasetQueries} queries" in source
    assert "${formatNumber(evaluated)} of ${formatNumber(datasetQueries)} queries" in source


def test_explorer_has_a_supersede_picker_and_conflicts_panel() -> None:
    source = _read("explorer.js")

    assert "Choose an active memory" in source
    assert "loadConflicts" in source
    assert "Open conflict candidates" in source
    assert "Keep both" in source


def test_explorer_has_lifecycle_legend() -> None:
    source = _read("explorer.js")

    assert "Current truth" in source
    assert "Historical audit record" in source
    assert "Caller-supplied user_id scopes memory storage" in source


def test_explorer_shows_and_inspects_actual_replacement_memory() -> None:
    source = _read("explorer.js")

    assert "Inspect replacement memory" in source
    assert "Replacement memory (current truth)" in source
    assert "api.getMemory(memory.superseded_by, ctx.userId)" in source


def test_explorer_shows_both_memories_before_superseding() -> None:
    source = _read("explorer.js")

    assert "Confirm Memory Supersession" in source
    assert "Becomes historical (superseded):" in source
    assert "Replacement memory (active truth):" in source


def test_explorer_searchable_supersede_picker_with_manual_fallback() -> None:
    source = _read("explorer.js")

    assert "Search active memories" in source
    assert "Choose an active memory" in source
    assert "Or paste an ID" in source
    assert "allOptions.filter" in source


def test_explorer_open_conflicts_actions_and_explanations() -> None:
    source = _read("explorer.js")

    assert "Resolve Conflict Candidate" in source
    assert "Keep A" in source
    assert "Keep B" in source
    assert "Keep both" in source
    assert "api.resolveConflict" in source


def test_explorer_prevents_duplicate_submissions() -> None:
    source = _read("explorer.js")

    assert "submit.disabled = true" in source


def test_explorer_keeps_the_exact_deletion_warning() -> None:
    source = _read("explorer.js")

    assert (
        "Conversation receipts (conversations.result_json) and optional stored raw "
        "messages are not erased. Complete data erasure is not provided." in source
    )


def test_dialogs_restore_focus_and_mark_busy_regions() -> None:
    source = _read("ui.js")

    assert "restoreTarget" in source
    assert "restoreTarget.focus()" in source
    assert '"aria-busy": "true"' in source


def test_no_result_card_is_identified_by_colour_alone() -> None:
    source = _read("debugger.js")
    styles = _read("styles.css")

    assert "classify-" in source
    for kind in ["expected", "unexpected", "forbidden", "foreign"]:
        assert f".result-card.classify-{kind}" in styles


def test_shared_ui_reports_invalid_input_rather_than_guessing() -> None:
    """The clients must surface API failures, never substitute placeholder data."""
    for asset in ["explorer.js", "evaluation.js", "debugger.js"]:
        source = _read(asset)
        assert "errorPanel" in source or 'outcome.status === "rejected"' in source


def test_dashboard_client_never_invents_metrics() -> None:
    source = _read("evaluation.js")

    # The Lab renders report values and computes only B-A differences.
    assert "formatMetric(" in source
    assert "Math.random" not in source
    assert "toFixed(2)" not in source


def test_dashboard_routes_still_serve_the_polished_assets(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "ui.db",
        eval_reports_path=tmp_path / "reports",
        environment="test",
    )
    Database(settings.database_path).initialize()
    with TestClient(create_app(settings)) as client:
        page = client.get("/dashboard/")
        script = client.get("/dashboard/assets/overview.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert b"DEMO_QUERIES" in script.content


def test_settings_reject_an_invalid_environment_prefix() -> None:
    """Guard the test helper itself: unknown keys are ignored, not silently typed."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_provider="not-a-provider")


def test_accessible_names_for_result_and_row_actions() -> None:
    """Action buttons must include descriptive accessible names."""
    explorer_source = _read("explorer.js")
    debugger_source = _read("debugger.js")

    assert '"aria-label": `Inspect memory ' in explorer_source
    assert '"aria-label": `Supersede memory ' in explorer_source
    assert '"aria-label": `Delete memory ' in explorer_source
    assert '"aria-label": `Keep candidate A for conflict ' in explorer_source
    assert '"aria-label": `Keep candidate B for conflict ' in explorer_source
    assert '"aria-label": `Keep both candidates for conflict ' in explorer_source

    assert '"aria-label": `Explain result ' in debugger_source
    assert '"aria-label": `Copy memory ID ' in debugger_source


def test_aria_busy_marks_loading_regions_across_views() -> None:
    """Loading regions across app, explorer, debugger, and evaluation must set aria-busy."""
    app_source = _read("app.js")
    explorer_source = _read("explorer.js")
    debugger_source = _read("debugger.js")
    eval_source = _read("evaluation.js")

    assert 'viewRoot.setAttribute("aria-busy", "true")' in app_source
    assert 'listHost.setAttribute("aria-busy", "true")' in explorer_source
    assert 'resultsHost.setAttribute("aria-busy", "true")' in debugger_source
    assert 'host.setAttribute("aria-busy", "true")' in eval_source


def test_responsive_layout_styles_include_mobile_dialogs() -> None:
    """Responsive styles must accommodate narrow viewports and dialogs."""
    styles = _read("styles.css")

    assert ".row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }" in styles
    assert ".row.space-between { justify-content: space-between; }" in styles
    assert ".dialog-foot { flex-direction: column-reverse; }" in styles
    assert ".dialog-actions { width: 100%; flex-direction: column-reverse; }" in styles
