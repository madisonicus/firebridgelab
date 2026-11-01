// test_spending_series.mjs — pin the SpendingSeries presentation helper (dsc1
// sub-slice A: the refactor-extracted spending-series semantics in web/app.js).
//
// SpendingSeries owns the spending chart's PRESENTATION glue — the two guide-line
// datasets (operational floor off the live spending_floor knob + the $45k
// absolute/survivable floor), the $-thousands y-axis options, the color/labels,
// and the single-value tooltip the deterministic line (sub-slice B) will use.
// These tests pin that contract WITHOUT a browser or Chart.js: app.js (plus its
// chart_view.js dep) is read off disk and run in a node:vm sandbox, the same way
// test_chart_explainers.mjs does it, with a MINIMAL `document` stub so the live
// floor-knob read (readGuardrailNumber → document.getElementById) is exercisable.
//
// Run:
//   (from the repo root) node --test tests/test_spending_series.mjs
//
// Zero npm deps; Node >= 18 (node:test + node:assert/strict).

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makeGetComputedStyle } from "./_css_tokens.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js", "scenario_store.js", "ss_claim.js"].map((f) =>
  path.join(WEB_DIR, f),
);
const APP_PATH = path.join(WEB_DIR, "app.js");

// Load app.js into a sandbox with a window stub and NO `document` at module-eval
// time — so app.js's bottom-of-file `if (typeof document !== "undefined" ...)`
// init() guard is FALSE and init() is skipped (the same trick test_chart_
// explainers.mjs relies on). AFTER eval we inject a minimal `document` onto the
// sandbox global, whose getElementById serves the spending-floor input we seed, so
// the helper's LIVE floor-knob read (readGuardrailNumber → document.getElementById,
// resolved at call time off the global) is exercisable. `floorInputs` maps
// element-id → value; ids not present return null (readGuardrailNumber then falls
// back to its default — exactly the live behavior with an empty/missing field).
function loadSpendingSeries(floorInputs = {}) {
  const ctx = makeCtx(floorInputs);
  return ctx.window.SpendingSeries;
}

// Same sandbox, but returns the whole window so a test can reach BOTH
// SpendingSeries (the config builder) AND ChartView (the line-family factory that
// wires the tooltip filter). The det-spending dup bug lived in
// ChartView.buildLineOptions silently dropping tooltipFilter — so the regression
// test must exercise the REAL lineChart path, not just lineConfig's declared
// option. capturedChartConfig holds the last Chart(...) config so the test can
// read the RESOLVED Chart.js options (tooltip.filter / tooltip.callbacks.label).
function loadWindow(floorInputs = {}) {
  return makeCtx(floorInputs);
}

let capturedChartConfig = null;

function makeCtx(floorInputs = {}) {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  // Chart stub records the config it's handed (type/data/options) so a test can
  // assert on the options ChartView actually built (incl. the wired tooltip.filter).
  ctx.Chart = function (_ctx2d, config) {
    capturedChartConfig = config;
    return {};
  };
  // cssVar() reads chart colors off :root at render time; back it with the real
  // tokens parsed from styles.css. Safe before eval — only invoked inside the
  // render helpers the tests call, not at module load.
  ctx.getComputedStyle = makeGetComputedStyle();
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  // Inject document now that init() has been skipped. readGuardrailNumber reads
  // `document` lazily from the global scope, so this drives the live floor read.
  // documentElement backs cssVar()'s getComputedStyle(document.documentElement).
  ctx.document = {
    documentElement: {},
    getElementById: (id) =>
      Object.prototype.hasOwnProperty.call(floorInputs, id)
        ? { value: floorInputs[id] }
        : null,
  };
  return ctx;
}

