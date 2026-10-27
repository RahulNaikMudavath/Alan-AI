/* Shared DOM and UI helpers. All text is rendered as text nodes, never as markup. */

let dialogCounter = 0;

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "value") node.value = value;
    else if (["checked", "disabled", "selected", "required"].includes(key)) {
      node[key] = Boolean(value);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  node.replaceChildren();
}

export function pageHead(title, subtitle, actions = []) {
  return el(
    "div",
    { class: "page-head" },
    el(
      "div",
      {},
      el("h1", { class: "page-title", tabindex: "-1", text: title }),
      subtitle ? el("p", { class: "page-subtitle", text: subtitle }) : null,
    ),
    actions.length ? el("div", { class: "page-actions" }, actions) : null,
  );
}

export function stat(label, value, hint) {
  return el(
    "div",
    { class: "stat" },
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value", text: String(value) }),
    hint ? el("div", { class: "stat-hint", text: hint }) : null,
  );
}

export function card(title, children, options = {}) {
  const section = el("section", { class: `card ${options.className || ""}`.trim() });
  if (title || options.actions) {
    section.append(
      el(
        "div",
        { class: "card-head" },
        el("h2", { class: "card-title", text: title || "" }),
        options.actions || null,
      ),
    );
  }
  section.append(el("div", { class: "card-body" }, children));
  return section;
}

export function badge(label, tone = "neutral") {
  return el("span", { class: `badge ${tone}`, text: label });
}

export function statusBadge(status) {
  if (status === "active") return badge("Active", "ok");
  if (status === "superseded") return badge("Superseded", "warn");
  return badge(status || "unknown", "neutral");
}

export function kindBadge(kind) {
  return badge(kind || "unknown", "neutral");
}

export function button(label, onClick, options = {}) {
  const { type = "button", variant = "", className = "", disabled, ...rest } = options;
  const node = el("button", {
    type,
    class: `btn ${variant} ${className}`.trim(),
    text: label,
    disabled,
    ...rest,
  });
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

export function field(labelText, control, hint) {
  const id = control.id || `field-${Math.random().toString(36).slice(2)}`;
  control.id = id;
  return el(
    "div",
    { class: "field" },
    el("label", { for: id, text: labelText }),
    control,
    hint ? el("span", { class: "hint", text: hint }) : null,
  );
}

export function loadingPanel(label = "Loading…") {
  return el(
    "div",
    { class: "state-panel", role: "status", "aria-live": "polite", "aria-busy": "true" },
    el(
      "div",
      { class: "skeleton-stack", "aria-hidden": "true" },
      el("div", { class: "skeleton" }),
      el("div", { class: "skeleton" }),
      el("div", { class: "skeleton" }),
    ),
    el("p", { class: "muted", text: label }),
  );
}

export function emptyPanel(title, message, action) {
  return el(
    "div",
    { class: "state-panel" },
    el("div", { class: "mark", "aria-hidden": "true", text: "∅" }),
    el("h2", { text: title }),
    el("p", { text: message }),
    action || null,
  );
}

export function errorText(error) {
  if (!error) return "Something went wrong.";
  if (error.payload && Array.isArray(error.payload.detail)) {
    return error.payload.detail
      .map((item) => `${(item.loc || []).join(".") || "request"}: ${item.msg}`)
      .join("; ");
  }
  if (error.payload && typeof error.payload.detail === "string") {
    return error.payload.detail;
  }
  if (typeof error.detail === "string" && error.detail) return error.detail;
  if (typeof error.message === "string" && error.message) return error.message;
  return String(error);
}

export function errorPanel(error, retry) {
  return el(
    "div",
    { class: "state-panel error", role: "alert" },
    el("div", { class: "mark", "aria-hidden": "true", text: "!" }),
    el("h2", { text: "Could not load this view" }),
    el("p", { text: errorText(error) }),
    retry ? el("div", {}, button("Try again", retry, { variant: "primary" })) : null,
  );
}

export function toast(message, tone = "success") {
  const container = document.getElementById("toasts");
  if (!container) return;
  const node = el("div", { class: `toast ${tone}`, role: "status", text: message });
  container.append(node);
  window.setTimeout(() => node.remove(), 4500);
}

export function openDialog({ title, body, footer, wide = false }) {
  const titleId = `dialog-title-${dialogCounter += 1}`;
  // Remember what opened the dialog so focus can return there when it closes.
  const restoreTarget = document.activeElement;
  const close = el("button", {
    class: "icon-btn",
    type: "button",
    "aria-label": "Close dialog",
    text: "×",
  });
  const dialog = el("dialog", {
    class: `dialog${wide ? " wide" : ""}`,
    "aria-labelledby": titleId,
  });
  close.addEventListener("click", () => dialog.close());
  dialog.append(
    el(
      "div",
      { class: "dialog-head" },
      el("h2", { class: "dialog-title", id: titleId, text: title }),
      close,
    ),
    el("div", { class: "dialog-body" }, body),
  );
  if (footer) dialog.append(el("div", { class: "dialog-foot" }, footer));
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (restoreTarget instanceof HTMLElement && document.contains(restoreTarget)) {
      restoreTarget.focus();
    } else {
      const main = document.getElementById("view") || document.querySelector("main") || document.body;
      if (main && typeof main.focus === "function") {
        if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
        main.focus();
      }
    }
  });
  document.body.append(dialog);
  dialog.showModal();
  requestAnimationFrame(() => {
    if (dialog.contains(document.activeElement) && document.activeElement !== dialog) {
      return;
    }
    const first = dialog.querySelector("input, select, textarea, button.btn-primary, button:not(.icon-btn)");
    (first || close).focus();
  });
  return dialog;
}

