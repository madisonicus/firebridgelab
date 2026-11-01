// test_reset_defaults.mjs — the Reset button's resetToDefaults() (web/app.js, rst1).
//
// Reset restores the FULL default-load state in place: it builds a default-shaped
// blob from the cached `defaultScenario` echo + the default view modes and replays
// it through applyScenarioToForm (the sv1 INVERSE map). The acceptance invariants:
//   - a lightweight confirm() gates the action; CANCEL is a no-op (no state touched,
//     no recompute, no network call);
//   - on confirm it restores the 12 scalar inputs + the four global modes and
//     EXITS any comparison (compareState.isComparing() -> false);
//   - it NEVER calls a DELETE/save endpoint — the saved-scenario library persists;
//   - it fires exactly ONE recompute (one POST), under the applyingScenario guard.
//
// Loaded exactly like test_saved_scenarios.mjs: app.js is read off disk and run in
// a node:vm sandbox with NO `document` (init() skipped), then a lightweight fake
// DOM + stub fetch/confirm are injected. The module-level globals (defaultScenario,
// simMode, compareState, ...) aren't context properties, so we read/write them via
// vm.runInContext — that's how the test drives a "messy" pre-reset state. Zero npm
// deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_reset_defaults.mjs

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

// ---------- the same auto-vivifying fake DOM as test_saved_scenarios.mjs ----------

function makeElement(id) {
  const children = [];
  const attrs = {};
  const el = {
    id: id || "",
    _value: "",
    children,
    dataset: {},
    style: {},
    hidden: false,
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() {
        return false;
      },
    },
    get value() {
      return this._value;
    },
    set value(v) {
      this._value = String(v);
    },
    set textContent(_v) {},
    get textContent() {
      return "";
    },
    set innerHTML(_v) {
      children.length = 0;
    },
    get innerHTML() {
      return "";
    },
    appendChild(c) {
      children.push(c);
      return c;
    },
    removeChild() {},
    setAttribute(k, v) {
      attrs[k] = v;
    },
    getAttribute(k) {
      return attrs[k];
    },
    removeAttribute(k) {
      delete attrs[k];
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return false;
    },
    closest() {
      return null;
    },
    focus() {},
    destroy() {},
    getContext() {
      return {};
    },
  };
  return el;
}

const NULL_CONTAINER_IDS = new Set([
  "allocation-rows",
  "returns-fields",
  "allocation-totals",
  "glidepath-phases",
  "bucket-matrices",
  "allocation-toggle",
  "preset-select",
]);

const MATRIX_INPUT_RE =
  /^(alloc-weight-|alloc-return-|alloc-vol-|glide-w-|bucket-w-)/;

function makeFakeDocument(seed) {
  const byId = new Map();
  for (const [id, value] of Object.entries(seed || {})) {
    const el = makeElement(id);
    el.value = value;
    byId.set(id, el);
  }
  return {
    getElementById(id) {
      if (NULL_CONTAINER_IDS.has(id)) return null;
      if (MATRIX_INPUT_RE.test(id)) return null;
      if (!byId.has(id)) byId.set(id, makeElement(id));
      return byId.get(id);
    },
    querySelector() {
      return makeElement("");
    },
    querySelectorAll() {
      return [];
    },
    createElement(tag) {
      const el = makeElement("");
      el.tagName = String(tag).toUpperCase();
      return el;
    },
    createTextNode(text) {
      return { nodeType: 3, textContent: String(text) };
    },
    addEventListener() {},
    body: makeElement("body"),
    _byId: byId,
  };
}

