// test_run_progress.mjs — pin the working indicator (tax-drag design td1,
// timing acceptance): RunProgressView (pure copy/formatting logic) +
// startRunProgress/stopRunProgress (the DOM binding + setInterval ticker).
//
// RunProgressView is DOM-free, tested the same way as TaxLocationView /
// FederalTaxView: app.js evaluated with NO `document` (init() skipped), pure
// object read off `window`.
//
// startRunProgress/stopRunProgress touch `document.getElementById` AND
// `setInterval`/`clearInterval`/`Date.now` — tested with node:test's built-in
// mock timers (t.mock.timers), injected into the vm sandbox by reference
// BEFORE vm.createContext so code running inside the sandbox drives the SAME
// fake clock the test controls. A minimal `document` stub (three elements:
// #run-progress / #run-progress-elapsed / #run-progress-copy) is injected
// AFTER app.js evaluates (same "no document at eval time, one at call time"
// trick as test_det_spending.mjs), so the bottom-of-file init()/wireUp() never
// runs against this reduced DOM.
//
// NOTE on node's mock timer semantics: a single t.mock.timers.tick(N) call
// advances the fake clock to its FINAL target BEFORE firing any due
// callbacks — every callback that fires within that one tick() call sees
// Date.now() already at the tick's end value, not its own individual
// scheduled time. Tests here assert POST-tick state (never an assertion that
// depends on distinguishing WHICH of several callbacks fired within a single
// tick() call), which is unaffected by that batching.
//
// Run:
//   (from the repo root) node --test tests/test_run_progress.mjs

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

// ---------------------------------------------------------------------------
// RunProgressView — pure copy/formatting, no document at all.
// ---------------------------------------------------------------------------

function loadRunProgressView() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window.RunProgressView;
}

const RPV = loadRunProgressView();

test("copyFor: split-active Monte Carlo -> the prominent ~15-25s copy (post-memoization)", () => {
  // Re-pinned deliberately (process-parallel dragged trials +
  // hot-loop micro-opts): split-MC 10k measured ~45-60s pre-memoization, ~15-25s after.
  const s = RPV.copyFor({ splitActive: true, mode: "monte_carlo" });
  assert.match(s, /10,000 trials/);
  assert.match(s, /15.25 seconds/);
});

test("copyFor: split-active Historical -> the few-seconds copy", () => {
  const s = RPV.copyFor({ splitActive: true, mode: "historical" });
  assert.match(s, /historical cohorts/);
  assert.match(s, /few seconds/);
});

test("copyFor: no split (any mode) -> the generic copy", () => {
  assert.equal(RPV.copyFor({ splitActive: false, mode: "monte_carlo" }), "Still working…");
  assert.equal(RPV.copyFor({ splitActive: false, mode: "deterministic" }), "Still working…");
});

test("copyFor: split-active but deterministic/stress -> the generic copy (not the MC/Historical prominence)", () => {
  assert.equal(RPV.copyFor({ splitActive: true, mode: "deterministic" }), "Still working…");
  assert.equal(RPV.copyFor({ splitActive: true, mode: "stress" }), "Still working…");
});

test("elapsedLabel: floors to whole seconds, clamped at 0", () => {
  assert.equal(RPV.elapsedLabel(0), "0s elapsed");
  assert.equal(RPV.elapsedLabel(1999), "1s elapsed");
  assert.equal(RPV.elapsedLabel(2000), "2s elapsed");
  assert.equal(RPV.elapsedLabel(-50), "0s elapsed");
});

test("APPEAR_DELAY_MS is 2000 (appear after ~2s — fast runs never see it)", () => {
  assert.equal(RPV.APPEAR_DELAY_MS, 2000);
});

// ---------------------------------------------------------------------------
// startRunProgress / stopRunProgress — the DOM binding + ticker, driven by
// node's mock timers injected into the vm sandbox by reference.
// ---------------------------------------------------------------------------

function makeElement() {
  return { hidden: true, textContent: "" };
}

// Load app.js with NO document (init() skipped), inject the (already-mocked,
// by reference) setInterval/clearInterval/Date into the sandbox BEFORE
// createContext so code running inside it drives the SAME fake clock the
// test's t.mock.timers controls, then attach a minimal document stub
// AFTERWARD (document is resolved live at call time, not eval time).
function loadAppWithMockTimers() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.setInterval = setInterval;
  ctx.clearInterval = clearInterval;
  ctx.Date = Date;
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  const wrap = makeElement();
  const elapsedEl = makeElement();
  const copyEl = makeElement();
  const byId = { "run-progress": wrap, "run-progress-elapsed": elapsedEl, "run-progress-copy": copyEl };
  ctx.document = {
    documentElement: {},
    getElementById: (id) => byId[id] || null,
  };
  return { ctx, wrap, elapsedEl, copyEl };
}

