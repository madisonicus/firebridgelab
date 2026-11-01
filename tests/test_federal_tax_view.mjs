// test_federal_tax_view.mjs — pin the pure federal-tax-overlay view logic
// (web/app.js FederalTaxView) for the federal-tax overlay design ft1.
//
// FederalTaxView is the DOM-free logic behind the federal-tax readout + its "how
// to read" explainer — the sibling of TaxLocationView (tb1) for the tax OVERLAY:
// the mode-aware readout text (deterministic path scalars; Historical/MC
// median-of-paths aggregate) and the verbatim explainer paragraph. Loaded the
// same way as test_tax_location_view.mjs: app.js is read off disk and run in a
// node:vm sandbox with a `window` stub and NO `document`, so init() is skipped
// (the `typeof document` guard) and only the pure FederalTaxView off `window` is
// exercised. Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_federal_tax_view.mjs

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

// Load app.js into a sandbox with a window stub and no document, so init() is
// skipped and we get the pure FederalTaxView off `window`.
function loadFederalTaxView() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window.FederalTaxView;
}

const FTV = loadFederalTaxView();

// ---------------------------------------------------------------------------
// deterministicReadout — avg annual tax, % of SPENDING, the worst-year BILL
// (never framed as a consumption hit — ft2 DECISION 9), the forced-RMD fact.
// ---------------------------------------------------------------------------

// A no-RMD deterministic block: forced_tax all zero (bridge-era ages), so the
// readout carries neither the forced sentence nor the worst-year qualifier.
function detBlock(overrides = {}) {
  return {
    ages: [60, 61, 62],
    forced_tax: [0, 0, 0],
    avg_annual_tax: 18_500,
    effective_rate: 0.12,
    worst_tax: 31_000,
    worst_tax_age: 62,
    total_forced_tax: 0,
    ...overrides,
  };
}

test("deterministicReadout: absent (null/undefined) -> empty string", () => {
  assert.equal(FTV.deterministicReadout(null), "");
  assert.equal(FTV.deterministicReadout(undefined), "");
});

test("deterministicReadout: composes avg tax, pct of spending, worst-year bill+age", () => {
  const s = FTV.deterministicReadout(detBlock());
  assert.match(s, /\$18\.5k\/yr/);
  assert.match(s, /12% of spending/);
  assert.match(s, /largest single-year bill of \$31k at age 62/);
  // NEVER "effective tax rate" — the denominator is spending, not income
  // (reviewer-mandated wording).
  assert.doesNotMatch(s.toLowerCase(), /effective tax rate/);
});

test("deterministicReadout: no forced tax -> single sentence, no RMD framing", () => {
  const s = FTV.deterministicReadout(detBlock());
  assert.doesNotMatch(s, /forced/i);
  assert.doesNotMatch(s, /RMD/);
});

