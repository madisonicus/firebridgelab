// test_payload_assembler.mjs — pin the PayloadAssembler contract.
//
// This is the safety net the old DOM-fused readScenarios() never had. The
// extraction (web/payload_assembler.js) is a behavior-preserving move: the
// assembly logic came VERBATIM out of readScenarios()/buildPostBody(), with the
// only change being that values arrive on a plain `formState` object instead of
// being read inline off the DOM. These tests assert the assembled { a, b } body
// EXACTLY — keys, nesting, values, and the conditional inclusion of glidepath /
// buckets / guardrails / MC-correlation / tax-location.
//
// Loaded the same way as test_chart_view.mjs / test_comparison_state.mjs:
// payload_assembler.js is read off disk and run inside a node:vm sandbox with a
// window stub. Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_payload_assembler.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PA_PATH = path.resolve(__dirname, "../web/payload_assembler.js");

// payload_assembler.js runs inside a vm sandbox with its OWN realm. Objects it
// returns have a different Object/Array constructor than the test realm, so
// node:assert's strict deepEqual rejects them as "same structure, not reference-
// equal". Coerce to same-realm plain values before deep-asserting.
function loadPayloadAssembler() {
  const source = fs.readFileSync(PA_PATH, "utf8");
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.PayloadAssembler;
}
function plain(x) {
  return JSON.parse(JSON.stringify(x));
}

// A representative constant-mode, single-sourcing formState. Two scalar fields
// shown linked, the rest stand in for the full 12-field set (the assembler is
// field-count-agnostic — it iterates fieldNames). aVal always present; bVal
// undefined when linked.
function constantFormState() {
  return {
    fieldNames: ["initial_portfolio", "annual_spending", "start_age"],
    fields: {
      initial_portfolio: { aVal: 2000000, bVal: undefined, linked: true },
      annual_spending: { aVal: 80000, bVal: undefined, linked: true },
      start_age: { aVal: 60, bVal: undefined, linked: true },
    },
    allocationBodyA: [
      { id: "us_equity", weight: 0.6, real_return: 0.05, volatility: 0.16 },
      { id: "bonds", weight: 0.4, real_return: 0.02, volatility: 0.05 },
    ],
    allocationBodyB: undefined,
    allocationLinked: true,
    spendingMode: "constant",
    allocationMode: "constant",
    phaseAllocations: undefined,
    sourcingMode: "single",
    bucketsBody: undefined,
    guardrails: null,
    correlation: 0,
    taxLocation: { active: false, taxable: null, taxDeferred: null, roth: null },
    // taxableGainsFraction intentionally omitted (undefined) — the baseline
    // formState mirrors app.js's "tax-location inactive" shape, where
    // readFormState() doesn't attach the knob either.
  };
}

test("constant/single formState assembles the baseline body (exact keys + values)", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());

  // Scenario A: exact shape.
  assert.deepEqual(plain(a), {
    initial_portfolio: 2000000,
    annual_spending: 80000,
    start_age: 60,
    allocationBody: [
      { id: "us_equity", weight: 0.6, real_return: 0.05, volatility: 0.16 },
      { id: "bonds", weight: 0.4, real_return: 0.02, volatility: 0.05 },
    ],
    spending_mode: "constant",
    allocation_mode: "constant",
    sourcing_mode: "single",
    correlation: 0,
  });
  // Linked → B mirrors A exactly.
  assert.deepEqual(plain(b), plain(a));

  // No conditional blocks leak in when their modes are off.
  for (const key of [
    "phase_allocations",
    "buckets",
    "taxable_balance",
    "tax_deferred_balance",
    "roth_balance",
    "taxable_gains_fraction",
    "bracket_fill_target",
  ]) {
    assert.equal(key in a, false, `A must not carry ${key}`);
    assert.equal(key in b, false, `B must not carry ${key}`);
  }
});

test("linked allocation: B reuses A's exact allocation body reference", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());
  // Old code did `b.allocationBody = a.allocationBody` when linked — same ref.
  assert.equal(b.allocationBody, a.allocationBody);
});

