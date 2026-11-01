// test_portfolio_fan_tooltip.mjs — pin portfolioFanTooltipRows (web/app.js),
// the Historical/MC single-scenario portfolio-fan tooltip, and its tax-drag
// design td1 (DECISION 7, item 1) extension: the gross-outflow-median
// decomposition for the ONE hist/MC surface that carries a "Spending" row for
// CONSTANT/LIFECYCLE spending too (the dedicated spending fan,
// renderSpendingFanChart, is dynamic-spending-only — see test_spending_series.mjs
// for its gross-outflow-median tests).
//
// Loaded the same way as test_federal_tax_view.mjs / test_tax_location_view.mjs:
// app.js is read off disk and run in a node:vm sandbox with a `window` stub and
// NO `document` (init() is skipped by the `typeof document` guard), so we're
// exercising a plain top-level function — accessible by name via
// vm.runInContext, since a script-mode function declaration becomes a property
// of the sandbox's global object (== window here). guaranteedIncomeAtAge /
// deterministicSpendAtAge / fmtMoney / alignMoneyRows are all pure (no DOM), so
// no document stub is needed at all.
//
// Run:
//   (from the repo root) node --test tests/test_portfolio_fan_tooltip.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js", "scenario_store.js", "ss_claim.js"].map((f) =>
  path.join(WEB_DIR, f),
);
const APP_PATH = path.join(WEB_DIR, "app.js");

function loadCtx() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx;
}

function call(ctx, expr) {
  return vm.runInContext(expr, ctx);
}

// A constant-mode scenario: no spending fan (spendBand null in the calls
// below), so "Spending" is reconstructed via deterministicSpendAtAge. One SS
// stream so GI is nonzero at 67+.
const SCENARIO = {
  annual_spending: 130000,
  spending_mode: "constant",
  person_a_pension_amount: 0,
  person_a_pension_start_age: 60,
  person_a_ss_amount: 40000,
  person_a_ss_start_age: 67,
  person_b_pension_amount: 0,
  person_b_pension_start_age: 65,
  person_b_ss_amount: 0,
  person_b_ss_start_age: 70,
};

const BAND = { median: 2_200_000, p10: 1_500_000, p90: 3_100_000 };

// ---------------------------------------------------------------------------
// Baseline (no federalTaxAgg) — byte-identical to pre-td1 behavior.
// ---------------------------------------------------------------------------

test("portfolioFanTooltipRows: federalTaxAgg absent -> byte-identical baseline (Portfolio/Spending/GI/p10/p90)", () => {
  const ctx = loadCtx();
  ctx.BAND = BAND;
  ctx.SCENARIO = SCENARIO;
  const rows = call(ctx, "portfolioFanTooltipRows(BAND, 61, SCENARIO, null, undefined)");
  assert.equal(rows.length, 5);
  assert.ok(rows[0].startsWith("Portfolio"));
  assert.ok(rows[1].startsWith("Spending"));
  assert.ok(rows[1].includes("130,000"));
  assert.ok(rows[2].startsWith("Guaranteed income"));
  assert.ok(rows[3].startsWith("Portfolio 10th pct"));
  assert.ok(rows[4].startsWith("Portfolio 90th pct"));
});

// ---------------------------------------------------------------------------
// Gross-outflow-median decomposition (tax-drag design td1, DECISION 7, item 1):
// the constant/lifecycle case, where "Spending" is reconstructed client-side
// via deterministicSpendAtAge (never derived from the federal_tax medians).
// ---------------------------------------------------------------------------

const FEDERAL_TAX_AGG = {
  ages: [60, 61, 62],
  tax_median: [12000, 13500, 9000],
  voluntary_tax_median: [0, 0, 0],
  forced_tax_median: [0, 0, 0],
  gross_outflow_median: [142000, 158500, 119000],
};

