/* Dashboard shell: hash router with query params, user context, health. */

import { api } from "./api.js";
import { clear, errorPanel, loadingPanel } from "./ui.js";
import { overviewView } from "./overview.js";
import { explorerView } from "./explorer.js";
import { debuggerView } from "./debugger.js";
import { evaluationView } from "./evaluation.js";

const VIEWS = {
  overview: overviewView,
  explorer: explorerView,
  debugger: debuggerView,
  evaluation: evaluationView,
};

const state = {
  userId: readUser(),
  runtime: null,
};

const viewRoot = document.getElementById("view");
const userForm = document.getElementById("user-form");
const userInput = document.getElementById("user-id");
const healthDot = document.getElementById("health-dot");

function readUser() {
  try {
    return window.localStorage.getItem("recallkit.user") || "demo-user";
  } catch {
    return "demo-user";
  }
}

function writeUser(value) {
  try {
    window.localStorage.setItem("recallkit.user", value);
  } catch {
    /* storage can be unavailable; the in-memory value still works */
  }
}

/**
 * Parse `#/route?key=value` into `{ name, params }`.
 * Unknown routes fall back to overview so a stale bookmark never breaks.
 */
export function parseRoute(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, search] = raw.split("?", 2);
  const name = VIEWS[path] ? path : "overview";
  const params = new URLSearchParams(search || "");
  return { name, params };
}

function navigate(path, params) {
  const search = params ? `?${new URLSearchParams(params).toString()}` : "";
  const target = `#/${path}${search}`;
  if (window.location.hash === target) {
    render();
    return;
  }
  window.location.hash = target;
}

function renderNav(name) {
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

async function refreshRuntime() {
  try {
    state.runtime = await api.runtime();
  } catch {
    state.runtime = null;
  }
}

async function refreshHealth() {
  if (!healthDot) return;
  try {
    await api.health();
    healthDot.className = "health-dot ok";
    healthDot.title = "Service and database healthy";
  } catch {
    healthDot.className = "health-dot error";
    healthDot.title = "Service or database unavailable";
  }
}

async function render() {
  const { name, params } = parseRoute(window.location.hash);
  if (!window.location.hash || window.location.hash === "#") {
    window.history.replaceState(null, "", "#/overview");
  }
  renderNav(name);
  clear(viewRoot);
  viewRoot.setAttribute("aria-busy", "true");
  viewRoot.append(loadingPanel(`Loading ${name}…`));
  try {
    await VIEWS[name](viewRoot, {
      userId: state.userId,
      runtime: state.runtime,
      navigate,
      refreshRuntime,
      params,
    });
  } catch (error) {
    clear(viewRoot);
    viewRoot.append(errorPanel(error, render));
  } finally {
    viewRoot.removeAttribute("aria-busy");
  }
  const heading = viewRoot.querySelector(".page-title");
  if (heading) heading.focus();
}

userInput.value = state.userId;
userForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = userInput.value.trim();
  if (!value) {
    window.alert("Enter a user ID.");
    return;
  }
  state.userId = value;
  writeUser(value);
  render();
});

window.addEventListener("hashchange", render);

(async function boot() {
  await Promise.all([refreshRuntime(), refreshHealth()]);
  await render();
})();