// test_glide_presets.mjs — pin the client glide-preset engine (web/app.js
// GlidePresets) for the gpp1 slice.
//
// GlidePresets is the pure, DOM-free logic behind the "Glide preset" <select>: it
// turns an endpoint-defined glide ({startEquityPct, endEquityPct}) into a per-phase
// equity% interpolation, maps each phase's equity% onto a per-class weight array
// (preserving each group's internal mix, summing to exactly 100), and handles the
// "Match current (flat)" copy-verbatim special + the degenerate all-equity guard.
//
// Loaded the same way as test_glidepath_view.mjs: app.js is read off disk and run in
// a node:vm sandbox with a `window` stub and NO `document`, so init() is skipped and
// only the pure helpers (attached to window) are exercised. Zero npm deps; Node>=18.
//
// The EQUITY classification is now INJECTED as data (a Set passed into the mapping),
// NOT embedded in GlidePresets. The single source of truth is
// firebridgelab.engine.buckets.EQUITY_CLASS_IDS, published per-class via /api/classes' `is_equity`
// flag and consumed by the client (equityClassIds()) — the server-side pytest
// (test_api.py) is now the sole consistency check against the engine set. These tests
// supply the classification as a TEST-LOCAL fixture (EQUITY_IDS below) so they
// exercise the interpolation + scaling + flat-copy + degenerate-guard behavior with
// classification handed in as data.
//
// Run:
//   (from the repo root) node --test tests/test_glide_presets.mjs

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

// Load app.js into a sandbox with a window stub and no document, so init() is skipped
// (the `typeof document` guard) and we get the pure GlidePresets off `window`.
function loadGlidePresets() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function (canvasCtx, config) {
    this.config = config;
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window.GlidePresets;
}

// TEST-LOCAL equity classification fixture. Supplied INTO the mapping (the way the
// app threads /api/classes' `is_equity` through equityClassIds()), so these tests no
// longer depend on a constant baked into GlidePresets. The engine↔API consistency
// check lives in tests/test_api.py; here we just need a representative split to
// exercise the interpolation/scaling/guard behavior.
const EQUITY_IDS = new Set([
  "us_total_market",
  "us_small_value",
  "intl_developed",
  "emerging_markets",
  "us_reit",
]);

// The default 2-class menu (US total market / US aggregate bonds), as the client
// holds it before /api/classes loads — and the steady state for his plan.
const TWO_CLASS_IDS = ["us_total_market", "us_agg_bonds"];
const TWO_CLASS_SEED = [60, 40]; // the 60/40 single allocation

// A 5-class multi-asset menu spanning equity + non-equity per the fixture above.
const MULTI_IDS = [
  "us_total_market", // equity
  "us_small_value", // equity
  "intl_developed", // equity
  "us_agg_bonds", // non-equity
  "tips", // non-equity
];

const sum = (a) => a.reduce((s, x) => s + x, 0);

// Arrays come back from the node:vm sandbox with that realm's Array prototype, so
// assert.deepEqual (strict) treats them as a different type than the test realm's
// arrays. Re-wrap through the test realm before comparing (the same fix
// test_glidepath_view.mjs uses with Array.from on GlidepathView output).
const arr = (a) => Array.from(a);
const obj = (o) => {
  const out = {};
  for (const k of Object.keys(o)) out[k] = Array.from(o[k]);
  return out;
};

test("GlidePresets is exposed and init() is skipped without a DOM", () => {
  const GP = loadGlidePresets();
  assert.equal(typeof GP, "object");
  assert.equal(typeof GP.interpolateEquity, "function");
  assert.equal(typeof GP.mapEquityToPhase, "function");
  assert.equal(typeof GP.buildPhaseWeights, "function");
});

test("GlidePresets does NOT embed an equity list — classification is injected", () => {
  const GP = loadGlidePresets();
  // The hardcoded JS replica of firebridgelab.engine.buckets.EQUITY_CLASS_IDS is GONE: equity is
  // now supplied as data (a Set) per /api/classes' is_equity. Guard against a
  // re-hardcode regression.
  assert.equal(GP.EQUITY_CLASS_IDS, undefined);
  assert.equal(typeof GP.isEquityId, "undefined");
});