// The default-scenario echo the app caches in init() (from the first POST). This is
// the 60/40 single-scenario default: $1.8m portfolio, $110k spend. Reset
// rebuilds the form from THIS object, so it stands in for the live engine echo.
function defaultScenarioEcho() {
  return {
    initial_portfolio: 1_800_000,
    annual_spending: 110_000,
    start_age: 50,
    end_age: 95,
    person_a_pension_amount: 24_000,
    person_a_pension_start_age: 60,
    // The LIVE engine echo carries the DERIVED effective SS amount + start age
    // (person_a_ss_amount / person_a_ss_start_age), NOT the request-only fra_amount/claim_age
    // fields. Person A claims at FRA (67, factor 1.0), so the effective amount
    // EQUALS the FRA amount; Person B claims at 70 (factor 1.24), so the echo
    // carries 26,000 × 1.24 = 32,240 and Reset must divide it back out to 26,000.
    // Reset's buildDefaultScenarioBlob must map these back onto the four SS
    // fields via the FIELDS `defaultFrom` / `defaultClaimAgeFrom` indirection — keying on
    // the (absent) fra_amount/claim_age names would silently skip them, which was
    // the live bug this test now guards. Mirror the live shape so the test and the
    // #reset-btn click can't diverge again.
    person_a_ss_amount: 33_000,
    person_a_ss_start_age: 67,
    person_b_pension_amount: 9_000,
    person_b_pension_start_age: 65,
    person_b_ss_amount: 32_240,
    person_b_ss_start_age: 70,
    correlation: 0.2,
    allocation: [
      { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
      { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
    ],
  };
}

// Load app.js (init skipped, no document at load), inject fake DOM + instrumented
// fetch/confirm, then cache the default-scenario echo into the module-level
// `defaultScenario` and register the comparison subscriber (init() would normally
// do both). Returns the ctx plus the recorder arrays the tests assert against.
function loadApp(seedInputs) {
  const fetchCalls = [];
  const ctx = { console: { ...console, warn() {}, error() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx._confirmReturn = true;
  ctx._confirmCalls = 0;
  ctx.confirm = function () {
    ctx._confirmCalls += 1;
    return ctx._confirmReturn;
  };
  // A fetch that RECORDS the method/url then rejects, so rerender()'s fetchScenario
  // catches the error and returns null (the render chain short-circuits). The unit
  // under test is the FORM-STATE + mode + comparison restore; the chart render is
  // the integrator's live-gate concern. Recording lets us assert "exactly one POST,
  // no DELETE".
  ctx.fetch = async (url, opts) => {
    fetchCalls.push({ url: String(url), method: (opts && opts.method) || "GET" });
    throw new Error("fetch disabled in reset unit test");
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = makeFakeDocument(seedInputs || {});
  // init() caches the echo + subscribes; replicate just those two so reset has a
  // default to rebuild from and the comparison transitions notify.
  ctx.__echo = defaultScenarioEcho();
  vm.runInContext("defaultScenario = this.__echo;", ctx);
  vm.runInContext("compareState.subscribe(onComparisonStateChanged);", ctx);
  ctx._fetchCalls = fetchCalls;
  return ctx;
}

// Helpers to peek/poke the module-level globals through the vm boundary.
function evalIn(ctx, expr) {
  return vm.runInContext(expr, ctx);
}

test("SavedScenarios.reset is exposed", () => {
  const ctx = loadApp({});
  assert.equal(typeof ctx.window.SavedScenarios.reset, "function");
});

test("reset: confirm-cancel is a no-op (no state change, no recompute)", async () => {
  const ctx = loadApp({ annual_spending: "999999" });
  // Put the app in a non-default state: MC + guardrails lens, a mangled input.
  evalIn(ctx, 'simMode = "monte_carlo"; spendingMode = "guardrails";');
  ctx._confirmReturn = false; // user cancels the confirm
  ctx._fetchCalls.length = 0;

  const result = await ctx.window.SavedScenarios.reset();

  assert.equal(result, null, "cancel returns null");
  assert.equal(ctx._confirmCalls, 1, "confirm was shown exactly once");
  assert.equal(ctx._fetchCalls.length, 0, "no recompute / network call on cancel");
  assert.equal(evalIn(ctx, "simMode"), "monte_carlo", "sim mode untouched on cancel");
  assert.equal(
    evalIn(ctx, "spendingMode"),
    "guardrails",
    "spending mode untouched on cancel",
  );
  assert.equal(
    ctx.document.getElementById("annual_spending").value,
    "999999",
    "scalar input untouched on cancel",
  );
});

test("reset: restores the default scalar inputs + the four modes", async () => {
  const ctx = loadApp({ annual_spending: "80000", initial_portfolio: "100" });
  // Drift every mode off-default and stomp two inputs.
  evalIn(
    ctx,
    'simMode = "historical"; spendingMode = "lifecycle"; ' +
      'allocationMode = "glidepath"; sourcingMode = "buckets";',
  );
  ctx._fetchCalls.length = 0;

  await ctx.window.SavedScenarios.reset();

  assert.equal(evalIn(ctx, "simMode"), "deterministic");
  assert.equal(evalIn(ctx, "spendingMode"), "constant");
  assert.equal(evalIn(ctx, "allocationMode"), "constant");
  assert.equal(evalIn(ctx, "sourcingMode"), "single");
  assert.equal(
    ctx.document.getElementById("annual_spending").value,
    "110000",
    "annual_spending restored to the default",
  );
  assert.equal(
    ctx.document.getElementById("initial_portfolio").value,
    "1800000",
    "initial_portfolio restored to the default",
  );
});

// Guards BUG 2: the SS claim-age slider feature (slice 2) added four request-only
// fields (*_ss_fra_amount / *_ss_claim_age) that the engine echo does NOT carry —
// it echoes the derived person_a_ss_amount/person_a_ss_start_age instead. Reset must map the
// echo back onto the four fields (via FIELDS `defaultFrom`); the original reset
// keyed on the fields' own absent names and silently skipped them, so a reset left
// them at their edited values. This drives the SAME SavedScenarios.reset() path the
// #reset-btn click invokes, so the test and live behavior cannot diverge.
test("reset: restores the four SS FRA-amount + claim-age fields", async () => {
  const ctx = loadApp({
    person_a_ss_fra_amount: "50000",
    person_a_ss_claim_age: "70",
    person_b_ss_fra_amount: "12000",
    person_b_ss_claim_age: "62",
  });
  ctx._fetchCalls.length = 0;

  await ctx.window.SavedScenarios.reset();

  // Defaults: FRA amounts recovered from the echo's derived effective amounts
  // (A: 33,000 at 67, factor 1.0; B: 32,240 / 1.24 = 26,000 at 70), claim ages from
  // person_a_ss_start_age/person_b_ss_start_age.
  assert.equal(
    ctx.document.getElementById("person_a_ss_fra_amount").value,
    "33000",
    "person_a_ss_fra_amount restored to the default",
  );
  assert.equal(
    ctx.document.getElementById("person_a_ss_claim_age").value,
    "67",
    "person_a_ss_claim_age restored to FRA",
  );
  assert.equal(
    ctx.document.getElementById("person_b_ss_fra_amount").value,
    "26000",
    "person_b_ss_fra_amount restored to the default FRA amount (not the effective 32,240)",
  );
  assert.equal(
    ctx.document.getElementById("person_b_ss_claim_age").value,
    "70",
    "person_b_ss_claim_age restored to the default claim age 70",
  );
});

// federal-tax overlay design ft1, item 7: "Reset must restore the knob to 50%."
// defaultScenarioEcho() doesn't carry taxable_gains_fraction, so
// buildDefaultScenarioBlob's inputs also omit it — applyScenarioToForm's own
// TAX_GAINS_FRACTION_DEFAULT fallback (50%) is what actually restores it here.
test("reset: restores the gains-fraction knob to 50%", async () => {
  const ctx = loadApp({ "tax-gains-fraction": "65" });
  ctx._fetchCalls.length = 0;

  await ctx.window.SavedScenarios.reset();

  assert.equal(
    ctx.document.getElementById("tax-gains-fraction").value,
    "50",
    "gains-fraction knob restored to the 50% default",
  );
});

// tax-drag design td1, DECISION 5: the lever has NO default value to fall back
// to (unlike the gains-fraction knob's 50%) — "empty = off" IS the default, so
// Reset must CLEAR the field to blank, not restore any particular number.
test("reset: clears the bracket-fill lever to blank (no default to restore)", async () => {
  const ctx = loadApp({ "bracket-fill-target": "100800" });
  ctx._fetchCalls.length = 0;

  await ctx.window.SavedScenarios.reset();

  assert.equal(
    ctx.document.getElementById("bracket-fill-target").value,
    "",
    "bracket-fill lever cleared to blank (off) on reset",
  );
});

test("reset: exits an active comparison (isComparing -> false)", async () => {
  const ctx = loadApp({});
  evalIn(ctx, "compareState.enterComparison();");
  assert.equal(evalIn(ctx, "compareState.isComparing()"), true, "comparing pre-reset");

  await ctx.window.SavedScenarios.reset();

  assert.equal(
    evalIn(ctx, "compareState.isComparing()"),
    false,
    "single scenario after reset",
  );
  assert.equal(
    evalIn(ctx, "compareState.allocationLinked()"),
    true,
    "allocation relinked after reset",
  );
});

test("reset: never calls a DELETE/save endpoint; exactly one POST (recompute)", async () => {
  const ctx = loadApp({});
  evalIn(ctx, "compareState.enterComparison();");
  ctx._fetchCalls.length = 0;

  await ctx.window.SavedScenarios.reset();

  const methods = ctx._fetchCalls.map((c) => c.method);
  assert.equal(
    methods.filter((m) => m === "DELETE").length,
    0,
    "no DELETE — saved scenarios are not touched",
  );
  assert.equal(
    methods.filter((m) => m === "PUT").length,
    0,
    "no PUT/save — the library is left intact",
  );
  assert.equal(
    methods.filter((m) => m === "POST").length,
    1,
    "exactly one recompute (POST) at the end",
  );
});

test("reset: no-op when the default echo isn't cached yet", async () => {
  const ctx = loadApp({});
  evalIn(ctx, "defaultScenario = null;"); // init() hasn't completed
  ctx._fetchCalls.length = 0;

  const result = await ctx.window.SavedScenarios.reset();

  assert.equal(result, null, "returns null with no default to rebuild from");
  assert.equal(ctx._fetchCalls.length, 0, "no recompute when there's nothing to reset to");
});
