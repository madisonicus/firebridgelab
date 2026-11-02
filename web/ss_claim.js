// ss_claim.js — Social Security claim-age actuarial adjustment (pure helper).
//
// A FAITHFUL JS MIRROR of firebridgelab/income/social_security.py. The Python helper is the source of
// truth; this module reproduces its per-MONTH formula EXACTLY so the web UI can
// show a live "effective benefit" readout as the claim-age slider moves WITHOUT a
// server round-trip, and so the front-end derivation never drifts from the API's.
// tests/test_ss_claim_parity.mjs is the anti-drift guard: it pins ssFactor() for
// every age 62..70 against the known SSA factor table to 1e-9.
//
// Effective benefit = FRA ("primary insurance amount", PIA) benefit * factor:
//
//     effectiveSsAmount = fraAmount * ssFactor(claimAge)
//
// EXACT SSA rules (per-MONTH formula, NOT a per-age lookup table, so it
// generalizes to any FRA — see firebridgelab/income/social_security.py docstring + 20 CFR §404.410 /
// §404.313):
//   - Early (claim before FRA): 5/9 of 1% per month for the first 36 months
//     before FRA, then 5/12 of 1% per month beyond 36.
//   - Delayed (claim after FRA): 2/3 of 1% per month (= 8%/yr); credits stop at 70.
//   - At FRA the factor is exactly 1.0.
//
// SHAPE (window-global IIFE, no ES modules — matches payload_assembler.js /
// scenario_store.js / comparison_state.js):
//
//   SsClaim.ssFactor(claimAge, fraAge = 67) -> number
//   SsClaim.effectiveSsAmount(fraAmount, claimAge, fraAge = 67) -> number
//   SsClaim.MIN_CLAIM_AGE / SsClaim.MAX_CLAIM_AGE / SsClaim.DEFAULT_FRA_AGE

(function (global) {
  // The model assumes both people are born 1960+, so FRA is 67. INTERNAL constant — the model
  // fixes FRA at 67 and exposes the CLAIM age as the user knob, never FRA itself.
  // ssFactor keeps fraAge parameterized (so the per-month formula generalizes and
  // is unit-testable at other FRAs), but the app only ever passes this default.
  const DEFAULT_FRA_AGE = 67;

  // The claim-age window: claim as early as 62, no gain past 70 (delayed credits
  // stop accruing at 70). Outside [62, 70] is a user error, not a clamp — ssFactor
  // throws (mirrors the Python ValueError the API turns into a 422).
  const MIN_CLAIM_AGE = 62;
  const MAX_CLAIM_AGE = 70;

  // Per-month SSA adjustment rates, as FRACTIONS of the FRA benefit (not percent).
  const EARLY_RATE_FIRST_36 = (5.0 / 9.0) / 100.0; // 5/9 of 1% per month, first 36 mo
  const EARLY_RATE_BEYOND_36 = (5.0 / 12.0) / 100.0; // 5/12 of 1% per month, months 37+
  const DELAYED_RATE_PER_MONTH = (2.0 / 3.0) / 100.0; // 2/3 of 1% per month after FRA
  const EARLY_FIRST_TIER_MONTHS = 36; // boundary between the two early tiers

  // Actuarial factor: effective benefit = FRA benefit * ssFactor(claimAge).
  // Derived from the per-MONTH SSA rules (NOT a per-age table) so it generalizes to
  // any fraAge. `months` is the signed count of months between claim age and FRA:
  // negative = early (reduction), positive = delayed (credit), zero = at FRA → 1.0.
  // Throws on an out-of-range claim age (the message is user-facing).
  function ssFactor(claimAge, fraAge = DEFAULT_FRA_AGE) {
    if (!(claimAge >= MIN_CLAIM_AGE && claimAge <= MAX_CLAIM_AGE)) {
      throw new Error(
        `claim_age must be between ${MIN_CLAIM_AGE} and ${MAX_CLAIM_AGE} (got ${claimAge})`,
      );
    }

    const months = (claimAge - fraAge) * 12;

    if (months === 0) {
      // Exactly at FRA: no adjustment.
      return 1.0;
    }

    if (months < 0) {
      // Early: reduce. The first 36 months before FRA reduce at 5/9 of 1%/mo; any
      // months beyond 36 reduce at the gentler 5/12 of 1%/mo.
      const monthsEarly = -months;
      const firstTier = Math.min(monthsEarly, EARLY_FIRST_TIER_MONTHS);
      const beyondTier = Math.max(0, monthsEarly - EARLY_FIRST_TIER_MONTHS);
      const reduction =
        firstTier * EARLY_RATE_FIRST_36 + beyondTier * EARLY_RATE_BEYOND_36;
      return 1.0 - reduction;
    }

    // Delayed: increase at 2/3 of 1%/mo. Credits stop at 70; with the [62, 70]
    // guard above, claimAge never exceeds 70, so `months` is already capped at the
    // FRA→70 span and no extra clamp is needed.
    return 1.0 + months * DELAYED_RATE_PER_MONTH;
  }

  // The effective annual SS benefit for claiming at `claimAge`. `fraAmount` is the
  // benefit if claimed exactly at FRA (the PIA). At FRA (factor 1.0) this returns
  // fraAmount unchanged. Propagates the throw from ssFactor for an out-of-range age.
  function effectiveSsAmount(fraAmount, claimAge, fraAge = DEFAULT_FRA_AGE) {
    return fraAmount * ssFactor(claimAge, fraAge);
  }

  global.SsClaim = {
    ssFactor,
    effectiveSsAmount,
    DEFAULT_FRA_AGE,
    MIN_CLAIM_AGE,
    MAX_CLAIM_AGE,
  };
})(typeof window !== "undefined" ? window : globalThis);
