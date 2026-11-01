// test_tax_location_view.mjs — pin the pure tax-location view logic (web/app.js
// TaxLocationView) for the tax-buckets tb1 slice.
//
// TaxLocationView is the DOM-free logic behind the tax-location feature: the
// running-sum helper ("balanced / over / under"), the mode-aware bridge-readout
// text (deterministic funded-vs-depleted; historical/MC frequency sentence), the
// verbatim "how to read" explainer + pre-tax caveat, and the one-line split
// summary for the key-parameters digest. Loaded the same way as
// test_chart_explainers.mjs / test_bucket_view.mjs: app.js is read off disk and
// run in a node:vm sandbox with a `window` stub and NO `document`, so init() is
// skipped (the `typeof document` guard) and only the pure TaxLocationView off
// `window` is exercised. The buckets-mode gating + saved-scenario wiring are
// asserted statically against the markup + source (the live DOM seam is covered by
// the integrator's Playwright gate, not this DOM-free sandbox). Zero npm deps;
// requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_tax_location_view.mjs

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
const INDEX_PATH = path.join(WEB_DIR, "index.html");

// Load app.js into a sandbox with a window stub and no document, so init() is
// skipped and we get the pure TaxLocationView off `window`.
function loadTaxLocationView() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window.TaxLocationView;
}

const TLV = loadTaxLocationView();

// ---------------------------------------------------------------------------
// hasSplit — the feature-off gate.
// ---------------------------------------------------------------------------

test("hasSplit: off only when ALL three are blank (null)", () => {
  assert.equal(TLV.hasSplit({ taxable: null, taxDeferred: null, roth: null }), false);
  assert.equal(TLV.hasSplit({ taxable: 0, taxDeferred: null, roth: null }), true);
  assert.equal(TLV.hasSplit({ taxable: null, taxDeferred: 700000, roth: null }), true);
  assert.equal(TLV.hasSplit({ taxable: 1, taxDeferred: 2, roth: 3 }), true);
});

// ---------------------------------------------------------------------------
// sumHelper — balanced / over / under (+ off).
// ---------------------------------------------------------------------------

test("sumHelper: off (no split) → inactive, no text", () => {
  const h = TLV.sumHelper({ taxable: null, taxDeferred: null, roth: null, target: 1_800_000 });
  assert.equal(h.active, false);
  assert.equal(h.status, "off");
  assert.equal(h.text, "");
});

test("sumHelper: balanced (exact) → 'balanced'", () => {
  const h = TLV.sumHelper({
    taxable: 450_000, taxDeferred: 1_080_000, roth: 270_000, target: 1_800_000,
  });
  assert.equal(h.status, "balanced");
  assert.equal(h.text, "$1.8M of $1.8M — balanced");
});

test("sumHelper: within $1 tolerance still reads balanced", () => {
  const h = TLV.sumHelper({
    taxable: 450_000.5, taxDeferred: 1_080_000, roth: 269_999.6, target: 1_800_000,
  });
  assert.equal(h.status, "balanced");
});

test("sumHelper: over → 'over' with the surplus", () => {
  const h = TLV.sumHelper({
    taxable: 450_000, taxDeferred: 1_180_000, roth: 270_000, target: 1_800_000,
  });
  assert.equal(h.status, "over");
  assert.equal(h.text, "$100k over ($1.9M of $1.8M)");
});

test("sumHelper: under → 'under' with the shortfall", () => {
  const h = TLV.sumHelper({
    taxable: 450_000, taxDeferred: 980_000, roth: 270_000, target: 1_800_000,
  });
  assert.equal(h.status, "under");
  assert.equal(h.text, "$100k under ($1.7M of $1.8M)");
});

test("sumHelper: blanks count as $0 in the running sum", () => {
  // Only taxable supplied → sum = taxable, under by the rest.
  const h = TLV.sumHelper({
    taxable: 1_000_000, taxDeferred: null, roth: null, target: 1_800_000,
  });
  assert.equal(h.active, true);
  assert.equal(h.status, "under");
  assert.equal(h.text, "$800k under ($1M of $1.8M)");
});

// ---------------------------------------------------------------------------
// Deterministic bridge readout — funded vs depleted.
// ---------------------------------------------------------------------------

