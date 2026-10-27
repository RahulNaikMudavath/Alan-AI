/**
 * Lightweight browser regression check for the RecallKit dashboard.
 *
 * Uses only Node built-ins (fetch, WebSocket) and a Chromium DevTools endpoint.
 * Prints a JSON summary on stdout and exits non-zero on the first failed check.
 *
 * Usage:
 *   node scripts/dashboard_browser_check.mjs --cdp-port 9222 --base http://127.0.0.1:8150
 */

const VIEWS = ["overview", "explorer", "debugger", "evaluation"];
const WIDTHS = [
  { label: "1280", width: 1280, height: 900 },
  { label: "768", width: 768, height: 900 },
  { label: "390", width: 390, height: 844, mobile: true },
];

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 2) {
    args[argv[index].replace(/^--/, "")] = argv[index + 1];
  }
  return args;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cdpPort = args["cdp-port"] || "9222";
  const base = (args.base || "http://127.0.0.1:8150").replace(/\/$/, "");

  const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
  const page = targets.find((item) => item.type === "page");
  if (!page) throw new Error("no page target on the DevTools endpoint");

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let nextId = 1;
  const pending = new Map();
  const consoleErrors = [];

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const call = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) call.reject(new Error(message.error.message));
      else call.resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      consoleErrors.push(String(message.params?.exceptionDetails?.text || "exception"));
    }
    if (message.method === "Runtime.consoleAPICalled" && message.params?.type === "error") {
      consoleErrors.push(String(message.params.args?.[0]?.value || "console.error"));
    }
  });

  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });

  const send = (method, params = {}) => {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  };

  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    return result.result.value;
  };

  await send("Page.enable");
  await send("Runtime.enable");

  const results = [];
  let failures = 0;

  for (const viewport of WIDTHS) {
    await send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: Boolean(viewport.mobile),
    });

    for (const view of VIEWS) {
      const query = view === "debugger" ? "?q=editor%20theme%20preference" : "";
      const url = `${base}/dashboard/#/${view}${query}`;
      await send("Page.navigate", { url });
      // Prefilled debugger queries run immediately; give the fetches room.
      await sleep(view === "debugger" ? 4500 : 3000);

      const metrics = JSON.parse(
        await evaluate(
          "JSON.stringify({inner: window.innerWidth, scroll: document.documentElement.scrollWidth})",
        ),
      );
      const overflow = metrics.scroll > metrics.inner + 1;
      results.push({ viewport: viewport.label, view, check: "no-horizontal-overflow", overflow });
      if (overflow) failures += 1;

      if (viewport.width === 390 && view === "explorer") {
        const opened = await evaluate(
          "(() => { const b = document.querySelector('.page-actions button'); if (b) { b.click(); return true; } return false; })()",
        );
        if (opened) {
          await sleep(500);
          const dialogMetrics = JSON.parse(
            await evaluate(
              "JSON.stringify({inner: window.innerWidth, scroll: document.documentElement.scrollWidth, dialogOpen: Boolean(document.querySelector('dialog[open]'))})",
            ),
          );
          const dialogOverflow = dialogMetrics.scroll > dialogMetrics.inner + 1;
          results.push({
            viewport: viewport.label,
            view,
            check: "dialog-no-horizontal-overflow",
            overflow: dialogOverflow,
          });
          if (dialogOverflow) failures += 1;
          await evaluate(
            "(() => { const c = document.querySelector('dialog[open] .dialog-head button'); if (c) c.click(); })()",
          );
          await sleep(300);
        }
      }
    }
  }

  // Behavioural checks at desktop width.
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1280,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });

  const behaviour = [
    {
      name: "overview-journey",
      view: "overview",
      expression:
        "(() => { const t = document.body.innerText; return t.includes('Demo walkthrough') && t.includes('Try a query'); })()",
    },
    {
      name: "keyboard-skip-link-and-nav",
      view: "overview",
      expression: `(() => {
        const skip = document.querySelector(".skip-link");
        if (!skip || skip.getAttribute("href") !== "#view") return false;
        const navLinks = document.querySelectorAll("[data-nav]");
        if (navLinks.length !== 4) return false;
        const activeLink = document.querySelector("[data-nav].active");
        if (!activeLink || activeLink.getAttribute("aria-current") !== "page") return false;
        return true;
      })()`,
    },
    {
      name: "explorer-has-rows",
      view: "explorer",
      expression: "document.querySelectorAll('.data-table tbody tr').length > 0",
    },
    {
      name: "explorer-delete-retention-warning",
      view: "explorer",
      expression: `(async () => {
        const delBtn = document.querySelector(".data-table tbody tr button.btn.danger");
        if (!delBtn) return false;
        delBtn.focus();
        delBtn.click();
        const dialog = document.querySelector("dialog.dialog[open]");
        if (!dialog) return false;
        const text = dialog.innerText || "";
        const hasWarning = text.includes("Conversation receipts (conversations.result_json) and optional stored raw messages are not erased. Complete data erasure is not provided.");
        const cancelBtn = dialog.querySelector(".dialog-actions button");
        if (!cancelBtn) return false;
        cancelBtn.click();
        await new Promise((r) => setTimeout(r, 100));
        const closed = !document.querySelector("dialog.dialog[open]");
        const focusRestored = document.activeElement === delBtn;
        return hasWarning && closed && focusRestored;
      })()`,
    },
    {
      name: "debugger-three-modes",
      view: "debugger",
      query: "?q=editor%20theme%20preference",
      expression:
        "(() => { const columns = document.querySelectorAll('.result-column'); const cards = document.querySelectorAll('.result-card'); return columns.length === 3 && cards.length >= 1; })()",
    },
    {
      name: "debugger-does-not-invent-expectations",
      view: "debugger",
      query: "?q=editor%20theme%20preference",
      expression:
        "(() => { const cards = document.querySelectorAll('.result-card'); if (!cards.length) return false; const text = [...cards].map(c => c.innerText).join(' '); return text.includes('retrieved') && !text.includes('unexpected'); })()",
    },
    {
      name: "evaluation-measured-block",
      view: "evaluation",
      expression:
        "(() => { const t = document.body.innerText.toLowerCase(); return t.includes('measured') && t.includes('context') && !t.includes('not a compatible evaluation report'); })()",
    },
    {
      name: "evaluation-defaults-to-a-complete-run",
      view: "evaluation",
      expression:
        "(() => { const select = document.querySelector('#eval-a'); if (!select || !select.value) return false; const label = select.selectedOptions[0].textContent; return !label.includes('(partial)') && !label.includes('(unreadable)'); })()",
    },
  ];

  for (const check of behaviour) {
    const url = `${base}/dashboard/#/${check.view}${check.query || ""}`;
    await send("Page.navigate", { url });
    await sleep(check.query ? 4500 : 3000);
    const passed = await evaluate(check.expression);
    results.push({ viewport: "1280", view: check.view, check: check.name, passed });
    if (!passed) failures += 1;
  }

  const summary = { checks: results.length, failures, consoleErrors, results };
  console.log(JSON.stringify(summary, null, 2));
  ws.close();
  process.exit(failures === 0 && consoleErrors.length === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error(JSON.stringify({ error: String(error.message || error) }));
  process.exit(2);
});