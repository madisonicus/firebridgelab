// test_scenario_fresh_state_load.mjs — the FRESH-PAGE saved-scenario load path.
//
// Regression for the saved-scenario allocation-revert bug: saving a scenario whose
// allocation uses a NON-DEFAULT class set (e.g. the Permanent Portfolio:
// us_total_market / lt_treasury / st_treasury / gold) then reloading on a FRESH page
// silently reverted the matrix to the default 60/40. ZERO console errors.
//
// Root cause: applyScenarioToForm wrote the new weights to the ASSET_CLASSES MODEL
// then called buildAllocation() -> renderAllocationRows(), which re-seeded every
// weight input from the EXISTING (stale, pre-load) DOM inputs — on a fresh page those
// still held the default 60/40 — silently discarding the model writes. The in-session
// path "worked" only because the DOM already happened to hold the loaded weights (the
// preset had written them there directly). The original round-trip test
// (test_saved_scenarios.mjs) reads weights straight from the MODEL (its fake DOM nulls
// the matrix inputs), so it never exercised the stale-DOM seeding and missed the bug.
//
// This test uses a FAITHFUL DOM: the allocation-rows / returns-fields containers are
// real, createElement+appendChild REGISTER inputs by id (so getElementById finds the
// rendered weight inputs), and the matrix weight inputs are PRE-SEEDED to the fresh
// page's default 60/40 BEFORE the load. It drives the real init-equivalent sequence
// (loadClassMenu -> a default render -> apply(blob) -> capture) and asserts the
// captured allocation matches the loaded blob — i.e. the rendered matrix reflects the
// loaded class set + weights, not the stale default.
//
// Run:
//   (from the repo root) node --test tests/test_scenario_fresh_state_load.mjs

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

// The 12-class menu /api/classes returns (subset of metadata app.js reads). PP uses
// us_total_market + lt_treasury + st_treasury + gold — the latter three are NOT in
// the 2-class fallback seed, so the fresh-page bug needs the real menu populated.
const CLASS_MENU = [
  { id: "us_total_market", label: "US Total Stock Market", real_return: 0.07, volatility: 0.174, data_start_year: 1871, is_equity: true },
  { id: "us_agg_bonds", label: "US Bonds (10-yr Treasury proxy)", real_return: 0.02, volatility: 0.088, data_start_year: 1871, is_equity: false },
  { id: "lt_treasury", label: "Long-Term Treasuries", real_return: 0.025, volatility: 0.11, data_start_year: 1871, is_equity: false },
  { id: "st_treasury", label: "Short-Term Treasuries", real_return: 0.01, volatility: 0.03, data_start_year: 1871, is_equity: false },
  { id: "gold", label: "Gold", real_return: 0.012, volatility: 0.16, data_start_year: 1968, is_equity: false },
  { id: "tips", label: "TIPS", real_return: 0.015, volatility: 0.07, data_start_year: 1997, is_equity: false },
];