test("deterministic: taxable survives the bridge → 'lasts the whole bridge' with the spare at 59", () => {
  const tl = {
    ages: [50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60],
    taxable: [2_100_000, 1_900_000, 1_700_000, 1_500_000, 1_300_000, 1_100_000, 900_000, 700_000, 500_000, 320_000, 100_000],
    tax_deferred: [670_000, 680_000, 690_000, 700_000, 710_000, 720_000, 730_000, 740_000, 750_000, 760_000, 770_000],
    roth: [285_000, 290_000, 295_000, 300_000, 305_000, 310_000, 315_000, 320_000, 325_000, 330_000, 335_000],
    taxable_depletion_age: null,
    reachable_depletion_age: null,
  };
  const s = TLV.deterministicReadout(tl);
  assert.match(s, /lasts the whole bridge/);
  // Spare = taxable at age 59 = $320,000 → "$320k".
  assert.match(s, /\$320k still in the taxable account/);
});

test("deterministic: taxable survives but age 59 not in series → survived, no spare figure", () => {
  const tl = {
    ages: [50, 51, 52],
    taxable: [1_500_000, 1_300_000, 1_100_000],
    tax_deferred: [700_000, 710_000, 720_000],
    roth: [300_000, 305_000, 310_000],
    taxable_depletion_age: null,
    reachable_depletion_age: null,
  };
  const s = TLV.deterministicReadout(tl);
  assert.match(s, /lasts the whole bridge/);
  assert.match(s, /still funded/);
  assert.doesNotMatch(s, /to spare|still in the taxable account/);
});

test("deterministic: taxable runs dry pre-door, reachable survives → just the taxable-dry sentence", () => {
  const tl = {
    ages: [50, 51, 52],
    taxable: [200_000, 0, 0],
    tax_deferred: [2_000_000, 2_000_000, 2_000_000],
    roth: [300_000, 200_000, 100_000],
    taxable_depletion_age: 51,
    reachable_depletion_age: null,
  };
  const s = TLV.deterministicReadout(tl);
  assert.match(s, /Taxable runs dry at age 51/);
  assert.doesNotMatch(s, /forced to tap tax-deferred/);
});

test("deterministic: taxable AND roth both exhausted → forced-tax-deferred-draw clause", () => {
  const tl = {
    ages: [50, 51, 52, 53],
    taxable: [200_000, 0, 0, 0],
    tax_deferred: [2_000_000, 2_000_000, 2_000_000, 1_900_000],
    roth: [300_000, 200_000, 0, 0],
    taxable_depletion_age: 51,
    reachable_depletion_age: 52,
  };
  const s = TLV.deterministicReadout(tl);
  assert.match(s, /Taxable runs dry at age 51/);
  assert.match(s, /Taxable and Roth are both exhausted by age 52/);
  assert.match(s, /forced to tap tax-deferred early/);
});

test("deterministic: null payload → empty string (no readout)", () => {
  assert.equal(TLV.deterministicReadout(null), "");
});

// ---------------------------------------------------------------------------
// Stochastic bridge readout — the headline frequency metric. Percentages are
// 0..100 in the payload (NOT 0..1) and are used verbatim.
// ---------------------------------------------------------------------------

test("stochastic: pct + median → the frequency sentence", () => {
  const risk = {
    count: 100,
    pct_taxable_depleted_pre_595: 37,
    pct_reachable_depleted_pre_595: 12,
    taxable_depletion: { median: 56, p10: 54, earliest: 52 },
    reachable_depletion: { median: 58, p10: 56, earliest: 55 },
  };
  const s = TLV.stochasticReadout(risk);
  assert.match(s, /Taxable bottoms out before 59½ in 37% of market paths \(median age 56\)/);
  assert.match(s, /forcing an early tax-deferred withdrawal — in 12% of paths/);
});

test("stochastic: 0% reads '0%' (never blank)", () => {
  const risk = {
    count: 100,
    pct_taxable_depleted_pre_595: 0,
    pct_reachable_depleted_pre_595: 0,
    taxable_depletion: { median: null, p10: null, earliest: null },
    reachable_depletion: { median: null, p10: null, earliest: null },
  };
  const s = TLV.stochasticReadout(risk);
  assert.match(s, /in 0% of market paths\./); // no median clause when median is null
  assert.match(s, /in 0% of paths\./);
  assert.doesNotMatch(s, /median age/);
});

