// test_household_editor.mjs — the household editor's payload assembly (M5).
//
// web/household_editor.js turns the editor's rows into the API's people /
// income_streams / cash_events / spending_adjustments and the M4 tax keys;
// web/payload_assembler.js applies them to both scenarios; web/plan_io.js turns
// an imported plan into a saved-scenario blob and builds the export request.
// Loaded into one node:vm sandbox, like test_payload_assembler.mjs. All
// households are fictional.
//
// Run: node --test tests/test_household_editor.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(__dirname, "../web");

function load() {
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  for (const f of ["household_editor.js", "plan_io.js", "payload_assembler.js"]) {
    vm.runInContext(fs.readFileSync(path.join(WEB, f), "utf8"), ctx);
  }
  return ctx.window;
}
const plain = (x) => JSON.parse(JSON.stringify(x));

// A two-person editor state the way the DOM leaves it: inputs are strings.
function coupleState() {
  return {
    enabled: true,
    people: [
      { id: "a", name: "Avery", start_age: "52", end_age: "95" },
      { id: "b", name: "", start_age: "47", end_age: "95" },
    ],
    streams: [
      { id: "a_consulting", kind: "work", owner: "a", amount: "30000", start_age: "52",
        end_age: "60", cola: "full", cola_rate: 0, nominal_until_start: false,
        survivor_fraction: null, tax_treatment: null, self_employed: true, label: "" },
      { id: "a_pension", kind: "pension", owner: "a", amount: "20000", start_age: "60",
        end_age: "", cola: "fixed", cola_rate: 0.01, nominal_until_start: true,
        survivor_fraction: 0.5, tax_treatment: "", self_employed: false, label: "FERS" },
      { id: "b_pension", kind: "pension", owner: "b", amount: "15000", start_age: "62",
        end_age: null, cola: "fers_diet", cola_rate: 0, nominal_until_start: false,
        survivor_fraction: null, tax_treatment: null, self_employed: false, label: "" },
      { id: "a_ss", kind: "social_security", owner: "a", amount: "31000", start_age: "67",
        end_age: "80", cola: "none", cola_rate: 0, nominal_until_start: true,
        survivor_fraction: 0.3, tax_treatment: null, self_employed: true, label: "" },
    ],
    cashEvents: [
      { id: "house", owner: "a", age: "70", amount: "450000", tax_treatment: "capital_gain",
        basis_fraction: 0.5, label: "" },
      { id: "", owner: "household", age: "56", amount: "-40000", tax_treatment: "tax_free",
        basis_fraction: 0, label: "car" },
    ],
    adjustments: [
      { id: "aca_b", owner: "b", amount: "9000", start_age: "47", end_age: "65",
        survivor_fraction: 1, label: "" },
    ],
  };
}

test("buildHousehold coerces strings and drops defaults", () => {
  const HE = load().HouseholdEditor;
  const body = plain(HE.buildHousehold(coupleState()));
  assert.deepEqual(body.people, [
    { id: "a", start_age: 52, end_age: 95, name: "Avery" },
    { id: "b", start_age: 47, end_age: 95 },
  ]);
  assert.deepEqual(body.income_streams, [
    { id: "a_consulting", kind: "work", owner: "a", amount: 30000, start_age: 52, end_age: 60,
      self_employed: true },
    { id: "a_pension", kind: "pension", owner: "a", amount: 20000, start_age: 60,
      cola: "fixed", cola_rate: 0.01, nominal_until_start: true, survivor_fraction: 0.5,
      label: "FERS" },
    { id: "b_pension", kind: "pension", owner: "b", amount: 15000, start_age: 62,
      cola: "fers_diet" },
    // Social Security ignores end age, COLA, nominal, survivor share and the
    // self-employed flag: the API would reject or ignore them.
    { id: "a_ss", kind: "social_security", owner: "a", amount: 31000, start_age: 67 },
  ]);
  assert.deepEqual(body.cash_events, [
    { age: 70, amount: 450000, owner: "a", tax_treatment: "capital_gain", basis_fraction: 0.5,
      id: "house" },
    { age: 56, amount: -40000, owner: "household", tax_treatment: "tax_free", label: "car" },
  ]);
  assert.deepEqual(body.spending_adjustments, [
    { amount: 9000, start_age: 47, end_age: 65, owner: "b", id: "aca_b" },
  ]);
});