test("unlinked fields + unlinked allocation: B diverges from A", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  // Unlink annual_spending with a distinct B value; unlink allocation too.
  fs0.fields.annual_spending = { aVal: 80000, bVal: 100000, linked: false };
  fs0.allocationLinked = false;
  fs0.allocationBodyB = [
    { id: "us_equity", weight: 0.8, real_return: 0.05, volatility: 0.16 },
    { id: "bonds", weight: 0.2, real_return: 0.02, volatility: 0.05 },
  ];

  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.annual_spending, 80000);
  assert.equal(b.annual_spending, 100000); // unlinked → B's own value
  assert.equal(a.initial_portfolio, b.initial_portfolio); // still linked
  // Allocation bodies are now distinct objects.
  assert.notEqual(b.allocationBody, a.allocationBody);
  assert.equal(b.allocationBody, fs0.allocationBodyB);
  assert.equal(a.allocationBody[0].weight, 0.6);
  assert.equal(b.allocationBody[0].weight, 0.8);
});

test("glidepath: allocation_mode + phase_allocations ride on BOTH scenarios", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.allocationMode = "glidepath";
  const phases = [
    { from_age: 60, to_age: 70, allocation: [{ id: "us_equity", weight: 1 }] },
    { from_age: 70, to_age: 90, allocation: [{ id: "bonds", weight: 1 }] },
  ];
  fs0.phaseAllocations = phases;

  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.allocation_mode, "glidepath");
  assert.equal(b.allocation_mode, "glidepath");
  assert.deepEqual(plain(a.phase_allocations), plain(phases));
  assert.deepEqual(plain(b.phase_allocations), plain(phases));
  // GLOBAL: same phases object on both sides (old code: a.phase = b.phase = phases).
  assert.equal(a.phase_allocations, b.phase_allocations);
});

test("constant: phase_allocations is omitted entirely", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());
  assert.equal("phase_allocations" in a, false);
  assert.equal("phase_allocations" in b, false);
});

test("buckets: sourcing_mode + buckets config ride on BOTH scenarios", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.sourcingMode = "buckets";
  const buckets = {
    n1: 3,
    n2: 7,
    cash: [{ id: "cash", weight: 1 }],
    income: [{ id: "bonds", weight: 1 }],
    growth: [{ id: "us_equity", weight: 1 }],
  };
  fs0.bucketsBody = buckets;

  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.sourcing_mode, "buckets");
  assert.equal(b.sourcing_mode, "buckets");
  assert.deepEqual(plain(a.buckets), plain(buckets));
  assert.deepEqual(plain(b.buckets), plain(buckets));
  assert.equal(a.buckets, b.buckets); // GLOBAL: same config object on both sides
});

test("single sourcing: buckets is omitted entirely", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());
  assert.equal("buckets" in a, false);
  assert.equal("buckets" in b, false);
});

test("guardrails: the shared knob set is Object.assigned onto BOTH scenarios", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.guardrails = {
    guardrail_band: 0.2,
    guardrail_adjust: 0.1,
    spending_floor: 50000,
    spending_ceiling: 140000,
  };

  const { a, b } = PA.buildScenario(fs0);
  for (const side of [a, b]) {
    assert.equal(side.guardrail_band, 0.2);
    assert.equal(side.guardrail_adjust, 0.1);
    assert.equal(side.spending_floor, 50000);
    assert.equal(side.spending_ceiling, 140000);
  }
});

test("no guardrails (null): none of the guardrail keys appear", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState());
  for (const key of [
    "guardrail_band",
    "guardrail_adjust",
    "spending_floor",
    "spending_ceiling",
  ]) {
    assert.equal(key in a, false);
    assert.equal(key in b, false);
  }
});

test("correlation always rides on both scenarios (non-default value)", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.correlation = 0.35;
  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.correlation, 0.35);
  assert.equal(b.correlation, 0.35);
});

test("tax-location ACTIVE: all three balances ride on BOTH scenarios (missing → 0)", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  // Active split with roth left blank (null) — the API's "any set → all set" rule
  // means roth must serialize as 0, not be dropped.
  fs0.taxLocation = {
    active: true,
    taxable: 1100000,
    taxDeferred: 900000,
    roth: null,
  };

  const { a, b } = PA.buildScenario(fs0);
  for (const side of [a, b]) {
    assert.equal(side.taxable_balance, 1100000);
    assert.equal(side.tax_deferred_balance, 900000);
    assert.equal(side.roth_balance, 0); // null → 0
  }
});

