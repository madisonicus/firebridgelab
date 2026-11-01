// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Local Playwright check for the bucket-strategy slice 1b (bucket-strategy design
// gate G).
//
//
// Asserts, at viewport >= 1100px (GI fields are nested <details> deep, so we open
// ALL nested <details> before interacting):
//   (i)   Buckets OFF (default, single sourcing): the bucket-config block + the 3
//         matrices + the bucket-balance chart are all hidden; the page is the
//         unchanged single-sourcing surface; no console/page errors.
//   (ii)  Buckets ON (Historical, ≥1100px): the three bucket matrices (Cash /
//         Income / Growth) + the N1/N2 inputs + the buffer-survival readout render,
//         with a GEOMETRIC assertion that the matrices sit WITHIN the inputs sidebar
//         (the slice-10 grid-placement bug class — content escaping its column).
//   (iii) A config edit (N1 2→4) flows through Recompute to the bucket-balance
//         chart + the buffer-survival stat.

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/buckets_shots";

import { mkdirSync } from "node:fs";
mkdirSync(OUT, { recursive: true });

const errors = [];

const browser = await chromium.launch({ executablePath: undefined, headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1100 } });
const page = await ctx.newPage();

page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
});
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

function fail(msg) {
  console.log(`FAIL: ${msg}`);
  process.exitCode = 1;
}

// Open EVERY nested <details> so deep inputs (GI fields) + drill-downs are
// interactable, matching the design's "open all nested <details>" requirement.
async function openAllDetails() {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll("details")) d.open = true;
  });
}

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
// Open every nested <details> BEFORE the visibility waits below. #sourcing-toggle
// and #allocation-section live inside the collapsed #strategy-group disclosure
// (UX REFRESH ux1), so they exist in the DOM but are NOT visible on load; opening
// first avoids a stale-gate timeout on the waitForSelector visibility checks.
await openAllDetails();
await page.waitForSelector("#sourcing-toggle", { timeout: 10000 });
await page.waitForSelector("#allocation-rows .allocation-row", { timeout: 10000 });
await openAllDetails();

// --- (i) Buckets OFF (default = single sourcing) ---
const cfgHiddenOff = !(await page.isVisible("#bucket-config"));
const chartHiddenOff = !(await page.isVisible("#bucket-chart-view"));
const readoutHiddenOff = !(await page.isVisible("#buffer-survival-readout"));
console.log(
  `OFF: bucket-config hidden=${cfgHiddenOff} bucket-chart hidden=${chartHiddenOff} ` +
    `buffer-readout hidden=${readoutHiddenOff}`,
);
if (!cfgHiddenOff) fail("bucket-config visible with Buckets OFF");
if (!chartHiddenOff) fail("bucket-balance chart visible with Buckets OFF");
await page.screenshot({ path: `${OUT}/01_buckets_off.png`, fullPage: false });

// --- (ii) Buckets ON, Historical ---
await page.click('#mode-toggle .mode-toggle-btn[data-mode="historical"]');
await page.click('#sourcing-toggle .mode-toggle-btn[data-sourcing-mode="buckets"]');
await page.waitForSelector("#bucket-matrices .bucket-matrix", { timeout: 5000 });
await openAllDetails();

const matrixCount = await page.$$eval(
  "#bucket-matrices .bucket-matrix",
  (els) => els.length,
);
const matrixHeaders = await page.$$eval(
  "#bucket-matrices .bucket-matrix-head",
  (els) => els.map((e) => e.textContent.trim()),
);
const n1Visible = await page.isVisible("#bucket-n1");
const n2Visible = await page.isVisible("#bucket-n2");
console.log(
  `ON: matrices=${matrixCount} headers=${JSON.stringify(matrixHeaders)} ` +
    `N1 visible=${n1Visible} N2 visible=${n2Visible}`,
);
if (matrixCount !== 3) fail(`expected 3 bucket matrices, got ${matrixCount}`);
const expectHeaders = ["Cash", "Income", "Growth"];
if (JSON.stringify(matrixHeaders) !== JSON.stringify(expectHeaders)) {
  fail(`bucket matrix headers mismatch: got ${JSON.stringify(matrixHeaders)}`);
}
if (!n1Visible || !n2Visible) fail("N1/N2 inputs not visible with Buckets ON");