test("buildHousehold names the row and field on a bad value", () => {
  const HE = load().HouseholdEditor;
  const bad = (mutate, re) => {
    const s = coupleState();
    mutate(s);
    assert.throws(() => HE.buildHousehold(s), re);
  };
  bad((s) => { s.streams[1].amount = "lots"; }, /Income stream 2 \(a_pension\): amount must be a number/);
  bad((s) => { s.streams[3].start_age = "61"; }, /claim age must be between 62 and 70/);
  bad((s) => { s.streams[0].end_age = "50"; }, /end age must be after the start age/);
  bad((s) => { s.streams[1].id = "a_consulting"; }, /used twice/);
  bad((s) => { s.cashEvents[1].tax_treatment = "ordinary"; }, /money going out must be tax-free/);
  bad((s) => { s.adjustments[0].end_age = "40"; }, /Required spending 1: end age/);
  bad((s) => { s.people[0].end_age = "50"; }, /Person A: the planning age/);
  bad((s) => { s.people = []; }, /one or two people/);
  // Person B's streams are invalid once the household is one person.
  bad((s) => { s.people = s.people.slice(0, 1); }, /owner must be one of a, household/);
});

test("buildTaxOptions sends only non-default keys", () => {
  const HE = load().HouseholdEditor;
  assert.deepEqual(plain(HE.buildTaxOptions(HE.defaultTaxState())), {});
  assert.deepEqual(plain(HE.buildTaxOptions({
    ss_taxation: "flat85", payroll_tax: true, irmaa: true, irmaa_prior_magi: "180000",
    roth_conversion_target: "22%", roth_conversion_start_age: "60", roth_conversion_end_age: "",
    roth_heir_tax_rate: 0.22, state: "GA", state_after_move: "FL", state_move_age: "70",
  })), {
    ss_taxation: "flat85", payroll_tax: true, irmaa: true, irmaa_prior_magi: 180000,
    roth_conversion_target: "22%", roth_conversion_start_age: 60,
    state: "GA", state_after_move: "FL", state_move_age: 70,
  });
  // A dollar target, a custom state with exemptions, a non-default heir rate.
  assert.deepEqual(plain(HE.buildTaxOptions({
    roth_conversion_target: "96950", roth_heir_tax_rate: 0.3,
    state: "custom", state_custom_rate: 0.04, state_custom_exempt_ss: true,
  })), {
    roth_conversion_target: 96950, roth_heir_tax_rate: 0.3,
    state: "custom", state_custom_rate: 0.04, state_custom_exempt_ss: true,
  });
  // IRMAA's prior income is not sent while IRMAA is off.
  assert.deepEqual(plain(HE.buildTaxOptions({ irmaa: false, irmaa_prior_magi: "1" })), {});
  assert.throws(() => HE.buildTaxOptions({ state: "CA" }), /State must be one of/);
  assert.throws(() => HE.buildTaxOptions({ state: "custom", state_custom_rate: 0.5 }),
    /between 0 and 0.2/);
});

