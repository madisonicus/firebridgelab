"""Absorbing depletion: a depleted path stays at 0 — GI can't resurrect it.

The bug: ``step_year`` is memoryless and floors each year independently, so a
path that depleted at (say) 53 recomputed ``end = 0 + 0 + gi − spend·(1+r/2)``
the year a GI stream landed — going POSITIVE whenever GI exceeds the funded gap
— and the "depleted" portfolio re-accumulated a balance. The fix makes depletion
absorbing at the loop seams (``sim_core._simulate_cohort``, the deterministic
loop in ``firebridgelab.engine.deterministic``, ``buckets.simulate_buckets_cohort``, and the
tax-location shadow's local scalar).

DIVERGENT START (feedback_preservation_vs_correctness): these tests run a
scenario that is NOT any canary — a tiny portfolio that depletes well BEFORE GI
onset, with a GI stream larger than spending starting at 67, i.e. exactly the
regime that resurrected under the old code (``step_year(0, r, gi, spend)`` is
asserted positive below, proving the naive recomputation differs). A byte-canary
that passes on both old and new code would prove nothing here.

Invariants pinned per mode (deterministic / historical / Monte Carlo / buckets):
  (a) the naive memoryless recomputation WOULD go positive after GI onset;
  (b) terminal balance == 0;
  (c) every post-depletion year's portfolio == 0 (the absorbing state);
  (d) depletion stats (first crossing / count) unchanged — pinned by equality
      with a GI-zeroed variant, whose pre-depletion path is identical because
      depletion happens before any GI stream starts.
Recorded post-depletion spend stays the GI-funded level (the existing
realized-spend semantics): 0 while no GI is active, the full (GI-covered)
intended spend once the 80k stream lands.
"""

from dataclasses import replace

from firebridgelab import (
    SOURCING_MODE_BUCKETS,
    default_bucket_config,
    default_scenario,
    simulate,
    simulate_historical,
    simulate_monte_carlo,
)
from firebridgelab.engine.step import step_year
from firebridgelab.engine.buckets import simulate_buckets_deterministic


def _resurrect_scenario(**overrides):
    """Tiny portfolio, spend below the eventual GI, GI starting after depletion.

    $200k funding a $60k/yr spend depletes at ~53 in EVERY return mode/cohort
    (no sequence can carry 200k through 17 years of 60k draws); the single $80k
    SS stream at 67 then exceeds the funded spend (60k·(1+r/2) < 80k for any
    r < 1), so the OLD memoryless step went positive at 67 — the resurrection
    closes. Built off the default so allocation / horizon /
    modes stay the production shapes.
    """
    base = replace(
        default_scenario(),
        initial_portfolio=200_000.0,
        annual_spending=60_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=80_000.0,
        person_b_ss_amount=0.0,
    )
    return replace(base, **overrides) if overrides else base


def _no_gi_variant(scenario):
    """The same scenario with the GI stream zeroed (the first-crossing control)."""
    return replace(scenario, person_a_ss_amount=0.0)


GI_ONSET_AGE = 67  # person_a_ss_start_age in the default scenario


def test_naive_memoryless_step_would_resurrect():
    """(a) The old per-year recomputation goes POSITIVE once GI > funded spend.

    ``step_year`` is the memoryless helper every loop calls: from a depleted
    (0) balance, a 80k GI year against a 60k spend returns a positive end —
    this is the resurrection the absorbing loops now suppress, proving the new
    trajectory invariants below genuinely diverge from naive recomputation.
    """
    end, depleted = step_year(0.0, 0.05, 80_000.0, 60_000.0)
    assert end > 0.0
    assert not depleted


def test_deterministic_depletion_is_absorbing():
    s = _resurrect_scenario()
    traj = simulate(s)

    # (d) First crossing: before GI onset, and identical to the GI-zeroed
    # control (depletion precedes every GI start age, so the crossing cannot
    # depend on the stream).
    assert traj.depleted_age == 53
    assert traj.depleted_age == simulate(_no_gi_variant(s)).depleted_age

    # (b) + (c): zero at the crossing and EVERY year after — including all
    # post-67 years where the old code re-accumulated 80k − 60k·(1+r/2) > 0.
    for p in traj.points:
        if p.age >= traj.depleted_age:
            assert p.end_portfolio == 0.0, f"age {p.age} resurrected: {p.end_portfolio}"
    assert traj.final_portfolio == 0.0

    # Recorded post-depletion spend is the GI-funded level (existing realized-
    # spend semantics, unchanged): 0 while no stream is active, the fully
    # GI-covered 60k once the 80k stream lands.
    for p in traj.points:
        if traj.depleted_age < p.age < GI_ONSET_AGE:
            assert p.spending == 0.0
        elif p.age >= GI_ONSET_AGE:
            assert p.spending == 60_000.0


