// test_glidepath_view.mjs — pin the client glidepath boundary/label derivation
// (web/app.js GlidepathView) against the engine's EXPECTED output.
//
// web/app.js GlidepathView.{boundaries,phaseLabels,phaseEvent} are a hand-written
// mirror of firebridgelab.scenario.glidepath_boundaries + the phase-label derivation.
// They agree today; nothing else pins them together, so this test catches the JS
// mirror drifting from the engine. The engine output itself is verified separately
// in pytest (tests/test_glidepath.py) — here we HARDCODE the engine's expected
// values and assert the JS reproduces them.
//
// Loaded the same way as test_chart_view.mjs / test_comparison_state.mjs: app.js is
// read off disk and run inside a node:vm sandbox with a `window` stub and NO
// `document`, so the module's init() bootstrap is skipped and only the pure
// GlidepathView helpers (attached to window) are exercised. Zero npm deps;
// requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_glidepath_view.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
// Mirror the browser <script> load order: chart_view + comparison_state define the
// globals (ChartView / ComparisonState) app.js references at module-eval time.
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js", "scenario_store.js", "ss_claim.js"].map((f) =>
  path.join(WEB_DIR, f),
);
const APP_PATH = path.join(WEB_DIR, "app.js");

// Load app.js into a sandbox with a window stub and no document, so init() is
// skipped (the `typeof document` guard at the bottom of app.js) and we get the
// pure GlidepathView off `window`. Its deps are loaded first into the same context
// (browser load order). Capture console.warn so we can assert the drift warning
// fires (and doesn't fire) when expected.
function loadGlidepathView() {
  const warnings = [];
  const ctx = {
    console: { ...console, warn: (msg) => warnings.push(msg) },
  };
  // In a browser `window` IS the global object, so `window.X = ...` (how
  // chart_view/comparison_state export, and how app.js then references bare
  // `ComparisonState`/`ChartView`) creates a true global. Mirror that by making the
  // sandbox's `window` alias the global context itself.
  ctx.window = ctx;
  // chart_view.js references a global Chart in its closures — stub it so the file
  // evaluates (matches test_chart_view.mjs).
  ctx.Chart = function (canvasCtx, config) {
    this.config = config;
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return { GlidepathView: ctx.window.GlidepathView, warnings };
}

// The default scenario's GI/horizon ages (mirrors default_scenario()):
// 401(k) access 60, pensions 60 (A) and 65 (B), SS 67 (A) and 70 (B), horizon 50→95.
const DEFAULT_AGES = {
  startAge: 50,
  endAge: 95,
  personAPension: 60,
  personBPension: 65,
  personASs: 67,
  personBSs: 70,
};

test("GlidepathView is exposed and init() is skipped without a DOM", () => {
  const { GlidepathView } = loadGlidepathView();
  assert.equal(typeof GlidepathView, "object");
  assert.equal(typeof GlidepathView.boundaries, "function");
  assert.equal(typeof GlidepathView.phaseEvent, "function");
  assert.equal(typeof GlidepathView.phaseLabels, "function");
});

test("default scenario → engine boundaries [50,60,65,67,70]", () => {
  const { GlidepathView } = loadGlidepathView();
  // Hardcoded engine expectation (test_glidepath.py::test_default_boundaries).
  assert.deepEqual(Array.from(GlidepathView.boundaries(DEFAULT_AGES)), [50, 60, 65, 67, 70]);
});

test("default scenario → engine phase labels", () => {
  const { GlidepathView } = loadGlidepathView();
  // Age 60 is both 401(k) access and Person A's pension start; Pension wins
  // (precedence SS ≻ Pension ≻ 401(k) ≻ Bridge).
  assert.deepEqual(Array.from(GlidepathView.phaseLabels(DEFAULT_AGES)), [
    "50–59 · Bridge",
    "60–64 · Pension",
    "65–66 · Pension",
    "67–69 · Social Security",
    "70–95 · Social Security",
  ]);
});

test("person_b_ss_start_age=70 adds a boundary at 70 (mirrors engine)", () => {
  const { GlidepathView } = loadGlidepathView();
  const sameClaim = { ...DEFAULT_AGES, personBSs: 67 };
  // Engine expectation: test_glidepath.py::test_differing_ss_claim_age_adds_boundary.
  assert.deepEqual(Array.from(GlidepathView.boundaries(sameClaim)), [50, 60, 65, 67]);
  const ages = { ...sameClaim, personBSs: 70 };
  assert.deepEqual(Array.from(GlidepathView.boundaries(ages)), [50, 60, 65, 67, 70]);
  // The extra phase opens an SS boundary; both 67 (Person A) and 70 (Person B) are SS events.
  assert.deepEqual(Array.from(GlidepathView.phaseLabels(ages)).slice(-2), [
    "67–69 · Social Security",
    "70–95 · Social Security",
  ]);
});

test("an out-of-horizon claim age is clipped, not added", () => {
  const { GlidepathView } = loadGlidepathView();
  const ages = { ...DEFAULT_AGES, personBSs: 120 };
  // Mirrors test_glidepath.py::test_out_of_horizon_claim_age_clips.
  assert.deepEqual(Array.from(GlidepathView.boundaries(ages)), [50, 60, 65, 67]);
});

test("phaseEvent precedence: SS ≻ Pension ≻ 401(k) ≻ Bridge; no spurious warning", () => {
  const { GlidepathView, warnings } = loadGlidepathView();
  // Coincident claim: Person A's pension AND SS both at 62 → the SS label wins.
  // (Moving A's pension off 60 leaves 60 as a bare 401(k)-access boundary.)
  const ages = { ...DEFAULT_AGES, personAPension: 62, personASs: 62 };
  assert.equal(GlidepathView.phaseEvent(50, ages), "Bridge");
  assert.equal(GlidepathView.phaseEvent(60, ages), "401(k) access");
  assert.equal(GlidepathView.phaseEvent(62, ages), "Social Security"); // SS beats pension
  assert.equal(GlidepathView.phaseEvent(65, ages), "Pension");
  assert.equal(GlidepathView.phaseEvent(70, ages), "Social Security");
  assert.equal(warnings.length, 0);
});

test("phaseEvent warns loudly on a boundary that matches no inflection (drift signal)", () => {
  const { GlidepathView, warnings } = loadGlidepathView();
  // Age 99 is not start_age, 401(k), a pension, or an SS age — unreachable by
  // construction; if it ever fires, the mirror has drifted, so we warn.
  assert.equal(GlidepathView.phaseEvent(99, DEFAULT_AGES), "Phase");
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /drifted from the engine/);
});
