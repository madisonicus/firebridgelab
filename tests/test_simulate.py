"""Tests pinning down the engine math.

Tests, each anchoring one property of simulate():
  1. Zero return, zero spending, zero GI → portfolio is invariant.
  2. One-step arithmetic matches the midpoint convention (age-50 canary).
  3. GI streams activate at the listed ages, not before.
  4. The four per-person streams sum correctly at the default ages 60/65/67/70.
"""

import pytest

from dataclasses import replace

from firebridgelab import (
    AssetClass,
    Scenario,
    blended_real_return,
    default_scenario,
    simulate,
    spending_for_age,
)
from firebridgelab.scenario import SPENDING_MODE_CONSTANT, SPENDING_MODE_LIFECYCLE
from tests._canary import DET_FINAL_CANARY, DET_YEAR0_CANARY


def _alloc(real_return: float) -> tuple[AssetClass, ...]:
    """A single-class allocation pinned to a given real return.

    Lets the math tests express a flat real return the way the old scalar
    real_return field did, without caring about asset-class structure.
    """
    return (AssetClass("all", "All", 1.0, real_return),)


def _bare_scenario(**overrides) -> Scenario:
    """Build a Scenario with sensible zero/inert defaults for testing.

    All four GI streams default to $0 so a test can switch on exactly the
    stream(s) it cares about.
    """
    base = dict(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=_alloc(0.0),
        annual_spending=0.0,
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


def test_baseline_invariance_zero_return_zero_flows():
    """$1.8M, 0% return, $0 spending, $0 GI → ends at $1.8M after 46 years (ages 50-95 inclusive)."""
    scenario = _bare_scenario()
    traj = simulate(scenario)

    assert traj.depleted_age is None
    assert traj.final_portfolio == 1_800_000.0
    # 46 years of flat line
    assert len(traj.points) == 46  # ages 50..95 inclusive = 46 points
    assert all(p.end_portfolio == 1_800_000.0 for p in traj.points)


def test_one_step_arithmetic_matches_midpoint_convention():
    """$1.8M, 5% real return, $110k spending → year-1 portfolio = $1.77725M.

    Midpoint withdrawal convention (design.md §Slice 1 Method):
        end = start * (1 + r) + GI * (1 + r/2) - spending * (1 + r/2)
        1_800_000 * 1.05 + 0 - 110_000 * 1.025 = 1_777_250

    GI is not yet active at age 50 (first pension at 60, SS at 67).
    """
    scenario = _bare_scenario(
        allocation=_alloc(0.05),
        annual_spending=110_000.0,
    )
    traj = simulate(scenario)

    year_one = traj.points[0]
    assert year_one.age == 50
    assert year_one.guaranteed_income == 0.0
    # 1_800_000 * 1.05 - 110_000 * 1.025 = 1_890_000 - 112_750 = 1_777_250
    assert year_one.end_portfolio == 1_777_250.0


def test_blended_real_return_weights_class_returns():
    """blended_real_return = sum(weight * real_return) across classes.

    The default 60/40 Equity/Bonds @ 7%/2% blends to exactly 0.050, and an
    80/20 split @ 7%/2% blends to 0.060.
    """
    sixty_forty = Scenario(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 0.60, 0.07), AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.40, 0.02)),
        annual_spending=0.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=70,
    )
    assert blended_real_return(sixty_forty) == pytest.approx(0.05)
    assert blended_real_return(default_scenario()) == pytest.approx(0.05)

    eighty_twenty = _bare_scenario(
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 0.80, 0.07), AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.20, 0.02)),
    )
    assert blended_real_return(eighty_twenty) == pytest.approx(0.06)


def test_default_scenario_canary_final_value():
    """Default scenario canary: no depletion, final ≈ $6.07M, age-50 ≈ $1.784M.

    Pins the headline result of the locked default scenario. The default
    annual_spending ($110k) sits between the $85k guardrail floor and $150k
    ceiling, so the two-sided GK flex is visible in the default panel.
    """
    traj = simulate(default_scenario())
    assert traj.depleted_age is None
    assert traj.points[0].end_portfolio == DET_YEAR0_CANARY
    assert round(traj.final_portfolio / 1_000_000, 2) == 6.07


def test_default_scenario_canary_exact_final_value():
    """Hard canary: the EXACT deterministic terminal, to full float precision.

    Pins ``final_portfolio == DET_FINAL_CANARY`` (the default scenario at
    $110k annual_spending). The variable-spending SpendingStrategy refactor
    routes constant mode through the PureAgeStrategy wrapper over
    ``spending_for_age`` — byte-for-byte identical to the pre-refactor inline
    call — so this exact number must not move. A drift here means the engine
    path perturbed the constant arithmetic (a blocking regression).
    """
    traj = simulate(default_scenario())
    assert traj.final_portfolio == DET_FINAL_CANARY