test("portfolioFanTooltipRows: federalTaxAgg present -> Portfolio-funds row ABOVE Spending, then Federal tax", () => {
  const ctx = loadCtx();
  ctx.BAND = BAND;
  ctx.SCENARIO = SCENARIO;
  ctx.FT = FEDERAL_TAX_AGG;
  const rows = call(ctx, "portfolioFanTooltipRows(BAND, 61, SCENARIO, null, FT)");
  // Portfolio, Portfolio-funds, Spending, Federal tax, GI, p10, p90 = 7 rows
  // (no voluntary/forced since both are 0 at every age in this fixture).
  assert.equal(rows.length, 7);
  assert.ok(rows[0].startsWith("Portfolio"));
  assert.ok(!rows[0].startsWith("Portfolio funds"));
  assert.ok(rows[1].startsWith("Portfolio funds, median (incl. tax)"));
  assert.ok(rows[1].includes("158,500"));
  assert.ok(rows[2].startsWith("Spending"));
  // Constant mode: Spending is the deterministic shaped target (annual_spending),
  // NOT derived from gross_outflow_median - tax_median (158500-13500=145000,
  // which happens to differ from the true annual_spending 130000 here on
  // purpose, so a regression to subtraction would be caught).
  assert.ok(rows[2].includes("130,000"), `spending row reads the shaped target, not a subtraction: ${rows[2]}`);
  assert.ok(rows[3].startsWith("Federal tax, median"));
  assert.ok(rows[3].includes("13,500"));
  assert.ok(rows[4].startsWith("Guaranteed income"));
  assert.ok(rows[5].startsWith("Portfolio 10th pct"));
  assert.ok(rows[6].startsWith("Portfolio 90th pct"));
});

test("portfolioFanTooltipRows: guardrails case reads spending_bands' own median, not the shaped target", () => {
  const ctx = loadCtx();
  ctx.BAND = BAND;
  ctx.SCENARIO = SCENARIO;
  ctx.FT = FEDERAL_TAX_AGG;
  ctx.SPEND_BAND = { median: 118_000, p10: 100_000, p90: 140_000 };
  const rows = call(ctx, "portfolioFanTooltipRows(BAND, 61, SCENARIO, SPEND_BAND, FT)");
  assert.ok(rows[2].startsWith("Spending"));
  assert.ok(rows[2].includes("118,000"), `spending row reads spendBand.median: ${rows[2]}`);
});

test("portfolioFanTooltipRows: voluntary + forced median rows appear when nonzero", () => {
  const ctx = loadCtx();
  ctx.BAND = BAND;
  ctx.SCENARIO = SCENARIO;
  ctx.FT = {
    ages: [60, 61, 62],
    tax_median: [12000, 13500, 9000],
    voluntary_tax_median: [2000, 3500, 0],
    forced_tax_median: [0, 1000, -0.000000001],
    gross_outflow_median: [142000, 158500, 119000],
  };
  const rowsAge61 = call(ctx, "portfolioFanTooltipRows(BAND, 61, SCENARIO, null, FT)");
  assert.equal(rowsAge61.length, 9);
  assert.ok(rowsAge61[4].startsWith("Voluntary (bracket-fill lever), median"));
  assert.ok(rowsAge61[4].includes("3,500"));
  assert.ok(rowsAge61[5].startsWith("Forced (RMD, not a spending cut), median"));
  assert.ok(rowsAge61[5].includes("1,000"));

  // Age 62: forced_tax_median is -1e-9 (deliberately unclamped by the engine)
  // -> guarded to 0, no forced row, no negative dollar anywhere.
  const rowsAge62 = call(ctx, "portfolioFanTooltipRows(BAND, 62, SCENARIO, null, FT)");
  for (const row of rowsAge62) assert.ok(!row.includes("-$"));
});

test("portfolioFanTooltipRows: age absent from federalTaxAgg.ages -> no new rows (degrades to baseline)", () => {
  const ctx = loadCtx();
  ctx.BAND = BAND;
  ctx.SCENARIO = SCENARIO;
  ctx.FT = FEDERAL_TAX_AGG; // ages 60-62 only
  const rows = call(ctx, "portfolioFanTooltipRows(BAND, 99, SCENARIO, null, FT)");
  assert.equal(rows.length, 5); // baseline shape, no gross/tax rows injected
  assert.ok(!rows.some((r) => r.startsWith("Portfolio funds")));
});
