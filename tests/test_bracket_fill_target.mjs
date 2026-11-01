// test_bracket_fill_target.mjs — pin the voluntary bracket-fill drawdown lever
// (tax-drag design td1, DECISION 5): web/app.js readBracketFillTarget /
// setBracketFillTargetInput + payload_assembler.js's attachBracketFillTarget +
// scenario_store.js's capture inclusion.
//
// The lever is a single optional dollar input (#bracket-fill-target) riding
// alongside the tb1 tax-location balances + the ft1 gains-fraction knob: same
// reveal/gate, but "empty = off" is the feature's own gate (unlike the gains
// knob, which always has a value — 50% default). readBracketFillTarget()
// parses the field into a dollar amount or null (blank/invalid), mirroring
// readTaxBalance's blank-is-null discipline (NOT readTaxableGainsFraction's
// blank-is-default discipline) — exercised here against a minimal fake
// `document` (same technique as test_tax_gains_fraction.mjs), since the
// function is a thin DOM read with no Chart.js dependency.
//
// Run:
//   (from the repo root) node --test tests/test_bracket_fill_target.mjs

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
const PA_PATH = path.join(WEB_DIR, "payload_assembler.js");

// Load app.js with NO document (init() skipped), then inject a minimal
// `document` whose getElementById serves #bracket-fill-target as a STABLE
// cached element (not rebuilt per call, so a write from
// setBracketFillTargetInput persists for a subsequent read). `value` seeds the
// element's initial value; pass `null` to simulate the element being ABSENT
// from the DOM entirely (the reduced-test-DOM case).
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
    getElementById: (id) => (id === "bracket-fill-target" ? el : null),
  };
  return ctx;
}

// ---------------------------------------------------------------------------
// readBracketFillTarget — blank/absent/invalid -> null ("empty = off" IS the
// feature gate, unlike the gains-fraction knob's blank-is-default discipline).
// ---------------------------------------------------------------------------

test("readBracketFillTarget: blank input -> null (off)", () => {
  const ctx = loadApp("");
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), null);
});

test("readBracketFillTarget: element absent (reduced test DOM) -> null", () => {
  const ctx = loadApp(null);
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), null);
});

test("readBracketFillTarget: parses a dollar amount", () => {
  const ctx = loadApp("100800");
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), 100_800);
});

test("readBracketFillTarget: 0 is a valid value, not treated as blank", () => {
  const ctx = loadApp("0");
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), 0);
});

test("readBracketFillTarget: NaN input -> null (no throw)", () => {
  const ctx = loadApp("abc");
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), null);
});

test("readBracketFillTarget: whitespace-only input -> null (off)", () => {
  const ctx = loadApp("   ");
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), null);
});

// ---------------------------------------------------------------------------
// setBracketFillTargetInput — the inverse (dollar amount -> string write, or
// clear to blank), used by applyScenarioToForm to restore a save / the reset
// default (which clears it — the lever has no default value to fall back to).
// ---------------------------------------------------------------------------

test("setBracketFillTargetInput: writes a whole-dollar string", () => {
  const ctx = loadApp("");
  vm.runInContext("setBracketFillTargetInput(100800)", ctx);
  assert.equal(ctx.document.getElementById("bracket-fill-target").value, "100800");
});

test("setBracketFillTargetInput: rounds a fractional dollar amount", () => {
  const ctx = loadApp("");
  vm.runInContext("setBracketFillTargetInput(211400.4)", ctx);
  assert.equal(ctx.document.getElementById("bracket-fill-target").value, "211400");
});

test("setBracketFillTargetInput: null/non-number clears the field to blank", () => {
  const ctx = loadApp("100800");
  vm.runInContext("setBracketFillTargetInput(null)", ctx);
  assert.equal(ctx.document.getElementById("bracket-fill-target").value, "");
});

test("setBracketFillTargetInput: round-trips through readBracketFillTarget", () => {
  const ctx = loadApp("");
  vm.runInContext("setBracketFillTargetInput(211400)", ctx);
  assert.equal(vm.runInContext("readBracketFillTarget()", ctx), 211_400);
});

// ---------------------------------------------------------------------------
// PayloadAssembler.attachBracketFillTarget — the POST-body attach gate: only a
// real number rides (undefined = split inactive; null = split active but
// lever blank); either way the key is omitted, byte-identical to pre-td1.
// ---------------------------------------------------------------------------

function loadPayloadAssembler() {
  const source = fs.readFileSync(PA_PATH, "utf8");
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.PayloadAssembler;
}

test("attachBracketFillTarget: a real number rides on the params object", () => {
  const PA = loadPayloadAssembler();
  const params = {};
  PA._attachBracketFillTarget(params, 100_800);
  assert.equal(params.bracket_fill_target, 100_800);
});

test("attachBracketFillTarget: undefined (split inactive) omits the key", () => {
  const PA = loadPayloadAssembler();
  const params = {};
  PA._attachBracketFillTarget(params, undefined);
  assert.equal("bracket_fill_target" in params, false);
});

test("attachBracketFillTarget: null (split active, lever blank) omits the key", () => {
  const PA = loadPayloadAssembler();
  const params = {};
  PA._attachBracketFillTarget(params, null);
  assert.equal("bracket_fill_target" in params, false);
});

test("attachBracketFillTarget: 0 still rides (falsy-but-valid dollar value)", () => {
  const PA = loadPayloadAssembler();
  const params = {};
  PA._attachBracketFillTarget(params, 0);
  assert.equal(params.bracket_fill_target, 0);
});

test("PayloadAssembler exposes _attachBracketFillTarget", () => {
  const PA = loadPayloadAssembler();
  assert.equal(typeof PA._attachBracketFillTarget, "function");
});

// ---------------------------------------------------------------------------
// buildScenario end-to-end — the lever rides BOTH scenarios when a number is
// supplied on formState.bracketFillTarget, mirroring taxableGainsFraction.
// ---------------------------------------------------------------------------

function constantFormState() {
  return {
    fieldNames: ["initial_portfolio", "annual_spending", "start_age"],
    fields: {
      initial_portfolio: { aVal: 2000000, bVal: undefined, linked: true },
      annual_spending: { aVal: 80000, bVal: undefined, linked: true },
      start_age: { aVal: 60, bVal: undefined, linked: true },
    },
    allocationBodyA: [{ id: "us_equity", weight: 1, real_return: 0.05, volatility: 0.16 }],
    allocationBodyB: undefined,
    allocationLinked: true,
    spendingMode: "constant",
    allocationMode: "constant",
    phaseAllocations: undefined,
    sourcingMode: "single",
    bucketsBody: undefined,
    guardrails: null,
    correlation: 0,
    taxLocation: { active: true, taxable: 1_500_000, taxDeferred: 700_000, roth: 300_000 },
  };
}

test("buildScenario: bracketFillTarget rides on BOTH scenarios when a number", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.bracketFillTarget = 100_800;
  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.bracket_fill_target, 100_800);
  assert.equal(b.bracket_fill_target, 100_800);
});

test("buildScenario: bracketFillTarget undefined (split inactive) -> no key on either side", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());
  assert.equal("bracket_fill_target" in a, false);
  assert.equal("bracket_fill_target" in b, false);
});

test("buildScenario: bracketFillTarget null (split active, lever blank) -> no key either", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.bracketFillTarget = null;
  const { a, b } = PA.buildScenario(fs0);
  assert.equal("bracket_fill_target" in a, false);
  assert.equal("bracket_fill_target" in b, false);
});