export function confirmAction({
  title,
  message,
  details,
  confirmLabel = "Confirm",
  danger = false,
  onConfirm,
}) {
  const errorBox = el("p", { class: "form-error", role: "alert" });
  const cancel = button("Cancel", () => dialog.close(), { variant: "ghost" });
  const confirm = button(confirmLabel, async () => {
    confirm.disabled = true;
    cancel.disabled = true;
    try {
      await onConfirm();
      dialog.close();
    } catch (error) {
      errorBox.textContent = errorText(error);
      confirm.disabled = false;
      cancel.disabled = false;
    }
  }, { variant: danger ? "danger" : "primary" });

  const body = el(
    "div",
    {},
    el("p", { class: "muted", text: message }),
    details || null,
    errorBox,
  );
  const dialog = openDialog({
    title,
    body,
    footer: el("div", { class: "dialog-actions" }, cancel, confirm),
  });
  confirm.focus();
  return dialog;
}

export function kvList(items) {
  const list = el("dl", { class: "kv" });
  for (const item of items) {
    list.append(
      el(
        "div",
        {},
        el("dt", { text: item.label }),
        el("dd", {}, item.value === null || item.value === undefined ? "—" : item.value),
      ),
    );
  }
  return list;
}

export function section(title, ...children) {
  return el(
    "section",
    { class: "section" },
    el("h3", { class: "section-title", text: title }),
    ...children,
  );
}

export function safeCodeBlock(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return el("pre", { class: "code-block" }, text);
}

export async function copyText(value, message = "Copied to clipboard.") {
  try {
    await navigator.clipboard.writeText(String(value));
    toast(message, "info");
  } catch {
    toast("Clipboard access is not available in this browser.", "error");
  }
}

export function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatNumber(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat().format(value);
}

export function formatMetric(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(4) : String(value);
}

export function formatDelta(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${number.toFixed(4)}`;
}

export function truncate(text, length = 140) {
  const value = String(text ?? "");
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

export function modeLabel(mode) {
  if (mode === "keyword") return "Keyword";
  if (mode === "semantic") return "Semantic";
  if (mode === "hybrid") return "Hybrid";
  return mode;
}

export function stepItem(number, title, description, status, action) {
  return el(
    "li",
    { class: "step" },
    el("span", { class: "step-number", "aria-hidden": "true", text: String(number) }),
    el(
      "div",
      { class: "step-body" },
      el("h3", { class: "step-title", text: title }),
      el("p", { class: "step-text", text: description }),
      action || null,
    ),
    status || null,
  );
}

export function countBadge(value, tone = "neutral", label = "") {
  const text = label ? `${value} ${label}` : String(value);
  return el("span", { class: `badge ${tone}`, text });
}

export function chipRow(items) {
  return el(
    "div",
    { class: "chip-row" },
    items.map((item) => el("span", { class: "chip", text: item })),
  );
}