// Simulate Chart.js interaction.mode "index" tooltip-BODY emission for a resolved
// Chart.js options object + dataset list at a hovered point index. Chart.js, under
// "index" mode, makes ALL datasets active at the hovered x, runs the tooltip
// `filter` to drop items, then calls the body `label` callback ONCE PER SURVIVING
// item and concatenates the returned strings (a callback may return an array). This
// is exactly the path that duplicated Spending/GI once per guide line. Returns the
// flat array of body row strings the tooltip would render.
function simulateIndexTooltipBody(options, datasets, dataIndex) {
  const tt = options.plugins.tooltip;
  const items = datasets.map((ds, datasetIndex) => ({
    datasetIndex,
    dataIndex,
    dataset: ds,
    label: String(dataIndex),
    parsed: { y: ds.data[dataIndex] },
  }));
  const filtered = tt.filter ? items.filter((it) => tt.filter(it)) : items;
  const rows = [];
  for (const it of filtered) {
    const out = tt.callbacks.label(it);
    if (Array.isArray(out)) rows.push(...out);
    else if (out != null) rows.push(out);
  }
  return rows;
}

// ---------------------------------------------------------------------------
// guideLines(n) — the two reference-line datasets.
// ---------------------------------------------------------------------------

test("guideLines(n) returns exactly two fill:false reference datasets", () => {
  const SpendingSeries = loadSpendingSeries();
  const lines = SpendingSeries.guideLines(5);
  assert.equal(lines.length, 2);
  for (const ds of lines) {
    assert.equal(ds.fill, false, "guide line must not participate in band fill");
  }
});

test("guideLines: colors + dashes match the fan's spending guide lines", () => {
  const SpendingSeries = loadSpendingSeries();
  const [oper, abs] = SpendingSeries.guideLines(3);
  // Operational floor: amber, [6,4] dash.
  assert.equal(oper.borderColor, "#c2740a");
  assert.deepEqual(Array.from(oper.borderDash), [6, 4]);
  // Absolute/survivable floor: grey, [3,4] dash.
  assert.equal(abs.borderColor, "#9aa5b1");
  assert.deepEqual(Array.from(abs.borderDash), [3, 4]);
});

test("guideLines: each line is n points all equal to its floor level (default floor)", () => {
  const SpendingSeries = loadSpendingSeries(); // no input seeded → default floor
  const n = 7;
  const [oper, abs] = SpendingSeries.guideLines(n);
  // Operational floor defaults to GUARDRAIL_DEFAULTS.floor === 85000 when the
  // knob is absent/empty.
  assert.equal(oper.data.length, n);
  assert.ok(oper.data.every((v) => v === 85000), "operational line flat at default 85000");
  // Absolute floor is the fixed $45k survivable level.
  assert.equal(abs.data.length, n);
  assert.ok(abs.data.every((v) => v === 45000), "absolute line flat at 45000");
  // Labels carry the $-thousands-formatted level.
  assert.equal(oper.label, "Floor ($85k)");
  assert.equal(abs.label, "Absolute floor (survivable, $45k)");
});

test("guideLines: operational line tracks a LIVE user-edited floor knob", () => {
  // Seed the spending-floor input id with a non-default value; the operational
  // line must follow it (read fresh each call), proving the live-knob read.
  const SpendingSeries = loadSpendingSeries({ "spending-floor": "135000" });
  const [oper] = SpendingSeries.guideLines(4);
  assert.ok(oper.data.every((v) => v === 135000), "operational line follows the edited floor");
  assert.equal(oper.label, "Floor ($135k)");
});

test("absoluteFloor is reachable on the helper and equals 45000", () => {
  const SpendingSeries = loadSpendingSeries();
  assert.equal(SpendingSeries.absoluteFloor, 45000);
});

// ---------------------------------------------------------------------------
// axisOptions / color + labels — the spending-series semantics.
// ---------------------------------------------------------------------------