test("the locked preset bundle: 4 presets, correct endpoints + the flat special", () => {
  const GP = loadGlidePresets();
  const byId = Object.fromEntries(GP.PRESETS.map((p) => [p.id, p]));
  assert.equal(GP.PRESETS.length, 4);
  assert.deepEqual(
    { s: byId.bond_tent.startEquityPct, e: byId.bond_tent.endEquityPct },
    { s: 30, e: 70 },
  );
  assert.deepEqual(
    { s: byId.steep_bond_tent.startEquityPct, e: byId.steep_bond_tent.endEquityPct },
    { s: 20, e: 75 },
  );
  assert.deepEqual(
    { s: byId.declining.startEquityPct, e: byId.declining.endEquityPct },
    { s: 70, e: 40 },
  );
  assert.equal(byId.match_current.flat, true);
});

// ---- interpolation ----

test("interpolation: endpoints land on phase[0] and phase[N-1]; linear between (4-phase)", () => {
  const GP = loadGlidePresets();
  const eq = arr(GP.interpolateEquity(30, 70, 4)); // bond tent, default 4-phase set
  assert.equal(eq.length, 4);
  assert.equal(eq[0], 30);
  assert.equal(eq[3], 70);
  // round(30 + 40·k/3): 30, 43.33→43, 56.67→57, 70
  assert.deepEqual(eq, [30, 43, 57, 70]);
});

test("interpolation: a 3-phase boundary set (fewer GI inflections)", () => {
  const GP = loadGlidePresets();
  const eq = arr(GP.interpolateEquity(30, 70, 3)); // bond tent over 3 phases
  assert.equal(eq.length, 3);
  assert.equal(eq[0], 30);
  assert.equal(eq[2], 70);
  // round(30 + 40·k/2): 30, 50, 70
  assert.deepEqual(eq, [30, 50, 70]);
});

test("interpolation: declining glide falls (70→40); single phase reports the start", () => {
  const GP = loadGlidePresets();
  assert.deepEqual(arr(GP.interpolateEquity(70, 40, 4)), [70, 60, 50, 40]);
  assert.deepEqual(arr(GP.interpolateEquity(20, 75, 1)), [20]); // n=1 can't glide
});

// ---- 2-class exactness (the default) ----

test("2-class exactness: bond tent phase endpoints → 30/70 and 70/30", () => {
  const GP = loadGlidePresets();
  const preset = GP.PRESETS.find((p) => p.id === "bond_tent");
  const phases = obj(GP.buildPhaseWeights(preset, TWO_CLASS_SEED, TWO_CLASS_IDS, 4, EQUITY_IDS));
  // phase[0] = 30% equity → [30, 70]; phase[3] = 70% equity → [70, 30].
  assert.deepEqual(phases[0], [30, 70]);
  assert.deepEqual(phases[3], [70, 30]);
  // Interior phases follow the interpolation, each summing to 100.
  assert.deepEqual(phases[1], [43, 57]);
  assert.deepEqual(phases[2], [57, 43]);
  for (const k of Object.keys(phases)) assert.equal(sum(phases[k]), 100);
});

test("2-class declining: phase[0]→70/30, phase[N-1]→40/60", () => {
  const GP = loadGlidePresets();
  const preset = GP.PRESETS.find((p) => p.id === "declining");
  const phases = obj(GP.buildPhaseWeights(preset, TWO_CLASS_SEED, TWO_CLASS_IDS, 4, EQUITY_IDS));
  assert.deepEqual(phases[0], [70, 30]);
  assert.deepEqual(phases[3], [40, 60]);
});

// ---- Match current (flat) ----

test("Match current (flat) copies the single allocation exactly into every phase", () => {
  const GP = loadGlidePresets();
  const preset = GP.PRESETS.find((p) => p.id === "match_current");
  // A non-round multi-class seed so a copy is distinguishable from a remap.
  const seed = [37, 18, 5, 30, 10];
  // Flat copy is classification-INDEPENDENT — no equityIds passed, proving it.
  const raw = GP.buildPhaseWeights(preset, seed, MULTI_IDS, 4);
  const phases = obj(raw);
  assert.equal(Object.keys(phases).length, 4);
  for (let k = 0; k < 4; k++) {
    assert.deepEqual(phases[k], seed);
    assert.notEqual(raw[k], seed); // a copy, not the same reference
  }
});