test("the assembler applies the household to both scenarios and drops legacy keys", () => {
  const W = load();
  const HE = W.HouseholdEditor;
  const household = HE.buildHousehold(coupleState());
  const formState = {
    fieldNames: ["initial_portfolio", "annual_spending", "start_age", "end_age",
      "person_a_pension_amount", "person_a_ss_claim_age"],
    fields: {
      initial_portfolio: { aVal: 1400000, linked: true },
      annual_spending: { aVal: 70000, bVal: 60000, linked: false },
      start_age: { aVal: 52, linked: true },
      end_age: { aVal: 100, linked: true },
      person_a_pension_amount: { aVal: 24000, linked: true },
      person_a_ss_claim_age: { aVal: 67, linked: true },
    },
    allocationBodyA: [{ id: "us_total_market", weight: 1, real_return: 0.07, volatility: 0.17 }],
    allocationLinked: true,
    spendingMode: "constant",
    allocationMode: "constant",
    sourcingMode: "single",
    guardrails: null,
    modelAssumptions: { pension_cola: "fers_diet", survivor_age: 88, survivor_decedent: "a" },
    correlation: 0.2,
    taxLocation: { active: true, taxable: 400000, taxDeferred: 800000, roth: 200000 },
    taxableGainsFraction: 0.5,
    household,
    taxOptions: { payroll_tax: true, state: "PA" },
    planExtras: { slow_go_start_age: 72 },
  };
  const { a, b } = W.PayloadAssembler.buildScenario(formState);
  for (const side of [a, b]) {
    for (const k of ["start_age", "end_age", "person_a_pension_amount", "person_a_ss_claim_age",
      "pension_cola"]) {
      assert.equal(k in side, false, `${k} should be dropped`);
    }
    assert.deepEqual(plain(side.people), plain(household.people));
    assert.equal(side.income_streams.length, 4);
    assert.equal(side.cash_events.length, 2);
    assert.equal(side.spending_adjustments.length, 1);
    assert.equal(side.payroll_tax, true);
    assert.equal(side.state, "PA");
    assert.equal(side.slow_go_start_age, 72);
    // The survivor case and the split still ride along.
    assert.equal(side.survivor_age, 88);
    assert.equal(side.taxable_balance, 400000);
  }
  assert.equal(a.annual_spending, 70000);
  assert.equal(b.annual_spending, 60000);
  // The POST body: allocationBody becomes allocation; the household rides through.
  const body = plain(W.PayloadAssembler.buildPostBody(a));
  assert.equal(body.allocation[0].id, "us_total_market");
  assert.deepEqual(body.people, plain(household.people));
});

test("without a household the assembler output is unchanged", () => {
  const W = load();
  const formState = {
    fieldNames: ["start_age"],
    fields: { start_age: { aVal: 50, linked: true } },
    allocationBodyA: [],
    allocationLinked: true,
    spendingMode: "constant",
    allocationMode: "constant",
    sourcingMode: "single",
    guardrails: null,
    correlation: 0.2,
    taxLocation: { active: false },
    household: null,
    taxOptions: null,
    planExtras: null,
  };
  const { a } = W.PayloadAssembler.buildScenario(formState);
  assert.deepEqual(plain(a), {
    start_age: 50, allocationBody: [], spending_mode: "constant", allocation_mode: "constant",
    sourcing_mode: "single", correlation: 0.2,
  });
});

test("fromLegacy converts two pensions and two Social Security benefits", () => {
  const HE = load().HouseholdEditor;
  const state = HE.fromLegacy({
    start_age: 50, end_age: 95,
    person_a_pension_amount: 24000, person_a_pension_start_age: 60,
    person_b_pension_amount: 9000, person_b_pension_start_age: 65,
    person_a_ss_fra_amount: 33000, person_a_ss_claim_age: 67,
    person_b_ss_fra_amount: 26000, person_b_ss_claim_age: 70,
  }, { pension_cola: "fers_diet", pension_nominal_until_start: true, survivor_pension_fraction: 0.5 });
  assert.equal(state.enabled, true);
  const body = plain(HE.buildHousehold(state));
  assert.deepEqual(body.people, [
    { id: "a", start_age: 50, end_age: 95 }, { id: "b", start_age: 50, end_age: 95 }]);
  assert.deepEqual(body.income_streams, [
    { id: "person_a_pension", kind: "pension", owner: "a", amount: 24000, start_age: 60,
      cola: "fers_diet", nominal_until_start: true, survivor_fraction: 0.5 },
    { id: "person_b_pension", kind: "pension", owner: "b", amount: 9000, start_age: 65,
      cola: "fers_diet", nominal_until_start: true, survivor_fraction: 0.5 },
    { id: "person_a_ss", kind: "social_security", owner: "a", amount: 33000, start_age: 67 },
    { id: "person_b_ss", kind: "social_security", owner: "b", amount: 26000, start_age: 70 },
  ]);
});