test("axisOptions exposes the spending y-axis title + $-thousands formatter", () => {
  const SpendingSeries = loadSpendingSeries();
  const { yAxisTitle, yTickFormatter } = SpendingSeries.axisOptions();
  assert.equal(yAxisTitle, "Spending (real $)");
  assert.equal(typeof yTickFormatter, "function");
  assert.equal(yTickFormatter(110000), "$110k");
  assert.equal(yTickFormatter(45000), "$45k");
});

test("color/labels conventions are exposed for fan + line consumers", () => {
  const SpendingSeries = loadSpendingSeries();
  assert.equal(SpendingSeries.medianLabel, "Median spending (real $)");
  assert.equal(typeof SpendingSeries.rgb, "string");
  assert.equal(typeof SpendingSeries.border, "string");
});

// ---------------------------------------------------------------------------
// singleValueTooltipRows — the deterministic-line tooltip (sub-slice B prep).
// ---------------------------------------------------------------------------

test("singleValueTooltipRows returns the 2-row aligned spending + GI shape", () => {
  const SpendingSeries = loadSpendingSeries();
  // Scenario with one active stream at the hovered age so GI is a clean number.
  const scenario = {
    person_a_pension_amount: 0,
    person_a_pension_start_age: 60,
    person_a_ss_amount: 40000,
    person_a_ss_start_age: 67,
    person_b_pension_amount: 0,
    person_b_pension_start_age: 65,
    person_b_ss_amount: 0,
    person_b_ss_start_age: 70,
  };
  const rows = SpendingSeries.singleValueTooltipRows(110000, 70, scenario);
  // Two rows: Spending, Guaranteed income.
  assert.equal(rows.length, 2);
  rows.forEach((r) => assert.equal(typeof r, "string"));
  assert.ok(rows[0].startsWith("Spending"));
  assert.ok(rows[1].startsWith("Guaranteed income"));
  // GI at 70 = the only active stream (Person A SS, on at 67) = $40,000.
  assert.ok(rows[1].includes("40,000"), `GI row carries 40,000: ${rows[1]}`);
});

// ---------------------------------------------------------------------------
// REGRESSION: the deterministic spending chart's tooltip must show
// EXACTLY ONE Spending row + ONE Guaranteed-income row, regardless of how many
// flat guide lines (floor / absolute-floor / ceiling) are present. Under
// interaction.mode "index" the body callback fires once per active dataset, so
// the only thing keeping the Spending/GI pair from repeating once-per-guide-line
// is ChartView wiring the lineConfig's tooltipFilter (datasetIndex === 0) into
// Chart.js's tooltip.filter. The bug: buildLineOptions silently dropped that
// filter (it wired label/title/footer but not filter), so the pair repeated 3x
// (2 guide lines) / 4x (amortization's 3 guide lines). This test drives the REAL
// ChartView.lineChart path + simulates index-mode emission, so it fails if the
// filter is ever dropped again.
// ---------------------------------------------------------------------------

const DET_SCENARIO = {
  person_a_pension_amount: 0,
  person_a_pension_start_age: 60,
  person_a_ss_amount: 40000,
  person_a_ss_start_age: 67,
  person_b_pension_amount: 0,
  person_b_pension_start_age: 65,
  person_b_ss_amount: 0,
  person_b_ss_start_age: 70,
};

const DET_POINTS = [
  { age: 68, spending: 110000 },
  { age: 69, spending: 112000 },
  { age: 70, spending: 108000 },
];

function buildDetTooltipRows(opts) {
  capturedChartConfig = null;
  const ctx = loadWindow();
  const { SpendingSeries, ChartView } = ctx.window;
  const cfg = SpendingSeries.lineConfig(DET_POINTS, DET_SCENARIO, opts);
  // Drive the REAL factory so the wired tooltip.filter is exercised (the bug site).
  ChartView.lineChart({ getContext: () => ({}) }, cfg);
  assert.ok(capturedChartConfig, "Chart should have been instantiated");
  const options = capturedChartConfig.options;
  const datasets = capturedChartConfig.data.datasets;
  // Hover the middle age (dataIndex 1).
  return { datasets, rows: simulateIndexTooltipBody(options, datasets, 1) };
}

