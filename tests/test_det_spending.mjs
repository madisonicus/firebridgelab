// test_det_spending.mjs — pin the DETERMINISTIC guardrails spending-line chart
// config builder (dsc1 sub-slice B: SpendingSeries.lineConfig in web/app.js).
//
// The deterministic mode draws a SINGLE guardrails spending line (the central
// return path's per-year spend), the deterministic analog of the Historical/MC
// spending fan. SpendingSeries.lineConfig(points, scenario) is the DOM-free Chart.js
// config builder behind it (mirroring TaxLocationView.fanConfig): one spending line
// dataset (index 0) from points[].spending, plus the SAME two guide lines the fan
// draws (SpendingSeries.guideLines), with the $-thousands axis options and the
// single-value (spending + GI) tooltip. These tests pin that contract WITHOUT a
// browser or Chart.js, the same node:vm sandbox harness test_spending_series.mjs /
// test_tax_location_view.mjs use.
//
// Run:
//   (from the repo root) node --test tests/test_det_spending.mjs
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

// Same loader trick as test_spending_series.mjs: eval app.js with NO `document` so
// its bottom-of-file init() guard is skipped, then inject a minimal `document` whose
// getElementById serves the spending-floor input we seed (so the live floor-knob
// read inside guideLines is exercisable). `floorInputs` maps element-id → value.
function loadSpendingSeries(floorInputs = {}) {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  // cssVar() reads chart colors off :root at render time; back it with the real
  // tokens parsed from styles.css. Safe to add before eval — it's only invoked
  // inside the render helpers the tests call, not at module load.
  ctx.getComputedStyle = makeGetComputedStyle();
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = {
    documentElement: {},
    getElementById: (id) =>
      Object.prototype.hasOwnProperty.call(floorInputs, id)
        ? { value: floorInputs[id] }
        : null,
  };
  return ctx.window.SpendingSeries;
}

// A small deterministic points array — three years with a prosperity ramp then a
// late cut, the shape the central guardrails path produces.
const POINTS = [
  { age: 60, spending: 130000 },
  { age: 61, spending: 145000 },
  { age: 62, spending: 110000 },
];

// Scenario with one active stream so GI is a clean number in the tooltip assertions.
const SCENARIO = {
  person_a_pension_amount: 0,
  person_a_pension_start_age: 60,
  person_a_ss_amount: 40000,
  person_a_ss_start_age: 67,
  person_b_pension_amount: 0,
  person_b_pension_start_age: 65,
  person_b_ss_amount: 0,
  person_b_ss_start_age: 70,
};

// ---------------------------------------------------------------------------
// lineConfig — labels + the single spending line.
// ---------------------------------------------------------------------------

test("lineConfig: labels are the points' ages", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  assert.deepEqual(cfg.labels, [60, 61, 62]);
});

test("lineConfig: dataset 0 is a single solid spending line from points[].spending", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  const line = cfg.datasets[0];
  // The spending line carries the per-year spend, the spending label/color, a
  // visible stroke, and is NOT a band fill.
  assert.deepEqual(line.data, [130000, 145000, 110000]);
  assert.equal(line.label, S.lineLabel);
  assert.equal(line.borderColor, S.border);
  assert.equal(line.fill, false);
  assert.ok(line.borderWidth > 0, "spending line has a visible stroke");
});

// ---------------------------------------------------------------------------
// lineConfig — the two guide lines, reused from SpendingSeries.guideLines.
// ---------------------------------------------------------------------------

test("lineConfig: appends exactly the two SpendingSeries guide lines after the line", () => {
  const S = loadSpendingSeries(); // default floor (85000)
  const cfg = S.lineConfig(POINTS, SCENARIO);
  // 1 spending line + 2 guide lines.
  assert.equal(cfg.datasets.length, 3);
  const [, oper, abs] = cfg.datasets;
  // Operational floor: amber dashed, flat at the default 85000, sized to n points.
  assert.equal(oper.borderColor, "#c2740a");
  assert.deepEqual(Array.from(oper.borderDash), [6, 4]);
  assert.equal(oper.fill, false);
  assert.equal(oper.data.length, POINTS.length);
  assert.ok(oper.data.every((v) => v === 85000));
  // Absolute/survivable floor: grey dashed, flat at 45000.
  assert.equal(abs.borderColor, "#9aa5b1");
  assert.deepEqual(Array.from(abs.borderDash), [3, 4]);
  assert.ok(abs.data.every((v) => v === 45000));
});