test("planAges and axisAges put person B's ages on person A's axis", () => {
  const HE = load().HouseholdEditor;
  const s = coupleState();
  assert.deepEqual(plain(HE.planAges(s)), { startAge: 52, endAge: 100 });
  const ages = plain(HE.axisAges(s));
  // b_pension starts at B's 62 = A's 67; a_consulting ends at 60.
  assert.deepEqual(ages, [
    { age: 52, kind: "work" }, { age: 60, kind: "end" },
    { age: 60, kind: "pension" }, { age: 67, kind: "pension" },
    { age: 67, kind: "social_security" },
  ]);
});

test("new rows get unique ids", () => {
  const HE = load().HouseholdEditor;
  const s = HE.defaultState(50, 95);
  s.streams.push(HE.newStream(s, "work", "a"));
  s.streams.push(HE.newStream(s, "work", "a"));
  s.streams.push(HE.newStream(s, "social_security", "b"));
  assert.deepEqual(plain(s.streams.map((x) => x.id)), ["a_work", "a_work_2", "b_ss"]);
  assert.equal(s.streams[2].start_age, 67);
  // Every new row is valid as it stands.
  s.cashEvents.push(HE.newCashEvent(s, "a"));
  s.adjustments.push(HE.newAdjustment(s, "b"));
  assert.doesNotThrow(() => HE.buildHousehold(s));
});

// The payload /api/examples/<name> returns, trimmed to what the mapping reads.
function importPayload() {
  return {
    name: "Gap couple",
    description: "fictional",
    trials: 5000,
    seed: 12345,
    stress: ["gfc_2008"],
    body: {
      initial_portfolio: 1050000,
      annual_spending: 62000,
      spending_mode: "constant",
      allocation_mode: "glidepath",
      sourcing_mode: "single",
      allocation: [
        { id: "us_total_market", weight: 0.45, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.55, real_return: 0.02, volatility: 0.088 },
      ],
      phase_allocations: [
        [{ id: "us_total_market", weight: 0.45 }, { id: "us_agg_bonds", weight: 0.55 }],
        [{ id: "us_total_market", weight: 0.6 }, { id: "us_agg_bonds", weight: 0.4 }],
      ],
      correlation: 0.2,
      guard_band: 0.2, guard_adjust: 0.1, spending_floor: 49600, spending_ceiling: 93000,
      amortization_real_return: 0.01,
      taxable_balance: 300000, tax_deferred_balance: 750000,
      taxable_gains_fraction: 0.5,
      annual_fee: 0.0005, assumed_inflation: 0.025, mc_distribution: "normal",
      guard_clamp_base: false, survivor_age: 84, survivor_decedent: "a",
      survivor_spending_fraction: 0.75,
      people: [
        { id: "a", name: "Jordan", start_age: 55, end_age: 95 },
        { id: "b", name: "Casey", start_age: 50, end_age: 95 },
      ],
      income_streams: [
        { id: "jordan_ss", kind: "social_security", owner: "a", amount: 30000, start_age: 67,
          end_age: null, cola: "full", cola_rate: 0, nominal_until_start: false,
          survivor_fraction: null, tax_treatment: null, label: "", self_employed: false },
      ],
      cash_events: [],
      spending_adjustments: [],
      ss_taxation: "provisional", payroll_tax: false, irmaa: false, roth_heir_tax_rate: 0.22,
      state: "NC", state_custom_rate: 0, state_custom_exempt_ss: false,
      state_custom_exempt_pensions: false, state_custom_exempt_ira: false,
      slow_go_start_age: 75, mc_t_df: 5,
    },
  };
}

