// test_tax_gains_fraction.mjs — pin the taxable-gains-fraction knob (federal-tax
// overlay design ft1, DECISION 4): web/app.js readTaxableGainsFraction /
// setTaxGainsFractionInput + the TAX_GAINS_FRACTION_DEFAULT constant.
//
// The knob is a single flat percent input (#tax-gains-fraction, default 50%)
// riding alongside the tb1 tax-location balances: same reveal/gate, but its own
// axis (not part of the running-sum check). readTaxableGainsFraction() parses
// the percent input into a 0..1 fraction for the API, clamping out-of-range
// input rather than throwing (mirroring readCorrelation's discipline) —
// exercised here against a minimal fake `document` (same technique as
// test_det_spending.mjs's floorInputs), since the function is a thin DOM read
// with no Chart.js dependency.
//
// Run:
//   (from the repo root) node --test tests/test_tax_gains_fraction.mjs

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

// Load app.js with NO document (init() skipped), then inject a minimal
// `document` whose getElementById serves #tax-gains-fraction as a STABLE
// cached element (not rebuilt per call, so a write from setTaxGainsFractionInput
// persists for a subsequent read — same discipline as the byId Map in
// test_scenario_store.mjs / test_reset_defaults.mjs's fake DOM). `value`
// seeds the element's initial value; pass `null` to simulate the element
// being ABSENT from the DOM entirely (the reduced-test-DOM case).
function loadApp(value = "") {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  const el = value === null ? null : { value };
  ctx.document = {
    documentElement: {},
    getElementById: (id) => (id === "tax-gains-fraction" ? el : null),
  };
  return ctx;
}

// ---------------------------------------------------------------------------
// readTaxableGainsFraction — percent input -> fraction, with the default
// fallback + clamp discipline.
// ---------------------------------------------------------------------------

test("readTaxableGainsFraction: default (blank input) -> 0.5", () => {
  const ctx = loadApp("");
  assert.equal(vm.runInContext("readTaxableGainsFraction()", ctx), 0.5);
});

test("readTaxableGainsFraction: element absent (reduced test DOM) -> 0.5", () => {
  const ctx = loadApp(null); // getElementById returns null for the id
  assert.equal(vm.runInContext("readTaxableGainsFraction()", ctx), 0.5);
});

test("readTaxableGainsFraction: parses a percent into a fraction", () => {
  const ctx = loadApp("35");
  assert.equal(vm.runInContext("readTaxableGainsFraction()", ctx), 0.35);
});

test("readTaxableGainsFraction: 0% and 100% are valid, not treated as blank", () => {
  assert.equal(vm.runInContext("readTaxableGainsFraction()", loadApp("0")), 0);
  assert.equal(vm.runInContext("readTaxableGainsFraction()", loadApp("100")), 1);
});

test("readTaxableGainsFraction: NaN input -> 0.5 default (no throw)", () => {
  const ctx = loadApp("abc");
  assert.equal(vm.runInContext("readTaxableGainsFraction()", ctx), 0.5);
});

test("readTaxableGainsFraction: clamps out-of-range input instead of throwing", () => {
  assert.equal(vm.runInContext("readTaxableGainsFraction()", loadApp("150")), 1);
  assert.equal(vm.runInContext("readTaxableGainsFraction()", loadApp("-20")), 0);
});

// ---------------------------------------------------------------------------
// setTaxGainsFractionInput — the inverse (fraction -> percent-string write),
// used by applyScenarioToForm to restore a save / the reset default.
// ---------------------------------------------------------------------------

test("setTaxGainsFractionInput: writes a fraction as a whole percent string", () => {
  const ctx = loadApp("");
  vm.runInContext("setTaxGainsFractionInput(0.35)", ctx);
  assert.equal(ctx.document.getElementById("tax-gains-fraction").value, "35");
});

test("setTaxGainsFractionInput: round-trips the 50% default", () => {
  const ctx = loadApp("");
  vm.runInContext("setTaxGainsFractionInput(TAX_GAINS_FRACTION_DEFAULT)", ctx);
  assert.equal(ctx.document.getElementById("tax-gains-fraction").value, "50");
});

// ---------------------------------------------------------------------------
// The default constant itself — 50%, matching the engine/API default (DECISION 4).
// ---------------------------------------------------------------------------

test("TAX_GAINS_FRACTION_DEFAULT is 0.5 (50%)", () => {
  const ctx = loadApp("");
  assert.equal(vm.runInContext("TAX_GAINS_FRACTION_DEFAULT", ctx), 0.5);
});

// ---------------------------------------------------------------------------
// Markup + source wiring — static checks, same idiom as
// test_tax_location_default_split.mjs (the live DOM behavior is the
// integrator's Playwright gate, not this DOM-free sandbox).
// ---------------------------------------------------------------------------

test("markup: the gains-fraction input sits inside the tax-location reveal, 0..100 range", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  const idx = html.indexOf('id="tax-gains-fraction"');
  assert.ok(idx !== -1, "tax-gains-fraction input present");
  const tag = html.slice(html.lastIndexOf("<input", idx), html.indexOf(">", idx) + 1);
  assert.match(tag, /min="0"/);
  assert.match(tag, /max="100"/);
  assert.match(tag, /value="50"/);
  // It sits between the reveal's opening and the buckets-mode gate note (the
  // reveal's next sibling once it closes), i.e. inside #tax-location-reveal
  // alongside the three balances + the sum helper.
  const revealStart = html.indexOf('id="tax-location-reveal"');
  const gateNoteIdx = html.indexOf('id="tax-location-gate-note"', revealStart);
  assert.ok(
    revealStart < idx && idx < gateNoteIdx,
    "gains-fraction input is inside #tax-location-reveal",
  );
});

test("source: readTaxableGainsFraction clamps to [0, 1]", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const fn = src.slice(
    src.indexOf("function readTaxableGainsFraction("),
    src.indexOf("function setTaxGainsFractionInput("),
  );
  assert.match(fn, /Math\.max\(0, Math\.min\(1, n \/ 100\)\)/);
});
