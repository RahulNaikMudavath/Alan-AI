/* Memory Explorer: browse, search, inspect, and explicitly mutate memories. */

import { api } from "./api.js";
import {
  el,
  pageHead,
  card,
  field,
  button,
  loadingPanel,
  errorPanel,
  emptyPanel,
  clear,
  statusBadge,
  kindBadge,
  formatDate,
  formatNumber,
  truncate,
  kvList,
  section,
  openDialog,
  confirmAction,
  toast,
  copyText,
} from "./ui.js";
import { renderExplain } from "./explain.js";

const KINDS = ["fact", "preference", "event", "task"];
const REASONS = ["user_correction", "outdated", "contradiction", "duplicate", "other"];

export async function explorerView(root, ctx) {
  const state = { q: "", kind: "", status: "active", offset: 0, limit: 20 };

  clear(root);
  root.append(
    pageHead(
      "Memory Explorer",
      `Browse memories for ${ctx.userId}. Active memories are current; superseded memories are history. (Caller-asserted user_id; local developer tool).`,
      [button("New memory", () => openCreate(), { variant: "primary" })],
    ),
  );

  const toolbar = el("form", { class: "toolbar" });
  const search = el("input", {
    class: "input",
    type: "search",
    placeholder: "Search content (keyword)",
    value: state.q,
  });
  const kind = el(
    "select",
    { class: "select" },
    el("option", { value: "", text: "All kinds" }),
    KINDS.map((item) => el("option", { value: item, text: item })),
  );
  const status = el(
    "select",
    { class: "select" },
    el("option", { value: "active", text: "Active" }),
    el("option", { value: "superseded", text: "Superseded" }),
    el("option", { value: "all", text: "All history" }),
  );
  toolbar.append(
    el("div", { class: "field grow" }, el("label", { for: "explorer-q", text: "Search" }), search),
    el("div", { class: "field" }, el("label", { for: "explorer-kind", text: "Kind" }), kind),
    el("div", { class: "field" }, el("label", { for: "explorer-status", text: "Lifecycle" }), status),
    button("Search", () => runSearch(), { variant: "primary" }),
    button("Reset", () => resetFilters(), { variant: "ghost" }),
  );
  search.id = "explorer-q";
  kind.id = "explorer-kind";
  status.id = "explorer-status";

  const legend = el(
    "div",
    { class: "legend mb-1" },
    el("span", {}, statusBadge("active"), " Current truth: returned in search and prompt context by default"),
    el("span", {}, statusBadge("superseded"), " Historical audit record: immutable, excluded from normal search"),
    el("span", { class: "small muted" }, "Caller-supplied user_id scopes memory storage; it is not an authentication claim."),
  );

  root.append(toolbar, legend);

  const conflictsHost = el("div", {});
  root.append(conflictsHost);

  const listHost = el("div", {});
  root.append(listHost);

  toolbar.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });

  function runSearch() {
    state.q = search.value.trim();
    state.kind = kind.value;
    state.status = status.value;
    state.offset = 0;
    load();
  }

  function resetFilters() {
    search.value = "";
    kind.value = "";
    status.value = "active";
    state.q = "";
    state.kind = "";
    state.status = "active";
    state.offset = 0;
    load();
  }

  async function load() {
    clear(listHost);
    listHost.setAttribute("aria-busy", "true");
    listHost.append(loadingPanel("Loading memories…"));
    loadConflicts();
    try {
      const body = await api.listMemories({
        userId: ctx.userId,
        q: state.q,
        kind: state.kind,
        status: state.status,
        limit: state.limit,
        offset: state.offset,
      });
      renderList(body);
    } catch (error) {
      clear(listHost);
      listHost.append(errorPanel(error, load));
    } finally {
      listHost.removeAttribute("aria-busy");
    }
  }

  async function loadConflicts() {
    clear(conflictsHost);
    conflictsHost.setAttribute("aria-busy", "true");
    try {
      const body = await api.conflicts({ userId: ctx.userId, status: "open", limit: 20 });
      const items = body.items || [];
      if (!items.length) {
        conflictsHost.append(
          card(
            "Open conflict candidates (0)",
            el("p", {
              class: "small muted mb-0",
              text: "No open conflict candidates for this user. Conflict detection flags explicit negation with high lexical overlap as review candidates; none currently need resolution.",
            }),
          ),
        );
        return;
      }
      conflictsHost.append(
        card(
          `Open conflict candidates (${body.total})`,
          el(
            "div",
            { class: "stack" },
            el("p", {
              class: "small muted",
              text: "Detected from overlapping statements with an explicit negation. Each decision is explicit and reversible only by creating a new memory.",
            }),
            items.map((conflict) => conflictPanelCard(conflict)),
          ),
        ),
      );
    } catch {
      /* Conflicts are supplementary; a failure here must not hide the list. */
    } finally {
      conflictsHost.removeAttribute("aria-busy");
    }
  }

  function conflictPanelCard(conflict) {
    const shared = Array.isArray(conflict.evidence?.shared_terms)
      ? conflict.evidence.shared_terms.join(", ")
      : "—";

    return el(
      "div",
      { class: "card conflict-card" },
      el(
        "div",
        { class: "card-body" },
        el(
          "div",
          { class: "chip-row" },
          el("span", { class: "badge info", text: conflict.rule }),
          el("span", { class: "badge warn", text: "heuristic" }),
          el("span", { class: "badge neutral", text: `ID: ${conflict.id}` }),
        ),
        el("p", { class: "small muted mt-05 mb-05", text: `Shared terms: ${shared}` }),
        el(
          "div",
          { class: "grid-2 mt-1" },
          el(
            "div",
            { class: "card" },
            el(
              "div",
              { class: "card-body" },
              el(
                "div",
                { class: "row space-between" },
                el("strong", { class: "small", text: "Candidate A" }),
                conflict.memory_a?.kind ? kindBadge(conflict.memory_a.kind) : null,
              ),
              el("p", { class: "memory-content small mt-05", text: truncate(conflict.memory_a?.content || "", 200) }),
              el("span", { class: "chip mono small", text: conflict.memory_a?.id || "—" }),
            ),
          ),
          el(
            "div",
            { class: "card" },
            el(
              "div",
              { class: "card-body" },
              el(
                "div",
                { class: "row space-between" },
                el("strong", { class: "small", text: "Candidate B" }),
                conflict.memory_b?.kind ? kindBadge(conflict.memory_b.kind) : null,
              ),
              el("p", { class: "memory-content small mt-05", text: truncate(conflict.memory_b?.content || "", 200) }),
              el("span", { class: "chip mono small", text: conflict.memory_b?.id || "—" }),
            ),
          ),
        ),
        el(
          "div",
          { class: "row-actions mt-1" },
          button("Keep A", () => resolveConflict(conflict, "keep_a", "Keep A", null), {
            variant: "small",
            "aria-label": `Keep candidate A for conflict ${conflict.id}`,
          }),
          button("Keep B", () => resolveConflict(conflict, "keep_b", "Keep B", null), {
            variant: "small",
            "aria-label": `Keep candidate B for conflict ${conflict.id}`,
          }),
          button("Keep both", () => resolveConflict(conflict, "keep_both", "Keep both", null), {
            variant: "ghost small",
            "aria-label": `Keep both candidates for conflict ${conflict.id}`,
          }),
        ),
      ),
    );
  }

  function resolveConflict(conflict, resolution, label, parentDialog) {
    let explanation = "";
    if (resolution === "keep_both") {
      explanation = "This closes the candidate and leaves both memories active.";
    } else if (resolution === "keep_a") {
      explanation = `This supersedes Memory B with reason "contradiction". Memory A remains active, while Memory B is retained as history.`;
    } else {
      explanation = `This supersedes Memory A with reason "contradiction". Memory B remains active, while Memory A is retained as history.`;
    }

    confirmAction({
      title: "Resolve Conflict Candidate",
      message: explanation,
      details: el(
        "div",
        { class: "stack" },
        el("p", { class: "small", text: `Resolution: ${label}` }),
        el("p", { class: "small", text: `Memory A: ${truncate(conflict.memory_a?.content || "", 120)}` }),
        el("p", { class: "small", text: `Memory B: ${truncate(conflict.memory_b?.content || "", 120)}` }),
        el("span", { class: "chip mono small", text: `Conflict ID: ${conflict.id}` }),
      ),
      confirmLabel: label,
      danger: resolution !== "keep_both",
      onConfirm: async () => {
        try {
          await api.resolveConflict(conflict.id, {
            user_id: ctx.userId,
            resolution,
          });
          toast(`Conflict resolved: ${label}.`, "success");
          if (parentDialog) parentDialog.close();
          load();
        } catch (err) {
          toast(err.message || "Failed to resolve conflict.", "error");
        }
      },
    });
  }

  function renderList(body) {
    const items = Array.isArray(body.items) ? body.items : [];
    const total = body.total || 0;
    clear(listHost);

    const first = total === 0 ? 0 : state.offset + 1;
    const last = Math.min(state.offset + items.length, total);
    listHost.append(
      el("p", {
        class: "muted small",
        text: `${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(total)} memories`,
      }),
    );

    if (!items.length) {
      listHost.append(
        emptyPanel(
          "No memories match",
          state.q
            ? "Try a broader search or a different lifecycle filter."
            : "No memories for this user in this view.",
          button("Create a memory", () => openCreate(), { variant: "primary" }),
        ),
      );
      return;
    }

    const tbody = el("tbody");
    for (const memory of items) {
      tbody.append(
        el(
          "tr",
          { class: memory.status === "superseded" ? "is-superseded" : "" },
          el(
            "td",
            { class: "content-cell", "data-label": "Memory" },
            el("p", { class: "memory-content truncate", text: memory.content }),
          ),
          el("td", { "data-label": "Kind" }, kindBadge(memory.kind)),
          el("td", { "data-label": "Agent", text: memory.agent_id || "—" }),
          el("td", { "data-label": "Status" }, statusBadge(memory.status)),
          el("td", { "data-label": "Updated", text: formatDate(memory.updated_at) }),
          el(
            "td",
            { "data-label": "Actions" },
            el(
              "div",
              { class: "row-actions" },
              button("Inspect", () => openDetail(memory.id), {
                variant: "small",
                "aria-label": `Inspect memory ${truncate(memory.content, 30)}`,
              }),
              memory.status === "active"
                ? button("Supersede", () => openSupersede(memory), {
                    variant: "small",
                    "aria-label": `Supersede memory ${truncate(memory.content, 30)}`,
                  })
                : null,
              button("Delete", () => openDelete(memory), {
                variant: "danger small",
                "aria-label": `Delete memory ${truncate(memory.content, 30)}`,
              }),
            ),
          ),
        ),
      );
    }

    listHost.append(
      el(
        "div",
        { class: "card" },
        el(
          "div",
          { class: "card-body" },
          el(
            "div",
            { class: "table-wrap" },
            el(
              "table",
              { class: "data-table" },
              el(
                "thead",
                {},
                el(
                  "tr",
                  {},
                  el("th", { text: "Memory" }),
                  el("th", { text: "Kind" }),
                  el("th", { text: "Agent" }),
                  el("th", { text: "Status" }),
                  el("th", { text: "Updated" }),
                  el("th", { text: "Actions" }),
                ),
              ),
              tbody,
            ),
          ),
          el(
            "div",
            { class: "pagination" },
            el("span", { class: "pagination-info", text: `Page offset ${state.offset}` }),
            el(
              "div",
              { class: "row-actions" },
              button(
                "Previous",
                () => {
                  state.offset = Math.max(0, state.offset - state.limit);
                  load();
                },
                { variant: "ghost", disabled: state.offset === 0 },
              ),
              button(
                "Next",
                () => {
                  state.offset += state.limit;
                  load();
                },
                { disabled: state.offset + state.limit >= total },
              ),
            ),
          ),
        ),
      ),
    );
  }

  function openCreate() {
    const content = el("textarea", { class: "textarea", rows: 5 });
    const kindSelect = el(
      "select",
      { class: "select" },
      KINDS.map((item) => el("option", { value: item, text: item })),
    );
    const agent = el("input", { class: "input", placeholder: "optional" });
    const errorBox = el("p", { class: "form-error", role: "alert" });
    const submit = button("Create memory", () => submitCreate(), { variant: "primary" });
    const dialog = openDialog({
      title: "New memory",
      body: el(
        "div",
        { class: "stack" },
        field("Content", content, "Stored exactly as text; rendered as text everywhere."),
        field("Kind", kindSelect),
        field("Agent", agent),
        errorBox,
      ),
      footer: el("div", { class: "dialog-actions" }, button("Cancel", () => dialog.close(), { variant: "ghost" }), submit),
    });

    async function submitCreate() {
      errorBox.textContent = "";
      const text = content.value.trim();
      if (!text) {
        errorBox.textContent = "Content is required.";
        content.focus();
        return;
      }
      submit.disabled = true;
      try {
        await api.createMemory({
          user_id: ctx.userId,
          content: text,
          kind: kindSelect.value,
          agent_id: agent.value.trim() || null,
        });
        toast("Memory created.", "success");
        dialog.close();
        load();
      } catch (error) {
        errorBox.textContent = error.message || "Could not create the memory.";
      } finally {
        submit.disabled = false;
      }
    }
  }

  async function openDetail(memoryId) {
    const dialog = openDialog({
      title: "Memory detail",
      wide: true,
      body: loadingPanel("Loading memory, provenance, and conflicts…"),
    });
    const body = dialog.querySelector(".dialog-body");
    try {
      const [memory, explain, conflicts] = await Promise.all([
        api.getMemory(memoryId, ctx.userId),
        api.explain(memoryId, ctx.userId),
        api.conflicts({ userId: ctx.userId, status: "open", limit: 50 }),
      ]);
      let replacementMemory = null;
      if (memory.status === "superseded" && memory.superseded_by) {
        try {
          replacementMemory = await api.getMemory(memory.superseded_by, ctx.userId);
        } catch {
          replacementMemory = null;
        }
      }
      renderDetail(dialog, body, memory, explain, conflicts.items || [], replacementMemory);
    } catch (error) {
      clear(body);
      body.append(errorPanel(error, () => openDetail(memoryId)));
    }
  }

  function renderDetail(dialog, body, memory, explain, conflicts, replacementMemory) {
    clear(body);
    const relevantConflicts = conflicts.filter(
      (item) => item.status === "open" && (item.memory_a?.id === memory.id || item.memory_b?.id === memory.id),
    );

    const isSuperseded = memory.status === "superseded";

    const memoryOverview = el(
      "div",
      { class: "stack" },
      el(
        "div",
        {},
        el("div", { class: "chip-row" }, statusBadge(memory.status), kindBadge(memory.kind)),
        el("p", { class: "memory-content mt-1", text: memory.content }),
        el(
          "div",
          { class: "chip-row mt-1" },
          el("span", { class: "chip mono", text: memory.id }),
          button("Copy ID", () => copyText(memory.id, "Memory ID copied."), { variant: "link" }),
        ),
      ),
      kvList([
        { label: "User scope", value: `${memory.user_id} (caller-asserted; not auth)` },
        { label: "Agent", value: memory.agent_id || "—" },
        { label: "Created", value: formatDate(memory.created_at) },
        { label: "Updated", value: formatDate(memory.updated_at) },
      ]),
    );

    const supersededCard = isSuperseded
      ? el(
          "div",
          { class: "card warning mt-1" },
          el(
            "div",
            { class: "card-body stack" },
            el(
              "div",
              { class: "row space-between" },
              el("strong", { text: "Historical memory (superseded)" }),
              statusBadge("superseded"),
            ),
            el("p", {
              class: "small mb-0",
              text: "This memory has been superseded and is excluded from normal retrieval. Audit provenance and historical receipts are preserved.",
            }),
            memory.superseded_by
              ? el(
                  "div",
                  { class: "card mt-05" },
                  el(
                    "div",
                    { class: "card-body" },
                    el(
                      "div",
                      { class: "row space-between" },
                      el("strong", { class: "small", text: "Replacement memory (current truth)" }),
                      replacementMemory?.status ? statusBadge(replacementMemory.status) : null,
                    ),
                    replacementMemory
                      ? el("p", { class: "memory-content small mt-05 mb-05", text: replacementMemory.content })
                      : el("p", { class: "small muted mb-05", text: "Replacement memory is unavailable or was deleted." }),
                    el(
                      "div",
                      { class: "row-actions mt-1" },
                      button(
                        "Inspect replacement memory",
                        () => {
                          dialog.close();
                          openDetail(memory.superseded_by);
                        },
                        { variant: "primary small" },
                      ),
                      button(
                        truncate(String(memory.superseded_by), 20),
                        () => copyText(memory.superseded_by, "Replacement ID copied."),
                        { variant: "link" },
                      ),
                    ),
                  ),
                )
              : el("p", {
                  class: "small muted mb-0",
                  text: "The replacement was deleted, so no pointer remains. The timestamp and reason are kept.",
                }),
            kvList([
              { label: "Superseded at", value: formatDate(memory.superseded_at) },
              { label: "Reason", value: memory.superseded_reason || "—" },
              { label: "Note", value: memory.superseded_note || "—" },
            ]),
          ),
        )
      : null;

    body.append(
      el(
        "div",
        { class: "stack" },
        memoryOverview,
        supersededCard,
        renderExplain(explain),
        section(
          "Open conflict candidates",
          relevantConflicts.length
            ? el(
                "div",
                { class: "stack" },
                relevantConflicts.map((conflict) => conflictCard(conflict, memory, dialog)),
              )
            : el("p", { class: "muted mb-0", text: "No open conflict candidates involve this memory." }),
        ),
        memory.status === "active"
          ? supersedeSection(memory, () => {
              dialog.close();
              load();
            })
          : null,
      ),
    );

    const footer = el("div", { class: "dialog-actions" });
    footer.append(
      button("Close", () => dialog.close(), { variant: "ghost" }),
      button("Delete", () => openDelete(memory, dialog), { variant: "danger" }),
    );
    const foot = dialog.querySelector(".dialog-foot");
    if (foot) {
      clear(foot);
      foot.append(footer);
    } else {
      dialog.append(el("div", { class: "dialog-foot" }, footer));
    }
  }

  function conflictCard(conflict, memory, parentDialog) {
    const other = conflict.memory_a?.id === memory.id ? conflict.memory_b : conflict.memory_a;
    const shared = Array.isArray(conflict.evidence?.shared_terms)
      ? conflict.evidence.shared_terms.join(", ")
      : "—";
    return el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "card-body" },
        el(
          "div",
          { class: "chip-row" },
          el("span", { class: "badge info", text: conflict.rule }),
          el("span", { class: "badge warn", text: "heuristic" }),
        ),
        el("p", { class: "small muted", text: `Shared terms: ${shared}` }),
        el("p", { class: "small", text: "Keep A" }),
        el("p", { class: "small muted", text: truncate(conflict.memory_a?.content || "", 200) }),
        el("p", { class: "small", text: "Keep B" }),
        el("p", { class: "small muted", text: truncate(conflict.memory_b?.content || "", 200) }),
        el("p", { class: "small muted", text: `Other memory: ${other?.id || "—"}` }),
        el(
          "div",
          { class: "row-actions mt-1" },
          button("Keep A", () => resolveConflict(conflict, "keep_a", "Keep A", parentDialog), {
            variant: "small",
          }),
          button("Keep B", () => resolveConflict(conflict, "keep_b", "Keep B", parentDialog), {
            variant: "small",
          }),
          button("Keep both", () => resolveConflict(conflict, "keep_both", "Keep both", parentDialog), {
            variant: "ghost small",
          }),
        ),
      ),
    );
  }

  /**
   * Searchable supersede form. Replacement is picked from the user's active memories
   * with live search filtering, while retaining manual ID entry as fallback.
   */
  function supersedeSection(memory, onDone) {
    let allOptions = [];
    const searchFilter = el("input", {
      class: "input",
      placeholder: "Filter active candidate memories by keyword…",
    });
    const picker = el("select", { class: "select" });
    picker.append(el("option", { value: "", text: "Choose an active memory…" }));

    const manual = el("input", { class: "input", placeholder: "or manually paste a memory ID" });
    const reason = el(
      "select",
      { class: "select" },
      REASONS.map((item) => el("option", { value: item, text: item })),
    );
    const note = el("textarea", { class: "textarea", rows: 2, placeholder: "Optional note" });
    const errorBox = el("p", { class: "form-error", role: "alert" });
    const hint = el("p", { class: "small muted mb-0", text: "Loading candidate memories…" });

    const previewBox = el(
      "div",
      { class: "card mt-05" },
      el("div", { class: "card-body small muted", text: "No replacement selected yet." }),
    );

    function updateOptions(filterQuery = "") {
      const q = filterQuery.toLowerCase().trim();
      const filtered = q
        ? allOptions.filter(
            (item) => item.content.toLowerCase().includes(q) || item.id.toLowerCase().includes(q),
          )
        : allOptions;

      clear(picker);
      picker.append(el("option", { value: "", text: "Choose an active memory…" }));
      for (const item of filtered) {
        picker.append(
          el("option", {
            value: item.id,
            text: `${truncate(item.content, 80)} · ${item.id.slice(0, 8)}`,
          }),
        );
      }
      hint.textContent = filtered.length
        ? `${filtered.length} active candidate(s) match.${q ? ` (filtered from ${allOptions.length})` : ""}`
        : "No active candidate memories match your filter.";
    }

    function updatePreview() {
      const id = (picker.value || manual.value).trim();
      clear(previewBox);
      if (!id) {
        previewBox.append(
          el("div", { class: "card-body small muted", text: "No replacement selected yet." }),
        );
        return;
      }
      const found = allOptions.find((item) => item.id === id);
      if (found) {
        previewBox.append(
          el(
            "div",
            { class: "card-body" },
            el(
              "div",
              { class: "row space-between" },
              el("strong", { class: "small", text: "Selected replacement:" }),
              kindBadge(found.kind),
            ),
            el("p", { class: "memory-content small mt-05 mb-05", text: found.content }),
            el("span", { class: "chip mono small", text: found.id }),
          ),
        );
      } else {
        previewBox.append(
          el(
            "div",
            { class: "card-body" },
            el("p", { class: "small muted mb-0", text: `Manual replacement ID: ${id}` }),
          ),
        );
      }
    }

    searchFilter.addEventListener("input", () => {
      updateOptions(searchFilter.value);
      updatePreview();
    });

    picker.addEventListener("change", () => {
      if (picker.value) manual.value = "";
      updatePreview();
    });

    manual.addEventListener("input", () => {
      if (manual.value.trim()) picker.value = "";
      updatePreview();
    });

    api
      .listMemories({ userId: ctx.userId, status: "active", limit: 100 })
      .then((body) => {
        allOptions = (body.items || []).filter((item) => item.id !== memory.id);
        updateOptions();
      })
      .catch(() => {
        hint.textContent = "Could not load candidates. Paste a memory ID instead.";
      });

    const submit = button(
      "Supersede memory",
      async () => {
        errorBox.textContent = "";
        const replacementId = (picker.value || manual.value).trim();
        if (!replacementId) {
          errorBox.textContent = "Choose or paste a replacement memory ID.";
          picker.focus();
          return;
        }
        if (replacementId === memory.id) {
          errorBox.textContent = "A memory cannot supersede itself.";
          return;
        }

        submit.disabled = true;
        let replacementObj = allOptions.find((item) => item.id === replacementId);
        if (!replacementObj) {
          try {
            replacementObj = await api.getMemory(replacementId, ctx.userId);
          } catch {
            replacementObj = null;
          }
        }

        confirmAction({
          title: "Confirm Memory Supersession",
          message:
            "The current memory becomes historical. It is retained, excluded from normal retrieval, and can still be inspected or deleted.",
          details: el(
            "div",
            { class: "stack" },
            el(
              "div",
              { class: "card warning" },
              el(
                "div",
                { class: "card-body" },
                el("strong", { class: "small", text: "Becomes historical (superseded):" }),
                el("p", { class: "memory-content small mt-05 mb-05", text: truncate(memory.content, 220) }),
                el("span", { class: "chip mono small", text: memory.id }),
              ),
            ),
            el(
              "div",
              { class: "card" },
              el(
                "div",
                { class: "card-body" },
                el("strong", { class: "small", text: "Replacement memory (active truth):" }),
                el("p", {
                  class: "memory-content small mt-05 mb-05",
                  text: replacementObj ? truncate(replacementObj.content, 220) : `[ID: ${replacementId}] (details unavailable)`,
                }),
                el("span", { class: "chip mono small", text: replacementId }),
              ),
            ),
            el("p", {
              class: "small muted mb-0",
              text: `Reason: ${reason.value}${note.value ? ` · Note: ${note.value.trim()}` : ""}`,
            }),
          ),
          confirmLabel: "Supersede",
          danger: true,
          onConfirm: async () => {
            try {
              await api.supersede(memory.id, ctx.userId, {
                superseded_by: replacementId,
                reason: reason.value,
                note: note.value.trim() || null,
              });
              toast("Memory superseded.", "success");
              onDone();
            } catch (err) {
              errorBox.textContent = err.message || "Could not supersede the memory.";
              toast(err.message || "Supersede failed.", "error");
            } finally {
              submit.disabled = false;
            }
          },
        });
        submit.disabled = false;
      },
      { variant: "primary" },
    );

    return section(
      "Supersede this memory",
      el(
        "div",
        { class: "stack" },
        field("Search active memories", searchFilter, "Filter candidates by keyword"),
        field("Replacement memory", picker, "Choose an active memory to replace this one"),
        field("Or paste an ID", manual, "Fallback: paste a memory ID directly"),
        previewBox,
        hint,
        field("Reason", reason),
        field("Note", note),
        errorBox,
        el("div", {}, submit),
      ),
    );
  }

  function openSupersede(memory) {
    const dialog = openDialog({
      title: "Supersede memory",
      body: el(
        "div",
        { class: "stack" },
        supersedeSection(memory, () => {
          dialog.close();
          load();
        }),
      ),
      footer: el("div", { class: "dialog-actions" }, button("Cancel", () => dialog.close(), { variant: "ghost" })),
    });
  }

  function openDelete(memory, parentDialog = null) {
    confirmAction({
      title: "Delete memory",
      message:
        "This permanently deletes the memory, its embeddings, provenance rows, and conflict candidates.",
      details: el(
        "div",
        { class: "stack" },
        el("p", { class: "small", text: truncate(memory.content, 260) }),
        el("p", { class: "small mono muted", text: memory.id }),
        el(
          "p",
          { class: "small muted mb-0" },
          "Conversation receipts (conversations.result_json) and optional stored raw messages are not erased. Complete data erasure is not provided.",
        ),
      ),
      confirmLabel: "Delete permanently",
      danger: true,
      onConfirm: async () => {
        try {
          await api.deleteMemory(memory.id, ctx.userId);
          toast("Memory deleted.", "success");
          if (parentDialog) parentDialog.close();
          load();
        } catch (err) {
          toast(err.message || "Could not delete memory.", "error");
        }
      },
    });
  }

  await load();
}