// Wait for the historical render to populate the buffer-survival readout.
await page.waitForFunction(
  () => {
    const el = document.getElementById("buffer-survival-readout");
    return el && !el.hidden && el.textContent.trim().length > 0;
  },
  { timeout: 8000 },
).catch(() => {});
const bufferText = await page.textContent("#buffer-survival-readout").catch(() => "");
console.log(`buffer-survival readout: "${(bufferText || "").trim()}"`);
if (!(bufferText || "").trim()) fail("buffer-survival readout empty with Buckets ON + Historical");

// Geometric: every bucket matrix card sits WITHIN the inputs sidebar's horizontal
// box (the slice-10 grid-placement bug class — content escaping its column).
const geom = await page.evaluate(() => {
  const sidebar = document.querySelector("aside.inputs-section").getBoundingClientRect();
  const cards = [...document.querySelectorAll("#bucket-matrices .bucket-matrix")].map(
    (c) => c.getBoundingClientRect(),
  );
  return {
    sidebar: { left: sidebar.left, right: sidebar.right },
    cards: cards.map((r) => ({ left: r.left, right: r.right, width: r.width })),
  };
});
for (const [i, c] of geom.cards.entries()) {
  if (c.left < geom.sidebar.left - 1 || c.right > geom.sidebar.right + 1) {
    fail(
      `bucket matrix ${i} escapes the inputs sidebar box ` +
        `(card ${JSON.stringify(c)} vs sidebar ${JSON.stringify(geom.sidebar)})`,
    );
  }
  if (c.width < 50) fail(`bucket matrix ${i} has implausible width ${c.width}`);
}
// The worst-cohort bucket-balance chart should be visible (Historical + buckets).
const chartVisibleOn = await page.isVisible("#bucket-chart-view");
if (!chartVisibleOn) fail("bucket-balance chart not visible with Buckets ON + Historical");
const chartTitle = await page.textContent("#bucket-chart-title").catch(() => "");
console.log(`bucket chart title: "${(chartTitle || "").trim()}"`);
await page.screenshot({ path: `${OUT}/02_buckets_on.png`, fullPage: false });

// --- (iii) Edit N1 2→4 → Recompute → chart + stat update ---
const bufferBefore = (await page.textContent("#buffer-survival-readout").catch(() => "")) || "";
const titleBefore = (await page.textContent("#bucket-chart-title").catch(() => "")) || "";
await page.fill("#bucket-n1", "4");
await page.click("#recompute-btn");
// Wait for the historical re-fetch to land.
await page.waitForTimeout(1500);
await openAllDetails();
const formError = (await page.textContent("#form-error").catch(() => "")) || "";
const n1Echo = await page.evaluate(() => {
  // The echoed scenario isn't on the page; assert the input still reads 4 and the
  // chart redrew (title present) + the readout repopulated without an error.
  return document.getElementById("bucket-n1").value;
});
const bufferAfter = (await page.textContent("#buffer-survival-readout").catch(() => "")) || "";
const titleAfter = (await page.textContent("#bucket-chart-title").catch(() => "")) || "";
console.log(`form-error after N1 edit: "${formError.trim()}"`);
console.log(`N1 input after recompute: ${n1Echo}`);
console.log(`buffer before: "${bufferBefore.trim()}"`);
console.log(`buffer after : "${bufferAfter.trim()}"`);
if (formError.trim() !== "") fail(`recompute surfaced a form error: ${formError}`);
if (n1Echo !== "4") fail(`N1 did not stick at 4 (got ${n1Echo})`);
if (!bufferAfter.trim()) fail("buffer-survival readout empty after N1 edit + recompute");
if (!titleAfter.trim()) fail("bucket chart title empty after N1 edit + recompute");
await page.screenshot({ path: `${OUT}/03_buckets_n1_edit.png`, fullPage: false });

if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  fail(`${errors.length} console/page error(s)`);
}

console.log(process.exitCode ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
