"""Golden tests for ``FundedRatioTierStrategy`` (fr-tiers, FR feature slice 4).

This is the correctness CAPSTONE of the funded-ratio feature: a port of the
legacy ``TierLadderPolicy`` "christmas tree". The decision LOGIC (phase routing,
hysteresis, crash and hard-floor clamps) follows the original golden tests; the
tier AMOUNTS are the baked preset table in ``firebridgelab/spending.py``,
restated here by hand so a silent edit to the table fails these tests. We drive
the strategy's PURE decision method ``_tier_decision`` with controlled inputs and
assert the preset numbers (go-go MAX $160k, BONUS $145k, FULL $130k, BELT $118k;
slow-go MAX $130k; FLOOR $85k; later flat $85k; the crash + hard-floor clamps;
the hysteresis hold/up/down).

The eight golden unit tests:
  1. test_phase_routing
  2. test_hysteresis_holds_within_band
  3. test_hysteresis_moves_down_outside_band
  4. test_hysteresis_moves_up_after_clearing_next_band
  5. test_crash_override_clamps_to_belt
  6. test_hard_floor_clamps_to_floor
  7. test_initial_decision_lands_in_a_tier
  8. test_year_zero_to_year_one_hysteresis_continuity

Plus: the NPV-target-invariant guard (a too-low target warns), two NPV closed-form
behaviors (re-asserted here against the current ``compute_npv_gaps_by_age``), and a
loose cross-engine integration sanity (fr_tiers in historical / MC lands in a sane
FLOOR..MAX band and doesn't crash).
"""

from __future__ import annotations

import warnings
from dataclasses import replace

import numpy as np
import pytest

from firebridgelab import default_scenario, simulate
from firebridgelab.finance import compute_npv_gaps_by_age
from firebridgelab.historical import simulate_historical
from firebridgelab.monte_carlo import simulate_monte_carlo
from firebridgelab.scenario import (
    AssetClass,
    Scenario,
    SPENDING_MODE_FR_TIERS,
)
from firebridgelab.spending import (
    FundedRatioStrategyBase,
    FundedRatioTierStrategy,
    _FR_TIER_NPV_TARGET,
    _fr_tier_floor_threshold,
    make_strategy,
)
import firebridgelab.spending as spending_module


# --------------------------------------------------------------------------- #
# shared helpers                                                              #
# --------------------------------------------------------------------------- #

def _tier_scenario(**overrides) -> Scenario:
    """A GI-free fr-tiers scenario (ages 50..95, $1.8M, $110k baseline).

    GI is zero so the NPV gaps are a clean target-perpetuity. The strategy's NPV
    target is the baked MAX tier amount ($160k), independent of annual_spending —
    so most decision tests engineer the FR directly via ``_tier_decision`` and the
    exact NPV value never enters the assertion.
    """
    base = dict(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.0),),
        annual_spending=110_000.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=70,
        spending_mode=SPENDING_MODE_FR_TIERS,
    )
    base.update(overrides)
    return Scenario(**base)


def _decide(strat, *, age, fr, smoothed, prev_portfolio, prev_state, gi,
            begin=None):
    """Drive the pure tier decision with controlled inputs (the archived
    ``PolicyContext`` analogue). ``begin`` defaults to ``smoothed`` (the no-crash
    case where the raw current balance equals the smoothed one)."""
    return strat._tier_decision(
        age=age,
        fr=fr,
        smoothed=smoothed,
        prev_portfolio=prev_portfolio,
        prev_state=prev_state,
        gi=gi,
        begin_year_portfolio=smoothed if begin is None else begin,
    )


# --------------------------------------------------------------------------- #
# dispatch + invariants                                                       #
# --------------------------------------------------------------------------- #

def test_dispatch_and_npv_target_is_max_tier():
    """The factory builds the tier strategy; NPV target is the MAX tier ($160k)."""
    strat = make_strategy(_tier_scenario())
    assert isinstance(strat, FundedRatioTierStrategy)
    # LOAD-BEARING: the FR denominator is the MAX go-go tier amount, NOT the floor
    # and NOT annual_spending.
    assert strat.npv_target_real == pytest.approx(160_000.0)
    assert strat.npv_target_real == pytest.approx(_FR_TIER_NPV_TARGET)
    assert strat.is_solvable is False
    assert strat.collects_spends is True


