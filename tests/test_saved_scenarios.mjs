// test_saved_scenarios.mjs — round-trip equivalence for the Saved Scenarios
// forward/inverse maps (web/app.js: captureScenario / applyScenarioToForm, sv1).
//
// The acceptance invariant from the design: captureScenario() AFTER
// applyScenarioToForm(blob) reproduces the same readScenarios() payload — a
// scenario survives a save/load cycle byte-equivalently in the payload the app
// produces. We assert it at the blob level: apply(blob) then capture(name) must
// reproduce blob.view + blob.inputs (the server stamps id/created/updated/rev,
// which capture does NOT emit, so we compare the client-owned fields).
//
// Loaded like test_glidepath_view.mjs / test_comparison_state.mjs: app.js is read
// off disk and evaluated in a node:vm sandbox. We load it with NO `document` so
// the init() bootstrap is skipped, then inject a lightweight fake DOM + a stub
// `fetch` so capture/apply (exposed on window.SavedScenarios) can run without a
// browser or the live service. Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_saved_scenarios.mjs

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

// ---------- a lightweight, auto-vivifying fake DOM ----------
//
// Every render/chrome function in app.js reads through document.getElementById /
// querySelector. We give them a fake DOM that never returns null for an element
// lookup by id (so `el.textContent = ...` etc. never throw) but DOES return a
// real, value-bearing element for the inputs we pre-seed by id — so reads return
// what applyScenarioToForm wrote. querySelectorAll returns [] (no toggle buttons
// to iterate). This keeps the round-trip focused on the data path, not the chart.

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

// Container ids whose presence makes a render function build DOM subtrees. We
// return null for these so buildAllocation / renderGlidepathPhases /
// renderBucketMatrices / refreshAllocation early-return (their `if (!container)
// return` guards), keeping the round-trip on the DATA path: reads then fall back
// to ASSET_CLASSES / the glidepath+bucket state that applyScenarioToForm set.
const NULL_CONTAINER_IDS = new Set([
  "allocation-rows",
  "returns-fields",
  "allocation-totals",
  "glidepath-phases",
  "bucket-matrices",
  "allocation-toggle",
  "preset-select",
]);

// Per-class matrix INPUT ids (weight / return / vol / glidepath / bucket). These
// are NOT rendered in the test (their containers are null), so they must also
// resolve to null — then readWeight/readReturn/readVolFraction/readGlidepathWeight
// /readBucketWeight fall back to the ASSET_CLASSES model + glidepath/bucket state
// that applyScenarioToForm set, which is exactly what capture must read back.
const MATRIX_INPUT_RE =
  /^(alloc-weight-|alloc-return-|alloc-vol-|glide-w-|bucket-w-)/;

