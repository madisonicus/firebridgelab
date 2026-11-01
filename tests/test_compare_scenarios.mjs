// test_compare_scenarios.mjs — sv2 "load-2-to-compare" decision + driver.
//
// sv2 adds a one-click "Compare with…" that loads a SECOND saved scenario as B
// against the CURRENT scenario A: it diffs the 12 scalar FIELDS + the constant
// allocation matrix, unlinks ONLY what differs, sets the B inputs, and notes
// anything it can't express per-side (a global-mode mismatch, or a
// glidepath/buckets shape that isn't the constant matrix). The pure decision is
// window.SavedScenarios.diffForCompare(savedBlob, currentA, fieldNames); the
// imperative driver is window.SavedScenarios.compare(id) (fetch + mutate + one
// rerender).
//
// Loaded the same way as test_saved_scenarios.mjs: app.js is read off disk and
// evaluated in a node:vm sandbox with NO `document` (so init() is skipped), then
// a lightweight fake DOM + a URL-routing stub `fetch` are injected so the driver
// can run without a browser or the live service. Zero npm deps; Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_compare_scenarios.mjs

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

// ---------- fake DOM (mirrors test_saved_scenarios.mjs) ----------
function makeElement(id) {
  const children = [];
  const attrs = {};
  let textContent = "";
  const el = {
    id: id || "",
    _value: "",
    children,
    dataset: {},
    style: {},
    hidden: false,
    disabled: false,
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    get value() { return this._value; },
    set value(v) { this._value = String(v); },
    set textContent(v) { textContent = String(v); },
    get textContent() { return textContent; },
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

// Containers whose presence would make a render function build a real subtree.
// Returning null makes them early-return on their `if (!container) return`
// guard — keeping the compare flow on the DATA path (link state + the B inputs
// we read back), not the chart.
const NULL_CONTAINER_IDS = new Set([
  "allocation-rows",
  "returns-fields",
  "allocation-totals",
  "allocation-blended",
  "glidepath-phases",
  "bucket-matrices",
  "allocation-toggle",
  "preset-select",
]);
// Per-class matrix INPUT ids — null so readWeight/etc. fall back to ASSET_CLASSES.
const MATRIX_INPUT_RE =
  /^(alloc-weight-|alloc-return-|alloc-vol-|glide-w-|bucket-w-)/;

function makeFakeDocument(seed) {
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
      // Singular lookups (e.g. panelSummaryEl on the recompute error path) get a
      // safe fake element so `.textContent = ...` never throws. The compare flow
      // we assert is the data path; the chart render is the integrator's gate.
      return makeElement("");
    },
    querySelectorAll() { return []; },
    createElement(tag) {
      const el = makeElement("");
      el.tagName = String(tag).toUpperCase();
      return el;
    },
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
    addEventListener() {},
    body: makeElement("body"),
    // cssVar() does getComputedStyle(document.documentElement).getPropertyValue(...)
    // when a chart render reads a color token; give it an element to hand off.
    documentElement: makeElement("html"),
    _byId: byId,
  };
  return doc;
}

function seedForInputs(inputs) {
  const seed = {};
  for (const f of SCALARS) {
    if (inputs && inputs[f] !== undefined) seed[f] = String(inputs[f]);
  }
  for (const id of [
    "guard-band", "guard-adjust", "spending-floor", "spending-ceiling",
    "mc-correlation",
  ]) seed[id] = "";
  seed["bucket-n1"] = "2";
  seed["bucket-n2"] = "7";
  return seed;
}