def test_no_activity_taper_and_no_operational_rails():
    """fr-tiers returns the tier amount DIRECTLY — no activity taper, no
    spending_floor / spending_ceiling clamp (unlike fr-smooth / fr-guardrail).

    Probed at a slow-go / no-go age with operational rails set absurdly tight: a
    taper or a floor/ceiling clamp would move the number off the tier amount. It
    must not.
    """
    # Operational floor $999k, ceiling $1: if fr-tiers used the rails this would be
    # forced to one of those. It must return the tier amount instead.
    strat = make_strategy(
        _tier_scenario(spending_floor=999_000.0, spending_ceiling=1.0)
    )
    # Age 70 (slow_go), FR high → slow-go MAX $130k, untouched by rails/taper.
    amount, name = _decide(
        strat, age=70, fr=2.6, smoothed=20e6,
        prev_portfolio=None, prev_state=None, gi=0.0,
    )
    assert name == "MAX"
    assert amount == pytest.approx(130_000.0)
    # Age 80 (later) → flat $85k, not tapered by a no-go multiplier.
    amount, name = _decide(
        strat, age=80, fr=2.6, smoothed=20e6,
        prev_portfolio=None, prev_state=None, gi=0.0,
    )
    assert name == "LATER"
    assert amount == pytest.approx(85_000.0)


# --------------------------------------------------------------------------- #
# the eight golden unit tests (preset tier amounts)                       #
# --------------------------------------------------------------------------- #

def test_phase_routing():
    """go_go / slow_go / later by age, with FR high enough to clear MAX.

    Preset: age 60 → MAX/$160k, age 70 → MAX/$130k, age 80 → LATER/$85k.
    """
    strat = make_strategy(_tier_scenario())
    # FR well above MAX's fr_min=1.50 at every age (engineer it directly).
    amount, name = _decide(strat, age=60, fr=2.6, smoothed=20e6,
                           prev_portfolio=None, prev_state=None, gi=100_000.0)
    assert name == "MAX"
    assert amount == pytest.approx(160_000.0)

    amount, name = _decide(strat, age=70, fr=2.6, smoothed=20e6,
                           prev_portfolio=None, prev_state=None, gi=100_000.0)
    assert name == "MAX"
    assert amount == pytest.approx(130_000.0)

    amount, name = _decide(strat, age=80, fr=2.6, smoothed=20e6,
                           prev_portfolio=None, prev_state=None, gi=100_000.0)
    assert name == "LATER"
    assert amount == pytest.approx(85_000.0)


def test_hysteresis_holds_within_band():
    """prev FULL, FR=0.97 (within band of FULL's 1.0) → holds FULL/$130k."""
    strat = make_strategy(_tier_scenario())
    amount, name = _decide(strat, age=60, fr=0.97, smoothed=20e6,
                           prev_portfolio=20e6, prev_state="FULL", gi=0.0)
    assert name == "FULL"
    assert amount == pytest.approx(130_000.0)


def test_hysteresis_moves_down_outside_band():
    """prev FULL, FR=0.90 (below FULL's 1.0 − 0.05 band) → BELT/$118k."""
    strat = make_strategy(_tier_scenario())
    amount, name = _decide(strat, age=60, fr=0.90, smoothed=20e6,
                           prev_portfolio=20e6, prev_state="FULL", gi=0.0)
    assert name == "BELT"
    assert amount == pytest.approx(118_000.0)


def test_hysteresis_moves_up_after_clearing_next_band():
    """prev FULL, FR=1.35 (clears BONUS's 1.25 + 0.05 band) → BONUS/$145k."""
    strat = make_strategy(_tier_scenario())
    amount, name = _decide(strat, age=60, fr=1.35, smoothed=20e6,
                           prev_portfolio=20e6, prev_state="FULL", gi=0.0)
    assert name == "BONUS"
    assert amount == pytest.approx(145_000.0)