def test_historical_depletion_is_absorbing():
    s = _resurrect_scenario()
    res = simulate_historical(s)

    # Every cohort depletes (200k cannot fund 60k/yr for 17 years on any
    # historical sequence) — so the whole fan must be pinned at 0 from GI onset.
    assert res.depletion_count == res.cohort_count

    # (d) Depletion stats identical to the GI-zeroed control: first crossings
    # all precede GI onset, so neither the count nor the earliest age may move.
    control = simulate_historical(_no_gi_variant(s))
    assert res.depletion_count == control.depletion_count
    assert res.earliest_depletion_age == control.earliest_depletion_age
    assert res.earliest_depletion_age < GI_ONSET_AGE

    # (b) + (c): p90 bounds the whole fan; from GI onset every cohort is
    # post-depletion, so every percentile at every age >= 67 must be 0 (the old
    # code resurrected EVERY cohort at 67, making p10..p90 positive here).
    onset_idx = res.ages.index(GI_ONSET_AGE)
    assert all(v == 0.0 for v in res.p90[onset_idx:])
    assert res.ending_range == (0.0, 0.0)


def test_monte_carlo_depletion_is_absorbing():
    s = _resurrect_scenario()
    res = simulate_monte_carlo(s, n_trials=300)  # default fixed seed — reproducible

    assert res.depletion_count == 300  # every trial depletes pre-onset

    # (d) Same fixed seed ⇒ identical draws; depletion precedes GI onset, so the
    # GI-zeroed control must report identical depletion stats.
    control = simulate_monte_carlo(_no_gi_variant(s), n_trials=300)
    assert res.depletion_count == control.depletion_count
    assert res.earliest_depletion_age == control.earliest_depletion_age
    assert res.earliest_depletion_age < GI_ONSET_AGE

    # (b) + (c): the entire fan pinned at 0 from GI onset.
    onset_idx = res.ages.index(GI_ONSET_AGE)
    assert all(v == 0.0 for v in res.p90[onset_idx:])
    assert res.ending_range == (0.0, 0.0)


def test_buckets_depletion_is_absorbing():
    """Buckets sourcing: GI landing on the cash bucket post-depletion must not
    resurrect any sleeve — the release-excess refill leg moved leftover GI into
    GROWTH under the old code, re-accumulating a 'depleted' portfolio."""
    s = _resurrect_scenario(
        sourcing_mode=SOURCING_MODE_BUCKETS, buckets=default_bucket_config()
    )
    traj = simulate_buckets_deterministic(s)

    # (d) First crossing before GI onset, identical to the GI-zeroed control.
    assert traj.depleted_age is not None
    assert traj.depleted_age < GI_ONSET_AGE
    control = simulate_buckets_deterministic(_no_gi_variant(s))
    assert traj.depleted_age == control.depleted_age

    # (b) + (c): every sleeve is 0 from the crossing on — no GI re-accumulation
    # in cash, and none routed to growth by the refill.
    for y in traj.years:
        if y.age >= traj.depleted_age:
            assert (y.cash, y.income, y.growth) == (0.0, 0.0, 0.0), (
                f"age {y.age} resurrected: {(y.cash, y.income, y.growth)}"
            )
            assert not y.forced_growth_sale
    assert traj.final_portfolio == 0.0

    # Recorded post-depletion spend is the GI-funded cascade level: 0 with no
    # active stream, the fully GI-covered 60k once the 80k stream lands.
    for y in traj.years:
        if traj.depleted_age < y.age < GI_ONSET_AGE:
            assert y.spending == 0.0
        elif y.age >= GI_ONSET_AGE:
            assert y.spending == 60_000.0

    # The aggregate Historical buckets path rides the same loop — fan pinned at
    # 0 from GI onset there too (every cohort depletes pre-onset).
    hist = simulate_historical(s)
    assert hist.depletion_count == hist.cohort_count
    onset_idx = hist.ages.index(GI_ONSET_AGE)
    assert all(v == 0.0 for v in hist.p90[onset_idx:])
    assert hist.ending_range == (0.0, 0.0)
