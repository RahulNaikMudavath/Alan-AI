/* Shared explanation rendering. Observed and inferred data stay separated. */

import { api } from "./api.js";
import {
  el,
  section,
  kvList,
  openDialog,
  loadingPanel,
  errorPanel,
  button,
  statusBadge,
  formatDate,
  formatMetric,
  clear,
} from "./ui.js";

export function renderExplain(explain) {
  const observed = explain.observed || {};
  const inferred = explain.inferred || {};
  const sources = Array.isArray(observed.sources) ? observed.sources : [];
  const candidates = Array.isArray(inferred.conflict_candidates)
    ? inferred.conflict_candidates
    : [];

  const sourceList = sources.length
    ? el(
        "ul",
        { class: "list" },
        sources.map((source) =>
          el(
            "li",
            {},
            el("span", { class: "mono", text: source.conversation_id }),
            el("span", {
              class: "muted",
              text: ` · messages [${(source.message_indexes || []).join(", ")}]`,
            }),
            source.extractor
              ? el("span", {
                  class: "muted",
                  text: ` · ${source.extractor.provider}/${source.extractor.model}`,
                })
              : null,
          ),
        ),
      )
    : el("p", { class: "muted", text: "No conversation source. This memory was created directly." });

  const lifecycle = observed.lifecycle || {};
  const lifecycleItems = [
    { label: "Status", value: statusBadge(lifecycle.status) },
    { label: "Superseded at", value: formatDate(lifecycle.superseded_at) },
    { label: "Replaced by", value: lifecycle.superseded_by || "—" },
    { label: "Reason", value: lifecycle.reason || "—" },
    { label: "Note", value: lifecycle.note || "—" },
  ];

  const conflictNodes = candidates.length
    ? el(
        "div",
        { class: "stack" },
        candidates.map((candidate) =>
          el(
            "div",
            { class: "card" },
            el(
              "div",
              { class: "card-body" },
              el(
                "div",
                { class: "chip-row" },
                el("span", { class: "badge info", text: candidate.rule }),
                el("span", { class: "badge neutral", text: candidate.status }),
                el("span", { class: "badge warn", text: "heuristic" }),
              ),
              el("p", {
                class: "small mt-1",
                text: `Other memory: ${candidate.other_memory_id}`,
              }),
              candidate.evidence && Array.isArray(candidate.evidence.shared_terms)
                ? el("p", {
                    class: "small muted",
                    text: `Shared terms: ${candidate.evidence.shared_terms.join(", ")} · token containment ${formatMetric(candidate.evidence.token_containment)}`,
                  })
                : null,
              el("p", {
                class: "small muted mb-0",
                text: "This is a lexical candidate, not a verdict or a confidence score.",
              }),
            ),
          ),
        ),
      )
    : el("p", { class: "muted", text: "No conflict candidates recorded." });

  return el(
    "div",
    {},
    section(
      "Observed",
      kvList([
        { label: "Origin", value: observed.origin || "—" },
        {
          label: "Keyword index",
          value: observed.indexes && observed.indexes.keyword ? "present" : "absent",
        },
        {
          label: "Embedding models",
          value:
            observed.indexes && observed.indexes.embedding_models
              ? observed.indexes.embedding_models.join(", ") || "none"
              : "none",
        },
      ]),
      el("h4", { class: "section-title mt-2", text: "Sources" }),
      sourceList,
      el("h4", { class: "section-title mt-2", text: "Lifecycle" }),
      kvList(lifecycleItems),
    ),
    section("Inferred", conflictNodes),
  );
}

export function openExplainDialog(memoryId, ctx) {
  const body = loadingPanel("Loading explanation…");
  const dialog = openDialog({
    title: "Memory explanation",
    body,
    wide: true,
    footer: button("Close", () => dialog.close(), { variant: "ghost" }),
  });

  api
    .explain(memoryId, ctx.userId)
    .then((explain) => {
      const memory = explain.memory || {};
      clear(body);
      body.append(
        el(
          "div",
          { class: "stack" },
          el(
            "div",
            {},
            el("div", { class: "chip-row" }, statusBadge(memory.status), memory.kind
              ? el("span", { class: "badge neutral", text: memory.kind })
              : null),
            el("p", { class: "memory-content mt-1", text: memory.content || "" }),
            el("p", { class: "small muted mb-0", text: memory.id || memoryId }),
          ),
          renderExplain(explain),
        ),
      );
    })
    .catch((error) => {
      clear(body);
      body.append(errorPanel(error, () => openExplainDialog(memoryId, ctx)));
    });

  return dialog;
}