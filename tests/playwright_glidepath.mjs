// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Local Playwright check for the glidepath slice (glidepath design gate #6).
//
//
// Asserts, at viewport >= 1100px:
//   1. Glidepath OFF: the single allocation matrix renders, the phase matrices +
//      caption are hidden, no console/page errors.
//   2. Glidepath ON: all N derived phase matrices render with "age-range · event"
//      headers, laid out INSIDE the inputs column (geometric: left edge right of
//      the sidebar's right edge is N/A — single column; we assert the phases sit
//      within the allocation section's box, not under some other region).
//   3. Editing one phase's weight + Recompute updates the chart.

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/glidepath_shots";

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
// The app collapses #strategy-group by default (UX REFRESH ux1), which leaves
// #allocation-section in the DOM but NOT visible; opening it before the
// visibility waits below avoids a stale-gate timeout. Resilient to null / ordering.
await page.evaluate(() => {
  const d = document.getElementById("strategy-group");
  if (d && d.tagName === "DETAILS" && !d.open) d.open = true;
});
await page.waitForSelector("#allocation-section", { timeout: 10000 });
await page.waitForSelector("#allocation-rows .allocation-row", { timeout: 10000 });

// --- 1. Glidepath OFF (default) ---
const constantBodyVisible = await page.isVisible("#constant-allocation-body");
const phasesHidden = !(await page.isVisible("#glidepath-phases"));
const captionHidden = !(await page.isVisible("#glidepath-caption"));
const constantRows = await page.$$eval(
  "#allocation-rows .allocation-row",
  (els) => els.length,
);
console.log(
  `OFF: constant-body visible=${constantBodyVisible} phases hidden=${phasesHidden} ` +
    `caption hidden=${captionHidden} constant-matrix-rows=${constantRows}`,
);
if (!constantBodyVisible) fail("constant allocation body not visible with glidepath OFF");
if (!phasesHidden) fail("phase matrices visible with glidepath OFF");
if (!captionHidden) fail("glidepath caption visible with glidepath OFF");
if (constantRows < 2) fail("single allocation matrix did not render its rows");
await page.screenshot({ path: `${OUT}/01_glidepath_off.png`, fullPage: false });

// --- 2. Glidepath ON ---
await page.click('#allocation-mode-toggle .mode-toggle-btn[data-allocation-mode="glidepath"]');
await page.waitForSelector("#glidepath-phases .glidepath-phase", { timeout: 5000 });
const phaseCount = await page.$$eval(
  "#glidepath-phases .glidepath-phase",
  (els) => els.length,
);
const phaseHeaders = await page.$$eval(
  "#glidepath-phases .glidepath-phase-head",
  (els) => els.map((e) => e.textContent.trim()),
);
const constantHiddenNow = !(await page.isVisible("#constant-allocation-body"));
const captionVisibleNow = await page.isVisible("#glidepath-caption");
console.log(
  `ON: phase matrices=${phaseCount} headers=${JSON.stringify(phaseHeaders)} ` +
    `constant-body hidden=${constantHiddenNow} caption visible=${captionVisibleNow}`,
);
if (phaseCount !== 5) fail(`expected 5 derived phases for the default scenario, got ${phaseCount}`);
if (!constantHiddenNow) fail("constant matrix still visible with glidepath ON");
if (!captionVisibleNow) fail("glidepath caption not visible with glidepath ON");
const expectHeaders = ["50–59 · Bridge", "60–64 · Pension", "65–66 · Pension", "67–69 · Social Security", "70–95 · Social Security"];
if (JSON.stringify(phaseHeaders) !== JSON.stringify(expectHeaders)) {
  fail(`phase headers mismatch: got ${JSON.stringify(phaseHeaders)}`);
}

// Geometric: every phase card sits within the inputs sidebar's horizontal box (the
// slice-10 grid-placement bug class — content escaping its column). Assert each
// phase card's box is inside the allocation section's box.
const geom = await page.evaluate(() => {
  const sec = document.getElementById("allocation-section").getBoundingClientRect();
  const cards = [...document.querySelectorAll("#glidepath-phases .glidepath-phase")].map(
    (c) => c.getBoundingClientRect(),
  );
  return {
    sec: { left: sec.left, right: sec.right },
    cards: cards.map((r) => ({ left: r.left, right: r.right, width: r.width })),
  };
});
for (const [i, c] of geom.cards.entries()) {
  if (c.left < geom.sec.left - 1 || c.right > geom.sec.right + 1) {
    fail(`phase card ${i} escapes the allocation section box (card ${JSON.stringify(c)} vs sec ${JSON.stringify(geom.sec)})`);
  }
  if (c.width < 50) fail(`phase card ${i} has implausible width ${c.width}`);
}
await page.screenshot({ path: `${OUT}/02_glidepath_on.png`, fullPage: false });

// --- 3. Edit one phase + Recompute updates the chart ---
// Read the chart's summary line before, edit phase 0 (bridge) to all bonds, recompute.
const summaryBefore = await page.textContent("#summary-a").catch(() => null);
// Set the bridge phase (phase 0): bonds = 100, equity = 0. Class order is the
// 12-class menu; find inputs by id glide-w-0-<i>. We set us_agg_bonds to 100 and
// us_total_market to 0 by reading the class labels.
await page.evaluate(() => {
  // ASSET_CLASSES order: us_total_market is index 0, us_agg_bonds index 5 in the
  // 12-class menu — but be robust: zero everything in phase 0, then set bonds=100.
  const inputs = [...document.querySelectorAll('[id^="glide-w-0-"]')];
  for (const el of inputs) {
    el.value = "0";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
});
// Find the bonds row in phase 0 by its label text and set it to 100.
const setBonds = await page.evaluate(() => {
  const card = document.querySelector('#glidepath-phases .glidepath-phase[data-phase="0"]');
  const rows = [...card.querySelectorAll(".allocation-row")];
  for (const row of rows) {
    const lbl = row.querySelector(".allocation-label");
    if (lbl && /bond/i.test(lbl.textContent)) {
      const input = row.querySelector("input");
      input.value = "100";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return lbl.textContent.trim();
    }
  }
  return null;
});
console.log(`edited phase-0 bonds row: ${setBonds}`);
// Phase 0 should now total 100% (valid).
const phase0Total = await page.textContent(
  '#glidepath-phases .glidepath-phase[data-phase="0"] .allocation-total-value',
);
console.log(`phase 0 total after edit: ${phase0Total}`);

await page.click("#recompute-btn");
await page.waitForTimeout(1200);
const summaryAfter = await page.textContent("#summary-a").catch(() => null);
const formError = await page.textContent("#form-error").catch(() => "");
console.log(`summary before: ${summaryBefore}`);
console.log(`summary after : ${summaryAfter}`);
console.log(`form-error after recompute: "${(formError || "").trim()}"`);
if ((formError || "").trim() !== "") fail(`recompute surfaced a form error: ${formError}`);
if (!summaryAfter) fail("no summary after recompute");
if (summaryAfter === summaryBefore) {
  fail("summary line did not change after editing a phase + recompute");
}
await page.screenshot({ path: `${OUT}/03_glidepath_recompute.png`, fullPage: false });

if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  fail(`${errors.length} console/page error(s)`);
}

console.log(process.exitCode ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
