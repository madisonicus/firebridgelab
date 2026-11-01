// test_scenario_store.mjs — pin the ScenarioStore contract + the round-trip
// invariant the Saved Scenarios store never had a test for.
//
// ScenarioStore (web/scenario_store.js) owns the two PURE jobs pulled out of
// app.js's captureScenario / diffScenarioForCompare:
//   - ScenarioStore.diff(savedBlob, currentA, fieldNames)  — VERBATIM diff record.
//   - ScenarioStore.capture(captureState)                  — PURE blob shaping.
// Both are realm-pure (no document), so they load in a node:vm sandbox exactly
// like test_payload_assembler.mjs.
//
// THE ROUND-TRIP: the documented acceptance invariant — captureScenario() after
// applyScenarioToForm(blob) reproduces the blob — is exercised through the same
// document-stub harness as test_saved_scenarios.mjs (apply is an async DOM mutator
// that is NOT pure, so we drive it against a fake DOM + stub fetch). With capture's
// shaping now backed by ScenarioStore.capture, this asserts the pure shaping AND
// the DOM-driven apply agree end-to-end.
//
// Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_scenario_store.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const SS_PATH = path.join(WEB_DIR, "scenario_store.js");

// ---------------------------------------------------------------------------
// PURE module: load scenario_store.js in its own vm realm (like the
// PayloadAssembler tests). Returned objects have a different realm's
// Object/Array constructor, so coerce to same-realm plain values before
// deep-asserting.
// ---------------------------------------------------------------------------
function loadScenarioStore() {
  const source = fs.readFileSync(SS_PATH, "utf8");
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.ScenarioStore;
}
function plain(x) {
  return JSON.parse(JSON.stringify(x));
}

const FIELD_NAMES = [
  "initial_portfolio",
  "annual_spending",
  "start_age",
  "end_age",
  "person_a_pension_amount",
  "person_a_pension_start_age",
  "person_a_ss_fra_amount",
  "person_a_ss_claim_age",
  "person_b_pension_amount",
  "person_b_pension_start_age",
  "person_b_ss_fra_amount",
  "person_b_ss_claim_age",
];

// ---------------------------------------------------------------------------
// ScenarioStore.diff
// ---------------------------------------------------------------------------

function currentA(overrides = {}) {
  return {
    view: {
      sim_mode: "deterministic",
      spending_mode: "constant",
      allocation_mode: "constant",
      sourcing_mode: "single",
      ...(overrides.view || {}),
    },
    inputs: {
      initial_portfolio: 2_000_000,
      annual_spending: 80_000,
      start_age: 60,
      allocation: [
        { id: "us_total_market", weight: 0.6 },
        { id: "us_agg_bonds", weight: 0.4 },
      ],
      ...(overrides.inputs || {}),
    },
  };
}
function savedBlob(overrides = {}) {
  return {
    name: "Saved B",
    view: {
      sim_mode: "deterministic",
      spending_mode: "constant",
      allocation_mode: "constant",
      sourcing_mode: "single",
      ...(overrides.view || {}),
    },
    inputs: {
      initial_portfolio: 2_000_000,
      annual_spending: 80_000,
      start_age: 60,
      allocation: [
        { id: "us_total_market", weight: 0.6 },
        { id: "us_agg_bonds", weight: 0.4 },
      ],
      ...(overrides.inputs || {}),
    },
  };
}

test("diff: identical scenarios -> identical:true, nothing to unlink", () => {
  const SS = loadScenarioStore();
  const d = SS.diff(savedBlob(), currentA(), FIELD_NAMES);
  assert.deepEqual(plain(d), {
    fieldsToUnlink: [],
    allocationDiffers: false,
    modeMismatch: false,
    mismatchedModes: [],
    identical: true,
  });
});

test("diff: a changed scalar field is flagged to unlink (not identical)", () => {
  const SS = loadScenarioStore();
  // A has 80k spending; the saved blob has 100k -> annual_spending unlinks.
  const d = SS.diff(
    savedBlob({ inputs: { annual_spending: 100_000 } }),
    currentA(),
    FIELD_NAMES,
  );
  assert.deepEqual(plain(d.fieldsToUnlink), ["annual_spending"]);
  assert.equal(d.allocationDiffers, false);
  assert.equal(d.identical, false);
});

test("diff: missing-on-one-side scalar is NOT a diff", () => {
  const SS = loadScenarioStore();
  // The saved blob omits start_age -> not flagged (only one value to show).
  const saved = savedBlob();
  delete saved.inputs.start_age;
  const d = SS.diff(saved, currentA(), FIELD_NAMES);
  assert.equal(d.fieldsToUnlink.includes("start_age"), false);
});