test("lineConfig: the operational guide line tracks a LIVE user-edited floor knob", () => {
  const S = loadSpendingSeries({ "spending-floor": "135000" });
  const cfg = S.lineConfig(POINTS, SCENARIO);
  const oper = cfg.datasets[1];
  assert.ok(oper.data.every((v) => v === 135000), "operational guide follows the edited floor");
  assert.equal(oper.label, "Floor ($135k)");
});

// ---------------------------------------------------------------------------
// Stress: the guide line must follow the EFFECTIVE (post-transform) floor, not
// the live knob. Under cost_of_living_shock the engine scales the floor ×1.20, so a
// dynamic strategy clamps spending to the PERTURBED floor — the guide line must
// draw + label THAT value, consistent with where the spending line floors.
// ---------------------------------------------------------------------------

test("lineConfig under stress: floor guide line uses opts.effectiveFloor, NOT the live knob", () => {
  // The live knob is still the un-perturbed $85k (selecting a stress must not
  // repaint the input). The cost-of-living-shock transform floors the run at $85k × 1.20 = $102k.
  const S = loadSpendingSeries({ "spending-floor": "85000" });
  // A dynamic spending path that the shocked world clamps at the perturbed floor:
  // every year sits AT $102k (the value the line actually floors against).
  const stressedPoints = [
    { age: 60, spending: 102000 },
    { age: 61, spending: 102000 },
    { age: 62, spending: 102000 },
  ];
  const cfg = S.lineConfig(stressedPoints, SCENARIO, { effectiveFloor: 102000 });
  const oper = cfg.datasets[1];
  // The guide line draws at the EFFECTIVE floor, overriding the live $85k knob.
  assert.ok(
    oper.data.every((v) => v === 102000),
    "guide line floors at the perturbed $102k, not the live $85k knob",
  );
  // And it LABELS the effective value (so it reads "Floor ($102k)" under the shock).
  assert.equal(oper.label, "Floor ($102k)");
  // Consistency check: the guide line sits exactly where the spending line floors
  // (the bug was a 17k gap between them). Compare element-wise — refLineDataset may
  // back its series with a typed array, so deepEqual on the container would trip on
  // the array TYPE even when every value agrees.
  const spendingLine = cfg.datasets[0];
  assert.equal(oper.data.length, spendingLine.data.length);
  for (let i = 0; i < spendingLine.data.length; i++) {
    assert.equal(
      Number(oper.data[i]),
      Number(spendingLine.data[i]),
      `floor guide line agrees with the floored spending line at index ${i}`,
    );
  }
});

test("lineConfig under stress: effectiveFloor == knob (Family A / ss_haircut) leaves the line correct", () => {
  // Family-A stresses + ss_haircut don't perturb the floor, so the effective floor
  // equals the live knob — the guide line must stay at $85k (don't break it).
  const S = loadSpendingSeries({ "spending-floor": "85000" });
  const cfg = S.lineConfig(POINTS, SCENARIO, { effectiveFloor: 85000 });
  const oper = cfg.datasets[1];
  assert.ok(oper.data.every((v) => v === 85000));
  assert.equal(oper.label, "Floor ($85k)");
});

// ---------------------------------------------------------------------------
// lineConfig — axis options + the single-value tooltip restricted to the line.
// ---------------------------------------------------------------------------

test("lineConfig: y-axis uses the SpendingSeries $-thousands axis options", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  assert.equal(cfg.options.yAxisTitle, "Spending (real $)");
  assert.equal(cfg.options.yTickFormatter(110000), "$110k");
});

test("lineConfig: tooltip is restricted to the spending line (datasetIndex 0)", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  // The flat guide lines must not surface a tooltip row.
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 0 }), true);
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 1 }), false);
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 2 }), false);
});

test("lineConfig: tooltipLabel returns the single-value spending + GI rows for the hovered age", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  // Hover the 2nd point (age 61, spend 145000). GI at 61 = 0 (Person A SS starts at 67).
  const rows = cfg.options.tooltipLabel({ dataIndex: 1, datasetIndex: 0 });
  assert.equal(rows.length, 2);
  assert.ok(rows[0].startsWith("Spending"));
  assert.ok(rows[0].includes("145,000"), `spending row carries 145,000: ${rows[0]}`);
  assert.ok(rows[1].startsWith("Guaranteed income"));
  // It's the SAME shape SpendingSeries.singleValueTooltipRows produces directly.
  const direct = S.singleValueTooltipRows(145000, 61, SCENARIO);
  assert.deepEqual(rows, direct);
});