test("stochastic: fractional percent keeps one decimal", () => {
  const risk = {
    count: 1000,
    pct_taxable_depleted_pre_595: 12.5,
    pct_reachable_depleted_pre_595: 3.2,
    taxable_depletion: { median: 57, p10: 55, earliest: 54 },
    reachable_depletion: { median: 59, p10: 57, earliest: 56 },
  };
  const s = TLV.stochasticReadout(risk);
  assert.match(s, /in 12.5% of market paths/);
  assert.match(s, /in 3.2% of paths/);
});

test("stochastic: null payload → empty string", () => {
  assert.equal(TLV.stochasticReadout(null), "");
});

// ---------------------------------------------------------------------------
// summarizeSplit — the key-parameters digest line.
// ---------------------------------------------------------------------------

test("summarizeSplit: '$1.5M taxable / $700k tax-deferred / $300k Roth'", () => {
  const s = TLV.summarizeSplit({ taxable: 1_500_000, taxDeferred: 700_000, roth: 300_000 });
  assert.equal(s, "$1.5M taxable / $700k tax-deferred / $300k Roth");
});

test("summarizeSplit: blanks read as $0", () => {
  const s = TLV.summarizeSplit({ taxable: 1_800_000, taxDeferred: null, roth: null });
  assert.equal(s, "$1.8M taxable / $0 tax-deferred / $0 Roth");
});

// ---------------------------------------------------------------------------
// Verbatim copy invariants — the explainer + caveat are present & non-trivial,
// and never leak a version label into user-facing copy.
// ---------------------------------------------------------------------------

test("explainer + balance note are present and non-trivial", () => {
  assert.equal(typeof TLV.EXPLAINER, "string");
  assert.ok(TLV.EXPLAINER.length > 100);
  assert.match(TLV.EXPLAINER, /59½/);
  assert.match(TLV.EXPLAINER, /bridge/);
  // tax-drag td1 DECISION 4: the post-75 RMD-movement disclosure — composition
  // past 75 is now honest (a real transfer), not a shadow tax calculation.
  assert.match(TLV.EXPLAINER, /From age 75/);
  assert.match(TLV.EXPLAINER, /move any excess over your/);
  assert.equal(typeof TLV.BALANCE_NOTE, "string");
  // tax-drag td1 DECISION 1 retires the ft1/ft2 "balances are pre-tax" claim —
  // a split now ALWAYS drags, so the note must say the OPPOSITE of what it
  // used to: these balances already pay the bill, honest not optimistic.
  assert.doesNotMatch(TLV.BALANCE_NOTE, /optimistic floor/);
  assert.doesNotMatch(TLV.BALANCE_NOTE, /pre-tax: the federal-tax overlay/);
  assert.doesNotMatch(TLV.BALANCE_NOTE, /aren't grossed up to pay it/);
  assert.match(TLV.BALANCE_NOTE, /already pay the federal tax bill/);
  assert.match(TLV.BALANCE_NOTE, /honest after-tax depletion picture/);
  // The one surviving pre-tax number is disclosed as the SEPARATE estate-cost
  // counterfactual, not this chart.
  assert.match(TLV.BALANCE_NOTE, /estate cost of taxes/);
});

test("no version label ('tb1' / 'v1' etc.) leaks into user-facing copy", () => {
  const corpus = [
    TLV.EXPLAINER,
    TLV.BALANCE_NOTE,
    TLV.deterministicReadout({
      ages: [50], taxable: [1], tax_deferred: [1], roth: [1],
      taxable_depletion_age: 50, reachable_depletion_age: 50,
    }),
    TLV.stochasticReadout({
      count: 1, pct_taxable_depleted_pre_595: 1, pct_reachable_depleted_pre_595: 1,
      taxable_depletion: { median: 55, p10: 54, earliest: 52 },
      reachable_depletion: { median: 56, p10: 55, earliest: 54 },
    }),
  ].join(" ");
  assert.doesNotMatch(corpus, /\btb1\b/i);
  assert.doesNotMatch(corpus, /\bv\d+\b/i);
});

// ---------------------------------------------------------------------------
// Buckets-mode gating + saved-scenario + reset wiring — STATIC checks against the
// markup + source (the live DOM behaviour is the integrator's Playwright gate).
// ---------------------------------------------------------------------------

test("markup: the three balance inputs + the gate note + sum helper exist", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  for (const id of ["tax-taxable", "tax-deferred", "tax-roth", "tax-location-sum", "tax-location-gate-note"]) {
    assert.ok(html.includes(`id="${id}"`), `${id} present in markup`);
  }
  // The gate note carries the single-mode-only message (buckets coming later).
  assert.match(html, /single-portfolio sourcing; bucket sourcing coming later/);
});