function countRows(rows, prefix) {
  return rows.filter((r) => r.startsWith(prefix)).length;
}

test("det-spending tooltip: ONE Spending + ONE GI with 2 guide lines (clamped modes)", () => {
  const { datasets, rows } = buildDetTooltipRows({});
  // 1 spending line + 2 guide lines (floor + absolute floor).
  assert.equal(datasets.length, 3, "clamped: spending line + 2 guide lines");
  assert.equal(countRows(rows, "Spending"), 1, `exactly one Spending row: ${JSON.stringify(rows)}`);
  assert.equal(
    countRows(rows, "Guaranteed income"),
    1,
    `exactly one Guaranteed income row: ${JSON.stringify(rows)}`,
  );
  assert.equal(rows.length, 2, `tooltip body is exactly the 2 rows: ${JSON.stringify(rows)}`);
});

test("det-spending tooltip: ONE Spending + ONE GI with 3 guide lines (amortization + ceiling)", () => {
  // The amortization frontier passes a ceiling, adding a 3rd guide line — the
  // change that surfaced the pre-existing dup as 3x → 4x.
  const { datasets, rows } = buildDetTooltipRows({ ceiling: 150000 });
  // 1 spending line + 3 guide lines (floor + absolute floor + ceiling).
  assert.equal(datasets.length, 4, "amortization: spending line + 3 guide lines");
  assert.equal(countRows(rows, "Spending"), 1, `exactly one Spending row: ${JSON.stringify(rows)}`);
  assert.equal(
    countRows(rows, "Guaranteed income"),
    1,
    `exactly one Guaranteed income row: ${JSON.stringify(rows)}`,
  );
  assert.equal(rows.length, 2, `tooltip body is exactly the 2 rows: ${JSON.stringify(rows)}`);
});

test("det-spending tooltip: ChartView wires lineConfig's tooltipFilter into Chart.js", () => {
  // Direct guard on the bug SITE: buildLineOptions must install tooltip.filter.
  capturedChartConfig = null;
  const ctx = loadWindow();
  const { SpendingSeries, ChartView } = ctx.window;
  const cfg = SpendingSeries.lineConfig(DET_POINTS, DET_SCENARIO, {});
  ChartView.lineChart({ getContext: () => ({}) }, cfg);
  const filter = capturedChartConfig.options.plugins.tooltip.filter;
  assert.equal(typeof filter, "function", "lineChart must wire a tooltip.filter");
  assert.equal(filter({ datasetIndex: 0 }), true, "spending line passes the filter");
  assert.equal(filter({ datasetIndex: 1 }), false, "a guide line is filtered out");
});

// ---------------------------------------------------------------------------
// flowOverlay(points, { spending }) — the flow-overlay builder.
// ---------------------------------------------------------------------------

// Minimal per-age flows: GI turns on partway through (the pension-start jump).
const FLOW_POINTS = [
  { age: 60, spending: 110000, guaranteed_income: 0 },
  { age: 61, spending: 110000, guaranteed_income: 0 },
  { age: 62, spending: 110000, guaranteed_income: 24000 },
  { age: 63, spending: 110000, guaranteed_income: 24000 },
];

test("flowOverlay(points): default emits BOTH datasets — spending + GI floor (slice 1, byte-identical)", () => {
  const SpendingSeries = loadSpendingSeries();
  const overlay = SpendingSeries.flowOverlay(FLOW_POINTS);
  assert.equal(overlay.datasets.length, 2, "spending line + GI floor");
  // Slice-1 order: spending first (datasetIndex 0), GI floor second.
  const [spend, gi] = overlay.datasets;
  assert.equal(spend.label, "Annual spending");
  assert.deepEqual(spend.data, [110000, 110000, 110000, 110000]);
  assert.equal(spend.yAxisID, "yFlows");
  assert.equal(gi.label, "Guaranteed-income floor");
  assert.deepEqual(gi.data, [0, 0, 24000, 24000]);
  assert.equal(gi.yAxisID, "yFlows");
  assert.deepEqual(Array.from(gi.borderDash), [6, 4], "GI floor is dashed");
  // The secondary-axis config the fan / line chart consumes.
  assert.equal(overlay.secondaryYAxis.id, "yFlows");
  assert.equal(overlay.secondaryYAxis.title, "Annual flows ($/yr)");
  assert.equal(overlay.secondaryYAxis.suggestedMax, 110000 * 1.2);
});

