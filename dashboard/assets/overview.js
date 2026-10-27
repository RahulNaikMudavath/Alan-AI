/* Overview: guided demo journey, live counts, and honest entry points. */

import { api } from "./api.js";
import {
  el,
  pageHead,
  stat,
  card,
  button,
  badge,
  loadingPanel,
  errorPanel,
  emptyPanel,
  clear,
  formatNumber,
  safeCodeBlock,
  stepItem,
  countBadge,
} from "./ui.js";

// Queries chosen to match the demo seed data. They are prefilled, never
// fabricated: every one of them is answered by the API at runtime.
export const DEMO_QUERIES = [
  { label: "editor theme preference", query: "editor theme preference" },
  { label: "oat milk flat white", query: "oat milk flat white" },
  { label: "Friday deploy", query: "Friday deploy" },
  { label: "greyhound Pixel", query: "greyhound Pixel" },
  { label: "payments backend", query: "payments backend" },
];

const DEMO_COMMANDS =
  '$env:RECALLKIT_DATABASE_PATH="$env:TEMP\\recallkit-demo.db"\n' +
  "python -m app.cli init\n" +
  "python -m app.cli seed-demo\n" +
  "python -m app.cli evaluate --dataset eval/datasets/retrieval-v1.json --output eval/reports/retrieval-v1-offline.json\n" +
  "python -m uvicorn app.main:app --reload";

export async function overviewView(root, ctx) {
  clear(root);
  root.append(
    pageHead(
      "Overview",
      `A local view of the memory layer for ${ctx.userId}.`,
      [
        button("Explore memories", () => ctx.navigate("explorer"), { variant: "primary" }),
        button("Debug retrieval", () => ctx.navigate("debugger")),
      ],
    ),
  );

  const host = el("div", { class: "stack" });
  root.append(host);
  host.append(loadingPanel("Loading counts and provider state…"));

  try {
    const [active, superseded, conflicts, reports] = await Promise.all([
      api.listMemories({ userId: ctx.userId, status: "active", limit: 1 }),
      api.listMemories({ userId: ctx.userId, status: "superseded", limit: 1 }),
      api.conflicts({ userId: ctx.userId, status: "open", limit: 1 }),
      api.reports(),
    ]);

    const items = Array.isArray(reports.items) ? reports.items : [];
    const reportCount = items.length;
    const fullRuns = items.filter(
      (item) =>
        item.readable !== false &&
        typeof item.evaluated_queries === "number" &&
        item.evaluated_queries === item.dataset_queries &&
        item.evaluated_queries > 1,
    ).length;
    const runtime = ctx.runtime;
    const hasMemories = active.total + superseded.total > 0;

    clear(host);
    host.append(
      el(
        "div",
        { class: "stats" },
        stat("Active memories", formatNumber(active.total), "searchable by default"),
        stat("Superseded", formatNumber(superseded.total), "history, excluded from search"),
        stat("Open conflicts", formatNumber(conflicts.total), "heuristic candidates"),
        stat("Evaluation reports", formatNumber(reportCount), `${fullRuns} full run(s)`),
      ),
    );

    host.append(aboutDemoCard());
    host.append(journeyCard({ active, superseded, conflicts, reportCount, fullRuns, runtime, ctx }));
    host.append(demoQueryCard(ctx, hasMemories));

    host.append(
      el(
        "div",
        { class: "grid-2" },
        card("Embedding provider", embeddingBody(runtime)),
        card("Runtime", runtimeBody(runtime)),
      ),
    );

    host.append(
      el(
        "div",
        { class: "grid-2" },
        card(
          "Memory state",
          el(
            "div",
            { class: "stack" },
            active.total
              ? el(
                  "p",
                  { class: "mb-0" },
                  `${active.total} active memories are current and returned by search. `,
                  button("Open Explorer", () => ctx.navigate("explorer"), { variant: "link" }),
                )
              : emptyPanel(
                  "No memories for this user yet",
                  "Create memories manually in the Explorer, or seed a temporary demo database with the CLI command below. Seeding is never automatic.",
                  button("Open Memory Explorer", () => ctx.navigate("explorer"), {
                    variant: "primary small",
                  }),
                ),
            superseded.total
              ? el("p", {
                  class: "small muted mb-0",
                  text: `${superseded.total} superseded ${superseded.total === 1 ? "memory is" : "memories are"} retained as history and never returned by search.`,
                })
              : null,
            conflicts.total
              ? el(
                  "p",
                  { class: "small muted mb-0" },
                  `${conflicts.total} open conflict ${conflicts.total === 1 ? "candidate needs" : "candidates need"} an explicit decision in the Explorer. `,
                  button("Resolve in Explorer", () => ctx.navigate("explorer"), { variant: "link" }),
                )
              : el("p", {
                  class: "small muted mb-0",
                  text: "No open conflict candidates. Conflict detection flags explicit negation with high word overlap as review candidates; none currently need resolution.",
                }),
          ),
        ),
        card(
          "Evaluation Lab",
          el(
            "div",
            { class: "stack" },
            reportCount
              ? el(
                  "div",
                  {},
                  el("p", {
                    class: "mb-0",
                    text: `${reportCount} report${reportCount === 1 ? "" : "s"} available (${fullRuns} full run${fullRuns === 1 ? "" : "s"} covering the complete dataset).`,
                  }),
                  el(
                    "div",
                    { class: "mt-1" },
                    button("Open Evaluation Lab", () => ctx.navigate("evaluation"), {
                      variant: "primary",
                    }),
                  ),
                )
              : el(
                  "div",
                  { class: "stack" },
                  emptyPanel(
                    "No evaluation reports yet",
                    "Generate a benchmark report with the offline evaluator, then refresh the lab to inspect measurements.",
                    button("Open Evaluation Lab", () => ctx.navigate("evaluation"), {
                      variant: "ghost small",
                    }),
                  ),
                  safeCodeBlock(
                    "python -m app.cli evaluate --dataset eval/datasets/retrieval-v1.json --output eval/reports/retrieval-v1-offline.json",
                  ),
                ),
          ),
        ),
      ),
    );

    host.append(
      card(
        "Clean demo database",
        el(
          "div",
          {},
          el("p", {
            class: "mb-0",
            text: "Run the demo against a temporary database. This command writes memories only when you run it, and never touches data/recallkit.db.",
          }),
          safeCodeBlock(DEMO_COMMANDS),
        ),
      ),
    );
  } catch (error) {
    clear(host);
    host.append(errorPanel(error, () => overviewView(root, ctx)));
  }
}