test("markup: the tax-location chart + bridge readout + explainer exist", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  for (const id of [
    "tax-location-view",
    "tax-location-chart",
    "tax-location-bridge",
    "tax-location-caveat",
    "explainer-tax-location-text",
  ]) {
    assert.ok(html.includes(`id="${id}"`), `${id} present in markup`);
  }
  // The chart is about ACCOUNTS / tax location, never "buckets".
  assert.match(html, /Account balances over time/);
});

test("source: buckets-mode gating disables the inputs + shows the note", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  // updateTaxLocationGate disables the three inputs + toggles the note off isBuckets.
  assert.match(src, /function updateTaxLocationGate\(\)/);
  assert.match(src, /const isBuckets = sourcingMode === "buckets";/);
  // setSourcingMode calls the gate so flipping to/from buckets re-gates the inputs.
  const setSrc = src.slice(src.indexOf("function setSourcingMode("));
  assert.match(setSrc.slice(0, 1200), /updateTaxLocationGate\(\)/);
});

test("source: the split rides on the POST body only when active (single + supplied)", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const pa = fs.readFileSync(path.join(WEB_DIR, "payload_assembler.js"), "utf8");
  // The PayloadAssembler extraction moved attachTaxLocation into the pure module:
  // it now gates on the pre-resolved taxLocation.active flag, not a live DOM read.
  assert.match(pa, /function attachTaxLocation\(params, taxLocation\)\s*{\s*\n\s*if \(!taxLocation \|\| !taxLocation\.active\) return;/);
  // app.js still owns the gate that PRODUCES that flag: single sourcing + a split.
  assert.match(src, /sourcingMode !== "buckets" && TaxLocationView\.hasSplit\(readTaxLocation\(\)\)/);
  // readFormState resolves the gate once; the assembler attaches it to both A and B.
  assert.match(src, /active: taxLocationActive\(\)/);
  assert.match(pa, /attachTaxLocation\(a, formState\.taxLocation\);\s*\n\s*attachTaxLocation\(b, formState\.taxLocation\);/);
});