// ---------------------------------------------------------------------------
// lineConfig — the GROSS-OUTFLOW line (tax-drag design td1, DECISION 7 —
// retires the ft2 net-of-tax line), opts.federalTax.
// ---------------------------------------------------------------------------

// A federal_tax block whose ages line up exactly with POINTS (60, 61, 62).
// td1 shape: gross_outflow == spending + tax (POINTS' own spending, since the
// fixture's spend matches the chart's own `spend` array); spending_tax +
// voluntary_tax + forced_tax == tax at every age (DECISION 6). Pre-75 ages ->
// voluntary_tax/forced_tax/penalty all 0 here (no lever, no RMD).
const FEDERAL_TAX = {
  ages: [60, 61, 62],
  tax: [12000, 13500, 9000],
  spending_tax: [12000, 13500, 9000],
  voluntary_tax: [0, 0, 0],
  forced_tax: [0, 0, 0],
  penalty: [0, 0, 0],
  gross_outflow: [142000, 158500, 119000], // POINTS.spending + tax, per age
};

// An RMD-era variant: age 61 carries a forced component (spending_tax 10000 +
// forced_tax 3500 == tax 13500), and age 62 carries a sub-ULP NEGATIVE forced
// value the engine deliberately doesn't clamp — the web's max(0,·) render
// guard must swallow it.
const FEDERAL_TAX_FORCED = {
  ages: [60, 61, 62],
  tax: [12000, 13500, 9000],
  spending_tax: [12000, 10000, 9000.000000001],
  voluntary_tax: [0, 0, 0],
  forced_tax: [0, 3500, -0.000000001],
  penalty: [0, 0, 0],
  gross_outflow: [142000, 158500, 119000],
};

// A lever-active variant: age 60 carries a voluntary component (spending_tax
// 10000 + voluntary_tax 2000 == tax 12000); age 61 fills further (spending_tax
// 10000 + voluntary_tax 3500 == tax 13500). Lever (<=74) and RMD (>=75) never
// co-occur (DECISION 6), so forced_tax stays 0 throughout this fixture.
const FEDERAL_TAX_VOLUNTARY = {
  ages: [60, 61, 62],
  tax: [12000, 13500, 9000],
  spending_tax: [10000, 10000, 9000],
  voluntary_tax: [2000, 3500, 0],
  forced_tax: [0, 0, 0],
  penalty: [0, 0, 0],
  gross_outflow: [142000, 158500, 119000],
};

// A penalty-bearing variant: age 60's spending_tax INCLUDES a $1,500 early-
// withdrawal penalty (a subset of spending_tax, not additional to it — DECISION
// 6: "spending_tax = tax(base) + penalty").
const FEDERAL_TAX_PENALTY = {
  ages: [60, 61, 62],
  tax: [12000, 13500, 9000],
  spending_tax: [12000, 13500, 9000],
  voluntary_tax: [0, 0, 0],
  forced_tax: [0, 0, 0],
  penalty: [1500, 0, 0],
  gross_outflow: [142000, 158500, 119000],
};

test("lineConfig: opts.federalTax ABSENT -> byte-identical to today (3 datasets, no gross line)", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO);
  assert.equal(cfg.datasets.length, 3);
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 1 }), false);
});

test("lineConfig: opts.federalTax present -> a 4th dataset (gross-outflow line) before the guide lines", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX });
  // 1 spending line + 1 gross-outflow line + 2 guide lines = 4.
  assert.equal(cfg.datasets.length, 4);
  const gross = cfg.datasets[1];
  assert.equal(gross.label, "What the portfolio funds (spending + tax)");
  assert.deepEqual(gross.data, [142000, 158500, 119000]);
  assert.equal(gross.fill, false);
  assert.ok(gross.borderWidth > 0, "gross-outflow line has a visible stroke");
  assert.equal(gross.borderColor, "#a3315c"); // --chart-tax-gross-line (light)
  // The guide lines still follow, now at indices 2/3.
  assert.equal(cfg.datasets[2].borderColor, "#c2740a"); // operational floor
  assert.equal(cfg.datasets[3].borderColor, "#9aa5b1"); // absolute floor
});