def test_crash_override_clamps_to_belt():
    """30% YoY raw drop + FR high → clamp BELT, state 'BELT (crash)'/$118k."""
    strat = make_strategy(_tier_scenario())
    # smoothed high (FR=1.60 → MAX absent the crash); raw prev=10M, cur=7M (−30%).
    amount, name = _decide(strat, age=60, fr=1.60, smoothed=20e6,
                           prev_portfolio=10e6, prev_state=None, gi=0.0,
                           begin=7e6)
    assert "(crash)" in name
    assert name.startswith("BELT")
    assert amount == pytest.approx(118_000.0)


def test_hard_floor_clamps_to_floor():
    """smoothed below the floor threshold + FR high → 'FLOOR (absolute)'/$85k.

    smoothed=$100k is below the floor threshold (≥ $250k minimum) while FR=2.0 is
    high enough to land MAX absent the clamp.
    """
    strat = make_strategy(_tier_scenario())
    # floor threshold at gi=0 is max(250k, 3*85k)=255k; smoothed 100k < 255k.
    assert _fr_tier_floor_threshold(60, 0.0) == pytest.approx(255_000.0)
    amount, name = _decide(strat, age=60, fr=2.0, smoothed=100_000.0,
                           prev_portfolio=100_000.0, prev_state=None, gi=0.0)
    assert "(absolute)" in name
    assert name.startswith("FLOOR")
    assert amount == pytest.approx(85_000.0)


def test_initial_decision_lands_in_a_tier():
    """First-call (year-0) picks a valid tier — no prev_state, no crash, no hold.

    Driven through the real spine (``spend_for_year``): the first call reproduces
    the archived ``initial_decision`` (unbanded lookup on the raw initial portfolio,
    smoothed == current).
    """
    # $1.8M against a GI-free $160k NPV target → initial FR < 1 → lands in a lower
    # tier, as the design intends (the ladder starts conservative).
    scenario = _tier_scenario()
    strat = make_strategy(scenario)
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1_800_000.0, gi=0.0)
    assert strat.prev_state in {"MAX", "BONUS", "FULL", "BELT", "FLOOR"}
    assert strat.prev_spend is not None and strat.prev_spend > 0


def test_year_zero_to_year_one_hysteresis_continuity():
    """Year-0 anchor tier holds into year-1 at a stable portfolio (the seam).

    A large portfolio lands the year-0 anchor in an upper tier; year-1 at a near-
    identical portfolio must HOLD the same tier (hysteresis across the seam).
    """
    scenario = _tier_scenario(initial_portfolio=15_000_000.0)
    strat = make_strategy(scenario)
    # Year 0.
    sp0 = strat.spend_for_year(scenario, age=50,
                               begin_year_portfolio=15_000_000.0, gi=0.0)
    anchor = strat.prev_state
    assert anchor != "FLOOR", "setup: anchor should land in an upper tier"
    # Year 1: ~0.5% drift, well within the 5% band; smoothing ≈ the prior value.
    sp1 = strat.spend_for_year(scenario, age=51,
                               begin_year_portfolio=15_000_000.0 * 1.005, gi=0.0)
    assert strat.prev_state == anchor, (
        f"hysteresis broke across year-0/year-1 seam: {anchor!r} → {strat.prev_state!r}"
    )
    assert sp1 == pytest.approx(sp0)


# --------------------------------------------------------------------------- #
# NPV-target invariant guard                                                  #
# --------------------------------------------------------------------------- #

def test_npv_target_below_max_tier_warns(monkeypatch):
    """Constructing with an NPV target below the highest tier amount WARNS.

    The classic floor-vs-target bug: the FR denominator must be the MAX (scheduled)
    tier spend, not the floor. The guard makes a silent regression impossible.
    """
    monkeypatch.setattr(spending_module, "_FR_TIER_NPV_TARGET", 100_000.0)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        FundedRatioTierStrategy(_tier_scenario())
    fired = [w for w in rec if issubclass(w.category, UserWarning)]
    assert len(fired) >= 1
    assert "below the max tier" in str(fired[0].message).lower()


