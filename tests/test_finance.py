"""Tests for the NPV-of-gaps helper (funded-ratio feature slice 1, FR foundation).

These are the EXTERNAL reference for ``compute_npv_gaps_by_age`` correctness —
ported from the archived ``tests/test_tier_ladder.py`` closed-form cases and
adapted to the current ``Scenario`` constructor + GI model:

  1. Zero-income perpetuity: with no GI, NPV at the start age must equal the
     closed form ``Σ_{t=0..N} target·(1+r)^-t`` to floating tolerance.
  2. Full coverage: when GI ≥ target at every age, every gap is 0 → every NPV
     is 0.

Minimal scenarios are built directly from the current ``Scenario`` dataclass
(single age axis, four GI streams) — there is no separate spouse-age offset in
the current model.
"""

from __future__ import annotations

import pytest

from firebridgelab.finance import compute_npv_gaps_by_age, npv_gaps_at_age
from firebridgelab.scenario import AssetClass, Scenario


def _scenario(**overrides) -> Scenario:
    """A minimal scenario for NPV closed-form checks (ages 50..95 by default).

    Spending mode / portfolio / allocation are irrelevant to
    ``compute_npv_gaps_by_age`` (it reads only the horizon + the four GI
    streams + the supplied ``target_real``), so they carry inert placeholders.
    GI defaults to zero (start ages pushed out) so the perpetuity case is the
    base; the coverage case overrides the SS streams to dominate.
    """
    base = dict(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.0),),
        annual_spending=100_000.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=70,
    )
    base.update(overrides)
    return Scenario(**base)


def test_compute_npv_gaps_by_age_zero_income_yields_target_perpetuity():
    """No income, target $100k, discount 1%, ages 50..95 →
    NPV(50) = Σ_{t=0..45} 100k / 1.01^t (the discounted perpetuity-to-horizon)."""
    scenario = _scenario()  # all GI streams are 0
    npv = compute_npv_gaps_by_age(scenario, target_real=100_000.0, discount_rate=0.01)

    # Closed form: Σ_{t=0..45} (1.01)^-t.
    expected = sum((1.01 ** -t) for t in range(0, 95 - 50 + 1))
    assert npv[50] == pytest.approx(100_000.0 * expected, rel=1e-6)
    # NPV monotonically declines as we approach the horizon.
    assert npv[50] > npv[60] > npv[80] > npv[94] > npv[95]
    # End age: just gap(95), since there are no future years to discount.
    assert npv[95] == pytest.approx(100_000.0)


def test_compute_npv_gaps_by_age_full_coverage_yields_zero_gaps():
    """If GI fully covers the target at every age, NPV is zero everywhere."""
    # Huge SS claimed at the start age, so from age 50 onward GI dominates the
    # $100k target at every age.
    scenario = _scenario(
        person_a_ss_amount=250_000.0,
        person_a_ss_start_age=50,
        person_b_ss_amount=250_000.0,
        person_b_ss_start_age=50,
    )
    npv = compute_npv_gaps_by_age(scenario, target_real=100_000.0, discount_rate=0.01)

    # GI = 500k > 100k target at every age → every gap is 0 → every NPV is 0.
    for age, value in npv.items():
        assert value == pytest.approx(0.0), f"age {age} npv={value}"


def test_npv_gaps_at_age_matches_full_table_every_age():
    """The single-age helper ``npv_gaps_at_age`` (the focused form the amortization
    frontier bisects against) must agree with the full backward table at EVERY
    in-horizon age, with GI active so the netting is exercised. If these drifted, the
    frontier's FR-inversion self-consistency would check against the wrong reference."""
    scenario = _scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    target = 175_000.0
    r = 0.015
    table = compute_npv_gaps_by_age(scenario, target_real=target, discount_rate=r)
    for age in range(scenario.start_age, scenario.end_age + 1):
        assert npv_gaps_at_age(scenario, age, target, r) == pytest.approx(
            table[age], abs=1e-6
        ), f"single-age helper drifted from the table at age {age}"


def test_npv_gaps_at_age_zero_income_perpetuity():
    """No GI, target $100k, r=1%, at the start age → the discounted perpetuity-to-
    horizon Σ_{t=0..45} 100k/1.01^t (first term undiscounted, t=0)."""
    scenario = _scenario()
    expected = 100_000.0 * sum((1.01 ** -t) for t in range(0, 95 - 50 + 1))
    assert npv_gaps_at_age(scenario, 50, 100_000.0, 0.01) == pytest.approx(
        expected, rel=1e-9
    )
    # At end_age it's just that year's gap (no future terms).
    assert npv_gaps_at_age(scenario, 95, 100_000.0, 0.01) == pytest.approx(100_000.0)
