/**
 * Capture screenshots for documentation from the running dashboard.
 *
 * Usage:
 *   node scripts/capture_screenshots.mjs --cdp-port 9222 --base http://127.0.0.1:8150 --out-dir docs/screenshots
 */
import fs from "node:fs";
import path from "node:path";

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
  const outDir = args["out-dir"] || "docs/screenshots";

  if (!fs.existsSync(outDir)) {
    fs.mkdirSync(outDir, { recursive: true });
  }

  const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
  const page = targets.find((item) => item.type === "page");
  if (!page) throw new Error("no page target on the DevTools endpoint");

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let nextId = 1;
  const pending = new Map();

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const call = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) call.reject(new Error(message.error.message));
      else call.resolve(message.result);
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

  await send("Page.enable");
  await send("Runtime.enable");

  // Consistent 1280x950 desktop viewport so headers and table rows fit comfortably
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1280,
    height: 950,
    deviceScaleFactor: 1,
    mobile: false,
  });

  async function capture(filename) {
    const screenshot = await send("Page.captureScreenshot", { format: "png" });
    const buffer = Buffer.from(screenshot.data, "base64");
    const filepath = path.join(outDir, filename);
    fs.writeFileSync(filepath, buffer);
    console.log(`Saved screenshot: ${filepath} (${buffer.length} bytes)`);
  }

  // 1. Overview
  console.log("Navigating to Overview...");
  await send("Page.navigate", { url: `${base}/dashboard/#/overview` });
  await sleep(2500);
  await capture("overview.png");

  // 2. Memory Explorer
  console.log("Navigating to Memory Explorer...");
  await send("Page.navigate", { url: `${base}/dashboard/#/explorer` });
  await sleep(2500);
  await capture("explorer.png");

  // 3. Retrieval Debugger with query
  console.log("Navigating to Retrieval Debugger...");
  await send("Page.navigate", {
    url: `${base}/dashboard/#/debugger?q=editor%20theme%20preference`,
  });
  // Debugger makes 3 search requests, give it plenty of time to populate columns
  await sleep(4000);
  await capture("debugger.png");

  // 4. Evaluation Lab
  console.log("Navigating to Evaluation Lab...");
  await send("Page.navigate", { url: `${base}/dashboard/#/evaluation` });
  await sleep(3000);
  await capture("evaluation.png");

  ws.close();
  console.log("All screenshots captured successfully.");
}

main().catch((err) => {
  console.error("Screenshot capture failed:", err);
  process.exit(1);
});