// The default-scenario echo (the canary 60/40), keyed into loadClassMenu so the two
// legacy classes keep their default weights/returns and the others default to 0%.
const DEFAULT_SCENARIO = {
  allocation: [
    { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
    { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
  ],
  correlation: 0.2,
};

// ---------- a faithful fake DOM that REGISTERS created inputs by id ----------
//
// Unlike test_saved_scenarios.mjs (which nulls the matrix containers + inputs so reads
// fall back to the model), this harness lets renderAllocationRows / renderReturnInputs
// actually BUILD inputs and registers each created element by its assigned id, so
// getElementById -> readWeight reads the RENDERED DOM. That's the path the bug lived in.

function makeElement(tag, byId) {
  const children = [];
  const attrs = {};
  let _id = "";
  const el = {
    tagName: String(tag || "").toUpperCase(),
    _value: "",
    children,
    dataset: {},
    style: {},
    hidden: false,
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    get id() { return _id; },
    set id(v) {
      _id = String(v);
      // Register by id on assignment so a later getElementById(this.id) finds it.
      if (byId && _id) byId.set(_id, el);
    },
    get value() { return this._value; },
    set value(v) { this._value = String(v); },
    set textContent(_v) {},
    get textContent() { return ""; },
    set innerHTML(_v) { children.length = 0; },
    get innerHTML() { return ""; },
    appendChild(c) { children.push(c); return c; },
    removeChild() {},
    setAttribute(k, v) { attrs[k] = v; },
    getAttribute(k) { return attrs[k]; },
    removeAttribute(k) { delete attrs[k]; },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return false; },
    closest() { return null; },
    focus() {},
    destroy() {},
    getContext() { return {}; },
  };
  return el;
}

// Real containers the allocation/return renderers build into (so they DON'T early
// return). Everything else auto-vivifies. The matrix INPUT ids are intentionally NOT
// null here — they resolve to the elements renderAllocationRows registered.
const REAL_CONTAINER_IDS = new Set([
  "allocation-rows",
  "returns-fields",
  "allocation-totals",
  "allocation-blended",
  "allocation-toggle",
  "preset-select",
  "preset-note",
  "correlation-field",
]);

function makeFakeDocument(seed) {
  const byId = new Map();
  for (const [id, value] of Object.entries(seed || {})) {
    const el = makeElement("input", byId);
    el.id = id;
    el.value = value;
  }
  const doc = {
    getElementById(id) {
      if (!byId.has(id)) {
        const el = makeElement("div", byId);
        el.id = id;
      }
      return byId.get(id);
    },
    querySelector() { return makeElement("div", byId); },
    querySelectorAll() { return []; },
    createElement(tag) { return makeElement(tag, byId); },
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
    addEventListener() {},
    body: makeElement("body", byId),
    _byId: byId,
  };
  // Keep REAL_CONTAINER_IDS referenced for clarity of intent (all ids are real here;
  // the set documents which ones the renderers depend on existing).
  void REAL_CONTAINER_IDS;
  return doc;
}

// Seed the 12 scalar field inputs + the fresh-page DEFAULT allocation weight inputs
// (us_total_market=60, us_agg_bonds=40, the rest 0). These pre-seeded weight inputs
// are the STALE DOM the bug used to re-read; the fix must override them from the model.
function seedFreshPage(inputs) {
  const seed = {};
  const SCALARS = [
    "initial_portfolio", "annual_spending", "start_age", "end_age",
    "person_a_pension_amount", "person_a_pension_start_age", "person_a_ss_fra_amount", "person_a_ss_claim_age",
    "person_b_pension_amount", "person_b_pension_start_age", "person_b_ss_fra_amount", "person_b_ss_claim_age",
  ];
  for (const f of SCALARS) {
    if (inputs[f] !== undefined) seed[f] = String(inputs[f]);
  }
  for (const id of ["guard-band", "guard-adjust", "spending-floor", "spending-ceiling", "mc-correlation"]) {
    seed[id] = "";
  }
  seed["bucket-n1"] = "2";
  seed["bucket-n2"] = "7";
  // Fresh-page default matrix: weight inputs for the 12-class menu order in CLASS_MENU.
  // Only the two legacy classes carry the canary 60/40; the others render at 0%.
  const defaultWeights = { us_total_market: 60, us_agg_bonds: 40 };
  CLASS_MENU.forEach((c, i) => {
    seed[`alloc-weight-a-${i}`] = String(defaultWeights[c.id] || 0);
    seed[`alloc-return-${i}`] = String(Math.round(c.real_return * 1000) / 10);
    seed[`alloc-vol-${i}`] = (c.volatility * 100).toFixed(1);
  });
  return seed;
}

// Load app.js with NO document (init skipped), then inject the faithful DOM + a fetch
// that serves the class menu / presets. ASSET_CLASSES starts as the 2-class fallback;
// the test calls SavedScenarios.loadClassMenu(DEFAULT_SCENARIO) to populate the 12.
function loadApp(seedInputs) {
  const ctx = { console: { ...console, warn() {}, error() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.confirm = () => true;
  ctx.fetch = async (url) => {
    const u = String(url);
    if (u.includes("api/classes")) {
      return { ok: true, json: async () => ({ classes: CLASS_MENU }) };
    }
    if (u.includes("api/presets")) {
      return { ok: true, json: async () => ({ presets: [] }) };
    }
    // Any other fetch (the post-apply rerender's trajectory POST) rejects; rerender
    // catches it and returns null. The form-state path is what this test asserts.
    throw new Error("fetch disabled for non-menu URLs in this unit");
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = makeFakeDocument(seedFreshPage(seedInputs));
  return ctx;
}

function plain(v) { return JSON.parse(JSON.stringify(v)); }

function baseBlob(overrides = {}) {
  return {
    schema_version: 1,
    name: overrides.name || "Permanent Portfolio",
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
      allocation: overrides.allocation || [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
      ],
      ...(overrides.inputs || {}),
    },
  };
}

// Drive the fresh-page sequence: populate the 12-class menu (as init does), render the
// default matrix once (so the stale default inputs exist), then apply(blob), capture.
async function freshLoadAndCapture(blob) {
  const ctx = loadApp(blob.inputs);
  const SS = ctx.window.SavedScenarios;
  assert.equal(typeof SS, "object", "window.SavedScenarios is exposed");
  assert.equal(typeof SS.loadClassMenu, "function", "loadClassMenu exposed for the fresh-state test");
  await SS.loadClassMenu(DEFAULT_SCENARIO); // ASSET_CLASSES -> 12-class menu, defaults 60/40
  await SS.apply(blob);
  return plain(SS.capture(blob.name));
}

// The Permanent Portfolio allocation: a 4-class set, three of which are NOT in the
// 2-class default seed — exactly the case that reverted to 60/40 on a fresh load.
const PP_ALLOCATION = [
  { id: "us_total_market", weight: 0.25, real_return: 0.07, volatility: 0.174 },
  { id: "lt_treasury", weight: 0.25, real_return: 0.025, volatility: 0.11 },
  { id: "st_treasury", weight: 0.25, real_return: 0.01, volatility: 0.03 },
  { id: "gold", weight: 0.25, real_return: 0.012, volatility: 0.16 },
];

test("fresh-state load: Permanent Portfolio (non-default class set) restores its full allocation", async () => {
  const blob = baseBlob({ name: "Permanent Portfolio", allocation: PP_ALLOCATION });
  const re = await freshLoadAndCapture(blob);
  // The captured allocation must be the PP class set + weights, NOT the default 60/40.
  assert.deepEqual(
    re.inputs.allocation,
    PP_ALLOCATION,
    "loaded PP allocation must survive a fresh-page load (regression: it reverted to 60/40)",
  );
  // Belt-and-suspenders: the default classes must NOT linger as a residual revert.
  const ids = re.inputs.allocation.map((a) => a.id);
  assert.deepEqual(ids, ["us_total_market", "lt_treasury", "st_treasury", "gold"]);
  assert.ok(!ids.includes("us_agg_bonds"), "us_agg_bonds (a default-only class) must be dropped to 0% and excluded");
});

test("fresh-state load: a single non-seed class (gold 100%) restores", async () => {
  // The starkest revert case: a class entirely absent from the 2-class seed, at 100%.
  const goldOnly = [{ id: "gold", weight: 1.0, real_return: 0.012, volatility: 0.16 }];
  const blob = baseBlob({ name: "All Gold", allocation: goldOnly });
  const re = await freshLoadAndCapture(blob);
  assert.deepEqual(re.inputs.allocation, goldOnly);
});

test("fresh-state load: default 60/40 still round-trips (no over-correction)", async () => {
  // Guard the fix didn't break the common case: a default-class-set save on a fresh
  // page must still restore 60/40 (the fromModel seam reads the model, which here IS
  // 60/40 after apply).
  const blob = baseBlob({ name: "Default" });
  const re = await freshLoadAndCapture(blob);
  assert.deepEqual(re.inputs.allocation, [
    { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
    { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
  ]);
});