test("startRunProgress: stays hidden before APPEAR_DELAY_MS (fast runs never see it)", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"] });
  const { ctx, wrap, elapsedEl } = loadAppWithMockTimers();
  vm.runInContext('startRunProgress({ splitActive: false, mode: "deterministic" })', ctx);
  assert.equal(wrap.hidden, true, "hidden immediately on start");
  t.mock.timers.tick(1000); // 1000ms < 2000ms APPEAR_DELAY_MS
  assert.equal(wrap.hidden, true, "still hidden before the appear threshold");
  assert.equal(elapsedEl.textContent, "", "no elapsed text written yet");
});

test("startRunProgress: reveals + ticks past APPEAR_DELAY_MS", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"] });
  const { ctx, wrap, elapsedEl, copyEl } = loadAppWithMockTimers();
  vm.runInContext('startRunProgress({ splitActive: true, mode: "monte_carlo" })', ctx);
  assert.match(copyEl.textContent, /15.25 seconds/, "MC+split copy seeded immediately");
  t.mock.timers.tick(1000); // cumulative 1000ms — still hidden
  assert.equal(wrap.hidden, true);
  t.mock.timers.tick(1200); // cumulative 2200ms — past the 2000ms threshold
  assert.equal(wrap.hidden, false, "revealed past the appear threshold");
  assert.equal(elapsedEl.textContent, "2s elapsed");
});

test("stopRunProgress: hides + clears text, and the ticker never fires again", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"] });
  const { ctx, wrap, elapsedEl, copyEl } = loadAppWithMockTimers();
  vm.runInContext('startRunProgress({ splitActive: true, mode: "historical" })', ctx);
  t.mock.timers.tick(2500); // revealed, elapsed text set
  assert.equal(wrap.hidden, false);
  vm.runInContext("stopRunProgress()", ctx);
  assert.equal(wrap.hidden, true, "hidden on stop");
  assert.equal(elapsedEl.textContent, "", "elapsed text cleared on stop");
  assert.equal(copyEl.textContent, "", "copy text cleared on stop");
  // Advancing the clock further must not resurrect the indicator — the
  // interval was cleared, so its callback can never fire again.
  t.mock.timers.tick(5000);
  assert.equal(wrap.hidden, true, "stays hidden — the ticker is dead, not just past-appeared");
  assert.equal(elapsedEl.textContent, "");
});

test("startRunProgress: calling it again mid-run resets the clock (defensive re-entrancy guard)", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"] });
  const { ctx, wrap, elapsedEl } = loadAppWithMockTimers();
  vm.runInContext('startRunProgress({ splitActive: false, mode: "deterministic" })', ctx);
  t.mock.timers.tick(2500); // revealed
  assert.equal(wrap.hidden, false);
  // A second start (the defensive stopRunProgress() at its top) must re-hide
  // and restart the clock from zero, not continue ticking the OLD run.
  vm.runInContext('startRunProgress({ splitActive: false, mode: "deterministic" })', ctx);
  assert.equal(wrap.hidden, true, "re-hidden on restart");
  assert.equal(elapsedEl.textContent, "", "elapsed text reset on restart");
});

test("startRunProgress: no #run-progress element -> no-op, never throws", () => {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = { documentElement: {}, getElementById: () => null };
  assert.doesNotThrow(() => {
    vm.runInContext('startRunProgress({ splitActive: false, mode: "deterministic" })', ctx);
    vm.runInContext("stopRunProgress()", ctx);
  });
});

test("startRunProgress: no setInterval in the environment (reduced test DOM) -> degrades silently, never throws", () => {
  // Mirrors the many existing node:vm test harnesses (test_scenario_store.mjs
  // etc.) that reach rerender()/applyScenarioToForm() with NO setInterval in
  // their sandbox at all — startRunProgress must degrade to "no ticking
  // indicator," never a thrown ReferenceError that would break every one of
  // those tests.
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  const wrap = makeElement();
  ctx.document = {
    documentElement: {},
    getElementById: (id) => (id === "run-progress" ? wrap : null),
  };
  assert.doesNotThrow(() => {
    vm.runInContext('startRunProgress({ splitActive: true, mode: "monte_carlo" })', ctx);
  });
  assert.equal(wrap.hidden, true, "never revealed — no timer to tick it");
});
