// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `firebridgelab ui` from a checkout with the data fetched. Needs the
// `playwright` (or `playwright-core`) npm package; set CHROMIUM_PATH to use a
// system Chromium. Screenshots go to SHOT_DIR (default /tmp/household_shots).
//
// The M5 household editor, plan files and examples:
//   1. Turning the editor on converts the two pensions and two Social Security
//      benefits into four income streams.
//   2. Adding a part-time work stream and recomputing posts people and
//      income_streams (and no start_age) to /api/trajectory, which answers 200.
//   3. The Examples picker loads part-time-bridge: its people, streams, cash
//      event and required spending show in the editor, and a recompute is 200.
//   4. Download plan posts to /api/plan/export and saves a .toml that names the
//      example's streams.
//   5. Load plan reads a .toml file through /api/plan/import.
//   6. Turning the editor off sends the legacy request again (no people).
//   7. No console or page errors.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

let pw;
try {
  pw = (await import("playwright")).default;
} catch {
  pw = (await import("playwright-core")).default;
}
const { chromium } = pw;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/household_shots";
const EXAMPLE = path.resolve(__dirname, "../firebridgelab/examples/rental-and-pension.toml");
mkdirSync(OUT, { recursive: true });

const errors = [];
const launch = { headless: true };
if (process.env.CHROMIUM_PATH) launch.executablePath = process.env.CHROMIUM_PATH;
const browser = await chromium.launch(launch);
const ctx = await browser.newContext({ viewport: { width: 1360, height: 1100 }, acceptDownloads: true });
const page = await ctx.newPage();
// "Failed to load resource" is the browser logging an HTTP error status (the
// deliberate 422 below, a missing favicon), not a script error.
page.on("console", (m) => {
  if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
    errors.push(`console.error: ${m.text()}`);
  }
});
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

function check(ok, msg) {
  console.log(`${ok ? "ok  " : "FAIL"} ${msg}`);
  if (!ok) process.exitCode = 1;
}

// Recompute, returning the first /api/trajectory POST body and its status.
async function recompute() {
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith("/api/trajectory") && r.method() === "POST"),
    page.click("#recompute-btn"),
  ]);
  const resp = await req.response();
  return { body: JSON.parse(req.postData() || "{}"), status: resp.status() };
}

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.waitForSelector("#household-group", { timeout: 10000 });
await page.waitForTimeout(500);

// 1. Enable -> four converted streams.
await page.click("#household-group > summary");
await page.check("#hh-enabled");
const streamRows = () => page.$$eval("#hh-streams .hh-row", (rows) => rows.length);
check((await streamRows()) === 4, `editor on: ${await streamRows()} income streams (want 4)`);
check(await page.isHidden("#primary-fields-ss"), "legacy Social Security sliders hidden");

// 2. Add part-time work, recompute.
await page.selectOption("#hh-add-kind", "work");
await page.click("#hh-add-stream");
check((await streamRows()) === 5, "a fifth stream added");
const work = page.locator("#hh-streams .hh-row").nth(4);
await work.locator('input[data-field="amount"]').fill("25000");
await work.locator('input[data-field="end_age"]').fill("55");
let run = await recompute();
check(run.status === 200, `recompute with streams -> HTTP ${run.status}`);
check(Array.isArray(run.body.people) && run.body.people.length === 2, "POST carries two people");
check(run.body.income_streams && run.body.income_streams.length === 5, "POST carries five streams");
check(!("start_age" in run.body) && !("person_a_pension_amount" in run.body),
  "POST drops the legacy start_age and person_* keys");
const posted = run.body.income_streams[4];
check(posted.kind === "work" && posted.amount === 25000 && posted.end_age === 55,
  `the work stream posts as entered (${JSON.stringify(posted)})`);
await page.locator("#household-group").screenshot({ path: `${OUT}/01-household-editor.png` });

// 3. Example picker.
await page.selectOption("#examples-select", "part-time-bridge");
await page.waitForFunction(() => /Loaded the example/.test(document.getElementById("plan-file-status").textContent));
await page.waitForTimeout(800);
check((await streamRows()) === 3, `part-time-bridge: ${await streamRows()} streams (want 3)`);
const evRows = await page.$$eval("#hh-events .hh-row", (r) => r.length);
const adjRows = await page.$$eval("#hh-adjustments .hh-row", (r) => r.length);
check(evRows === 1 && adjRows === 2, `one cash event (${evRows}) and two required-spending rows (${adjRows})`);
const startAge = await page.inputValue("#start_age");
check(startAge === "48", `horizon follows the people: start age ${startAge}`);
run = await recompute();
check(run.status === 200, `recompute after the example -> HTTP ${run.status}`);
check(run.body.taxable_balance === 500000 && run.body.spending_mode === "lifecycle",
  "example's tax split and lifecycle rule are in the POST");
await page.screenshot({ path: `${OUT}/02-example-loaded.png`, fullPage: false });

// 4. Download plan.
const [exportReq, download] = await Promise.all([
  page.waitForRequest((r) => r.url().endsWith("/api/plan/export")),
  page.waitForEvent("download"),
  page.click("#plan-download-btn"),
]);
check((await exportReq.response()).status() === 200, "plan export -> HTTP 200");
const saved = path.join(OUT, download.suggestedFilename());
await download.saveAs(saved);
const toml = readFileSync(saved, "utf8");
check(download.suggestedFilename() === "part-time-bridge.toml", `download named ${download.suggestedFilename()}`);
check(/id = "sam_consulting"/.test(toml) && /\[\[spending_adjustments\]\]/.test(toml),
  "downloaded plan names the example's streams and adjustments");

// 5. Load plan from a file.
await page.setInputFiles("#plan-file-input", EXAMPLE);
await page.waitForFunction(() => /Loaded rental-and-pension\.toml/.test(document.getElementById("plan-file-status").textContent));
await page.waitForTimeout(800);
const kinds = await page.$$eval('#hh-streams select[data-field="kind"]', (els) => els.map((e) => e.value));
check(kinds.includes("rental") && kinds.includes("pension"), `loaded file streams: ${kinds.join(", ")}`);
await page.locator("#hh-streams .hh-row").first().screenshot({ path: `${OUT}/03-stream-row.png` });

// A bad file shows the API's message.
const bad = path.join(OUT, "bad.toml");
writeFileSync(bad, "[[people]]\nstart_age = 50\nend_age = 90\n[portfolio]\nbalance = 1\n[spending]\nanual = 1\n");
await page.setInputFiles("#plan-file-input", bad);
await page.waitForFunction(() => /Could not load/.test(document.getElementById("plan-file-status").textContent));
const msg = await page.textContent("#plan-file-status");
check(/anual/.test(msg) && /annual/.test(msg), `bad file message: ${msg.trim()}`);

// 6. Editor off -> legacy request.
await page.uncheck("#hh-enabled");
run = await recompute();
check(run.status === 200 && !("people" in run.body) && "start_age" in run.body,
  `editor off: legacy request (HTTP ${run.status})`);

// 7. Errors.
check(errors.length === 0, `no console/page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
console.log(`screenshots in ${OUT}`);
await browser.close();