function aboutDemoCard() {
  return card(
    "What this demo demonstrates",
    el(
      "div",
      { class: "stack" },
      el("p", {
        class: "small mb-0",
        text: "RecallKit provides an explainable persistent memory layer for AI agents with complete data integrity and offline verification.",
      }),
      el(
        "ul",
        { class: "list small mb-0" },
        el(
          "li",
          {},
          el("strong", { text: "Memory lifecycle: " }),
          "Active memories are current and searchable. Superseding a memory explicitly retires older records to history without deleting them, keeping search clean while preserving audit history.",
        ),
        el(
          "li",
          {},
          el("strong", { text: "Provenance: " }),
          "Every extracted memory traces back to its source conversation and turn index. The explain view distinguishes observed extraction facts from heuristic conflict candidates.",
        ),
        el(
          "li",
          {},
          el("strong", { text: "Retrieval comparison: " }),
          "Run the exact same query through keyword (FTS5 BM25), semantic (cosine similarity), and hybrid (reciprocal rank fusion with recency bonus) side-by-side with match explanations.",
        ),
        el(
          "li",
          {},
          el("strong", { text: "Evaluation: " }),
          "Versioned ground-truth benchmarking measuring Precision@K, Recall@K, MRR, and nDCG with bit-for-bit deterministic fingerprints and compatibility-gated metric deltas.",
        ),
      ),
    ),
  );
}

