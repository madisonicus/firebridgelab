# ADR 0001 — Byte-stable canary discipline

## Context

This is a quantitative decision-support tool whose user cannot independently audit the
math — a plausible-but-wrong number is worse than no number, because it will be trusted. Across
17+ build slices and an architectural consolidation pass, we needed a way to prove that a refactor
changed *structure* without changing *results*.

## Decision

Every refactor preserves the default-scenario output **to the float**. Each return mode pins a
canary: Deterministic `final_portfolio == 8397280.186644783` (`tests/test_simulate.py`), Historical
`cohort_count` + depletion counts, Monte Carlo `trial_count == 10_000` + `depletion_count`
under a fixed seed, plus five API payload-snapshot tests pinning dict key order. A canary moving by
even one ULP fails the build.

## Consequences

- A refactor that is purely structural is *provably* so — the canary is green or the work is wrong.
- A genuine result change (e.g. a new spending mode that alters the default) requires deliberately
  re-pinning the canary, which forces a conscious "this number really did change, and here's why."
- Sub-ULP floating-point drift from algebraically-identical re-expressions is **not** automatically
  acceptable — it is arbitrated case-by-case (see ADR 0002 and the docstring of `firebridgelab/_step.py`).

## Amendment (2026-11): pins depend on the fetched data

The data is fetched from its publishers, not committed, and Robert Shiller revises
recent months with each monthly update. The deterministic default-scenario pins are
therefore exact only on the `ie_data.xls` they were taken from, whose sha256 is
recorded in `tests/_canary.py`. On any other revision the same tests hold to
`rel=1e-4`. Exactness between code paths (identity tests) is unaffected, because
both sides read the same data. Re-pinning on purpose stays a deliberate commit.

## Status

Accepted. Held to the cent through the entire 2026-05-28 architectural pass.