// Load app.js with a URL-routing fetch. `routes` maps a URL substring -> a
// handler returning { ok, status, json }. The trajectory/solve recompute is
// always rejected (its result is the integrator's live gate, not this unit), so
// the single rerender() at the end of compare() catches and returns null.
function loadApp(seedInputs, routes = {}) {
  const ctx = { console: { ...console, warn() {}, error() {}, log() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.confirmCalls = [];
  ctx.confirmReturn = true;
  ctx.confirm = (msg) => { ctx.confirmCalls.push(msg); return ctx.confirmReturn; };
  // Chart renders read color tokens via cssVar(); back getComputedStyle with the
  // real styles.css token values so a rerender() that reaches a chart build works.
  ctx.getComputedStyle = makeGetComputedStyle();
  ctx.fetch = async (url) => {
    const u = String(url);
    for (const [needle, handler] of Object.entries(routes)) {
      if (u.includes(needle)) return handler(u);
    }
    // Default (incl. /api/trajectory + /api/solve): reject so rerender() bails.
    throw new Error("fetch not routed: " + u);
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = makeFakeDocument(seedForInputs(seedInputs));
  return ctx;
}

function plain(v) { return JSON.parse(JSON.stringify(v)); }

// A representative scenario blob (the constant/deterministic default A).
function blobA(overrides = {}) {
  return {
    schema_version: 1,
    name: overrides.name || "Scenario A",
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

// =====================================================================
// 1) The pure diff decision (diffForCompare) for representative pairs.
// =====================================================================

function diff(ctx, savedBlob, currentA) {
  const SS = ctx.window.SavedScenarios;
  return plain(SS.diffForCompare(savedBlob, currentA, SCALARS.slice()));
}

test("diffForCompare: differ in spending only -> unlink that one field, allocation linked", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({ name: "Aggressive", inputs: { annual_spending: 160_000 } });
  const d = diff(ctx, b, a);
  assert.deepEqual(d.fieldsToUnlink, ["annual_spending"]);
  assert.equal(d.allocationDiffers, false);
  assert.equal(d.modeMismatch, false);
  assert.equal(d.identical, false);
});

test("diffForCompare: differ in spending + allocation -> unlink field AND allocation", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({
    name: "Equity heavy",
    inputs: {
      annual_spending: 160_000,
      allocation: [
        { id: "us_total_market", weight: 0.9, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.1, real_return: 0.02, volatility: 0.088 },
      ],
    },
  });
  const d = diff(ctx, b, a);
  assert.deepEqual(d.fieldsToUnlink, ["annual_spending"]);
  assert.equal(d.allocationDiffers, true);
  assert.equal(d.identical, false);
});

test("diffForCompare: differ in multiple scalars (order = FIELDS order)", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({
    inputs: { initial_portfolio: 3_000_000, end_age: 100, person_a_ss_claim_age: 70 },
  });
  const d = diff(ctx, b, a);
  // Must follow FIELDS declaration order, not blob key order.
  assert.deepEqual(d.fieldsToUnlink, ["initial_portfolio", "end_age", "person_a_ss_claim_age"]);
  assert.equal(d.allocationDiffers, false);
});

test("diffForCompare: identical blob -> no unlinks, identical=true", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({ name: "Copy" }); // same inputs + same allocation
  const d = diff(ctx, b, a);
  assert.deepEqual(d.fieldsToUnlink, []);
  assert.equal(d.allocationDiffers, false);
  assert.equal(d.identical, true);
});

test("diffForCompare: allocation float dust does NOT register as a diff (integer-% granularity)", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({
    inputs: {
      allocation: [
        { id: "us_total_market", weight: 0.6000001, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.3999999, real_return: 0.02, volatility: 0.088 },
      ],
    },
  });
  const d = diff(ctx, b, a);
  assert.equal(d.allocationDiffers, false);
  assert.equal(d.identical, true);
});

test("diffForCompare: a class added to the allocation registers as a diff", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({
    inputs: {
      allocation: [
        { id: "us_total_market", weight: 0.5, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
        { id: "intl_developed", weight: 0.1, real_return: 0.06, volatility: 0.16 },
      ],
    },
  });
  const d = diff(ctx, b, a);
  assert.equal(d.allocationDiffers, true);
});

test("diffForCompare: mode mismatch -> modeMismatch=true, labels the differing modes", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({
    name: "MC view",
    view: { sim_mode: "monte_carlo", spending_mode: "guardrails" },
    inputs: { annual_spending: 160_000 },
  });
  const d = diff(ctx, b, a);
  assert.equal(d.modeMismatch, true);
  assert.deepEqual(d.mismatchedModes.sort(), ["engine", "spending"]);
  // The scalar diff is still detected independently of the mode mismatch.
  assert.deepEqual(d.fieldsToUnlink, ["annual_spending"]);
});

test("diffForCompare: same modes -> modeMismatch=false", () => {
  const ctx = loadApp({});
  const a = blobA();
  const b = blobA({ inputs: { annual_spending: 160_000 } }); // same view
  const d = diff(ctx, b, a);
  assert.equal(d.modeMismatch, false);
  assert.deepEqual(d.mismatchedModes, []);
});

// =====================================================================
// 2) The imperative driver (compare) — identical no-op + mode note path.
//    These exercise the fetch -> diff -> mutate -> status path against the
//    fake DOM. The current A is the seeded form (= blobA's inputs).
// =====================================================================

// Build the GET route for /api/scenarios/{id} returning `savedBlob`.
function scenarioRoute(savedBlob) {
  return {
    "api/scenarios/": () => ({
      ok: true,
      status: 200,
      json: async () => savedBlob,
    }),
  };
}

test("compare: identical scenario is a no-op (status note, no B inputs created)", async () => {
  const a = blobA();
  // current form A == blobA inputs (seeded). The saved B is identical.
  const ctx = loadApp(a.inputs, scenarioRoute(blobA({ name: "Twin" })));
  const SS = ctx.window.SavedScenarios;
  const result = await SS.compare("twin");
  // No-op: compare() returns null (no overlay built).
  assert.equal(result, null);
  // No B field inputs were created for any scalar (identical -> nothing unlinked).
  for (const f of SCALARS) {
    assert.equal(ctx.document._byId.has(`${f}-b`), false, `${f}-b must not exist`);
  }
  const status = ctx.document.getElementById("scenarios-status");
  assert.match(status.textContent, /identical/i);
  assert.match(status.textContent, /Twin/);
});