function journeyCard({ active, superseded, conflicts, reportCount, fullRuns, runtime, ctx }) {
  const hasMemories = active.total + superseded.total > 0;
  const body = el(
    "ol",
    { class: "steps" },
    stepItem(
      1,
      "System Overview",
      "Inspect runtime health, active embedding & LLM providers, and system configuration.",
      runtime?.embedding?.provider
        ? countBadge(runtime.embedding.provider, "ok", "provider")
        : countBadge("online", "ok"),
      button("View Overview", () => ctx.navigate("overview"), { variant: "small" }),
    ),
    stepItem(
      2,
      "Memory Explorer",
      "Browse active vs superseded memories, inspect conversation provenance, and resolve conflicts.",
      hasMemories
        ? countBadge(`${active.total} active, ${superseded.total} history`, "ok")
        : countBadge("0 memories", "warn"),
      button("Open Explorer", () => ctx.navigate("explorer"), { variant: "small" }),
    ),
    stepItem(
      3,
      "Retrieval Debugger",
      "Compare keyword (FTS5), semantic (cosine), and hybrid (RRF) search side-by-side with match reasons.",
      hasMemories ? countBadge("ready to search", "info") : countBadge("needs memories", "warn"),
      button("Open Debugger", () => ctx.navigate("debugger"), { variant: "small" }),
    ),
    stepItem(
      4,
      "Evaluation Lab",
      "Load generated benchmark reports, compare full runs, and inspect compatibility-gated metric deltas.",
      reportCount
        ? countBadge(`${fullRuns} full / ${reportCount} total`, "info")
        : countBadge("no reports", "warn"),
      button("Open Evaluation Lab", () => ctx.navigate("evaluation"), { variant: "small" }),
    ),
  );
  return card(
    "Demo walkthrough",
    el(
      "div",
      {},
      el("p", {
        class: "muted small",
        text: "Four steps linking to each dashboard section, backed by live data from this server. Nothing on this page is precomputed.",
      }),
      body,
    ),
  );
}

function demoQueryCard(ctx, hasMemories) {
  return card(
    "Try a query",
    el(
      "div",
      {},
      el("p", {
        class: "muted small",
        text: hasMemories
          ? "Each button opens the Debugger with the query prefilled and runs all three modes."
          : "These queries are prefilled for the demo dataset. Seed a temporary database first, or type your own query in the Debugger.",
      }),
      el(
        "div",
        { class: "chip-row" },
        DEMO_QUERIES.map((item) =>
          button(item.label, () => ctx.navigate("debugger", { q: item.query }), {
            variant: "ghost small",
          }),
        ),
      ),
      el(
        "p",
        { class: "small muted mt-1 mb-0" },
        "The Debugger shows only metadata the API returns. It never estimates relevance.",
      ),
    ),
  );
}

function embeddingBody(runtime) {
  if (!runtime) {
    return el("p", { class: "muted mb-0", text: "Runtime information is unavailable." });
  }
  const embedding = runtime.embedding || {};
  const fake = embedding.provider === "fake";
  return el(
    "div",
    { class: "stack" },
    el("p", { class: "mb-0" }, badge(embedding.provider || "unknown", fake ? "warn" : "info")),
    el("p", { class: "small muted", text: `Model: ${embedding.model || "—"}` }),
    el("p", { class: "small muted", text: `Dimensions: ${embedding.dimensions ?? "—"}` }),
    el("p", {
      class: "small muted mb-0",
      text: embedding.external
        ? "External embeddings are configured. Runs may differ across model versions."
        : "Offline lexical hash embeddings. These measure word overlap, not meaning.",
    }),
  );
}

function runtimeBody(runtime) {
  if (!runtime) {
    return el("p", { class: "muted mb-0", text: "Runtime information is unavailable." });
  }
  const llm = runtime.llm || {};
  const features = runtime.features || {};
  return el(
    "div",
    { class: "stack" },
    el("p", { class: "mb-0" }, badge(runtime.app?.environment || "unknown", "neutral")),
    el("p", { class: "small muted mb-0", text: `Version: ${runtime.app?.version || "—"}` }),
    el("p", {
      class: "small muted mb-0",
      text: `LLM provider: ${llm.provider || "—"} (${llm.model || "—"})`,
    }),
    el("p", {
      class: "small muted mb-0",
      text: `Raw message storage: ${features.store_messages ? "enabled" : "disabled"}`,
    }),
    el("p", {
      class: "small muted mb-0",
      text: `Conflict detection: ${features.conflict_detection ? "enabled" : "disabled"}`,
    }),
  );
}