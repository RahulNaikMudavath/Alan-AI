/* Retrieval Debugger: compare keyword, semantic, and hybrid for one query. */

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
  statusBadge,
  kindBadge,
  formatMetric,
  modeLabel,
  copyText,
  truncate,
} from "./ui.js";
import { openExplainDialog } from "./explain.js";

const MODES = ["keyword", "semantic", "hybrid"];
const KINDS = ["", "fact", "preference", "event", "task"];
const DEFAULT_QUERY = "editor theme preference";

const MODE_HELP = {
  keyword: {
    title: "Keyword",
    text: "SQLite FTS5 full-text search. Every query word must appear (matching as prefixes); ordered by bm25 relevance. Falls back to substring matching if FTS5 is not ready.",
  },
  semantic: {
    title: "Semantic",
    text: "Cosine similarity over stored embeddings for the configured model. Note: with the offline fake embedding provider, vectors are deterministic lexical hashes measuring shared words, not semantic meaning.",
  },
  hybrid: {
    title: "Hybrid",
    text: "Reciprocal-rank fusion (RRF) combining keyword and semantic ranks, plus an actual recency signal (hybrid_recency_weight) reordering hits by newest created_at. Never a confidence score or probability.",
  },
};

export async function debuggerView(root, ctx) {
  clear(root);
  root.append(
    pageHead(
      "Retrieval Debugger",
      "Run the same query through all three retrieval modes and compare the ranked results.",
    ),
  );

  const prefill = ctx.params ? ctx.params.get("q") || "" : "";
  const state = {
    query: prefill || DEFAULT_QUERY,
    expected: [],
    forbidden: [],
    malformed: [],
    autoRan: false,
  };

  const queryInput = el("input", {
    class: "input",
    type: "search",
    placeholder: "e.g. editor theme preference",
    value: state.query,
  });
  const kindInput = el(
    "select",
    { class: "select" },
    KINDS.map((item) => el("option", { value: item, text: item || "Any kind" })),
  );
  const agentInput = el("input", { class: "input", placeholder: "optional" });
  const kInput = el("input", { class: "input", type: "number", min: "1", max: "50", value: "10" });
  const expectedInput = el("input", {
    class: "input",
    placeholder: "comma separated memory IDs (optional)",
  });
  const forbiddenInput = el("input", {
    class: "input",
    placeholder: "comma separated memory IDs (optional)",
  });

  const form = el(
    "form",
    { class: "toolbar" },
    el("div", { class: "field grow" }, el("label", { for: "dbg-query", text: "Query" }), queryInput),
    el("div", { class: "field" }, el("label", { for: "dbg-kind", text: "Kind" }), kindInput),
    el("div", { class: "field" }, el("label", { for: "dbg-agent", text: "Agent" }), agentInput),
    el("div", { class: "field" }, el("label", { for: "dbg-k", text: "K" }), kInput),
    el(
      "div",
      { class: "field grow" },
      el("label", { for: "dbg-expected", text: "Expected memory IDs" }),
      expectedInput,
    ),
    el(
      "div",
      { class: "field grow" },
      el("label", { for: "dbg-forbidden", text: "Must-not-include IDs" }),
      forbiddenInput,
    ),
    el(
      "div",
      { class: "field" },
      el("label", { text: "Examples" }),
      el(
        "div",
        { class: "chip-row" },
        [
          "editor theme preference",
          "oat milk flat white",
          "Friday deploy",
          "greyhound Pixel",
          "payments backend",
        ].map((example) =>
          button(
            example,
            () => {
              queryInput.value = example;
              run();
            },
            { variant: "ghost small" },
          ),
        ),
      ),
    ),
    button("Run comparison", () => run(), { variant: "primary" }),
    button("Check superseded history", () => checkSuperseded(), { variant: "ghost" }),
    button("Copy request", () => copyRequest(), { variant: "ghost" }),
  );
  queryInput.id = "dbg-query";
  kindInput.id = "dbg-kind";
  agentInput.id = "dbg-agent";
  kInput.id = "dbg-k";
  expectedInput.id = "dbg-expected";
  forbiddenInput.id = "dbg-forbidden";

  const resultsHost = el("div", { class: "stack" });
  const supersededHost = el("div", { class: "stack mt-2" });
  root.append(modeHelpCard(), form, resultsHost, supersededHost);
  resultsHost.append(initialState());
  renderSupersededInitial();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });

  function modeHelpCard() {
    return el(
      "div",
      { class: "mode-help" },
      MODES.map((mode) =>
        el(
          "div",
          { class: "mode-help-item" },
          el("strong", { text: MODE_HELP[mode].title }),
          el("p", { class: "small muted mb-0", text: MODE_HELP[mode].text }),
        ),
      ),
    );
  }

  function initialState() {
    return emptyPanel(
      "Run a query to compare retrieval",
      `The query field is prefilled with "${DEFAULT_QUERY}". Add expected memory IDs to see what was missed, or must-not-include IDs to catch results that should never appear.`,
    );
  }

  function renderSupersededInitial() {
    clear(supersededHost);
    supersededHost.append(
      card(
        "Superseded history check (read-only)",
        el(
          "div",
          { class: "stack" },
          el("p", {
            class: "small muted mb-0",
            text: "Normal retrieval (keyword, semantic, and hybrid) searches active memories only. Superseded memories are archived history and are strictly excluded from normal search. Use this check to see if memories matching your query exist in superseded history.",
          }),
          el(
            "div",
            {},
            button("Check superseded history", () => checkSuperseded(), {
              variant: "primary small",
            }),
          ),
        ),
      ),
    );
  }

  function currentBase() {
    const k = Number(kInput.value);
    const base = { user_id: ctx.userId, query: queryInput.value.trim(), limit: k };
    if (kindInput.value) base.kind = kindInput.value;
    if (agentInput.value.trim()) base.agent_id = agentInput.value.trim();
    return base;
  }

  function copyRequest() {
    copyText(JSON.stringify({ ...currentBase(), mode: "hybrid" }, null, 2), "Request copied.");
  }

  function parseIds(input) {
    const raw = (input.value || "").trim();
    if (!raw) return { valid: [], malformed: [] };
    const tokens = raw.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean);
    const valid = [];
    const malformed = [];
    const ID_PATTERN = /^[a-zA-Z0-9_-]{1,128}$/;
    for (const token of tokens) {
      if (ID_PATTERN.test(token)) {
        if (!valid.includes(token)) valid.push(token);
      } else {
        if (!malformed.includes(token)) malformed.push(token);
      }
    }
    return { valid, malformed };
  }

  async function checkSuperseded() {
    const q = (queryInput.value || "").trim();
    if (!q) {
      queryInput.focus();
      return;
    }
    clear(supersededHost);
    supersededHost.append(
      card(
        "Superseded history check (read-only)",
        loadingPanel(`Searching superseded history for "${q}"…`),
      ),
    );

    try {
      const data = await api.listMemories({
        userId: ctx.userId,
        q,
        kind: kindInput.value || undefined,
        status: "superseded",
        limit: 10,
      });
      const items = Array.isArray(data?.items) ? data.items : [];
      clear(supersededHost);
      supersededHost.append(
        card(
          "Superseded history check (read-only)",
          el(
            "div",
            { class: "stack" },
            el("p", {
              class: "small muted mb-0",
              text: "Normal search returns active memories only. Superseded memories are retained for audit history and are excluded from all active search modes.",
            }),
            items.length
              ? el(
                  "div",
                  { class: "stack" },
                  el("p", {
                    class: "small mb-0",
                    text: `Found ${data.total || items.length} matching superseded ${data.total === 1 ? "memory" : "memories"}:`,
                  }),
                  items.map((m) => supersededMemoryCard(m)),
                )
              : emptyPanel(
                  "No superseded memories match",
                  `No superseded history rows matched "${q}" for user ${ctx.userId}.`,
                ),
            el(
              "div",
              { class: "mt-1" },
              button("Re-check superseded history", () => checkSuperseded(), {
                variant: "ghost small",
              }),
            ),
          ),
        ),
      );
    } catch (err) {
      clear(supersededHost);
      supersededHost.append(
        card(
          "Superseded history check (read-only)",
          errorPanel(err, () => checkSuperseded()),
        ),
      );
    }
  }

  function supersededMemoryCard(memory) {
    const chips = [statusBadge("superseded")];
    if (memory.kind) chips.push(kindBadge(memory.kind));
    if (memory.superseded_reason) {
      chips.push(el("span", { class: "badge neutral", text: `reason: ${memory.superseded_reason}` }));
    }

    return el(
      "div",
      { class: "result-card is-superseded" },
      el("div", { class: "chip-row" }, chips),
      el("p", { class: "result-content mt-05", text: memory.content || "" }),
      memory.superseded_by
        ? el("p", {
            class: "small muted mb-0",
            text: `Replaced by: ${memory.superseded_by}`,
          })
        : null,
      memory.superseded_note
        ? el("p", { class: "small muted mb-0", text: `Note: ${memory.superseded_note}` })
        : null,
      el(
        "div",
        { class: "row-actions mt-1" },
        button("Explain", () => openExplainDialog(memory.id, ctx), {
          variant: "small",
          "aria-label": `Explain superseded memory ${truncate(memory.content || memory.id, 30)}`,
        }),
        button("Copy ID", () => copyText(memory.id, "Memory ID copied."), {
          variant: "link",
          "aria-label": `Copy superseded memory ID ${memory.id}`,
        }),
      ),
    );
  }

  async function run() {
    const base = currentBase();
    if (!base.query) {
      queryInput.focus();
      return;
    }
    if (!Number.isFinite(base.limit) || base.limit < 1 || base.limit > 50) {
      kInput.focus();
      return;
    }
    const expectedParsed = parseIds(expectedInput);
    const forbiddenParsed = parseIds(forbiddenInput);
    state.query = base.query;
    state.expected = expectedParsed.valid;
    state.forbidden = forbiddenParsed.valid;
    state.malformed = [...expectedParsed.malformed, ...forbiddenParsed.malformed];

    clear(resultsHost);
    resultsHost.setAttribute("aria-busy", "true");
    if (state.malformed.length) {
      resultsHost.append(
        el(
          "div",
          { class: "card warning mb-1" },
          el(
            "div",
            { class: "card-body" },
            el("strong", { text: "Ignored malformed ID(s)" }),
            el("p", {
              class: "small muted mb-0",
              text: `The following token(s) are not valid alphanumeric identifiers and were skipped: ${state.malformed.join(", ")}`,
            }),
          ),
        ),
      );
    }

    const columns = el("div", { class: "result-grid" });
    for (const mode of MODES) {
      columns.append(
        el(
          "section",
          { class: "result-column", "aria-busy": "true" },
          el(
            "div",
            { class: "result-column-head" },
            el("strong", { text: modeLabel(mode) }),
            badge("running", "info"),
          ),
          el("div", { class: "result-column-body" }, loadingPanel("Retrieving…")),
        ),
      );
    }
    resultsHost.append(columns);

    try {
      const settled = await Promise.allSettled(
        MODES.map((mode) => api.search({ ...base, mode })),
      );

      for (let index = 0; index < MODES.length; index += 1) {
        const column = columns.children[index];
        column.removeAttribute("aria-busy");
        clear(column);
        column.append(
          el(
            "div",
            { class: "result-column-head" },
            el("strong", { text: modeLabel(MODES[index]) }),
            settled[index].status === "fulfilled"
              ? badge(`${(settled[index].value?.results || []).length} hits`, "neutral")
              : badge("failed", "danger"),
          ),
        );
        const body = el("div", { class: "result-column-body" });
        column.append(body);
        renderMode(MODES[index], settled[index], body, base.limit);
      }
    } finally {
      resultsHost.removeAttribute("aria-busy");
    }

    resultsHost.append(classificationLegend());
    renderSupersededInitial();
  }

  function renderMode(mode, outcome, body, limit) {
    if (outcome.status === "rejected") {
      body.append(
        el(
          "div",
          { class: "state-panel error" },
          el("h3", { text: "Request failed" }),
          el("p", { text: outcome.reason?.message || "The retrieval request failed." }),
        ),
      );
      return;
    }

    const response = outcome.value;
    const results = Array.isArray(response?.results) ? response.results : [];

    if (response?.degraded) {
      body.append(
        el(
          "div",
          { class: "card warning" },
          el(
            "div",
            { class: "card-body" },
            el("p", {
              class: "mb-0",
              text: "The semantic signal was unavailable, so these results come from keyword matching only.",
            }),
          ),
        ),
      );
    }

    const foreign = results.filter((item) => item.memory?.user_id && item.memory.user_id !== ctx.userId);
    if (foreign.length) {
      body.append(
        el(
          "div",
          { class: "card danger" },
          el(
            "div",
            { class: "card-body" },
            el("p", {
              class: "mb-0",
              text: "Integrity warning: results from another user were returned. This check runs in the browser as a diagnostic and is not a security boundary; server-side user isolation remains essential.",
            }),
            el("ul", { class: "list small" }, foreign.map((item) => el("li", { text: item.memory.id }))),
          ),
        ),
      );
    }

    if (response?.coverage && typeof response.coverage.embedded === "number" && typeof response.coverage.considered === "number") {
      body.append(
        el("p", {
          class: "small muted",
          text: `Coverage: ${response.coverage.embedded} of ${response.coverage.considered} scoped memories have vectors.`,
        }),
      );
    }

    if (state.expected.length || state.forbidden.length) {
      body.append(comparisonSummary(results));
    }

    if (!results.length) {
      body.append(emptyModePanel(mode));
    }

    for (let index = 0; index < results.length; index += 1) {
      body.append(resultCard(results[index], index));
    }

    const retrievedIds = results.map((item) => item.memory?.id).filter(Boolean);
    const missing = state.expected.filter((id) => !retrievedIds.includes(id));
    if (missing.length > 0) {
      const missingSection = el(
        "div",
        { class: "stack mt-1" },
        el(
          "div",
          { class: "row space-between" },
          el("strong", { class: "small", text: `Missing expected (${missing.length})` }),
          badge(`${missing.length} missing`, "warn"),
        ),
        missing.map((id) => missingCard(id, mode, limit)),
      );
      body.append(missingSection);
    }

    body.append(
      el("p", {
        class: "small muted",
        text: "ranking score is a fused reciprocal-rank-fusion (RRF) value, relative within this response only (not a confidence score or probability). similarity is the cosine score when the semantic signal matched.",
      }),
    );
  }

  function emptyModePanel(mode) {
    const steps = [];
    if (mode === "keyword") {
      steps.push(
        el("li", {}, "SQLite FTS5 requires every query word to appear (as prefix). Try fewer or more general keywords."),
        el("li", {}, "Check the Semantic or Hybrid columns to see if conceptual similarity matched related memories."),
        el("li", {}, "Verify in Memory Explorer that active memories exist for this user."),
        el("li", {}, "Check if a relevant memory was superseded into history using the check below."),
      );
    } else if (mode === "semantic") {
      steps.push(
        el("li", {}, "No memory passed the similarity threshold for this query vector."),
        el("li", {}, "Try phrasing the query as a complete descriptive phrase or sentence rather than short keywords."),
        el("li", {}, "Check the coverage line above; memories without vectors are excluded from semantic search."),
        el("li", {}, "Note: with the fake embedding provider, vectors are lexical word hashes and require shared words."),
      );
    } else {
      steps.push(
        el("li", {}, "Neither keyword matching nor semantic scoring found any eligible active memories."),
        el("li", {}, "Try one of the verified demo query chips above (e.g. \"editor theme preference\")."),
        el("li", {}, "Check whether relevant memories were superseded into history using the check below."),
        el("li", {}, "Open Memory Explorer to review active memories or create new ones."),
      );
    }

    return el(
      "div",
      { class: "state-panel" },
      el("h3", { text: `No ${modeLabel(mode)} results` }),
      el("p", {
        class: "small muted mb-05",
        text: "No active memories matched this retrieval mode. Useful next steps to try:",
      }),
      el("ul", { class: "list small mb-0" }, steps),
    );
  }

  /**
   * Classification is only meaningful against expectations the user supplied.
   * Without them, a result is simply "retrieved": calling it unexpected would
   * imply a judgment the API never made.
   */
  function classify(result) {
    const memory = result?.memory || {};
    const id = memory.id;
    if (memory.user_id && memory.user_id !== ctx.userId) return "foreign_user";
    if (state.forbidden.includes(id)) return "forbidden";
    if (state.expected.includes(id)) return "expected";
    if (state.expected.length || state.forbidden.length) return "unexpected";
    return "retrieved";
  }

  function comparisonSummary(results) {
    const retrievedIds = results.map((item) => item.memory?.id).filter(Boolean);
    const missing = state.expected.filter((id) => !retrievedIds.includes(id));
    const unexpected = retrievedIds.filter((id) => !state.expected.includes(id));
    const forbiddenHits = retrievedIds.filter((id) => state.forbidden.includes(id));
    const foreign = results.filter((item) => item.memory?.user_id && item.memory.user_id !== ctx.userId);
    const rows = [];

    if (state.expected.length) {
      rows.push(
        el(
          "p",
          { class: "small mb-0" },
          missing.length
            ? `Missing expected (${missing.length}): ${missing.join(", ")}`
            : `All ${state.expected.length} expected memory ID(s) were retrieved.`,
        ),
      );
      rows.push(
        el("p", {
          class: "small muted mb-0",
          text: unexpected.length
            ? `Unexpected (${unexpected.length}): ${unexpected.join(", ")}`
            : "No unexpected results.",
        }),
      );
    }
    if (state.forbidden.length) {
      rows.push(
        el("p", {
          class: forbiddenHits.length ? "small danger-text mb-0" : "small muted mb-0",
          text: forbiddenHits.length
            ? `Forbidden result(s) returned: ${forbiddenHits.join(", ")}`
            : "No forbidden results returned.",
        }),
      );
    }
    if (foreign.length) {
      rows.push(
        el("p", {
          class: "small danger-text mb-0",
          text: `Integrity alert: ${foreign.length} result(s) belong to another user (${foreign.map((f) => f.memory.id).join(", ")}). Diagnostic only: server isolation remains essential.`,
        }),
      );
    }
    return el("div", { class: "card" }, el("div", { class: "card-body" }, rows));
  }

  function resultCard(result, index) {
    const memory = result?.memory || {};
    const kind = classify(result);
    const isForeign = kind === "foreign_user";

    const chips = [];
    if (memory.status) chips.push(statusBadge(memory.status));
    if (memory.kind) chips.push(kindBadge(memory.kind));
    if (Array.isArray(result.matched_by) && result.matched_by.length > 0) {
      for (const source of result.matched_by) {
        chips.push(el("span", { class: "chip on", text: source }));
      }
    }

    const metaItems = [];
    if (isForeign) {
      metaItems.push(
        el("span", {
          class: "danger-text small",
          text: `Foreign user memory (${memory.user_id || "unknown"}). Not scored: server isolation required.`,
        }),
      );
    } else {
      if (typeof result.score === "number" && Number.isFinite(result.score)) {
        metaItems.push(el("span", { text: `ranking score ${formatMetric(result.score)}` }));
      }
      if (typeof result.similarity === "number" && Number.isFinite(result.similarity)) {
        metaItems.push(el("span", { text: `similarity ${formatMetric(result.similarity)}` }));
      }
    }

    const cardClasses = ["result-card", `classify-${kind}`];
    if (isForeign) cardClasses.push("classify-foreign");

    return el(
      "div",
      { class: cardClasses.join(" ") },
      el(
        "div",
        { class: "result-rank" },
        el("span", { text: `#${index + 1}` }),
        classificationBadge(kind),
      ),
      el("p", { class: "result-content", text: memory.content || "" }),
      chips.length ? el("div", { class: "chip-row" }, chips) : null,
      metaItems.length ? el("div", { class: "result-meta" }, metaItems) : null,
      el(
        "div",
        { class: "row-actions mt-1" },
        button("Explain", () => openExplainDialog(memory.id, ctx), {
          variant: "small",
          "aria-label": `Explain result ${truncate(memory.content || memory.id, 30)}`,
        }),
        button("Copy ID", () => copyText(memory.id, "Memory ID copied."), {
          variant: "link",
          "aria-label": `Copy memory ID ${memory.id}`,
        }),
      ),
    );
  }

  function missingCard(id, mode, limit) {
    return el(
      "div",
      { class: "result-card classify-missing" },
      el(
        "div",
        { class: "result-rank" },
        el("span", { class: "small muted", text: `Not in top ${limit}` }),
        classificationBadge("missing"),
      ),
      el("p", { class: "result-content text-code small mb-0", text: `Memory ID: ${id}` }),
      el("p", {
        class: "small muted mb-0 mt-05",
        text: `Expected memory was not retrieved by ${modeLabel(mode)}. It may lack matching terms, fall below semantic similarity, or be superseded into history.`,
      }),
      el(
        "div",
        { class: "row-actions mt-1" },
        button("Explain", () => openExplainDialog(id, ctx), { variant: "small" }),
        button("Copy ID", () => copyText(id, "Memory ID copied."), { variant: "link" }),
      ),
    );
  }

  function classificationBadge(kind) {
    if (kind === "expected") return badge("expected", "ok");
    if (kind === "forbidden") return badge("forbidden", "danger");
    if (kind === "foreign_user" || kind === "foreign") return badge("foreign user", "danger");
    if (kind === "unexpected") return badge("unexpected", "neutral");
    if (kind === "missing") return badge("missing", "warn");
    return badge("retrieved", "neutral");
  }

  function classificationLegend() {
    return el(
      "div",
      { class: "legend" },
      el("span", {}, badge("expected", "ok"), " matches an ID you listed"),
      el("span", {}, badge("unexpected", "neutral"), " returned but not in expected list"),
      el("span", {}, badge("forbidden", "danger"), " matches a must-not-include ID"),
      el("span", {}, badge("foreign user", "danger"), " owned by another user (browser-side check)"),
      el("span", {}, badge("missing", "warn"), " expected ID not retrieved in top K"),
    );
  }

  // A query passed from Overview runs immediately, which is what the chip
  // promises. A manual visit shows the explained empty state instead.
  if (prefill && !state.autoRan) {
    state.autoRan = true;
    await run();
  }
}
