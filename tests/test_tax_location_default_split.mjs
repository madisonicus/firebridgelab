// test_tax_location_default_split.mjs — pin the tax-location DEFAULT-SPLIT
// convenience feature (tax-buckets tb1b): the "Model accounts by tax location"
// enable checkbox + its pre-fill math.
//
// The pre-fill math is the pure TaxLocationView.defaultSplit(P): taxable rounds to
// 25% of P, tax-deferred to 60%, and ROTH IS THE EXACT-SUM BALANCER
// (roth = P − taxable − tax_deferred) so the three always sum to EXACTLY P — no $1
// rounding drift that would trip the sum-validation. That's exercised directly in
// the node:vm sandbox (window stub, no document), like test_tax_location_view.mjs.
//
// The checkbox WIRING (check → pre-fill, uncheck → clear, indicator sync, gate
// disables it under buckets, cache-bust) is asserted statically against the markup
// + source — the live DOM behaviour is the integrator's Playwright gate, not this
// DOM-free sandbox.
//
// Run:
//   (from the repo root) node --test tests/test_tax_location_default_split.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js"].map((f) =>
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
// defaultSplit — the pre-fill math. The three values ALWAYS sum to EXACTLY P
// (Roth is the balancer), with taxable≈25% / tax-deferred≈60% / roth≈15%.
// ---------------------------------------------------------------------------

test("defaultSplit: P=1,800,000 → 450,000 / 1,080,000 / 270,000 (the pinned default split)", () => {
  const s = TLV.defaultSplit(1_800_000);
  assert.equal(s.taxable, 450_000);
  assert.equal(s.tax_deferred, 1_080_000);
  assert.equal(s.roth, 270_000);
  assert.equal(s.taxable + s.tax_deferred + s.roth, 1_800_000);
});

test("defaultSplit: odd P=1,965,194 still sums EXACTLY to P (Roth absorbs the drift)", () => {
  const P = 1_965_194;
  const s = TLV.defaultSplit(P);
  // 0.25·P = 491,298.5 → rounds to 491,299; 0.60·P = 1,179,116.4 → 1,179,116;
  // Roth balances: 1,965,194 − 491,299 − 1,179,116 = 294,779.
  assert.equal(s.taxable, 491_299);
  assert.equal(s.tax_deferred, 1_179_116);
  assert.equal(s.roth, 294_779);
  assert.equal(s.taxable + s.tax_deferred + s.roth, P);
});

test("defaultSplit: several P sum to EXACTLY P with the right approximate proportions", () => {
  for (const P of [1_800_000, 1_965_194, 999_999, 3_333_333, 1, 1_000_001]) {
    const s = TLV.defaultSplit(P);
    // Exact-sum invariant (the whole point of the Roth balancer).
    assert.equal(
      s.taxable + s.tax_deferred + s.roth,
      P,
      `sum must equal P for P=${P}`,
    );
    // Whole-dollar integers.
    for (const v of [s.taxable, s.tax_deferred, s.roth]) {
      assert.ok(Number.isInteger(v), `whole dollars for P=${P}`);
    }
    if (P >= 100_000) {
      // Approximate proportions (within $1 of the rounded fraction).
      assert.ok(Math.abs(s.taxable - 0.25 * P) <= 1, `taxable≈25% for P=${P}`);
      assert.ok(Math.abs(s.tax_deferred - 0.6 * P) <= 1, `tax-deferred≈60% for P=${P}`);
      assert.ok(Math.abs(s.roth - 0.15 * P) <= 1, `roth≈15% for P=${P}`);
    }
  }
});

test("defaultSplit: blank / NaN portfolio → all zero (sum 0)", () => {
  for (const P of [0, NaN, null, undefined, ""]) {
    const s = TLV.defaultSplit(P);
    assert.equal(s.taxable, 0, `taxable P=${String(P)}`);
    assert.equal(s.tax_deferred, 0, `tax_deferred P=${String(P)}`);
    assert.equal(s.roth, 0, `roth P=${String(P)}`);
  }
});

// ---------------------------------------------------------------------------
// Markup — the enable checkbox exists with the documented id + label, and the
// versioned includes are cache-busted to dark2 (dark-mode input-bg follow-up bumped dark1 → dark2).
// ---------------------------------------------------------------------------