test("flowOverlay(points, { spending:false }): GI floor ONLY, still on the secondary axis ", () => {
  const SpendingSeries = loadSpendingSeries();
  const overlay = SpendingSeries.flowOverlay(FLOW_POINTS, { spending: false });
  // Path-dependent spending modes (guardrails / FR / amortization) draw the GI
  // floor only — no single spending line (a separate spending fan exists).
  assert.equal(overlay.datasets.length, 1, "GI floor only");
  const [gi] = overlay.datasets;
  assert.equal(gi.label, "Guaranteed-income floor");
  assert.equal(gi.yAxisID, "yFlows");
  assert.deepEqual(gi.data, [0, 0, 24000, 24000]);
  // The secondary axis is still emitted (the GI line rides it).
  assert.equal(overlay.secondaryYAxis.id, "yFlows");
  assert.equal(overlay.secondaryYAxis.title, "Annual flows ($/yr)");
});

test("flowOverlay(points, { spending:true }) matches the no-arg default (explicit == implicit)", () => {
  const SpendingSeries = loadSpendingSeries();
  const implicit = SpendingSeries.flowOverlay(FLOW_POINTS);
  const explicit = SpendingSeries.flowOverlay(FLOW_POINTS, { spending: true });
  assert.equal(explicit.datasets.length, implicit.datasets.length);
  assert.deepEqual(
    explicit.datasets.map((d) => d.label),
    implicit.datasets.map((d) => d.label),
  );
});

// ---------------------------------------------------------------------------
// grossOutflowMedianDataset (tax-drag design td1, DECISION 7) — the Historical/
// MC spending FAN's gross-outflow-median line + tooltip-row builder. Retires
// the ft2 net-of-tax median line. Dynamic-spending-only surface (guardrails /
// funded-ratio / amortization), so `spendingBands` here is always the
// median-of-paths spend fan (never constant/lifecycle).
// ---------------------------------------------------------------------------

const SPENDING_BANDS = [
  { age: 60, median: 100000, p10: 85000, p90: 115000 },
  { age: 61, median: 105000, p10: 88000, p90: 125000 },
  { age: 62, median: 95000, p10: 80000, p90: 110000 },
];

// A federal_tax AGGREGATE block whose ages line up with SPENDING_BANDS. No
// aggregate carries a penalty median (FederalTaxOverlayAggregate has none).
const FEDERAL_TAX_AGG = {
  ages: [60, 61, 62],
  tax_median: [12000, 13500, 9000],
  voluntary_tax_median: [0, 0, 0],
  forced_tax_median: [0, 0, 0],
  gross_outflow_median: [142000, 158500, 119000],
};

const FEDERAL_TAX_AGG_LEVER = {
  ages: [60, 61, 62],
  tax_median: [12000, 13500, 9000],
  voluntary_tax_median: [2000, 3500, 0],
  forced_tax_median: [0, 0, 0],
  gross_outflow_median: [142000, 158500, 119000],
};

const FEDERAL_TAX_AGG_RMD = {
  ages: [60, 61, 62],
  tax_median: [12000, 13500, 9000],
  voluntary_tax_median: [0, 0, 0],
  forced_tax_median: [0, 3500, -0.000000001],
  gross_outflow_median: [142000, 158500, 119000],
};

