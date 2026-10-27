/* Evaluation Lab: load generated reports, rank full runs, compare compatibly. */

import { api } from "./api.js";
import {
  el,
  pageHead,
  card,
  button,
  badge,
  loadingPanel,
  errorPanel,
  emptyPanel,
  clear,
  formatDate,
  formatMetric,
  formatDelta,
  formatNumber,
  safeCodeBlock,
  toast,
  kvList,
  modeLabel,
} from "./ui.js";

const REQUIRED_KEYS = ["dataset", "run", "modes"];

function isFullRun(summary) {
  if (!summary || summary.readable === false) return false;
  const dq = summary.dataset_queries;
  const eq = summary.evaluated_queries;
  if (typeof dq !== "number" || typeof eq !== "number" || dq <= 1) return false;
  if (summary.all_modes_full === false) return false;
  return summary.evaluated_queries === summary.dataset_queries;
}

function isOfflineReport(item) {
  if (!item) return false;
  return Boolean(
    item.is_offline ||
      item.run_mode === "offline" ||
      item.search_configuration?.provider?.name === "fake" ||
      item.run?.mode === "offline" ||
      item.search_configuration?.provider?.external === false,
  );
}

/**
 * Rank:
 * 1. Compatible full runs (where every mode evaluated dataset.queries and dataset.queries > 1)
 * 2. Readable reports
 * 3. Offline reports
 * 4. Newer reports (modified_at descending)
 */
function rankReports(items) {
  return [...items].sort((left, right) => {
    const fullDelta = Number(isFullRun(right)) - Number(isFullRun(left));
    if (fullDelta !== 0) return fullDelta;
    const readableDelta = Number(right.readable !== false) - Number(left.readable !== false);
    if (readableDelta !== 0) return readableDelta;
    const offlineDelta = Number(isOfflineReport(right)) - Number(isOfflineReport(left));
    if (offlineDelta !== 0) return offlineDelta;
    return String(right.modified_at || "").localeCompare(String(left.modified_at || ""));
  });
}

function validateReport(report) {
  if (!report || typeof report !== "object") return "the file is not a JSON object";
  for (const key of REQUIRED_KEYS) {
    if (!(key in report)) return `missing "${key}"`;
  }
  if (!report.dataset || typeof report.dataset !== "object") return '"dataset" is not an object';
  if (!report.run || typeof report.run !== "object") return '"run" is not an object';
  if (typeof report.modes !== "object" || report.modes === null) return '"modes" is not an object';
  const modeNames = Object.keys(report.modes);
  if (!modeNames.length) return "no retrieval modes in the report";
  for (const name of modeNames) {
    const result = report.modes[name];
    if (!result || typeof result !== "object") return `mode "${name}" is not an object`;
    if (!Array.isArray(result.per_query)) return `mode "${name}" has no per-query results`;
    if (!result.aggregate || typeof result.aggregate !== "object") {
      return `mode "${name}" has no aggregate metrics`;
    }
  }
  return null;
}