// ---- multi-class scaling ----

test("multi-class: equity group ≈ target%, non-equity ≈ remainder, preserves internal mix, total exactly 100", () => {
  const GP = loadGlidePresets();
  // Seed: equity = 30+15+15 = 60 (ratio 2:1:1), non-equity = 30+10 = 40 (ratio 3:1).
  const seed = [30, 15, 15, 30, 10];
  // Target 70% equity.
  const phase = arr(GP.mapEquityToPhase(70, seed, MULTI_IDS, EQUITY_IDS));
  const isEq = MULTI_IDS.map((id) => EQUITY_IDS.has(id));
  const eqSum = phase.reduce((s, w, i) => s + (isEq[i] ? w : 0), 0);
  const bondSum = phase.reduce((s, w, i) => s + (isEq[i] ? 0 : w), 0);
  // HARD: the column totals exactly 100 (the matrix's validation rule).
  assert.equal(sum(phase), 100);
  // Group sums hit the target within rounding (the residual fix-up lands on the
  // single largest-weight class, so a group can drift ≤2 from rounding — per design
  // "±rounding"; the 100% total is the invariant that must be exact).
  assert.ok(Math.abs(eqSum - 70) <= 2, `equity ${eqSum} ≈ 70`);
  assert.ok(Math.abs(bondSum - 30) <= 2, `non-equity ${bondSum} ≈ 30`);
  // Internal RELATIVE mix preserved within each group (the per-class scale before the
  // residual fix-up): equity 2:1:1 → us_small_value == intl_developed; the largest
  // (us_total_market) is largest. Non-equity 3:1 → us_agg_bonds > tips.
  assert.equal(phase[1], phase[2]); // the two equal equity classes stay equal
  assert.ok(phase[0] >= phase[1]); // us_total_market is the largest equity class
  assert.ok(phase[3] > phase[4]); // us_agg_bonds (3) > tips (1)
});

test("multi-class build over phases: every phase totals exactly 100 (rounding fixed up)", () => {
  const GP = loadGlidePresets();
  const seed = [33, 17, 10, 28, 12]; // deliberately awkward, sums to 100
  for (const preset of GP.PRESETS) {
    for (const n of [3, 4, 5]) {
      const phases = GP.buildPhaseWeights(preset, seed, MULTI_IDS, n, EQUITY_IDS);
      assert.equal(Object.keys(phases).length, n);
      for (let k = 0; k < n; k++) {
        assert.equal(sum(phases[k]), 100, `${preset.id} n=${n} phase ${k}`);
      }
    }
  }
});

// ---- degenerate guard ----

test("degenerate 100%-equity seed + a <100% target does not throw; clamps to all-equity", () => {
  const GP = loadGlidePresets();
  const ids = ["us_total_market", "intl_developed"]; // both equity
  const seed = [70, 30]; // 100% equity, nothing to scale into bonds
  let phase;
  assert.doesNotThrow(() => {
    phase = arr(GP.mapEquityToPhase(30, seed, ids, EQUITY_IDS)); // ask for 30% equity — impossible
  });
  assert.equal(sum(phase), 100); // still a valid 100% column
  // Can't synthesize bonds → stays all-equity, preserving the 70/30 internal mix.
  assert.deepEqual(phase, [70, 30]);
});

test("degenerate guard through buildPhaseWeights does not throw", () => {
  const GP = loadGlidePresets();
  const ids = ["us_total_market"]; // single equity class, 100%
  const seed = [100];
  const preset = GP.PRESETS.find((p) => p.id === "bond_tent");
  let phases;
  assert.doesNotThrow(() => {
    phases = obj(GP.buildPhaseWeights(preset, seed, ids, 4, EQUITY_IDS));
  });
  for (let k = 0; k < 4; k++) assert.deepEqual(phases[k], [100]);
});