# ---------- Lifecycle spending (go-go / slow-go / no-go) ----------
#
# spending_for_age is the ONE shared helper both engines call to shape spending.
# These pin: constant mode is a no-op (== annual_spending at every age), and
# lifecycle mode applies the age-anchored phase multipliers at the exact phase
# boundaries (74 vs 75, 84 vs 85).


def test_spending_for_age_constant_mode_is_annual_spending():
    """Constant mode returns annual_spending at every age (today's behavior)."""
    scenario = _bare_scenario(annual_spending=110_000.0)
    assert scenario.spending_mode == SPENDING_MODE_CONSTANT
    for age in (50, 74, 75, 84, 85, 95):
        assert spending_for_age(scenario, age) == 110_000.0


def test_spending_for_age_lifecycle_phase_boundaries():
    """Lifecycle phases: go-go ≤74 ×1.00, slow-go 75-84 ×0.85, no-go ≥85 ×0.75.

    annual_spending ($110k) is the go-go (peak) level; the boundary pairs
    74 vs 75 and 84 vs 85 are the load-bearing transitions.
    """
    scenario = _bare_scenario(
        annual_spending=110_000.0, spending_mode=SPENDING_MODE_LIFECYCLE
    )
    # Go-go runs through 74 (peak), drops to slow-go at 75.
    assert spending_for_age(scenario, 50) == 110_000.0
    assert spending_for_age(scenario, 74) == 110_000.0
    assert spending_for_age(scenario, 75) == pytest.approx(93_500.0)  # ×0.85
    # Slow-go runs through 84, drops to no-go at 85.
    assert spending_for_age(scenario, 84) == pytest.approx(93_500.0)
    assert spending_for_age(scenario, 85) == pytest.approx(82_500.0)   # ×0.75
    assert spending_for_age(scenario, 95) == pytest.approx(82_500.0)


def test_lifecycle_ends_higher_than_constant_same_input():
    """Lifecycle spends LESS late, so the same input ends with MORE in the bank.

    Sanity sign-check on the engine path: turning lifecycle on (same go-go peak)
    must raise the ending value, since slow-go/no-go withdrawals are smaller.
    """
    constant = default_scenario()
    lifecycle = replace(constant, spending_mode=SPENDING_MODE_LIFECYCLE)
    assert simulate(lifecycle).final_portfolio > simulate(constant).final_portfolio
    # And constant mode itself is unchanged — the canary still holds.
    assert round(simulate(constant).final_portfolio / 1_000_000, 2) == 6.07


def test_guaranteed_income_activates_at_listed_ages():
    """A single stream (Person A's pension) kicks in at its start age, not before."""
    scenario = _bare_scenario(
        person_a_pension_amount=40_000.0,
        person_a_pension_start_age=60,
    )
    traj = simulate(scenario)
    by_age = {p.age: p for p in traj.points}

    # Pre-pension: no income.
    assert by_age[50].guaranteed_income == 0.0
    assert by_age[59].guaranteed_income == 0.0
    # Pension active from its start age onward.
    assert by_age[60].guaranteed_income == 40_000.0
    assert by_age[95].guaranteed_income == 40_000.0


def test_default_guaranteed_income_sums_four_streams():
    """The four per-person streams sum correctly at the staged default ages.

    Defaults (fictional household):
      age 60: A pension on                 → 24,000
      age 65: + B pension                  → 24,000 + 9,000           = 33,000
      age 67: + A SS (claimed at FRA)      → 33,000 + 33,000          = 66,000
      age 70: + B SS (26,000 × 1.24 at 70) → 66,000 + 32,240          = 98,240
    The hand-summed total at 70 ($98,240) is the cross-check.
    """
    traj = simulate(default_scenario())
    by_age = {p.age: p for p in traj.points}

    # Before any stream activates.
    assert by_age[50].guaranteed_income == 0.0
    assert by_age[59].guaranteed_income == 0.0
    assert by_age[60].guaranteed_income == 24_000.0
    assert by_age[64].guaranteed_income == 24_000.0
    assert by_age[65].guaranteed_income == 33_000.0
    assert by_age[66].guaranteed_income == 33_000.0
    assert by_age[67].guaranteed_income == 66_000.0
    assert by_age[69].guaranteed_income == 66_000.0
    # All four active from 70.
    assert by_age[70].guaranteed_income == pytest.approx(98_240.0)
    assert by_age[95].guaranteed_income == pytest.approx(98_240.0)