def test_npv_target_at_max_tier_does_not_warn():
    """The default ($160k == MAX) construction is silent — no false-positive guard."""
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        make_strategy(_tier_scenario())
    assert [w for w in rec if issubclass(w.category, UserWarning)] == []


# --------------------------------------------------------------------------- #
# NPV closed-form behaviors (re-asserted against the current finance helper)   #
# --------------------------------------------------------------------------- #

def test_npv_zero_income_yields_target_perpetuity():
    """No income, target $100k, 1% discount, ages 50..95 → the discounted sum."""
    scenario = _tier_scenario()  # GI-free
    npv = compute_npv_gaps_by_age(scenario, target_real=100_000.0, discount_rate=0.01)
    expected = sum((1.01 ** -t) for t in range(0, 95 - 50 + 1))
    assert npv[50] == pytest.approx(100_000.0 * expected, rel=1e-6)
    assert npv[50] > npv[60] > npv[80] > npv[94] > npv[95]
    assert npv[95] == pytest.approx(100_000.0)


def test_npv_full_coverage_yields_zero_gaps():
    """If GI fully covers the target at every age, every NPV is zero."""
    # SS of $250k each, claimed at 50 → GI = 500k > 100k target at every age.
    scenario = _tier_scenario(
        person_a_ss_amount=250_000.0, person_a_ss_start_age=50,
        person_b_ss_amount=250_000.0, person_b_ss_start_age=50,
    )
    npv = compute_npv_gaps_by_age(scenario, target_real=100_000.0, discount_rate=0.01)
    for age, value in npv.items():
        assert value == pytest.approx(0.0), f"age {age} npv={value}"


# --------------------------------------------------------------------------- #
# cross-engine integration sanity                                             #
# --------------------------------------------------------------------------- #

def test_fr_tiers_runs_across_all_three_engines():
    """fr-tiers runs in deterministic / historical / MC and returns a spend series."""
    scenario = replace(default_scenario(), spending_mode=SPENDING_MODE_FR_TIERS)
    det = simulate(scenario)
    assert len(det.points) == scenario.end_age - scenario.start_age + 1
    assert all(p.spending >= 0.0 for p in det.points)
    hist = simulate_historical(scenario)
    assert hist.spend_median is not None and len(hist.spend_median) > 0
    mc = simulate_monte_carlo(scenario, n_trials=200, seed=7)
    assert mc.spend_median is not None and len(mc.spend_median) > 0


def test_fr_tiers_spend_lands_in_floor_to_max_band():
    """The spending fan stays within the tier ladder's FLOOR..MAX envelope.

    Every realized spend must be a tier amount (or a clamp tier) — so the whole
    series sits in [$85k FLOOR, $160k go-go MAX]. A loose-but-real sanity that the
    ladder governs spending and the strategy doesn't blow past its own envelope.
    """
    scenario = replace(default_scenario(), spending_mode=SPENDING_MODE_FR_TIERS)
    mc = simulate_monte_carlo(scenario, n_trials=500, seed=11)
    med = np.array(mc.spend_median, dtype=float)
    # The median spend at every age is a tier amount in [FLOOR, MAX].
    assert med.min() >= 85_000.0 - 1e-6
    assert med.max() <= 160_000.0 + 1e-6
    # And the percentiles, where present, stay in-band too.
    for series in (mc.spend_p10, mc.spend_p90):
        if series:
            arr = np.array(series, dtype=float)
            # p10 can sit at FLOOR; p90 can reach toward MAX — never outside.
            assert arr.min() >= 85_000.0 - 1e-6
            assert arr.max() <= 160_000.0 + 1e-6


if __name__ == "__main__":
    test_phase_routing()
    test_hysteresis_holds_within_band()
    test_hysteresis_moves_down_outside_band()
    test_hysteresis_moves_up_after_clearing_next_band()
    test_crash_override_clamps_to_belt()
    test_hard_floor_clamps_to_floor()
    test_initial_decision_lands_in_a_tier()
    test_year_zero_to_year_one_hysteresis_continuity()
    print("OK")