test("markup: the enable checkbox exists with id + the documented label", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  assert.ok(html.includes('id="tax-location-enable"'), "tax-location-enable present");
  assert.match(html, /type="checkbox" id="tax-location-enable"/);
  assert.match(html, /Model accounts by tax location/);
});

test("markup: the six JS includes are cache-busted to ?v=r1011", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  // The cache-bust token bumped td1 → r1011 with the split-MC perf work (issue
  // ): the working indicator's expected-duration copy dropped from
  // ~45-60s to ~15-25s. All six JS includes + styles.css share ONE token.
  for (const inc of [
    "chart_view.js?v=r1011",
    "comparison_state.js?v=r1011",
    "payload_assembler.js?v=r1011",
    "scenario_store.js?v=r1011",
    "ss_claim.js?v=r1011",
    "app.js?v=r1011",
  ]) {
    assert.ok(html.includes(inc), `${inc} present`);
  }
  // The JS includes share ONE token; no stale prior token left on a versioned
  // include (ft1 / ft2 / td1 / bug1 / dark2 / the tb1 family / fr1..fr6 /
  // st1..st4 / ss1 / nb1 / af1..af2 / br1..br2).
  assert.doesNotMatch(html, /\.(?:css|js)\?v=ft1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=ft2"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=td1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=bug1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=dark2"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=tb1[a-e]?"/);
  assert.doesNotMatch(html, /\.js\?v=fr1"/);
  assert.doesNotMatch(html, /\.js\?v=fr2"/);
  assert.doesNotMatch(html, /\.js\?v=fr3"/);
  assert.doesNotMatch(html, /\.js\?v=fr4"/);
  assert.doesNotMatch(html, /\.js\?v=fr5"/);
  assert.doesNotMatch(html, /\.js\?v=fr6"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=st1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=st2"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=st3"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=st4"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=ss1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=nb1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=af1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=af2"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=af3"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=af4"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=br1"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=br2"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=br3"/);
  assert.doesNotMatch(html, /\.(?:css|js)\?v=ff1"/);
});

// ---------------------------------------------------------------------------
// Source wiring — check → pre-fill / uncheck → clear, indicator sync, the gate
// disables the box under buckets. Static against the source (DOM seam is the
// integrator's gate).
// ---------------------------------------------------------------------------

test("source: CHECK pre-fills the default split from the portfolio; UNCHECK clears", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  // The handler exists and branches on the box's checked state.
  assert.match(src, /function onTaxLocationEnableToggle\(\)/);
  // CHECK → defaultSplit(P) written via setTaxLocationInputs.
  assert.match(src, /setTaxLocationInputs\(TaxLocationView\.defaultSplit\(P\)\)/);
  // UNCHECK → setTaxLocationInputs(null) (clear / feature off).
  const handler = src.slice(src.indexOf("function onTaxLocationEnableToggle("));
  assert.match(handler.slice(0, 700), /else\s*{\s*\n\s*setTaxLocationInputs\(null\)/);
  // It is wired to the checkbox's change event.
  assert.match(src, /addEventListener\("change", onTaxLocationEnableToggle\)/);
});

test("source: NO recompute on toggle (button-driven app)", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const start = src.indexOf("function onTaxLocationEnableToggle(");
  const handler = src.slice(start, src.indexOf("\n}", start) + 2);
  assert.doesNotMatch(handler, /rerender\(\)|renderSingle\(|onRecompute\(/);
});

test("source: the checkbox is a 'has split' INDICATOR synced from the inputs", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  // syncTaxLocationEnable sets box.checked from hasSplit(readTaxLocation()).
  assert.match(src, /function syncTaxLocationEnable\(\)/);
  assert.match(src, /box\.checked = TaxLocationView\.hasSplit\(readTaxLocation\(\)\)/);
  // setTaxLocationInputs re-syncs the box (load / reset / fill / clear).
  const setSrc = src.slice(src.indexOf("function setTaxLocationInputs("));
  assert.match(setSrc.slice(0, 600), /syncTaxLocationEnable\(\)/);
  // updateTaxLocationSum re-syncs the box (manual typing / portfolio edit).
  const sumSrc = src.slice(src.indexOf("function updateTaxLocationSum("));
  assert.match(sumSrc.slice(0, 500), /syncTaxLocationEnable\(\)/);
});

test("source: taxLocationActive stays input-driven (the box is NOT a state flag)", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  // Unchanged: active is hasSplit(readTaxLocation()), never the checkbox.
  assert.match(src, /sourcingMode !== "buckets" && TaxLocationView\.hasSplit\(readTaxLocation\(\)\)/);
  const activeSrc = src.slice(
    src.indexOf("function taxLocationActive("),
    src.indexOf("function taxLocationActive(") + 200,
  );
  assert.doesNotMatch(activeSrc, /tax-location-enable|TAX_LOCATION_ENABLE_ID|\.checked/);
});

test("source: buckets-mode gate also disables the enable checkbox", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const gate = src.slice(
    src.indexOf("function updateTaxLocationGate("),
    src.indexOf("function destroyTaxLocationChart("),
  );
  assert.match(gate, /TAX_LOCATION_ENABLE_ID/);
  assert.match(gate, /inp\.disabled = isBuckets/);
});

test("source: the default-split fractions are 25 / 60 / 15 percent", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  assert.match(src, /TAX_DEFAULT_TAXABLE_FRAC = 0\.25;/);
  assert.match(src, /TAX_DEFAULT_DEFERRED_FRAC = 0\.6/);
  assert.match(src, /TAX_DEFAULT_ROTH_FRAC = 0\.15;/);
});

// ---------------------------------------------------------------------------
// Taxable-gains-fraction knob (federal-tax overlay design ft1, DECISION 4) —
// markup + gating exist alongside the balance inputs. Functional behavior
// (readTaxableGainsFraction parsing/clamping/default) is covered in its own
// test_tax_gains_fraction.mjs; this file only pins that the knob rides the SAME
// section + buckets gate as the enable checkbox above.
// ---------------------------------------------------------------------------

test("markup: the gains-fraction input exists, defaulting to 50%, inside the reveal", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  assert.match(html, /id="tax-gains-fraction"/);
  assert.match(html, /id="tax-gains-fraction"[^>]*value="50"/);
  assert.match(html, /Taxable-gains share of withdrawals/);
});

test("source: buckets-mode gate also disables the gains-fraction knob", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const gate = src.slice(
    src.indexOf("function updateTaxLocationGate("),
    src.indexOf("function destroyTaxLocationChart("),
  );
  assert.match(gate, /TAX_GAINS_FRACTION_INPUT_ID/);
});

// ---------------------------------------------------------------------------
// Bracket-fill lever (tax-drag design td1, DECISION 5) — markup + gating exist
// alongside the balance inputs and the gains-fraction knob, gated identically.
// Functional behavior (readBracketFillTarget/setBracketFillTargetInput) is
// covered in its own test_bracket_fill_target.mjs; this file only pins that
// the lever rides the SAME reveal + buckets gate as the rest of the section.
// ---------------------------------------------------------------------------

test("markup: the bracket-fill lever input exists inside the reveal, empty by default", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  assert.match(html, /id="bracket-fill-target"/);
  const idx = html.indexOf('id="bracket-fill-target"');
  const tag = html.slice(html.lastIndexOf("<input", idx), html.indexOf(">", idx) + 1);
  assert.match(tag, /min="0"/);
  assert.match(tag, /max="5000000"/);
  // Empty = off (DECISION 5) — no seeded value attribute.
  assert.doesNotMatch(tag, /value="/);
  assert.match(html, /Bracket-fill lever/);
  // The two real 2026 MFJ bracket-top reference points are disclosed.
  assert.match(html, /\$100,800/);
  assert.match(html, /\$211,400/);
  // It sits inside #tax-location-reveal, alongside the balances + gains knob.
  const revealStart = html.indexOf('id="tax-location-reveal"');
  const gateNoteIdx = html.indexOf('id="tax-location-gate-note"', revealStart);
  assert.ok(
    revealStart < idx && idx < gateNoteIdx,
    "bracket-fill-target input is inside #tax-location-reveal",
  );
});

test("source: buckets-mode gate also disables the bracket-fill lever", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const gate = src.slice(
    src.indexOf("function updateTaxLocationGate("),
    src.indexOf("function destroyTaxLocationChart("),
  );
  assert.match(gate, /BRACKET_FILL_TARGET_INPUT_ID/);
});