test("lineConfig: gross-outflow line is age-KEYED, not index-zipped (mismatched age range degrades gracefully)", () => {
  const S = loadSpendingSeries();
  // federalTax only covers ages 60-61 (missing 62) — a defensive edge case that
  // should never happen in practice (the engine's ages always match), but the
  // builder must not crash or misalign.
  const partial = {
    ages: [60, 61],
    tax: [12000, 13500],
    spending_tax: [12000, 13500],
    voluntary_tax: [0, 0],
    forced_tax: [0, 0],
    penalty: [0, 0],
    gross_outflow: [142000, 158500],
  };
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: partial });
  const gross = cfg.datasets[1];
  assert.deepEqual(gross.data, [142000, 158500, null]);
});

test("lineConfig: tooltip surfaces BOTH the spending line and the gross-outflow line", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX });
  // dataIndex 0 has a real (non-null) gross-outflow value in FEDERAL_TAX, so the
  // filter can pass on datasetIndex alone for the spending line, and on
  // datasetIndex+dataIndex (its null-guard) for the gross-outflow dataset.
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 0, dataIndex: 0 }), true, "spending line");
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 1, dataIndex: 0 }), true, "gross-outflow line");
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 2, dataIndex: 0 }), false, "guide line stays silent");
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 3, dataIndex: 0 }), false, "guide line stays silent");
});

test("lineConfig: gross-outflow tooltip decomposes — gross, spending, spending-tax only when no voluntary/forced", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX });
  // Hover the 2nd point (age 61): gross 158500, spending 145000, spending_tax
  // 13500, voluntary/forced both 0 -> NO voluntary/forced rows.
  const rows = cfg.options.tooltipLabel({ dataIndex: 1, datasetIndex: 1 });
  assert.equal(rows.length, 3);
  assert.ok(rows[0].startsWith("Portfolio funds"));
  assert.ok(rows[0].includes("158,500"), `gross row carries 158,500: ${rows[0]}`);
  assert.ok(rows[1].startsWith("Spending"));
  assert.ok(rows[1].includes("145,000"), `spending row carries 145,000: ${rows[1]}`);
  assert.ok(rows[2].startsWith("Federal tax — spending"));
  assert.ok(rows[2].includes("13,500"), `spending-tax row carries 13,500: ${rows[2]}`);
});

test("lineConfig: gross-outflow tooltip adds the voluntary-lever row when voluntary_tax > 0", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX_VOLUNTARY });
  // Hover age 61: spending_tax 10000 + voluntary_tax 3500.
  const rows = cfg.options.tooltipLabel({ dataIndex: 1, datasetIndex: 1 });
  assert.equal(rows.length, 4);
  assert.ok(rows[2].startsWith("Federal tax — spending"));
  assert.ok(rows[2].includes("10,000"), `spending-tax row carries 10,000: ${rows[2]}`);
  assert.ok(
    rows[3].startsWith("Federal tax — voluntary (bracket-fill lever)"),
    `voluntary row labelled: ${rows[3]}`,
  );
  assert.ok(rows[3].includes("3,500"), `voluntary-tax row carries 3,500: ${rows[3]}`);
});

test("lineConfig: gross-outflow tooltip adds the forced-RMD row when forced_tax > 0", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX_FORCED });
  // Hover age 61: spending_tax 10000 + forced_tax 3500.
  const rows = cfg.options.tooltipLabel({ dataIndex: 1, datasetIndex: 1 });
  assert.equal(rows.length, 4);
  assert.ok(rows[2].startsWith("Federal tax — spending"));
  assert.ok(rows[2].includes("10,000"), `spending-tax row carries 10,000: ${rows[2]}`);
  assert.ok(
    rows[3].startsWith("Federal tax — forced (RMD, not a spending cut)"),
    `forced row labelled "not a spending cut": ${rows[3]}`,
  );
  assert.ok(rows[3].includes("3,500"), `forced-tax row carries 3,500: ${rows[3]}`);
});

test("lineConfig: gross-outflow tooltip calls out the early-withdrawal penalty when nonzero", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX_PENALTY });
  // Hover age 60: penalty 1500 (a SUBSET of spending_tax 12000, not additive).
  const rows = cfg.options.tooltipLabel({ dataIndex: 0, datasetIndex: 1 });
  assert.equal(rows.length, 4);
  assert.ok(
    rows[3].startsWith("Includes early-withdrawal penalty"),
    `penalty row labelled: ${rows[3]}`,
  );
  assert.ok(rows[3].includes("1,500"), `penalty row carries 1,500: ${rows[3]}`);
});