test("grossOutflowMedianDataset: absent/malformed federalTax -> null", () => {
  const SpendingSeries = loadSpendingSeries();
  assert.equal(SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, null), null);
  assert.equal(SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, undefined), null);
  assert.equal(SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, {}), null);
});

test("grossOutflowMedianDataset: dataset carries the gross-outflow-median series + the gross-line color", () => {
  const SpendingSeries = loadSpendingSeries();
  const cfg = SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, FEDERAL_TAX_AGG);
  assert.equal(cfg.dataset.label, "Portfolio funds, median (spending + tax)");
  assert.deepEqual(cfg.dataset.data, [142000, 158500, 119000]);
  assert.equal(cfg.dataset.fill, false);
  assert.ok(cfg.dataset.borderWidth > 0, "visible stroke");
  assert.equal(cfg.dataset.borderColor, "#a3315c"); // --chart-tax-gross-line (light)
  assert.deepEqual(cfg.grossByIdx, [142000, 158500, 119000]);
});

test("grossOutflowMedianDataset: age-KEYED, not index-zipped (mismatched age range degrades gracefully)", () => {
  const SpendingSeries = loadSpendingSeries();
  const partial = {
    ages: [60, 61],
    tax_median: [12000, 13500],
    voluntary_tax_median: [0, 0],
    forced_tax_median: [0, 0],
    gross_outflow_median: [142000, 158500],
  };
  const cfg = SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, partial);
  assert.deepEqual(cfg.dataset.data, [142000, 158500, null]);
});

test("tooltipRows: gross + spending-median + tax-median only, when voluntary/forced are both 0", () => {
  const SpendingSeries = loadSpendingSeries();
  const cfg = SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, FEDERAL_TAX_AGG);
  // Age 61 (index 1): gross 158500, spend median 105000, tax median 13500.
  const rows = cfg.tooltipRows(1, SPENDING_BANDS[1].median);
  assert.equal(rows.length, 3);
  assert.ok(rows[0].startsWith("Portfolio funds, median"));
  assert.ok(rows[0].includes("158,500"));
  assert.ok(rows[1].startsWith("Spending, median"));
  assert.ok(rows[1].includes("105,000"));
  assert.ok(rows[2].startsWith("Federal tax, median"));
  assert.ok(rows[2].includes("13,500"));
});

test("tooltipRows: voluntary-lever median row appears when nonzero", () => {
  const SpendingSeries = loadSpendingSeries();
  const cfg = SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, FEDERAL_TAX_AGG_LEVER);
  const rows = cfg.tooltipRows(1, SPENDING_BANDS[1].median);
  assert.equal(rows.length, 4);
  assert.ok(
    rows[3].startsWith("Voluntary (bracket-fill lever), median"),
    `voluntary row labelled: ${rows[3]}`,
  );
  assert.ok(rows[3].includes("3,500"));
});

test("tooltipRows: forced-RMD median row appears when nonzero, sub-ULP negative guarded", () => {
  const SpendingSeries = loadSpendingSeries();
  const cfg = SpendingSeries.grossOutflowMedianDataset(SPENDING_BANDS, FEDERAL_TAX_AGG_RMD);
  const rowsAge61 = cfg.tooltipRows(1, SPENDING_BANDS[1].median);
  assert.equal(rowsAge61.length, 4);
  assert.ok(
    rowsAge61[3].startsWith("Forced (RMD, not a spending cut), median"),
    `forced row labelled: ${rowsAge61[3]}`,
  );
  assert.ok(rowsAge61[3].includes("3,500"));
  // Age 62: forced_tax_median is -1e-9 (engine deliberately doesn't clamp) ->
  // guarded to 0, no forced row, no negative dollar anywhere.
  const rowsAge62 = cfg.tooltipRows(2, SPENDING_BANDS[2].median);
  assert.equal(rowsAge62.length, 3);
  for (const row of rowsAge62) assert.ok(!row.includes("-$"));
});
