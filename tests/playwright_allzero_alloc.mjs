// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Local Playwright check "All-zero allocation via a non-Recompute
// rerender silently shows the default 60/40."
//
//
// THE BUG (pre-fix): onRecompute gated an all-zero allocation, but the non-
// Recompute rerender paths (the link/unlink toggle here) did NOT. They sailed
// past the guard; buildPostBody then OMITS the all-zero allocation body, and the
// server silently falls back to its 60/40 default — rendered into the chart —
// while the live Total row still read 0%. A confusing silent mismatch.
//
// THE FIX (asserted here): rerender() now runs the SAME validateStrategyAllocation()
// guard onRecompute uses. So zeroing every weight and then flipping the link
// toggle must:
//   (a) fire NO /api/trajectory fetch (no silent 60/40 round-trip), and
//   (b) surface the invalid-state indicator: the #form-error "must total 100%"
//       message AND the Total readout cell flagged .is-invalid reading 0%.

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/allzero_alloc_shots";

import { mkdirSync } from "node:fs";
mkdirSync(OUT, { recursive: true });

const errors = [];

const browser = await chromium.launch({
  executablePath: undefined,
  headless: true,
});
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await ctx.newPage();

page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
});
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

function fail(msg) {
  console.log(`FAIL: ${msg}`);
  process.exitCode = 1;
}

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });

// Open the "Strategy & tuning" disclosure so the allocation matrix is visible.
// The app collapses #strategy-group by default (UX REFRESH ux1).
await page.evaluate(() => {
  const d = document.getElementById("strategy-group");
  if (d && d.tagName === "DETAILS" && !d.open) d.open = true;
});
await page.waitForSelector("#allocation-section", { timeout: 10000 });
await page.waitForSelector("#allocation-rows .allocation-row", { timeout: 10000 });

// --- Zero EVERY allocation weight (all 12 classes => 0%) ---
// Inputs are id `alloc-weight-a-<i>`. Dispatch `input` so the live readout
// (refreshAllocation) tracks the 0% Total, exactly as a real user typing would.
const zeroedCount = await page.evaluate(() => {
  const inputs = [...document.querySelectorAll('[id^="alloc-weight-a-"]')];
  for (const el of inputs) {
    el.value = "0";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  return inputs.length;
});
console.log(`zeroed ${zeroedCount} allocation weight inputs`);
if (zeroedCount < 12) fail(`expected >=12 allocation weight inputs, zeroed ${zeroedCount}`);

// Sanity: the live Total now reads 0% and is flagged invalid (the on-type backstop).
const totalText0 = await page.textContent("#allocation-totals .allocation-total-value");
const totalInvalid0 = await page.$eval(
  "#allocation-totals .allocation-total-value",
  (el) => el.classList.contains("is-invalid"),
);
console.log(`after zeroing: Total="${(totalText0 || "").trim()}" is-invalid=${totalInvalid0}`);
if (!totalInvalid0) fail("Total readout not flagged is-invalid after zeroing all weights");

// --- Trigger the NON-Recompute rerender: the allocation link/unlink toggle ---
// Count any /api/trajectory fetches that fire while the toggle's rerender runs.
// Pre-fix this path silently fetched the 60/40 default; post-fix the guard bails
// with NO fetch.
let trajectoryFetches = 0;
const onReq = (req) => {
  if (req.url().includes("/api/trajectory")) trajectoryFetches++;
};
page.on("request", onReq);

await page.click("#allocation-link");
// Give any (erroneous) fetch + its render time to land before asserting.
await page.waitForTimeout(1500);
page.off("request", onReq);

const formError = (await page.textContent("#form-error").catch(() => "")) || "";
const totalTextAfter = await page.textContent("#allocation-totals .allocation-total-value");
const totalInvalidAfter = await page.$eval(
  "#allocation-totals .allocation-total-value",
  (el) => el.classList.contains("is-invalid"),
);
console.log(`after toggle: trajectory-fetches=${trajectoryFetches}`);
console.log(`after toggle: form-error="${formError.trim()}"`);
console.log(`after toggle: Total="${(totalTextAfter || "").trim()}" is-invalid=${totalInvalidAfter}`);

await page.screenshot({ path: `${OUT}/01_allzero_after_toggle.png`, fullPage: false });

// (a) No silent 60/40 round-trip: the guard must short-circuit BEFORE any fetch.
if (trajectoryFetches !== 0) {
  fail(`link/unlink toggle fired ${trajectoryFetches} /api/trajectory fetch(es) on an all-zero allocation — the silent 60/40 path is still live`);
}
// (b) The invalid-state indicator is surfaced: the same messaging onRecompute shows.
if (!/total 100%/i.test(formError)) {
  fail(`expected an allocation "must total 100%" form error after the toggle, got: "${formError.trim()}"`);
}
// And the live Total still reflects the invalid 0% state (not a phantom 100%).
if (!totalInvalidAfter) {
  fail("Total readout lost its is-invalid flag after the toggle (would imply a silent default render)");
}

if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  fail(`${errors.length} console/page error(s)`);
}

console.log(process.exitCode ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