test("lineConfig: sub-ULP NEGATIVE forced_tax is guarded max(0,·) — no forced row", () => {
  const S = loadSpendingSeries();
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: FEDERAL_TAX_FORCED });
  // Hover age 62: forced_tax is -1e-9 (the engine deliberately doesn't clamp);
  // the web render guard clamps to 0, so the forced row must NOT appear and
  // nothing renders a negative dollar figure.
  const rows = cfg.options.tooltipLabel({ dataIndex: 2, datasetIndex: 1 });
  assert.equal(rows.length, 3);
  for (const row of rows) {
    assert.ok(!row.includes("-$"), `no negative money in: ${row}`);
  }
});

test("lineConfig: a null (mismatched-age) gross-outflow point never surfaces a tooltip row", () => {
  const S = loadSpendingSeries();
  const partial = {
    ages: [60, 61],
    tax: [12000, 13500],
    spending_tax: [12000, 13500],
    voluntary_tax: [0, 0],
    forced_tax: [0, 0],
    penalty: [0, 0],
    gross_outflow: [142000, 158500],
  };
  const cfg = S.lineConfig(POINTS, SCENARIO, { federalTax: partial });
  // Age 62 (dataIndex 2) has no matching federal_tax row -> filtered out, not
  // handed to tooltipLabel (which would otherwise crash formatting null).
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 1, dataIndex: 2 }), false);
  assert.equal(cfg.options.tooltipFilter({ datasetIndex: 1, dataIndex: 0 }), true);
});

// ---------------------------------------------------------------------------
// flowOverlay (slice 1) — the spending (solid) + GI-floor (dashed)
// series that ride a SECONDARY right-hand axis on #trajectory-chart-a in the
// Deterministic single view. Pulled per-point off spending / guaranteed_income.
// ---------------------------------------------------------------------------

// Points carrying BOTH spending + guaranteed_income, including the depletion
// "forced onto the floor" moment (age 62 spend drops to the GI level).
const FLOW_POINTS = [
  { age: 60, spending: 110000, guaranteed_income: 0 },
  { age: 61, spending: 110000, guaranteed_income: 40500 },
  { age: 62, spending: 98000, guaranteed_income: 98000 },
];

test("flowOverlay: two datasets — spending (solid) + GI floor (dashed), both on the secondary axis", () => {
  const S = loadSpendingSeries();
  const o = S.flowOverlay(FLOW_POINTS);
  assert.equal(o.datasets.length, 2);

  const [spend, gi] = o.datasets;
  // Annual spending: SOLID (no borderDash), warm-orange token, on yFlows.
  assert.equal(spend.label, "Annual spending");
  assert.deepEqual(spend.data, [110000, 110000, 98000]);
  assert.equal(spend.yAxisID, "yFlows");
  assert.equal(spend.borderColor, "#e8833a"); // --chart-spending-line (light)
  assert.equal(spend.borderDash, undefined);
  assert.equal(spend.fill, false);
  assert.equal(spend.borderWidth, 2);

  // Guaranteed-income floor: DASHED [6,4], teal/green token, on yFlows. Falls to
  // meet the spending line at depletion (both 98000 at age 62).
  assert.equal(gi.label, "Guaranteed-income floor");
  assert.deepEqual(gi.data, [0, 40500, 98000]);
  assert.equal(gi.yAxisID, "yFlows");
  assert.equal(gi.borderColor, "#2ea3a3"); // --chart-gi-floor (light)
  assert.deepEqual(Array.from(gi.borderDash), [6, 4]);
  assert.equal(gi.fill, false);
});

test("flowOverlay: secondaryYAxis is the $Nk right-hand 'Annual flows' ruler with 1.2x headroom", () => {
  const S = loadSpendingSeries();
  const o = S.flowOverlay(FLOW_POINTS);
  const ax = o.secondaryYAxis;
  assert.equal(ax.id, "yFlows");
  assert.equal(ax.title, "Annual flows ($/yr)");
  // suggestedMax = max spending (110000) * 1.2.
  assert.equal(ax.suggestedMax, 110000 * 1.2);
  // Ticks render as $Nk.
  assert.equal(ax.tickFormatter(132000), "$132k");
  assert.equal(ax.tickFormatter(40500), "$41k");
});