test("source: saved-scenario round-trip + reset clear the three inputs", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const ss = fs.readFileSync(path.join(WEB_DIR, "scenario_store.js"), "utf8");
  // The ScenarioStore extraction moved the blob shaping into the pure module:
  // app.js still OWNS the active-gate that resolves the split, then hands it on
  // captureState.taxLocation only when taxLocationActive().
  assert.match(src, /if \(taxLocationActive\(\)\) {\s*\n\s*taxLocation = {/);
  // ScenarioStore.capture emits inputs.tax_location only when that block is supplied.
  assert.match(ss, /if \(captureState\.taxLocation\) {\s*\n\s*inputs\.tax_location = captureState\.taxLocation;/);
  // applyScenarioToForm restores via setTaxLocationInputs, treating a missing block
  // (old saves / feature-off) as "clear the inputs".
  assert.match(src, /setTaxLocationInputs\(inputs\.tax_location \|\| null\)/);
  // setTaxLocationInputs(null) clears to blank (the reset / feature-off path).
  assert.match(src, /function setTaxLocationInputs\(split\)/);
});

// ---------------------------------------------------------------------------
// fanConfig — the Historical / MC chart config builder (txlb hotfix). DOM-free:
// asserts the dataset shape (TOTAL outer band + 3 account median LINES), colours,
// labels, axis title, and the combined tooltip. Uses the same node:vm sandbox
// (so ChartView.fanDatasets / tooltipRows are the real implementations) with stub
// deps for the colours + formatters app.js would pass at the live seam.
// ---------------------------------------------------------------------------

const FAN_DEPS = {
  lineColors: { taxable: "#46698c", tax_deferred: "#6b5b95", roth: "#a07418" },
  totalRgb: "100, 116, 139",
  totalLine: "#475569",
  lineWidth: 4,
  fmtMoney: (n) => `$${Math.round(n)}`,
  fmtMillions: (n) => `$${(n / 1e6).toFixed(2)}m`,
};

function stubRisk() {
  const ages = [50, 51, 52, 53];
  return {
    ages,
    // taxable median present in both old + new payloads.
    taxable_median: [800_000, 600_000, 400_000, 200_000],
    tax_deferred_median: [1_500_000, 1_520_000, 1_540_000, 1_560_000],
    roth_median: [135_000, 130_000, 125_000, 115_000],
    total_p10: [2_300_000, 2_100_000, 1_900_000, 1_700_000],
    total_median: [2_435_000, 2_250_000, 2_065_000, 1_875_000],
    total_p90: [2_600_000, 2_450_000, 2_400_000, 2_300_000],
  };
}

test("fanConfig: builds the total outer band + three account median lines", () => {
  const cfg = TLV.fanConfig(stubRisk(), FAN_DEPS);
  assert.deepEqual(cfg.labels, [50, 51, 52, 53]);

  // outer band only (p10 + p90 band-edge, no inner p25/p75) → 2 band datasets,
  // then the total-median line, then the 3 account median lines = 6 total.
  assert.equal(cfg.datasets.length, 6);

  // The two band-edge datasets are zero-width (hidden from legend / tooltip).
  const bandEdges = cfg.datasets.filter((d) => d.borderWidth === 0);
  assert.equal(bandEdges.length, 2);

  // The drawn lines (borderWidth > 0): total median (width 4) + 3 account lines.
  const lines = cfg.datasets.filter((d) => d.borderWidth > 0);
  assert.equal(lines.length, 4);

  const byLabel = Object.fromEntries(cfg.datasets.map((d) => [d.label, d]));
  // Total median line: slate, width 4.
  const total = byLabel["Total investments — median (real $)"];
  assert.ok(total);
  assert.equal(total.borderColor, "#475569");
  assert.equal(total.borderWidth, 4);

  // Three account median lines: correct colours, width 2, fill:false, the new data.
  const tax = byLabel["Taxable — median"];
  const td = byLabel["Tax-deferred — median"];
  const roth = byLabel["Roth — median"];
  assert.ok(tax && td && roth);
  assert.equal(tax.borderColor, "#46698c");
  assert.equal(td.borderColor, "#6b5b95");
  assert.equal(roth.borderColor, "#a07418");
  for (const d of [tax, td, roth]) {
    assert.equal(d.borderWidth, 2);
    assert.equal(d.fill, false);
    assert.equal(d.pointRadius, 0);
  }
  assert.deepEqual(tax.data, [800_000, 600_000, 400_000, 200_000]);
  assert.deepEqual(td.data, [1_500_000, 1_520_000, 1_540_000, 1_560_000]);
  assert.deepEqual(roth.data, [135_000, 130_000, 125_000, 115_000]);
});

test("fanConfig: axis title + legend filter hide the band-edge datasets", () => {
  const cfg = TLV.fanConfig(stubRisk(), FAN_DEPS);
  assert.equal(cfg.options.yAxisTitle, "Balance (real $)");
  assert.equal(typeof cfg.options.yTickFormatter, "function");

  // Legend filter keeps only borderWidth > 0 datasets (4 lines), hides 2 band edges.
  const filter = cfg.options.legend.labels.filter;
  const data = { datasets: cfg.datasets };
  const shown = cfg.datasets.filter((_, i) => filter({ datasetIndex: i }, data));
  assert.equal(shown.length, 4);
});

test("fanConfig: tooltip shows total p90/median/p10 then the 3 account medians", () => {
  const cfg = TLV.fanConfig(stubRisk(), FAN_DEPS);
  // tooltipFilter targets exactly the total-median dataset (its index in the array).
  const totalIndex = cfg.datasets.findIndex(
    (d) => d.label === "Total investments — median (real $)",
  );
  assert.ok(cfg.options.tooltipFilter({ datasetIndex: totalIndex }));
  assert.ok(!cfg.options.tooltipFilter({ datasetIndex: totalIndex + 1 }));

  // The tooltip rows at age index 1 (age 51): six aligned rows in order.
  const rows = cfg.options.tooltipLabel({ dataIndex: 1, datasetIndex: totalIndex });
  assert.equal(rows.length, 6);
  assert.match(rows[0], /^Total p90/);
  assert.match(rows[1], /^Total median/);
  assert.match(rows[2], /^Total p10/);
  assert.match(rows[3], /^Taxable/);
  assert.match(rows[4], /^Tax-deferred/);
  assert.match(rows[5], /^Roth/);
  // Values come from the stub at index 1.
  assert.match(rows[1], /2250000/);
  assert.match(rows[4], /1520000/);
});