test("diff: allocation differing in integer-percent flags allocationDiffers", () => {
  const SS = loadScenarioStore();
  const d = SS.diff(
    savedBlob({
      inputs: {
        allocation: [
          { id: "us_total_market", weight: 0.8 },
          { id: "us_agg_bonds", weight: 0.2 },
        ],
      },
    }),
    currentA(),
    FIELD_NAMES,
  );
  assert.equal(d.allocationDiffers, true);
  assert.equal(d.identical, false);
});

test("diff: float dust (0.6 vs 0.6000001) does NOT flag a phantom allocation diff", () => {
  const SS = loadScenarioStore();
  const d = SS.diff(
    savedBlob({
      inputs: {
        allocation: [
          { id: "us_total_market", weight: 0.6000001 },
          { id: "us_agg_bonds", weight: 0.4 },
        ],
      },
    }),
    currentA(),
    FIELD_NAMES,
  );
  assert.equal(d.allocationDiffers, false);
});

test("diff: a mismatched GLOBAL mode is reported (glidepath case)", () => {
  const SS = loadScenarioStore();
  const d = SS.diff(
    savedBlob({ view: { allocation_mode: "glidepath" } }),
    currentA(),
    FIELD_NAMES,
  );
  assert.equal(d.modeMismatch, true);
  assert.deepEqual(plain(d.mismatchedModes), ["allocation"]);
});

test("diff: multiple mode mismatches preserve the canonical label order", () => {
  const SS = loadScenarioStore();
  const d = SS.diff(
    savedBlob({
      view: { sim_mode: "monte_carlo", sourcing_mode: "buckets" },
    }),
    currentA(),
    FIELD_NAMES,
  );
  // MODE_KEYS order is engine, spending, allocation, sourcing.
  assert.deepEqual(plain(d.mismatchedModes), ["engine", "sourcing"]);
});

test("diff: a saved bucket config with a differing scalar still unlinks the scalar", () => {
  // Sanity: the diff is scalar+constant-matrix only; a bucket-mode mismatch is
  // reported via mismatchedModes, and any scalar delta still unlinks.
  const SS = loadScenarioStore();
  const d = SS.diff(
    savedBlob({
      view: { sourcing_mode: "buckets" },
      inputs: { annual_spending: 95_000 },
    }),
    currentA(),
    FIELD_NAMES,
  );
  assert.deepEqual(plain(d.fieldsToUnlink), ["annual_spending"]);
  assert.deepEqual(plain(d.mismatchedModes), ["sourcing"]);
  assert.equal(d.identical, false);
});

// ---------------------------------------------------------------------------
// ScenarioStore.capture — exact blob keys/values + conditional inclusion
// ---------------------------------------------------------------------------

function baseCaptureState(overrides = {}) {
  return {
    schemaVersion: 1,
    name: "Cap",
    fieldNames: FIELD_NAMES,
    side: {
      initial_portfolio: 1_800_000,
      annual_spending: 110_000,
      start_age: 50,
      end_age: 95,
      person_a_pension_amount: 30_000,
      person_a_pension_start_age: 60,
      person_a_ss_fra_amount: 40_000,
      person_a_ss_claim_age: 67,
      person_b_pension_amount: 20_000,
      person_b_pension_start_age: 65,
      person_b_ss_fra_amount: 35_000,
      person_b_ss_claim_age: 70,
      allocationBody: [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
      ],
      ...(overrides.side || {}),
    },
    view: {
      sim_mode: "deterministic",
      spending_mode: "constant",
      allocation_mode: "constant",
      sourcing_mode: "single",
      ...(overrides.view || {}),
    },
    glidepathPhaseWeights: null,
    bucketsBody: null,
    guardrails: null,
    monteCarlo: null,
    taxLocation: null,
    taxableGainsFraction: null,
    ...overrides.top,
  };
}

test("capture: constant/single shapes the baseline blob (exact keys + values)", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(baseCaptureState());
  assert.deepEqual(plain(blob), {
    schema_version: 1,
    name: "Cap",
    view: {
      sim_mode: "deterministic",
      spending_mode: "constant",
      allocation_mode: "constant",
      sourcing_mode: "single",
    },
    inputs: {
      initial_portfolio: 1_800_000,
      annual_spending: 110_000,
      start_age: 50,
      end_age: 95,
      person_a_pension_amount: 30_000,
      person_a_pension_start_age: 60,
      person_a_ss_fra_amount: 40_000,
      person_a_ss_claim_age: 67,
      person_b_pension_amount: 20_000,
      person_b_pension_start_age: 65,
      person_b_ss_fra_amount: 35_000,
      person_b_ss_claim_age: 70,
      allocation: [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
      ],
    },
  });
  // No conditional blocks leak in when their modes are off.
  for (const key of [
    "glidepath_phase_weights",
    "buckets",
    "guardrails",
    "correlation",
    "volatilities",
    "tax_location",
    "taxable_gains_fraction",
    "bracket_fill_target",
  ]) {
    assert.equal(key in blob.inputs, false, `inputs must not carry ${key}`);
  }
});