function makeFakeDocument(seed) {
  // seed: { id -> initial value } for inputs we want to read back.
  const byId = new Map();
  for (const [id, value] of Object.entries(seed || {})) {
    const el = makeElement(id);
    el.value = value;
    byId.set(id, el);
  }
  const doc = {
    getElementById(id) {
      if (NULL_CONTAINER_IDS.has(id)) return null;
      if (MATRIX_INPUT_RE.test(id)) return null;
      if (!byId.has(id)) byId.set(id, makeElement(id));
      return byId.get(id);
    },
    querySelector() {
      // Singular lookups (e.g. panelSummaryEl) get a safe fake element so
      // `.textContent = ...` never throws on the error/render path.
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
  return doc;
}

// The 12 scalar fields, seeded by their bare-name input id (aId(name) === name).
function seedForInputs(inputs) {
  const seed = {};
  const SCALARS = [
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
  for (const f of SCALARS) {
    if (inputs[f] !== undefined) seed[f] = String(inputs[f]);
  }
  // Guardrail + correlation inputs exist in the form; seed empty so reads use the
  // value applyScenarioToForm writes into them.
  for (const id of [
    "guard-band",
    "guard-adjust",
    "spending-floor",
    "spending-ceiling",
    "mc-correlation",
  ]) {
    seed[id] = "";
  }
  // The bucket-year inputs are STATIC HTML that is never re-rendered, so they
  // carry the page's literal default values ("2"/"7") at load. Seed them with
  // those real defaults — NOT empty strings — so readBucketN1/N2 (which read the
  // DOM first, falling back to the global only when blank) exercise the actual
  // load path: applyScenarioToForm must WRITE these inputs, not just set globals.
  seed["bucket-n1"] = "2";
  seed["bucket-n2"] = "7";
  return seed;
}

// Load app.js with NO document (init skipped), then inject the fake DOM + fetch.
function loadApp(seedInputs) {
  const ctx = { console: { ...console, warn() {}, error() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.confirm = () => true;
  // A stub fetch that REJECTS, so rerender()'s fetchScenario catches the error
  // and returns null (its try/catch sets an error line + returns null). The
  // render chain then short-circuits on the null payload. The round-trip we test
  // is the FORM-STATE path (apply sets the form, capture reads it back); the
  // actual chart render is the integrator's live-gate concern, not this unit.
  ctx.fetch = async () => {
    throw new Error("fetch disabled in round-trip unit test");
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  // Inject the DOM now (after load), so init()'s `typeof document` guard saw no
  // document and was skipped, but our functions can run against a real fake DOM.
  ctx.document = makeFakeDocument(seedForInputs(seedInputs));
  return ctx;
}

// Coerce a sandbox-realm value to a plain same-realm object for deepEqual.
function plain(v) {
  return JSON.parse(JSON.stringify(v));
}

// Run apply(blob) then capture(name); return the captured blob's client-owned
// fields (view + inputs + schema_version + name) for comparison against the
// input blob.
async function roundTrip(blob) {
  const ctx = loadApp(blob.inputs);
  const SS = ctx.window.SavedScenarios;
  assert.equal(typeof SS, "object", "window.SavedScenarios is exposed");
  await SS.apply(blob);
  const recaptured = SS.capture(blob.name);
  return plain(recaptured);
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

// Assert the recaptured blob reproduces the client-owned fields of the input.
function assertRoundTrip(input, recaptured) {
  assert.equal(recaptured.schema_version, input.schema_version);
  assert.equal(recaptured.name, input.name);
  assert.deepEqual(recaptured.view, input.view);
  assert.deepEqual(recaptured.inputs, input.inputs);
}

test("SavedScenarios is exposed and init() is skipped without a DOM", () => {
  const ctx = loadApp({});
  assert.equal(typeof ctx.window.SavedScenarios, "object");
  assert.equal(typeof ctx.window.SavedScenarios.capture, "function");
  assert.equal(typeof ctx.window.SavedScenarios.apply, "function");
});

test("round-trip: constant / deterministic", async () => {
  const blob = baseBlob();
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: lifecycle spending", async () => {
  const blob = baseBlob({ view: { spending_mode: "lifecycle" } });
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: guardrails (knobs survive)", async () => {
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
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: fr_smooth (funded-ratio smooth; floor/ceiling survive)", async () => {
  // FR slice 2: fr_smooth reuses the same floor/ceiling rails as guardrails (the
  // four guardrail knobs are harmless/validated under it), so a saved fr_smooth view
  // must round-trip its mode + the rail inputs.
  const blob = baseBlob({
    view: { spending_mode: "fr_smooth" },
    inputs: {
      guardrails: {
        guard_band: 0.2,
        guard_adjust: 0.1,
        spending_floor: 85000,
        spending_ceiling: 150000,
      },
    },
  });
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: glidepath (explicit per-phase weight map survives)", async () => {
  // Default GI ages -> boundaries [50,60,65,67,70] -> 5 phases. Each phase weight
  // row is parallel to the 2-class default menu (loadClassMenu didn't run, so
  // ASSET_CLASSES is the 2-class fallback). Weights must total 100 per phase.
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
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: buckets sourcing (config survives)", async () => {
  // n1=5 / n2=10 are DELIBERATELY non-default (the static HTML defaults are 2/7,
  // which the test now seeds). readBucketN1/N2 read the DOM first, so this only
  // round-trips if applyScenarioToForm WRITES the #bucket-n1/#bucket-n2 inputs;
  // a setter that merely updates the globals would silently capture back 2/7.
  const blob = baseBlob({
    view: { sourcing_mode: "buckets" },
    inputs: {
      buckets: {
        n1: 5,
        n2: 10,
        cash: {
          id: "cash",
          allocation: [
            { id: "us_agg_bonds", weight: 1.0, real_return: 0.02, volatility: 0.088 },
          ],
        },
        income: {
          id: "income",
          allocation: [
            { id: "us_agg_bonds", weight: 1.0, real_return: 0.02, volatility: 0.088 },
          ],
        },
        growth: {
          id: "growth",
          allocation: [
            { id: "us_total_market", weight: 1.0, real_return: 0.07, volatility: 0.174 },
          ],
        },
      },
    },
  });
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("round-trip: monte carlo (correlation + per-class vols survive)", async () => {
  // In MC mode the per-class vol in `allocation[]` and the `volatilities` map
  // both derive from the SAME ASSET_CLASSES[i].volatility, so a real capture
  // keeps them consistent. The fixture mirrors that: allocation vols == the
  // volatilities map (0.18 / 0.07), which is what apply->capture reproduces.
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
  const re = await roundTrip(blob);
  assertRoundTrip(blob, re);
});

test("capture reflects the four view modes from the globals", async () => {
  const blob = baseBlob({
    view: {
      sim_mode: "historical",
      spending_mode: "lifecycle",
      allocation_mode: "constant",
      sourcing_mode: "single",
    },
  });
  const re = await roundTrip(blob);
  assert.deepEqual(re.view, blob.view);
});