test("tax-location INACTIVE: no balance keys appear (canary preserved)", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  // Even with values present, active:false → the gate omits all three.
  fs0.taxLocation = {
    active: false,
    taxable: 1100000,
    taxDeferred: 900000,
    roth: 500000,
  };
  const { a, b } = PA.buildScenario(fs0);
  for (const side of [a, b]) {
    assert.equal("taxable_balance" in side, false);
    assert.equal("tax_deferred_balance" in side, false);
    assert.equal("roth_balance" in side, false);
  }
});

test("taxable-gains-fraction ACTIVE-shaped formState: rides on BOTH scenarios", () => {
  // formState.taxableGainsFraction arrives already gated by app.js
  // (taxLocationActive() ? readTaxableGainsFraction() : undefined) — the
  // assembler just attaches whatever it's handed, so "active" here just means
  // "a number was supplied".
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.taxableGainsFraction = 0.35;
  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.taxable_gains_fraction, 0.35);
  assert.equal(b.taxable_gains_fraction, 0.35);
});

test("taxable-gains-fraction absent (undefined): no key appears (canary preserved)", () => {
  const PA = loadPayloadAssembler();
  const { a, b } = PA.buildScenario(constantFormState()); // taxableGainsFraction undefined
  assert.equal("taxable_gains_fraction" in a, false);
  assert.equal("taxable_gains_fraction" in b, false);
});

test("taxable-gains-fraction of exactly 0 still rides (falsy-but-valid value)", () => {
  const PA = loadPayloadAssembler();
  const fs0 = constantFormState();
  fs0.taxableGainsFraction = 0;
  const { a, b } = PA.buildScenario(fs0);
  assert.equal(a.taxable_gains_fraction, 0);
  assert.equal(b.taxable_gains_fraction, 0);
});

test("buildPostBody: allocationBody → allocation; other keys ride through", () => {
  const PA = loadPayloadAssembler();
  const body = PA.buildPostBody({
    initial_portfolio: 2000000,
    spending_mode: "constant",
    correlation: 0,
    allocationBody: [{ id: "bonds", weight: 1, real_return: 0.02, volatility: 0.05 }],
  });
  assert.deepEqual(plain(body), {
    initial_portfolio: 2000000,
    spending_mode: "constant",
    correlation: 0,
    allocation: [{ id: "bonds", weight: 1, real_return: 0.02, volatility: 0.05 }],
  });
  assert.equal("allocationBody" in body, false);
});

test("buildPostBody: empty/absent allocationBody drops the allocation key", () => {
  const PA = loadPayloadAssembler();
  const emptyArr = PA.buildPostBody({ x: 1, allocationBody: [] });
  assert.equal("allocation" in emptyArr, false);
  assert.equal(emptyArr.x, 1);

  // Null params → empty body.
  assert.deepEqual(plain(PA.buildPostBody(null)), {});
});

test("buildScenario → buildPostBody round-trip yields a clean ScenarioBody", () => {
  // End-to-end: the assembler's output fed through the POST transform produces a
  // body with `allocation` (not allocationBody) and every scalar intact.
  const PA = loadPayloadAssembler();
  const { a } = PA.buildScenario(constantFormState());
  const body = PA.buildPostBody(a);
  assert.equal("allocationBody" in body, false);
  assert.equal(body.allocation.length, 2);
  assert.equal(body.initial_portfolio, 2000000);
  assert.equal(body.spending_mode, "constant");
  assert.equal(body.sourcing_mode, "single");
});

test("PayloadAssembler exposes its public surface", () => {
  const PA = loadPayloadAssembler();
  assert.equal(typeof PA.buildScenario, "function");
  assert.equal(typeof PA.buildPostBody, "function");
  assert.equal(typeof PA._attachTaxLocation, "function");
  assert.equal(typeof PA._attachTaxableGainsFraction, "function");
  assert.equal(typeof PA._attachBracketFillTarget, "function");
});