test("capture: allocation entries are COPIED (not the same refs as side.allocationBody)", () => {
  const SS = loadScenarioStore();
  const state = baseCaptureState();
  const blob = SS.capture(state);
  // captureScenario did `(side.allocationBody||[]).map(e => ({...e}))` — fresh objects.
  assert.notEqual(blob.inputs.allocation[0], state.side.allocationBody[0]);
  assert.deepEqual(plain(blob.inputs.allocation), plain(state.side.allocationBody));
});

test("capture: missing allocationBody yields an empty allocation array", () => {
  const SS = loadScenarioStore();
  const state = baseCaptureState();
  delete state.side.allocationBody;
  const blob = SS.capture(state);
  assert.deepEqual(plain(blob.inputs.allocation), []);
});

test("capture: glidepath -> glidepath_phase_weights ride in inputs", () => {
  const SS = loadScenarioStore();
  const map = { "0": [80, 20], "1": [70, 30], "2": [60, 40], "3": [40, 60] };
  const blob = SS.capture(
    baseCaptureState({
      view: { allocation_mode: "glidepath" },
      top: { glidepathPhaseWeights: map },
    }),
  );
  assert.deepEqual(plain(blob.inputs.glidepath_phase_weights), map);
});

test("capture: buckets -> the bucket config rides in inputs", () => {
  const SS = loadScenarioStore();
  const buckets = { n1: 5, n2: 10, cash: {}, income: {}, growth: {} };
  const blob = SS.capture(
    baseCaptureState({
      view: { sourcing_mode: "buckets" },
      top: { bucketsBody: buckets },
    }),
  );
  assert.deepEqual(plain(blob.inputs.buckets), buckets);
});

test("capture: guardrails -> the knob set rides in inputs", () => {
  const SS = loadScenarioStore();
  const g = {
    guard_band: 0.2,
    guard_adjust: 0.1,
    spending_floor: 85000,
    spending_ceiling: 150000,
  };
  const blob = SS.capture(
    baseCaptureState({
      view: { spending_mode: "guardrails" },
      top: { guardrails: g },
    }),
  );
  assert.deepEqual(plain(blob.inputs.guardrails), g);
});

test("capture: monte carlo -> correlation + volatilities ride in inputs", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(
    baseCaptureState({
      view: { sim_mode: "monte_carlo" },
      top: {
        monteCarlo: {
          correlation: 0.25,
          volatilities: { us_total_market: 0.18, us_agg_bonds: 0.07 },
        },
      },
    }),
  );
  assert.equal(blob.inputs.correlation, 0.25);
  assert.deepEqual(plain(blob.inputs.volatilities), {
    us_total_market: 0.18,
    us_agg_bonds: 0.07,
  });
});

test("capture: tax-location -> the three balances ride in inputs", () => {
  const SS = loadScenarioStore();
  const tl = { taxable: 1_100_000, tax_deferred: 900_000, roth: 0 };
  const blob = SS.capture(baseCaptureState({ top: { taxLocation: tl } }));
  assert.deepEqual(plain(blob.inputs.tax_location), tl);
});

test("capture: taxable-gains-fraction (federal-tax overlay ft1) -> rides in inputs when a number", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(
    baseCaptureState({ top: { taxableGainsFraction: 0.35 } }),
  );
  assert.equal(blob.inputs.taxable_gains_fraction, 0.35);
});

test("capture: taxable-gains-fraction of exactly 0 still rides (falsy-but-valid)", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(
    baseCaptureState({ top: { taxableGainsFraction: 0 } }),
  );
  assert.equal(blob.inputs.taxable_gains_fraction, 0);
});

test("capture: taxable-gains-fraction null (split inactive) -> omitted from inputs", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(baseCaptureState()); // taxableGainsFraction: null (default)
  assert.equal("taxable_gains_fraction" in blob.inputs, false);
});

test("capture: bracket-fill lever (tax-drag td1) -> rides in inputs when a number", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(baseCaptureState({ top: { bracketFillTarget: 100_800 } }));
  assert.equal(blob.inputs.bracket_fill_target, 100_800);
});

test("capture: bracket-fill lever of exactly 0 still rides (falsy-but-valid)", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(baseCaptureState({ top: { bracketFillTarget: 0 } }));
  assert.equal(blob.inputs.bracket_fill_target, 0);
});

test("capture: bracket-fill lever null (split active, lever blank) -> omitted from inputs", () => {
  const SS = loadScenarioStore();
  const blob = SS.capture(baseCaptureState()); // bracketFillTarget: undefined (default)
  assert.equal("bracket_fill_target" in blob.inputs, false);
});

