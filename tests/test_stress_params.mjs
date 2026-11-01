// test_stress_params.mjs — pin PayloadAssembler.buildStressParams (stress-test
// design slice 1, the web seam).
//
// A stress is an OVERLAY on the current plan: buildStressParams takes the base
// scenario params the deterministic path would send and only ADDS the request
// flavor (mode=stress + the chosen stress_id), leaving allocation / spend / GI
// untouched. These tests pin that contract — the only unit-testable seam in the
// stress web wiring (the VIEW lens / sub-selector + fetch live in the DOM-fused
// app.js, whose live behavior is the integrator's Playwright gate).
//
// Loaded the same way as test_payload_assembler.mjs: payload_assembler.js read
// off disk and run inside a node:vm sandbox with a window stub. Zero npm deps;
// requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_stress_params.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PA_PATH = path.resolve(__dirname, "../web/payload_assembler.js");
const INDEX_PATH = path.resolve(__dirname, "../web/index.html");

// The full slice-4 battery the #stress-select must offer: the six adverse Family-A
// worlds (slice 2; the three tailwind / false-positive framework scenarios are out
// of scope) PLUS the four Family-B perturbations (slice 3 — SS haircut +
// cost-of-living shock; slice 4 — government-income austerity + sustained cost-of-living drift).
const SLICE4_STRESS_IDS = [
  "stagflation_1970s",
  "historical_depression_1929",
  "slow_grind_2000",
  "gfc_2008",
  "valuation_unwind_2025",
  "depression_extreme",
  "ss_haircut_23",
  "cost_of_living_shock",
  "government_austerity",
  "sustained_cost_drift",
];

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

test("buildStressParams adds mode=stress + stress_id, preserves plan params", () => {
  const PA = loadPayloadAssembler();
  const base = {
    initial_portfolio: 1800000,
    annual_spending: 110000,
    allocationBody: [{ id: "us_total_market", weight: 0.6 }],
  };
  const out = PA.buildStressParams(base, "stagflation_1970s");
  assert.equal(out.mode, "stress");
  assert.equal(out.stress_id, "stagflation_1970s");
  // The plan params ride through untouched (the OVERLAY contract).
  assert.equal(out.initial_portfolio, 1800000);
  assert.equal(out.annual_spending, 110000);
  assert.deepEqual(plain(out.allocationBody), [
    { id: "us_total_market", weight: 0.6 },
  ]);
});

test("buildStressParams does not mutate the input params object", () => {
  const PA = loadPayloadAssembler();
  const base = { annual_spending: 110000 };
  PA.buildStressParams(base, "stagflation_1970s");
  // No mode / stress_id leaked back onto the caller's base scenario params.
  assert.equal(base.mode, undefined);
  assert.equal(base.stress_id, undefined);
});

test("buildStressParams with no stressId returns base params unchanged (no half-formed request)", () => {
  const PA = loadPayloadAssembler();
  const base = { annual_spending: 110000 };
  const out = PA.buildStressParams(base, "");
  assert.equal(out.mode, undefined);
  assert.equal(out.stress_id, undefined);
  assert.equal(out.annual_spending, 110000);
});

test("buildStressParams tolerates null params", () => {
  const PA = loadPayloadAssembler();
  const out = PA.buildStressParams(null, "stagflation_1970s");
  assert.equal(out.mode, "stress");
  assert.equal(out.stress_id, "stagflation_1970s");
});

test("the #stress-select sub-selector lists the full slice-4 battery (10 scenarios)", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  // Pull the <select id="stress-select"> ... </select> block and read its option values.
  const block = html.match(/<select id="stress-select"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(block, "#stress-select present in index.html");
  const optionValues = [...block[1].matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);

  // Every scenario (6 Family-A + 4 Family-B) is offered, with a non-empty human label.
  for (const id of SLICE4_STRESS_IDS) {
    assert.ok(optionValues.includes(id), `#stress-select offers ${id}`);
  }
  // Exactly the ten scenarios — no stray / dropped-tailwind option leaked in.
  assert.equal(optionValues.length, SLICE4_STRESS_IDS.length);
  assert.deepEqual([...optionValues].sort(), [...SLICE4_STRESS_IDS].sort());
});
