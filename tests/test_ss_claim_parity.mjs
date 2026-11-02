// test_ss_claim_parity.mjs — anti-drift guard for web/ss_claim.js (SS claim-age
// slider, slice 2 — the web seam).
//
// web/ss_claim.js is a hand-written JS MIRROR of firebridgelab/income/social_security.py: it powers the
// live "effective benefit" readout under the claim-age slider WITHOUT a server
// round-trip, so it MUST reproduce the Python helper's per-month factor exactly.
// These tests pin SsClaim.ssFactor() for every age 62..70 against the known SSA
// factor table for FRA 67 — the table in firebridgelab/income/social_security.py's docstring and SSA's
// published factors (62 → 0.70, 67 → 1.00, 70 → 1.24).
// If the JS formula ever drifts from the regulation (or someone hardcodes a wrong
// table), this fails. The 62/63/64/67/68/69/70 values are exact; 65/66 are the
// repeating-decimal closed forms (NOT rounded literals) so the assertion is tight.
//
// Loaded the same way as test_stress_params.mjs / test_payload_assembler.mjs:
// ss_claim.js read off disk and run inside a node:vm sandbox with a window stub.
// Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node tests/test_ss_claim_parity.mjs
//   (or: node --test tests/test_ss_claim_parity.mjs)

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SS_PATH = path.resolve(__dirname, "../web/ss_claim.js");

function loadSsClaim() {
  const source = fs.readFileSync(SS_PATH, "utf8");
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.SsClaim;
}

const EPS = 1e-9;

// The known SSA factor table for FRA 67. 62/63/64 exercise the first-36-months
// (5/9 of 1%/mo) tier; 65/66 cross into / sit within it as repeating decimals
// (closed forms, not rounded literals); 67 is FRA; 68/69/70 are the +8%/yr delayed
// credits. These match firebridgelab/income/social_security.py and the reference table verbatim.
const EXPECTED = {
  62: 0.70, // 60 months early: 36*(5/9%) + 24*(5/12%) = 0.20 + 0.10 = 0.30 cut
  63: 0.75, // 48 months early: 0.20 + 12*(5/12%) = 0.20 + 0.05 = 0.25 cut
  64: 0.80, // 36 months early: 0.20 cut exactly
  65: 1.0 - 24 * ((5.0 / 9.0) / 100.0), // 24 months early, all in first tier → 0.86666…
  66: 1.0 - 12 * ((5.0 / 9.0) / 100.0), // 12 months early, all in first tier → 0.93333…
  67: 1.0, // FRA
  68: 1.08, // +12 months * 2/3%/mo = +8%
  69: 1.16, // +24 months = +16%
  70: 1.24, // +36 months = +24%
};

test("ssFactor matches the SSA factor table for FRA 67 (ages 62..70) to 1e-9", () => {
  const SsClaim = loadSsClaim();
  assert.equal(typeof SsClaim, "object", "window.SsClaim is exposed");
  for (let age = 62; age <= 70; age++) {
    const got = SsClaim.ssFactor(age);
    const want = EXPECTED[age];
    assert.ok(
      Math.abs(got - want) < EPS,
      `ssFactor(${age}) = ${got}, expected ${want} (Δ=${Math.abs(got - want)})`,
    );
  }
});

test("effectiveSsAmount = fraAmount * ssFactor (e.g. $33,000 FRA at 64 → $26,400)", () => {
  const SsClaim = loadSsClaim();
  // 33_000 * 0.80 = 26_400.
  assert.ok(Math.abs(SsClaim.effectiveSsAmount(33_000, 64) - 26_400) < 1e-6);
  // At FRA the effective amount equals the FRA amount unchanged (factor 1.0).
  assert.equal(SsClaim.effectiveSsAmount(40_000, 67), 40_000);
});

test("ssFactor throws outside [62, 70] (user error, not a clamp)", () => {
  const SsClaim = loadSsClaim();
  assert.throws(() => SsClaim.ssFactor(61), /between 62 and 70/);
  assert.throws(() => SsClaim.ssFactor(71), /between 62 and 70/);
});