export async function evaluationView(root, ctx) {
  clear(root);
  root.append(
    pageHead(
      "Evaluation Lab",
      "Load generated evaluation reports and compare them. Reports are read-only JSON files; this view never runs an evaluation.",
    ),
  );

  const state = {
    items: [],
    local: new Map(),
    cache: new Map(),
    a: "",
    b: "",
    unreadable: new Map(),
  };

  const selectA = el("select", { class: "select" });
  const selectB = el("select", { class: "select" });
  const fileInput = el("input", { type: "file", accept: ".json,application/json" });

  const toolbar = el(
    "form",
    { class: "toolbar" },
    el("div", { class: "field" }, el("label", { for: "eval-a", text: "Report A" }), selectA),
    el("div", { class: "field" }, el("label", { for: "eval-b", text: "Report B" }), selectB),
    el(
      "div",
      { class: "field" },
      el("label", { for: "eval-file", text: "Local report" }),
      fileInput,
      el("span", { class: "hint", text: "Read in your browser. Nothing is uploaded." }),
    ),
    button("Refresh", () => refresh(), { variant: "ghost" }),
    button("Compare", () => render(), { variant: "primary" }),
  );
  selectA.id = "eval-a";
  selectB.id = "eval-b";
  fileInput.id = "eval-file";

  const host = el("div", { class: "stack" });
  root.append(toolbar, host);

  selectA.addEventListener("change", () => {
    state.a = selectA.value;
    render();
  });
  selectB.addEventListener("change", () => {
    state.b = selectB.value;
    render();
  });
  toolbar.addEventListener("submit", (event) => {
    event.preventDefault();
    render();
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const problem = validateReport(parsed);
      if (problem) throw new Error(problem);
      state.local.set(file.name, parsed);
      await refresh();
      state.b = file.name;
      selectB.value = file.name;
      render();
      toast(`Loaded ${file.name} from your computer.`, "info");
    } catch (error) {
      toast(`Could not read ${file.name}: ${error.message}`, "error");
    }
  });

  async function refresh() {
    clear(host);
    host.append(loadingPanel("Loading available reports…"));
    let items = [];
    try {
      const listing = await api.reports();
      items = Array.isArray(listing.items) ? listing.items : [];
    } catch (error) {
      clear(host);
      host.append(errorPanel(error, refresh));
      return;
    }
    for (const [name, parsed] of state.local.entries()) {
      if (!items.some((item) => item.name === name)) {
        const modeCounts = Object.values(parsed?.modes || {}).map(
          (m) => (m?.per_query || []).length,
        );
        const dq = parsed?.dataset?.queries;
        const eq = modeCounts.length ? Math.min(...modeCounts) : null;
        const allFull =
          modeCounts.length > 0 &&
          typeof dq === "number" &&
          dq > 1 &&
          modeCounts.every((count) => count === dq);
        items.push({
          name,
          readable: true,
          dataset_queries: dq,
          evaluated_queries: eq,
          all_modes_full: allFull,
          is_offline: isOfflineReport(parsed),
          modified_at: new Date().toISOString(),
        });
      }
    }

    state.items = rankReports(items);
    state.unreadable = new Map(
      state.items
        .filter((item) => item.readable === false)
        .map((item) => [item.name, item.unreadable_reason || "unreadable"]),
    );

    const names = state.items.map((item) => item.name);
    fillSelect(selectA, names, state.a, state.unreadable);
    fillSelect(selectB, names, state.b, state.unreadable);

    // Default to full runs. A partial or unreadable report is never preferred automatically.
    const fullRuns = state.items.filter(isFullRun).map((item) => item.name);
    const readableNames = state.items
      .filter((item) => item.readable !== false)
      .map((item) => item.name);

    if (fullRuns.length) {
      if (!state.a || state.unreadable.has(state.a) || !names.includes(state.a)) {
        state.a = fullRuns[0];
      }
      if (!state.b && fullRuns.length > 1) {
        state.b = fullRuns.find((name) => name !== state.a) || "";
      }
    } else {
      if (!state.a && readableNames.length) {
        state.a = readableNames[0];
      }
    }
    if (state.a) selectA.value = state.a;
    if (state.b) selectB.value = state.b;
    render();
  }

  function fillSelect(select, names, selected, unreadable) {
    clear(select);
    select.append(el("option", { value: "", text: names.length ? "None" : "No reports" }));
    for (const item of state.items) {
      const isUnreadable = unreadable && unreadable.has(item.name);
      let label = item.name;
      if (isUnreadable) {
        label += " (unreadable)";
      } else if (!isFullRun(item)) {
        if (
          typeof item.evaluated_queries === "number" &&
          typeof item.dataset_queries === "number"
        ) {
          label += ` (partial: ${item.evaluated_queries} of ${item.dataset_queries} queries)`;
        } else {
          label += " (partial)";
        }
      }
      select.append(el("option", { value: item.name, text: label }));
    }
    if (selected && names.includes(selected)) select.value = selected;
  }

  async function loadReport(name) {
    if (!name) return null;
    if (state.local.has(name)) return state.local.get(name);
    if (state.cache.has(name)) return state.cache.get(name);
    const report = await api.report(name);
    state.cache.set(name, report);
    return report;
  }

  async function render() {
    clear(host);
    if (!state.items.length) {
      host.append(
        emptyPanel(
          "No evaluation reports yet",
          "Generate one with the offline evaluator. This view reads existing files; it never fabricates a run.",
          safeCodeBlock(
            "python -m app.cli evaluate --dataset eval/datasets/retrieval-v1.json --output eval/reports/retrieval-v1-offline.json",
          ),
        ),
      );
      return;
    }
    if (!state.a) {
      host.append(emptyPanel("Select a report", "Choose report A to inspect its metrics."));
      return;
    }

    host.setAttribute("aria-busy", "true");
    host.append(loadingPanel("Loading report…"));
    let reportA = null;
    let reportB = null;
    let errorA = null;
    let errorB = null;
    try {
      try {
        reportA = await loadReport(state.a);
      } catch (error) {
        errorA = error;
      }
      if (state.b && state.b !== state.a) {
        try {
          reportB = await loadReport(state.b);
        } catch (error) {
          errorB = error;
        }
      }

      const problemA = reportA ? validateReport(reportA) : null;
      const problemB = reportB ? validateReport(reportB) : null;

      clear(host);
      if (errorA || problemA) {
        host.append(
          incompatibleCard(state.a, errorA?.message || problemA),
          reportB && !errorB && !problemB ? reportCard("Report B", reportB, state.b) : null,
        );
        return;
      }
      if (!reportA) {
        host.append(emptyPanel("Report not available", "The selected report could not be loaded."));
        return;
      }

      host.append(reportCard("Report A", reportA, state.a));
      if (errorB || problemB) {
        host.append(incompatibleCard(state.b, errorB?.message || problemB));
      } else if (reportB) {
        host.append(reportCard("Report B", reportB, state.b));
        host.append(comparisonCard(reportA, reportB));
      }
      host.append(perQueryCard(reportA));
    } finally {
      host.removeAttribute("aria-busy");
    }
  }

  function incompatibleCard(name, reason) {
    return el(
      "div",
      { class: "card danger" },
      el(
        "div",
        { class: "card-body" },
        el("p", { class: "mb-0", text: `"${name}" is not a compatible evaluation report.` }),
        el("p", { class: "small muted mb-0", text: reason || "unknown reason" }),
        el("p", {
          class: "small muted mb-0",
          text: "No metrics are shown, and no values are estimated from the file.",
        }),
      ),
    );
  }

  function reportCard(title, report, name) {
    const provider = report.search_configuration?.provider || {};
    const isOffline = isOfflineReport(report);
    const modeNames = Object.keys(report.modes || {});
    const perModeCounts = modeNames.map((mode) => (report.modes[mode].per_query || []).length);
    const evaluated = perModeCounts.length ? Math.min(...perModeCounts) : 0;
    const datasetQueries = report.dataset?.queries ?? null;
    const full =
      typeof datasetQueries === "number" &&
      datasetQueries > 1 &&
      perModeCounts.length > 0 &&
      perModeCounts.every((count) => count === datasetQueries);

    const measured = el(
      "div",
      {},
      el("h3", { class: "block-title", text: "Measured" }),
      el("p", {
        class: "small muted",
        text: "Values below come directly from the report file. Nothing here is recalculated or estimated by the dashboard.",
      }),
      metricsTable(report),
      failuresTable(report),
    );

    const context = el(
      "div",
      {},
      el("h3", { class: "block-title", text: "Context" }),
      kvList([
        { label: "File", value: name || "—" },
        { label: "Dataset SHA-256", value: report.dataset?.sha256 || "—" },
        {
          label: "Queries evaluated",
          value:
            typeof datasetQueries === "number"
              ? `${formatNumber(evaluated)} of ${formatNumber(datasetQueries)} queries`
              : `${formatNumber(evaluated)} queries`,
        },
        { label: "Provider model", value: provider.model || "—" },
        { label: "Generated at", value: formatDate(report.run?.generated_at) },
        { label: "K values", value: (report.run?.k_values || []).join(", ") || "—" },
        { label: "Repeats", value: formatNumber(report.run?.repeat) },
        { label: "Fingerprint", value: report.deterministic_fingerprint || "—" },
      ]),
      el("ul", { class: "list small" }, (report.caveats || []).map((item) => el("li", { text: item }))),
    );

    return card(
      title,
      el(
        "div",
        { class: "stack" },
        el(
          "div",
          { class: "chip-row" },
          badge(report.dataset?.version || "unknown dataset", "accent"),
          badge(report.run?.mode || "unknown mode", isOffline ? "warn" : "info"),
          badge(provider.name || "unknown provider", provider.external ? "info" : "neutral"),
          full
            ? badge("full run", "ok")
            : badge(
                typeof datasetQueries === "number"
                  ? `partial: ${evaluated} of ${datasetQueries} queries`
                  : `partial: ${evaluated} queries`,
                "warn",
              ),
        ),
        isOffline
          ? el(
              "div",
              { class: "card warning" },
              el(
                "div",
                { class: "card-body" },
                el("p", {
                  class: "mb-0",
                  text: "Offline plumbing evaluation. The provider is a deterministic lexical hash embedding, so these numbers measure ranking and filtering behaviour, not semantic quality.",
                }),
              ),
            )
          : null,
        measured,
        context,
      ),
    );
  }

  function metricsTable(report) {
    const kValues = report.run?.k_values || [];
    const header = el(
      "tr",
      {},
      el("th", { text: "Mode" }),
      kValues.map((k) => el("th", { text: `P@${k}` })),
      kValues.map((k) => el("th", { text: `R@${k}` })),
      kValues.map((k) => el("th", { text: `nDCG@${k}` })),
      el("th", { text: "MRR" }),
      el("th", { text: "Scored" }),
    );
    const rows = Object.entries(report.modes || {}).map(([mode, result]) =>
      el(
        "tr",
        {},
        el("td", { text: modeLabel(mode) }),
        kValues.map((k) => el("td", { text: formatMetric(result.aggregate?.[`precision@${k}`]) })),
        kValues.map((k) => el("td", { text: formatMetric(result.aggregate?.[`recall@${k}`]) })),
        kValues.map((k) => el("td", { text: formatMetric(result.aggregate?.[`ndcg@${k}`]) })),
        el("td", { text: formatMetric(result.aggregate?.mrr) }),
        el("td", { text: formatNumber(result.aggregate?.queries_scored) }),
      ),
    );
    return el(
      "div",
      {},
      el("h4", { class: "section-title", text: "Metrics" }),
      el(
        "div",
        { class: "table-wrap" },
        el("table", { class: "data-table" }, el("thead", {}, header), el("tbody", {}, rows)),
      ),
    );
  }

  function failuresTable(report) {
    const header = el(
      "tr",
      {},
      el("th", { text: "Mode" }),
      el("th", { text: "Relevance misses" }),
      el("th", { text: "Isolation" }),
      el("th", { text: "Forbidden" }),
      el("th", { text: "Duplicates" }),
      el("th", { text: "Degraded" }),
      el("th", { text: "Unstable" }),
      el("th", { text: "No-answer pass" }),
    );
    const rows = Object.entries(report.modes || {}).map(([mode, result]) => {
      const failures = result.failures || {};
      return el(
        "tr",
        {},
        el("td", { text: modeLabel(mode) }),
        el("td", { text: formatNumber(failures.relevance_misses) }),
        el("td", { text: formatNumber(failures.isolation_violations) }),
        el("td", { text: formatNumber(failures.forbidden_hits) }),
        el("td", { text: formatNumber(failures.duplicate_results) }),
        el("td", { text: formatNumber(failures.degraded_queries) }),
        el("td", { text: formatNumber(failures.unstable_queries) }),
        el("td", { text: formatMetric(result.no_answer?.pass_rate) }),
      );
    });
    return el(
      "div",
      {},
      el("h4", { class: "section-title", text: "Failure categories" }),
      el(
        "div",
        { class: "table-wrap" },
        el("table", { class: "data-table" }, el("thead", {}, header), el("tbody", {}, rows)),
      ),
    );
  }

  function comparisonCard(reportA, reportB) {
    const sameDataset =
      Boolean(reportA.dataset?.sha256) &&
      reportA.dataset?.sha256 === reportB.dataset?.sha256;
    const sameK =
      Array.isArray(reportA.run?.k_values) &&
      reportA.run.k_values.length > 0 &&
      JSON.stringify(reportA.run.k_values) === JSON.stringify(reportB.run?.k_values || []);
    const modesA = Object.keys(reportA.modes || {}).sort();
    const modesB = Object.keys(reportB.modes || {}).sort();
    const sameModes = modesA.length > 0 && JSON.stringify(modesA) === JSON.stringify(modesB);
    const compatible = sameDataset && sameK && sameModes;

    const kValues = reportA.run?.k_values || [];
    const metricKeys = [];
    for (const k of kValues) {
      metricKeys.push(`precision@${k}`, `recall@${k}`, `ndcg@${k}`);
    }
    metricKeys.push("mrr");

    const body = el("div", { class: "stack" });
    if (!compatible) {
      const reasons = [];
      if (!sameDataset) reasons.push("different dataset hashes");
      if (!sameK) reasons.push("different K values");
      if (!sameModes) reasons.push("different retrieval modes");
      body.append(
        el(
          "div",
          { class: "card warning" },
          el(
            "div",
            { class: "card-body" },
            el("p", {
              class: "mb-0",
              text: `These reports are not comparable: ${reasons.join(", ")}. No deltas are shown.`,
            }),
          ),
        ),
      );
      return card("Comparison", body);
    }

    const header = el(
      "tr",
      {},
      el("th", { text: "Mode" }),
      el("th", { text: "Metric" }),
      el("th", { text: "A" }),
      el("th", { text: "B" }),
      el("th", { text: "Delta (B − A)" }),
    );
    const rows = [];
    for (const mode of Object.keys(reportA.modes || {})) {
      for (const key of metricKeys) {
        const valueA = reportA.modes[mode].aggregate?.[key];
        const valueB = reportB.modes[mode].aggregate?.[key];
        rows.push(
          el(
            "tr",
            {},
            el("td", { text: modeLabel(mode) }),
            el("td", { text: key }),
            el("td", { text: formatMetric(valueA) }),
            el("td", { text: formatMetric(valueB) }),
            el("td", {
              text:
                valueA === null || valueA === undefined || valueB === null || valueB === undefined
                  ? "—"
                  : formatDelta(Number(valueB) - Number(valueA)),
            }),
          ),
        );
      }
    }

    body.append(
      el("p", {
        class: "small muted mb-0",
        text: "Same dataset hash, K values, and mode set. Deltas compare the two measured runs only.",
      }),
      el(
        "div",
        { class: "table-wrap" },
        el("table", { class: "data-table" }, el("thead", {}, header), el("tbody", {}, rows)),
      ),
    );
    return card("Comparison", body);
  }

  function perQueryCard(report) {
    const modes = Object.keys(report.modes || {});
    const modeSelect = el(
      "select",
      { class: "select" },
      modes.map((mode) => el("option", { value: mode, text: modeLabel(mode) })),
    );
    const listHost = el("div", { class: "stack" });
    const renderMode = () => {
      clear(listHost);
      const result = report.modes[modeSelect.value];
      if (!result) return;
      for (const record of result.per_query || []) {
        listHost.append(
          el(
            "details",
            { class: "card" },
            el("summary", {
              class: "card-head",
              text: `${record.query_id} · ${record.category} · ${record.relevance_outcome}`,
            }),
            el(
              "div",
              { class: "card-body" },
              kvList([
                { label: "Query", value: record.query },
                { label: "Expected", value: (record.expected || []).join(", ") || "—" },
                { label: "Retrieved", value: (record.retrieved || []).join(", ") || "—" },
                { label: "Missing relevant", value: (record.missing || []).join(", ") || "none" },
                { label: "Irrelevant", value: (record.irrelevant || []).join(", ") || "none" },
                {
                  label: "Forbidden hits",
                  value: (record.forbidden_hits || []).join(", ") || "none",
                },
                {
                  label: "Foreign user hits",
                  value: (record.foreign_user_hits || []).join(", ") || "none",
                },
                { label: "Degraded", value: record.degraded ? "yes" : "no" },
                { label: "Latency (ms)", value: formatNumber(record.latency_ms) },
              ]),
              el("p", { class: "small muted", text: `Rationale: ${record.rationale || "—"}` }),
              record.reproduce?.command ? safeCodeBlock(record.reproduce.command) : null,
            ),
          ),
        );
      }
    };
    modeSelect.addEventListener("change", renderMode);
    if (modes.length) {
      modeSelect.value = modes[0];
      renderMode();
    }
    return card(
      "Per-query results",
      el(
        "div",
        { class: "stack" },
        el("div", { class: "field" }, el("label", { for: "eval-mode", text: "Mode" }), modeSelect),
        listHost,
      ),
    );
  }

  await refresh();
}