test("blobFromImport maps an imported plan onto a saved-scenario blob", () => {
  const W = load();
  const blob = plain(W.PlanIO.blobFromImport(importPayload(), {
    classIds: ["us_total_market", "us_agg_bonds", "gold"],
    simMode: "historical",
  }));
  assert.deepEqual(blob.view, {
    sim_mode: "historical", spending_mode: "constant", allocation_mode: "glidepath",
    sourcing_mode: "single",
  });
  const i = blob.inputs;
  assert.equal(i.initial_portfolio, 1050000);
  // The horizon is the plan's: A's 55 to B's 95 (= A's 100).
  assert.equal(i.start_age, 55);
  assert.equal(i.end_age, 100);
  assert.deepEqual(i.glidepath_phase_weights, { 0: [45, 55, 0], 1: [60, 40, 0] });
  assert.deepEqual(i.tax_location, { taxable: 300000, tax_deferred: 750000, roth: 0 });
  assert.equal(i.model_assumptions.survivor_age, 84);
  assert.equal(i.model_assumptions.survivor_decedent, "a");
  assert.equal(i.household.enabled, true);
  assert.equal(i.household.people[1].name, "Casey");
  assert.equal(i.tax_options.state, "NC");
  assert.deepEqual(i.plan_extras, { slow_go_start_age: 75, mc_t_df: 5 });
  assert.deepEqual(i.plan_meta, {
    name: "Gap couple", description: "fictional", trials: 5000, seed: 12345, stress: ["gfc_2008"],
  });
  assert.deepEqual(blob.warnings, []);
  // The editor state the blob carries builds the same household the plan had.
  const hh = plain(W.HouseholdEditor.buildHousehold(i.household));
  assert.deepEqual(hh.income_streams, [
    { id: "jordan_ss", kind: "social_security", owner: "a", amount: 30000, start_age: 67 }]);
  // ...and the tax state builds the plan's tax keys.
  assert.deepEqual(plain(W.HouseholdEditor.buildTaxOptions(i.tax_options)), { state: "NC" });
});

test("blobFromImport warns about what the UI cannot hold", () => {
  const W = load();
  const p = importPayload();
  p.body.correlations = [[1, 0.1], [0.1, 1]];
  p.body.allocation.push({ id: "us_reit", weight: 0, real_return: 0.05 });
  const blob = W.PlanIO.blobFromImport(p, { classIds: ["us_total_market", "us_agg_bonds"] });
  assert.equal(blob.warnings.length, 2);
  assert.match(blob.warnings[0], /correlation matrix/);
  assert.match(blob.warnings[1], /us_reit/);
});

test("exportRequest drops run-only keys and keeps the plan settings", () => {
  const W = load();
  const req = plain(W.PlanIO.exportRequest(
    { mode: "historical", confidence: 0.8, window_start_year: 1900, initial_portfolio: 1 },
    { name: "Gap couple", description: "d", trials: 5000, seed: 7, stress: ["gfc_2008"] },
  ));
  assert.deepEqual(req, {
    scenario: { initial_portfolio: 1 }, name: "Gap couple", description: "d",
    trials: 5000, seed: 7, stress: ["gfc_2008"],
  });
  assert.deepEqual(plain(W.PlanIO.exportRequest({ a: 1 }, null)),
    { scenario: { a: 1 }, name: "", description: "" });
  assert.equal(W.PlanIO.fileName("Part-time bridge"), "part-time-bridge.toml");
  assert.equal(W.PlanIO.fileName(""), "plan.toml");
});
