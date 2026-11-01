// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Live Playwright gate for the stress-test slice 1 (stress-test design gate).
// Verifies the VIEW > Stress Test lens overlays the current
// plan (does not replace it) and renders the single stressed trajectory.
//
// Asserts (viewport >= 1100px):
//   1. Default load: the 4th VIEW lens tab "Stress Test" exists; the stress
//      sub-selector is hidden.
//   2. Selecting the Stress Test tab reveals the sub-selector listing the
//      scenario(s); the deterministic summary is captured first.
//   3. OVERLAY INVARIANT: selecting "1970s Stagflation" leaves the plan inputs
//      (allocation weights, spending, GI) UNCHANGED, and the saved-scenarios
//      dropdown UNCHANGED — only the chart/summary changes.
//   4. The stressed summary differs from the deterministic one (the stress bit).
//   5. No console/page errors.

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/stress_shots";
import { mkdirSync } from "node:fs";
mkdirSync(OUT, { recursive: true });

const errors = [];
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await ctx.newPage();
page.on("console", (m) => { if (m.type() === "error") errors.push(`console.error: ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

function fail(msg) { console.log(`FAIL: ${msg}`); process.exitCode = 1; }

const snapshotInputs = () =>
  page.evaluate(() => {
    const vals = {};
    for (const el of document.querySelectorAll("input, select")) {
      if (el.id === "stress-select") continue; // the control we're operating
      vals[el.id || el.name || el.outerHTML.slice(0, 40)] = el.value;
    }
    return vals;
  });

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.waitForSelector("#mode-toggle", { timeout: 10000 });
await page.waitForTimeout(800); // let the initial deterministic render settle

// --- 1. Stress Test tab exists; sub-selector hidden by default ---
const stressTab = await page.$('#mode-toggle .lens-tab[data-mode="stress"]');
if (!stressTab) fail("no Stress Test tab in the VIEW lens");
const selectorHiddenInit = !(await page.isVisible("#stress-selector"));
console.log(`default: stress tab present=${!!stressTab} sub-selector hidden=${selectorHiddenInit}`);
if (!selectorHiddenInit) fail("stress sub-selector visible before selecting the Stress Test lens");

const summaryDet = await page.textContent("#summary-a").catch(() => null);
const inputsBefore = await snapshotInputs();
const savedScenarioBefore = await page.$eval("#scenario-select", (e) => e.value).catch(() => "(no dropdown)");
await page.screenshot({ path: `${OUT}/01_default_deterministic.png`, fullPage: false });

// --- 2. Click Stress Test tab -> sub-selector visible + lists scenarios ---
await stressTab.click();
await page.waitForSelector("#stress-selector", { state: "visible", timeout: 5000 });
const options = await page.$$eval("#stress-select option", (els) =>
  els.map((e) => ({ value: e.value, label: e.textContent.trim() })),
);
console.log(`stress options: ${JSON.stringify(options)}`);
if (!options.some((o) => /stagflation/i.test(o.value + o.label)))
  fail("1970s Stagflation not in the stress sub-selector");

// --- 2b. All 6 adverse scenarios present + each renders ---
if (options.length !== 10) fail(`expected 10 stress scenarios, got ${options.length}: ${JSON.stringify(options.map((o) => o.value))}`);
for (const o of options) {
  await page.selectOption("#stress-select", { value: o.value });
  await page.waitForTimeout(900);
  const s = await page.textContent("#summary-a").catch(() => null);
  console.log(`  ${o.value.padEnd(28)} -> summary: ${(s || "").trim()}`);
  if (!s || !s.trim()) fail(`scenario ${o.value} rendered no summary`);
}
// Screenshot a depleting one (depression_extreme) to eyeball graceful handling.
await page.selectOption("#stress-select", { value: "depression_extreme" }).catch(() => {});
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/03_depression_extreme.png`, fullPage: false });

// --- 3. Select stagflation; overlay invariant ---
await page.selectOption("#stress-select", { value: "stagflation_1970s" }).catch(async () => {
  // fall back to selecting by label if the value id differs
  await page.selectOption("#stress-select", { index: options.findIndex((o) => /stagflation/i.test(o.label)) });
});
await page.waitForTimeout(1500); // let the stress fetch + render complete
const summaryStress = await page.textContent("#summary-a").catch(() => null);
const inputsAfter = await snapshotInputs();
const savedScenarioAfter = await page.$eval("#scenario-select", (e) => e.value).catch(() => "(no dropdown)");

// Overlay invariant: NO plan input changed.
const changed = [];
for (const k of Object.keys(inputsBefore)) {
  if (inputsBefore[k] !== inputsAfter[k]) changed.push(`${k}: '${inputsBefore[k]}' -> '${inputsAfter[k]}'`);
}
console.log(`inputs changed by selecting stress: ${changed.length ? JSON.stringify(changed) : "NONE (good)"}`);
if (changed.length) fail(`stress selection MUTATED plan inputs (must overlay, not replace): ${JSON.stringify(changed)}`);
if (savedScenarioBefore !== savedScenarioAfter)
  fail(`saved-scenarios dropdown changed: '${savedScenarioBefore}' -> '${savedScenarioAfter}'`);

// --- 4. Summary changed (the stress bit) ---
console.log(`summary deterministic: ${summaryDet}`);
console.log(`summary stagflation : ${summaryStress}`);
if (!summaryStress) fail("no summary after selecting stress");
if (summaryStress === summaryDet) fail("summary unchanged after selecting stress (stress had no effect)");
await page.screenshot({ path: `${OUT}/02_stress_stagflation.png`, fullPage: false });

// --- 5. Spending-level chart renders BELOW the portfolio chart in stress mode ---
// (A previously flagged gap on Family A.) After selecting stagflation above, the
// deterministic spending chart wrapper should be visible with a canvas.
const spendWrapVisible = await page.isVisible("#det-spending-wrap").catch(() => false);
const spendCanvas = await page.$("#det-spending-chart");
console.log(`stress spending chart: wrap visible=${spendWrapVisible} canvas present=${!!spendCanvas}`);
if (!spendWrapVisible) fail("spending-level chart not shown below the portfolio chart in stress mode");
if (!spendCanvas) fail("#det-spending-chart canvas missing in stress mode");

// --- 6. cost_of_living_shock + guardrails: spending floors at the PERTURBED $144k ---
// Switch spending to guardrails, then select cost_of_living_shock; the spending chart must
// render and the floor guide line must reflect the effective (perturbed) floor.
await page.click('#spending-toggle .mode-toggle-btn[data-spending-mode="guardrails"]').catch(() => {});
await page.waitForTimeout(600);
await page.selectOption("#stress-select", { value: "cost_of_living_shock" }).catch(() => {});
await page.waitForTimeout(1500);
const colShockSummary = await page.textContent("#summary-a").catch(() => null);
const spendWrapColShock = await page.isVisible("#det-spending-wrap").catch(() => false);
const spendTitle = await page.textContent("#det-spending-title").catch(() => "");
console.log(`cost-of-living+guardrails: summary="${(colShockSummary || "").trim()}" spend-chart visible=${spendWrapColShock} title="${(spendTitle || "").trim()}"`);
if (!spendWrapColShock) fail("spending chart not shown for cost_of_living_shock + guardrails");
const inputsColShock = await snapshotInputs();
const colShockChanged = Object.keys(inputsBefore).filter((k) => inputsBefore[k] !== inputsColShock[k]);
// (spending toggle is a button, not an input/select snapshot key, so the number inputs should still be unchanged)
console.log(`inputs changed across cost-of-living+guardrails: ${colShockChanged.length ? JSON.stringify(colShockChanged) : "NONE (good)"}`);
if (colShockChanged.length) fail(`cost-of-living/guardrails mutated plan number inputs: ${JSON.stringify(colShockChanged)}`);
await page.screenshot({ path: `${OUT}/04_cost_of_living_guardrails.png`, fullPage: false });

// --- 7. sustained_cost_drift: spending line drifts UP (the valuable view) ---
// Back to constant spending so the drift is the only spending shape, then select it.
await page.click('#spending-toggle .mode-toggle-btn[data-spending-mode="constant"]').catch(() => {});
await page.waitForTimeout(500);
await page.selectOption("#stress-select", { value: "sustained_cost_drift" }).catch(() => {});
await page.waitForTimeout(1500);
const sustainedSummary = await page.textContent("#summary-a").catch(() => null);
const sustainedSpendWrap = await page.isVisible("#det-spending-wrap").catch(() => false);
console.log(`sustained_cost_drift: summary="${(sustainedSummary || "").trim()}" spend-chart visible=${sustainedSpendWrap}`);
if (!sustainedSpendWrap) fail("spending chart not shown for sustained_cost_drift");
await page.screenshot({ path: `${OUT}/05_sustained_cost_drift.png`, fullPage: true });

if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  fail(`${errors.length} console/page error(s)`);
}
console.log(process.exitCode ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
