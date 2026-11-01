// test_worst_cohort_summary.mjs — pin the worst-cohort depletion sentence
// (the drill-down used to assert "guaranteed income carrying the
// floor thereafter" unconditionally, false whenever the four GI streams sum
// below the floor).
//
// The fix computes the clause from the echoed scenario via two pure helpers in
// web/app.js — `depletedGiClause(scenario, depletedAge)` and
// `worstCohortSummary(wc, scenario)` — pinned here WITHOUT a browser: app.js
// (plus its script deps) is read off disk and run in a node:vm sandbox, the
// same way test_spending_series.mjs / test_chart_explainers.mjs do it. No
// `document` at module-eval time, so app.js's bottom-of-file init() guard is
// FALSE and init() is skipped; the helpers under test never touch the DOM.
//
// Run:
//   (from the repo root) node --test tests/test_worst_cohort_summary.mjs
//
// Zero npm deps; Node >= 18 (node:test + node:assert/strict).

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

function loadApp() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {
    return {};
  };
  ctx.getComputedStyle = makeGetComputedStyle();
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window;
}

const app = loadApp();
const { depletedGiClause, worstCohortSummary } = app;

// A scenario shaped like the API's echoed `scenario` (asdict(Scenario)): four GI
// streams (SS amounts already claim-age-derived), the $85k operational floor,
// plan end at 92. Per-test overrides tune the case under test.
function scenario(overrides = {}) {
  return {
    end_age: 92,
    annual_spending: 110000,
    spending_floor: 85000,
    person_a_pension_amount: 24000,
    person_a_pension_start_age: 60,
    person_b_pension_amount: 9000,
    person_b_pension_start_age: 65,
    person_a_ss_amount: 33000,
    person_a_ss_start_age: 67,
    person_b_ss_amount: 26000,
    person_b_ss_start_age: 70,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// depletedGiClause — the four cases.
// ---------------------------------------------------------------------------

test("case a: GI >= floor for every post-depletion year → covers the floor thereafter", () => {
  // Depletion at 75: all four streams are on (last start 70), 24+9+33+26 =
  // $92k >= the $85k floor for the whole window.
  const clause = depletedGiClause(scenario(), 75);
  assert.equal(clause, "with guaranteed income covering the floor thereafter");
});

test("case b: all streams on but GI < floor → N% of the floor thereafter", () => {
  // Shrink Person B's SS so the full stack is 24+9+33+16 = $82,000 —
  // 96.5% of the $85k floor → rendered 96%.
  const s = scenario({ person_b_ss_amount: 16000 });
  const clause = depletedGiClause(s, 75);
  assert.equal(clause, "with guaranteed income covering 96% of the floor thereafter");
});

test("case c: depletion before the last GI start age → eventual coverage + step-up age", () => {
  // Depletion at 64: both pensions ($33k from 65) are on in the window, both SS
  // streams (67, 70) start INSIDE it. Eventual GI $92k >= floor, but only
  // from Person B's SS at 70 — the sentence must not claim the floor before then.
  const clause = depletedGiClause(scenario(), 64);
  assert.equal(
    clause,
    "with guaranteed income covering the floor from age 70 (less before then)",
  );
});

test("case c with eventual shortfall: percent coverage + step-up age", () => {
  // Same early depletion, but the eventual stack tops out at $82,000 < floor:
  // states the EVENTUAL 96% and that it's lower before age 70.
  const s = scenario({ person_b_ss_amount: 16000 });
  const clause = depletedGiClause(s, 64);
  assert.equal(
    clause,
    "with guaranteed income covering 96% of the floor from age 70 (less before then)",
  );
});

test("zero-GI scenario → no false comfort at all", () => {
  const s = scenario({
    person_a_pension_amount: 0,
    person_b_pension_amount: 0,
    person_a_ss_amount: 0,
    person_b_ss_amount: 0,
  });
  const clause = depletedGiClause(s, 75);
  assert.equal(clause, "with no guaranteed income to fall back on");
});

test("a stream starting past end_age never counts (no phantom step-up)", () => {
  // Person B's SS pushed past the plan horizon: it neither contributes to the
  // eventual GI ($66k = 77.6% of floor → 78%) nor names a step-up age beyond end_age.
  const s = scenario({ person_b_ss_start_age: 95 });
  const clause = depletedGiClause(s, 75);
  assert.equal(clause, "with guaranteed income covering 78% of the floor thereafter");
});

test("a stream starting exactly in the first post-depletion year is no step-up", () => {
  // Depletion at 66 → window starts at 67; Person A's SS (start 67) is on for the
  // ENTIRE window, so only Person B's 70 start is a step inside it.
  const clause = depletedGiClause(scenario(), 66);
  assert.equal(
    clause,
    "with guaranteed income covering the floor from age 70 (less before then)",
  );
});

// ---------------------------------------------------------------------------
// worstCohortSummary — the full sentence the panel renders.
// ---------------------------------------------------------------------------

test("summary appends the computed clause when the cohort depleted", () => {
  // Eventual GI 24+9+33+16.5 = $82,500 = 97.06% of the $85k floor → 97%.
  const wc = { start_year: 1966, min_spend: 85000, depleted_age: 88 };
  const text = worstCohortSummary(wc, scenario({ person_b_ss_amount: 16500 }));
  assert.equal(
    text,
    "Starting in 1966 gave the lowest total real spending of any history. " +
      "Spending bottomed at $85k. Its portfolio depleted at age 88, " +
      "with guaranteed income covering 97% of the floor thereafter.",
  );
});

test("summary carries NO depletion sentence when the cohort never depleted", () => {
  const wc = { start_year: 1929, min_spend: 86000, depleted_age: null };
  const text = worstCohortSummary(wc, scenario());
  assert.equal(
    text,
    "Starting in 1929 gave the lowest total real spending of any history. " +
      "Spending bottomed at $86k.",
  );
});

test("the old unconditional 'carrying the floor' wording is gone from app.js", () => {
  // Regression tripwire on the SOURCE: the hardcoded comfort string must not
  // reappear in any render path.
  const src = fs.readFileSync(APP_PATH, "utf8");
  assert.ok(
    !src.includes("guaranteed income carrying the floor"),
    "app.js must not hardcode the depletion comfort clause",
  );
});