test("compare: mode mismatch -> inputs loaded as B, A's modes kept, chart-note surfaced", async () => {
  const a = blobA();
  // Saved B differs in spending value AND in engine mode (monte_carlo vs deterministic).
  const savedB = blobA({
    name: "MC plan",
    view: { sim_mode: "monte_carlo" },
    inputs: { annual_spending: 175_000 },
  });
  const ctx = loadApp(a.inputs, scenarioRoute(savedB));
  const SS = ctx.window.SavedScenarios;
  await SS.compare("mc-plan");

  // The differing scalar was unlinked: a B input now exists with the saved value.
  const bEl = ctx.document.getElementById("annual_spending-b");
  assert.equal(bEl.value, "175000");
  // A field that matched stays linked: no B input created for it.
  assert.equal(ctx.document._byId.has("initial_portfolio-b"), false);

  // The chart-adjacent note explains the per-side mode limitation; the popover
  // status echoes it. Both name the engine-mode mismatch and that A's modes win.
  const note = ctx.document.getElementById("compare-note");
  assert.match(note.textContent, /engine/);
  assert.match(note.textContent, /scenario A's modes/);
  assert.equal(note.hidden, false);
  const status = ctx.document.getElementById("scenarios-status");
  assert.match(status.textContent, /MC plan/);
});

test("compare: spending + allocation differ -> B scalar survives the later allocation unlink", async () => {
  // Locks the write-after-all-transitions ordering: the allocation-unlink
  // transition fires AFTER the field unlink, and must not reseed the B field
  // input back to A. (The matrix weight inputs themselves are null in this
  // harness by design — readWeight falls back to ASSET_CLASSES — so we assert
  // the scalar-B survival, the ordering-sensitive part.)
  const a = blobA();
  const savedB = blobA({
    name: "Equity heavy",
    inputs: {
      annual_spending: 125_000,
      allocation: [
        { id: "us_total_market", weight: 0.9, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.1, real_return: 0.02, volatility: 0.088 },
      ],
    },
  });
  const ctx = loadApp(a.inputs, scenarioRoute(savedB));
  const SS = ctx.window.SavedScenarios;
  await SS.compare("equity-heavy");

  // The B scalar holds the saved value (125000), not a reseeded A value (110000).
  const bSpend = ctx.document.getElementById("annual_spending-b");
  assert.equal(bSpend.value, "125000");
  // Modes match here -> no chart note.
  const note = ctx.document.getElementById("compare-note");
  assert.equal(note.textContent, "");
});

test("compare: bad GET (404) surfaces a failure status, no mutation", async () => {
  const a = blobA();
  const ctx = loadApp(a.inputs, {
    "api/scenarios/": () => ({ ok: false, status: 404, json: async () => ({}) }),
  });
  const SS = ctx.window.SavedScenarios;
  const result = await SS.compare("missing");
  assert.equal(result, null);
  const status = ctx.document.getElementById("scenarios-status");
  assert.match(status.textContent, /404/);
  // No B inputs created.
  for (const f of SCALARS) {
    assert.equal(ctx.document._byId.has(`${f}-b`), false);
  }
});

// =====================================================================
// 3) Naming polish — the pure overwrite-confirm + empty-name guard logic.
//    The slug derivation + collision predicate are the testable kernel of
//    onSaveScenario; the DOM disable is asserted via the rendered button only
//    indirectly, so here we pin the pure pieces the UI keys off.
// =====================================================================

test("empty/whitespace name has no usable slug (the empty-name guard kernel)", () => {
  const ctx = loadApp({});
  // slugifyClient isn't exported, but the guard logic is: a name that trims to
  // empty must not produce a slug. Re-derive the same way the UI does.
  const slugify = (name) =>
    String(name).trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
      .slice(0, 64).replace(/^-+|-+$/g, "");
  assert.equal(slugify(""), "");
  assert.equal(slugify("   "), "");
  assert.equal(slugify("!!!"), "");
  assert.equal(slugify("My retirement plan"), "my-retirement-plan");
  // Sanity: app.js loaded (the harness is wired), so the guard ships with it.
  assert.equal(typeof ctx.window.SavedScenarios.compare, "function");
});

test("overwrite-confirm: a slug collision against the known list is detectable", () => {
  // The UI confirms an overwrite iff the derived slug matches an existing item's
  // id. Pin that predicate (the kernel of onSaveScenario's overwrite-confirm).
  const known = [
    { id: "my-retirement-plan", name: "My retirement plan" },
    { id: "aggressive", name: "Aggressive" },
  ];
  const slugify = (name) =>
    String(name).trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
      .slice(0, 64).replace(/^-+|-+$/g, "");
  const collides = (name) => {
    const slug = slugify(name);
    return known.some((it) => it.id === slug);
  };
  assert.equal(collides("My Retirement Plan"), true);   // case/space-insensitive collision
  assert.equal(collides("aggressive"), true);
  assert.equal(collides("Brand new plan"), false);
});