test("capture: ScenarioStore exposes its public surface", () => {
  const SS = loadScenarioStore();
  assert.equal(typeof SS.capture, "function");
  assert.equal(typeof SS.diff, "function");
  assert.equal(typeof SS.allocationPercentMap, "function");
});

// ---------------------------------------------------------------------------
// THE ROUND-TRIP — capture ∘ apply reproduces the blob, through the document-stub
// harness (apply is an async DOM mutator and not pure). Same fake-DOM technique as
// test_saved_scenarios.mjs; this file asserts the invariant now that capture's
// shaping is backed by ScenarioStore.capture.
// ---------------------------------------------------------------------------

const DEP_PATHS = [
  "chart_view.js",
  "comparison_state.js",
  "payload_assembler.js",
  "scenario_store.js",
  "ss_claim.js",
].map((f) => path.join(WEB_DIR, f));
const APP_PATH = path.join(WEB_DIR, "app.js");

function makeElement(id) {
  const children = [];
  const attrs = {};
  return {
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
const MATRIX_INPUT_RE = /^(alloc-weight-|alloc-return-|alloc-vol-|glide-w-|bucket-w-)/;

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

function seedForInputs(inputs) {
  const seed = {};
  for (const f of FIELD_NAMES) {
    if (inputs[f] !== undefined) seed[f] = String(inputs[f]);
  }
  for (const id of [
    "guard-band",
    "guard-adjust",
    "spending-floor",
    "spending-ceiling",
    "mc-correlation",
  ]) {
    seed[id] = "";
  }
  seed["bucket-n1"] = "2";
  seed["bucket-n2"] = "7";
  return seed;
}

function loadApp(seedInputs) {
  const ctx = { console: { ...console, warn() {}, error() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.confirm = () => true;
  ctx.fetch = async () => {
    throw new Error("fetch disabled in round-trip unit test");
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = makeFakeDocument(seedForInputs(seedInputs));
  return ctx;
}

async function roundTrip(blob) {
  const ctx = loadApp(blob.inputs);
  const SS = ctx.window.SavedScenarios;
  assert.equal(typeof SS, "object", "window.SavedScenarios is exposed");
  await SS.apply(blob);
  return plain(SS.capture(blob.name));
}

function baseBlob(overrides = {}) {
  return {
    schema_version: 1,
    name: overrides.name || "Round Trip",
    view: {
      sim_mode: "deterministic",
      spending_mode: "constant",
      allocation_mode: "constant",
      sourcing_mode: "single",
      ...(overrides.view || {}),
    },
    inputs: {
      initial_portfolio: 1_800_000,
      annual_spending: 110_000,
      start_age: 50,
      end_age: 95,
      person_a_pension_amount: 30_000,
      person_a_pension_start_age: 60,
      person_a_ss_fra_amount: 40_000,
      person_a_ss_claim_age: 67,
      person_b_pension_amount: 20_000,
      person_b_pension_start_age: 65,
      person_b_ss_fra_amount: 35_000,
      person_b_ss_claim_age: 70,
      allocation: [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
      ],
      ...(overrides.inputs || {}),
    },
  };
}

function assertRoundTrip(input, re) {
  assert.equal(re.schema_version, input.schema_version);
  assert.equal(re.name, input.name);
  assert.deepEqual(re.view, input.view);
  assert.deepEqual(re.inputs, input.inputs);
}

test("ROUND-TRIP: constant / deterministic reproduces the blob", async () => {
  const blob = baseBlob();
  assertRoundTrip(blob, await roundTrip(blob));
});

test("ROUND-TRIP: guardrails (knobs survive capture∘apply)", async () => {
  const blob = baseBlob({
    view: { spending_mode: "guardrails" },
    inputs: {
      guardrails: {
        guard_band: 0.2,
        guard_adjust: 0.1,
        spending_floor: 85000,
        spending_ceiling: 150000,
      },
    },
  });
  assertRoundTrip(blob, await roundTrip(blob));
});

test("ROUND-TRIP: glidepath (per-phase weight map survives)", async () => {
  const blob = baseBlob({
    view: { allocation_mode: "glidepath" },
    inputs: {
      glidepath_phase_weights: {
        "0": [80, 20],
        "1": [70, 30],
        "2": [60, 40],
        "3": [40, 60],
        "4": [30, 70],
      },
    },
  });
  assertRoundTrip(blob, await roundTrip(blob));
});

test("ROUND-TRIP: monte carlo (correlation + per-class vols survive)", async () => {
  const blob = baseBlob({
    view: { sim_mode: "monte_carlo" },
    inputs: {
      allocation: [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.18 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.07 },
      ],
      correlation: 0.25,
      volatilities: { us_total_market: 0.18, us_agg_bonds: 0.07 },
    },
  });
  assertRoundTrip(blob, await roundTrip(blob));
});