test("deterministicReadout: forced tax present -> the estate-drag sentence (ft2)", () => {
  const s = FTV.deterministicReadout(
    detBlock({
      ages: [74, 75, 76],
      forced_tax: [0, 4_000, 6_000],
      worst_tax_age: 76,
      total_forced_tax: 95_000,
    }),
  );
  assert.match(s, /\$95k of the lifetime bill is tax on forced RMD distributions you don't spend/);
  assert.match(s, /paid from the distributions themselves/);
  assert.match(s, /estate drag rather than a spending cut/);
  // At most two sentences (ft2 readout cap).
  assert.ok(s.split(". ").length <= 2, `at most two sentences: ${s}`);
});

test("deterministicReadout: worst year mostly forced -> the qualifier fires (ft2)", () => {
  // Worst year at 76 has forced 20k of the 31k bill (> 50%).
  const s = FTV.deterministicReadout(
    detBlock({
      ages: [75, 76, 77],
      forced_tax: [2_000, 20_000, 3_000],
      worst_tax_age: 76,
      total_forced_tax: 25_000,
    }),
  );
  assert.match(s, /at age 76, mostly paid from forced RMD distributions\./);
});

test("deterministicReadout: worst year NOT mostly forced -> no qualifier (ft2)", () => {
  // Worst year's forced share (10k of 31k) is under half -> plain bill clause.
  const s = FTV.deterministicReadout(
    detBlock({
      ages: [75, 76, 77],
      forced_tax: [2_000, 10_000, 3_000],
      worst_tax_age: 76,
      total_forced_tax: 15_000,
    }),
  );
  assert.doesNotMatch(s, /mostly paid from forced/);
});

test("deterministicReadout: NEGATIVE forced values are guarded max(0,·) (ft2)", () => {
  // Sub-ULP negatives (the engine deliberately doesn't clamp): a negative
  // total_forced_tax must not produce a forced sentence or a negative dollar.
  const s = FTV.deterministicReadout(
    detBlock({
      forced_tax: [-1e-9, -1e-9, -1e-9],
      total_forced_tax: -3e-9,
    }),
  );
  assert.doesNotMatch(s, /forced/i);
  assert.doesNotMatch(s, /-\$/);
});

test("deterministicReadout: fractional percent keeps one decimal", () => {
  const s = FTV.deterministicReadout(
    detBlock({
      avg_annual_tax: 10_000,
      effective_rate: 0.085,
      worst_tax: 15_000,
      worst_tax_age: 62,
    }),
  );
  assert.match(s, /8\.5% of spending/);
});

// ---------------------------------------------------------------------------
// stochasticReadout — the Historical/MC median-of-paths twin.
// ---------------------------------------------------------------------------

// A no-RMD aggregate block, same discipline as detBlock.
function aggBlock(overrides = {}) {
  return {
    ages: [60, 61, 62],
    forced_tax_median: [0, 0, 0],
    median_avg_annual_tax: 22_000,
    median_effective_rate: 0.15,
    median_worst_tax: 40_000,
    median_worst_tax_age: 62,
    median_total_forced_tax: 0,
    ...overrides,
  };
}

test("stochasticReadout: absent -> empty string", () => {
  assert.equal(FTV.stochasticReadout(null), "");
  assert.equal(FTV.stochasticReadout(undefined), "");
});

test("stochasticReadout: composes median tax, pct of spending, median worst-year bill+age", () => {
  const s = FTV.stochasticReadout(aggBlock());
  assert.match(s, /median/i);
  assert.match(s, /\$22k\/yr/);
  assert.match(s, /15% of spending/);
  assert.match(s, /median worst-year bill of \$40k at age 62/);
  assert.doesNotMatch(s.toLowerCase(), /effective tax rate/);
});

test("stochasticReadout: forced tax present -> the median estate-drag sentence (ft2)", () => {
  const s = FTV.stochasticReadout(
    aggBlock({
      ages: [79, 80, 81],
      forced_tax_median: [5_000, 25_000, 8_000],
      median_worst_tax_age: 80,
      median_total_forced_tax: 140_000,
    }),
  );
  assert.match(s, /A median \$140k of the lifetime bill is tax on forced RMD distributions you don't spend/);
  assert.match(s, /paid from the distributions themselves/);
  assert.match(s, /estate drag rather than a spending cut/);
  // Median worst year (80) is mostly forced (25k of 40k) -> qualifier fires too.
  assert.match(s, /at age 80, mostly paid from forced RMD distributions\./);
  assert.ok(s.split(". ").length <= 2, `at most two sentences: ${s}`);
});

test("stochasticReadout: no forced tax -> no RMD framing (ft2)", () => {
  const s = FTV.stochasticReadout(aggBlock());
  assert.doesNotMatch(s, /forced/i);
  assert.doesNotMatch(s, /RMD/);
});

test("stochasticReadout: NEGATIVE median forced total is guarded max(0,·) (ft2)", () => {
  const s = FTV.stochasticReadout(
    aggBlock({
      forced_tax_median: [-1e-9, -1e-9, -1e-9],
      median_total_forced_tax: -3e-9,
    }),
  );
  assert.doesNotMatch(s, /forced/i);
  assert.doesNotMatch(s, /-\$/);
});

// ---------------------------------------------------------------------------
// _pctOfFraction — the shared percent formatter (whole vs one-decimal).
// ---------------------------------------------------------------------------

test("_pctOfFraction: whole percent has no decimal; fractional keeps one", () => {
  assert.equal(FTV._pctOfFraction(0.12), "12%");
  assert.equal(FTV._pctOfFraction(0.085), "8.5%");
  assert.equal(FTV._pctOfFraction(0), "0%");
});

// ---------------------------------------------------------------------------
// EXPLAINER — the verbatim "how to read" paragraph. Content assertions only
// (not a snapshot of the whole string, so minor copy edits don't churn this
// test) — the load-bearing claims the tax-drag design td1 requires (retires
// the ft1/ft2 "display-only, portfolio never pays it" framing).
// ---------------------------------------------------------------------------

test("EXPLAINER: the portfolio itself pays the bill — fan/success/depletion/solve are after-tax", () => {
  assert.match(FTV.EXPLAINER, /the portfolio itself pays this bill/i);
  assert.match(FTV.EXPLAINER, /already after-tax/i);
  // The two ft1/ft2 falsehoods are named as RETIRED, never restated as current.
  assert.doesNotMatch(FTV.EXPLAINER, /PRE-TAX/);
  assert.doesNotMatch(FTV.EXPLAINER, /portfolio never actually pays this bill/i);
  assert.doesNotMatch(FTV.EXPLAINER, /display-only accounting pass/i);
});

test("EXPLAINER: names the two retired falsehoods — real RMD movement, real portfolio drag", () => {
  assert.match(FTV.EXPLAINER, /actually moves from tax-deferred into taxable/i);
  assert.match(FTV.EXPLAINER, /shrinks by the tax it owes/i);
});

test("EXPLAINER: federal only, MFJ, no state tax", () => {
  assert.match(FTV.EXPLAINER, /Married Filing Jointly/i);
  assert.match(FTV.EXPLAINER, /no state income tax/i);
  assert.doesNotMatch(FTV.EXPLAINER, /abroad/i);
});

test("EXPLAINER: explains the bracket-fill lever — ages 60-74, shrinks the RMD base", () => {
  assert.match(FTV.EXPLAINER, /bracket-fill lever/i);
  assert.match(FTV.EXPLAINER, /age 60 through 74/);
  assert.match(FTV.EXPLAINER, /shrink the tax-deferred balance/i);
  assert.match(FTV.EXPLAINER, /Required Minimum Distributions/i);
});

test("EXPLAINER: documented simplifications are named", () => {
  assert.match(FTV.EXPLAINER, /85%/); // flat SS taxable inclusion
  assert.match(FTV.EXPLAINER, /gains/i); // gains-fraction knob
  assert.match(FTV.EXPLAINER, /real \(inflation-adjusted\) dollars/i);
  assert.match(FTV.EXPLAINER, /Rev\. Proc\. 2025-32/); // 2026 law citation
  // NIIT frozen-nominal threshold, modeled fixed-real -> mildly optimistic
  // drift (design risk callout — reviewer-mandated disclosure).
  assert.match(FTV.EXPLAINER, /3\.8% net-investment-income tax threshold/);
  assert.match(FTV.EXPLAINER, /\(\$250k\) is held fixed in real dollars/);
  assert.match(FTV.EXPLAINER, /drifts mildly optimistic over the horizon/);
  assert.match(FTV.EXPLAINER, /10% early-withdrawal penalty/);
  assert.match(FTV.EXPLAINER, /dividends/i);
  assert.match(FTV.EXPLAINER, /foreign \(non-US\) taxation is out of scope/i);
});

test("EXPLAINER: post-depletion GI stays taxable in reality, but isn't charged to a gone portfolio", () => {
  assert.match(FTV.EXPLAINER, /guaranteed income keeps flowing/i);
  assert.match(FTV.EXPLAINER, /stays taxable in reality/i);
  assert.match(FTV.EXPLAINER, /portfolio that no longer exists/i);
});

test("EXPLAINER: stress runs pay tax the same honest way — the gap in the tooltip is the hidden tax", () => {
  assert.match(FTV.EXPLAINER, /Stress runs pay tax the same honest way/i);
  assert.match(FTV.EXPLAINER, /hidden, not broken out/i);
});

test("EXPLAINER: the Monte Carlo max-spend solve is fenced with a split", () => {
  assert.match(FTV.EXPLAINER, /Monte Carlo solve for max sustainable spend isn't available/i);
  assert.match(FTV.EXPLAINER, /Deterministic and Historical solves already ride/i);
});

test("EXPLAINER: never uses the 'effective tax rate' phrase", () => {
  assert.doesNotMatch(FTV.EXPLAINER.toLowerCase(), /effective tax rate/);
});

test("EXPLAINER: reads as a best estimate, not truth-to-the-dollar", () => {
  assert.match(FTV.EXPLAINER, /best estimate/i);
  assert.match(FTV.EXPLAINER, /not a truth-to-the-dollar tax return/i);
});

// ---------------------------------------------------------------------------
// _voluntarySentence / deterministicReadout+stochasticReadout voluntary clause
// (tax-drag design td1, DECISION 5/6) — the lever's lifetime share, framed as
// money MOVED (not spent). Mirrors the existing _forcedSentence tests.
// ---------------------------------------------------------------------------

test("_voluntarySentence: zero -> empty string", () => {
  assert.equal(FTV._voluntarySentence(0, false), "");
  assert.equal(FTV._voluntarySentence(-1e-9, false), "");
});

test("_voluntarySentence: nonzero -> money-moved-not-spent framing", () => {
  const s = FTV._voluntarySentence(45_000, false);
  assert.match(s, /\$45k/);
  assert.match(s, /voluntary bracket-fill/);
  assert.match(s, /money moved from tax-deferred to taxable, not spent/);
});

test("_voluntarySentence: medianPrefix distinguishes the Historical/MC phrasing", () => {
  const s = FTV._voluntarySentence(45_000, true);
  assert.match(s, /^ A median \$45k/);
});

test("deterministicReadout: lever active -> voluntary sentence appended, no forced sentence", () => {
  const s = FTV.deterministicReadout(
    detBlock({
      total_forced_tax: 0,
      total_voluntary_tax: 30_000,
    }),
  );
  assert.match(s, /\$30k of the lifetime bill is tax on voluntary bracket-fill drawdowns/);
  assert.match(s, /money moved from tax-deferred to taxable, not spent/);
  assert.doesNotMatch(s, /forced RMD/);
});

test("stochasticReadout: lever active -> median voluntary sentence appended", () => {
  const s = FTV.stochasticReadout(
    aggBlock({
      median_total_forced_tax: 0,
      median_total_voluntary_tax: 30_000,
    }),
  );
  assert.match(s, /A median \$30k of the lifetime bill is tax on voluntary bracket-fill drawdowns/);
});

test("deterministicReadout: NEGATIVE voluntary total is guarded max(0,·)", () => {
  const s = FTV.deterministicReadout(
    detBlock({ total_forced_tax: 0, total_voluntary_tax: -1e-9 }),
  );
  assert.doesNotMatch(s, /voluntary/i);
  assert.doesNotMatch(s, /-\$/);
});

// ---------------------------------------------------------------------------
// estateCostDeterministicReadout / estateCostStochasticReadout (tax-drag design
// td1, DECISION 9) — the estate-cost sentence PAIRED with the success-rate
// delta whenever it's nonzero (the mutual-depletion guard).
// ---------------------------------------------------------------------------

function estateCostBlock(overrides = {}) {
  return {
    count: 1,
    median_estate_cost: 140_000,
    pretax_success_rate: 1.0,
    dragged_success_rate: 1.0,
    success_rate_delta: 0.0,
    ...overrides,
  };
}

test("estateCostDeterministicReadout: absent -> empty string", () => {
  assert.equal(FTV.estateCostDeterministicReadout(null, 95), "");
  assert.equal(FTV.estateCostDeterministicReadout(undefined, 95), "");
});

test("estateCostDeterministicReadout: positive cost, no success delta -> plain sentence", () => {
  const s = FTV.estateCostDeterministicReadout(estateCostBlock(), 95);
  assert.match(s, /Taxes cost your estate ~\$140k by age 95\./);
  assert.doesNotMatch(s, /success rate/);
});

test("estateCostStochasticReadout: median prefix in the phrasing", () => {
  const s = FTV.estateCostStochasticReadout(estateCostBlock(), 95);
  assert.match(s, /Taxes cost your estate a median ~\$140k by age 95\./);
});

test("estateCostReadout: mutual-depletion guard — $0 cost + nonzero success delta still discloses the failure", () => {
  // Both runs deplete -> per-path cost is $0, but the after-tax world failed
  // more often than the pre-tax one. Without the paired sentence this would
  // misread as "taxes are free."
  const s = FTV.estateCostDeterministicReadout(
    estateCostBlock({
      median_estate_cost: 0,
      pretax_success_rate: 1.0,
      dragged_success_rate: 0.0,
      success_rate_delta: -1.0,
    }),
    95,
  );
  assert.match(s, /Taxes cost your estate ~\$0 by age 95\./);
  assert.match(s, /success rate runs 100 points lower/);
  assert.match(s, /doesn't mean taxes were free/);
});

test("estateCostReadout: negative median cost (dynamic-strategy quirk) is disclosed, not hidden", () => {
  const s = FTV.estateCostDeterministicReadout(
    estateCostBlock({ median_estate_cost: -20_000 }),
    90,
  );
  assert.match(s, /after-tax run actually ends ~\$20k AHEAD of the pre-tax comparison by age 90/);
  assert.match(s, /dynamic-spending quirk/);
});

test("estateCostReadout: fractional success-rate delta keeps one decimal point", () => {
  const s = FTV.estateCostDeterministicReadout(
    estateCostBlock({ success_rate_delta: -0.085 }),
    95,
  );
  assert.match(s, /8\.5 points lower/);
});
