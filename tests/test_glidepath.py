"""Tests for the glidepath / bond-tent allocation slice (glidepath design).

Each anchors one property of the glidepath generalization:
  1. Boundary derivation: the default scenario derives {50,60,65,67,70}; a differing
     SS claim age (Person B at 70) adds a boundary at 70; an out-of-horizon age clips.
  2. Off-by-one at each boundary: phases are contiguous, half-open, lower-bound
     INCLUSIVE — age 65 belongs to the second-pension phase, not the prior one.
  3. ``blended_real_return_at_age`` returns the right phase's blend at each
     boundary age (constant mode is flat; glidepath steps at the boundaries).
  4. Degenerate-glide identity: glidepath ON with EVERY phase == the default
     allocation reproduces the constant result byte-for-byte (all three engines).
  5. Directional sanity: a conservative bond bridge lowers the deterministic
     terminal vs constant 60/40 (the return moves the way the allocations imply).
"""

from dataclasses import replace

import pytest

from firebridgelab import (
    ALLOCATION_401K_ACCESS_AGE,
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    AssetClass,
    blended_real_return,
    blended_real_return_at_age,
    default_scenario,
    glidepath_boundaries,
    phase_index_for_age,
    simulate,
    simulate_historical,
    simulate_monte_carlo,
)
from firebridgelab.scenario import validate_glidepath_class_consistency
from tests._canary import DET_FINAL_CANARY


def _degenerate_glide(scenario):
    """The same scenario with glidepath ON and every phase == the single allocation.

    The degenerate glide: by construction it must be a no-op vs constant mode (the
    phase machinery is a clean generalization, not a new code path with drift).
    """
    boundaries = glidepath_boundaries(scenario)
    return replace(
        scenario,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=tuple(scenario.allocation for _ in boundaries),
    )


def _bond_tent(scenario):
    """A real bond tent: 100% bonds through the bridge, 60/40 once income arrives.

    Phases for the default scenario are 50–59 / 60–64 / 65–66 / 67–69 / 70–95; the
    bridge phase (before Person A's pension at 60) holds all bonds (@2% real), the
    post-income phases hold the default 60/40. Used for the directional-sanity check.
    """
    bonds = (AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 1.0, 0.02),)
    sixty_forty = scenario.allocation
    boundaries = glidepath_boundaries(scenario)  # [50, 60, 65, 67, 70]
    # Bridge = phases that open before the first pension; re-risk at the pension.
    phases = tuple(
        bonds if start < scenario.person_a_pension_start_age else sixty_forty
        for start in boundaries
    )
    return replace(
        scenario,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=phases,
    )


# ---------- Boundary derivation ----------


def test_default_boundaries():
    """Default scenario derives {50, 60, 65, 67, 70} → five phases.

    The set is {401k access 60} ∪ {pensions 60, 65} ∪ {SS 67, 70}, plus the
    start_age 50 the first phase always opens at (60 appears once).
    """
    assert glidepath_boundaries(default_scenario()) == [50, 60, 65, 67, 70]


def test_401k_constant_is_60():
    """The one new fixed constant: 401(k) penalty-free access modeled at age 60."""
    assert ALLOCATION_401K_ACCESS_AGE == 60


def test_differing_ss_claim_age_adds_boundary():
    """Person B claiming SS at 70 (Person A at 67) adds a boundary at 70.

    The whole point of deriving rather than entering: a per-person claim-age
    difference simply makes an extra phase appear. With both claiming at 67 the
    70 boundary is absent; the default (B at 70) has it.
    """
    same_claim = replace(default_scenario(), person_b_ss_start_age=67)
    assert glidepath_boundaries(same_claim) == [50, 60, 65, 67]
    scenario = replace(same_claim, person_b_ss_start_age=70)
    assert glidepath_boundaries(scenario) == [50, 60, 65, 67, 70]


def test_out_of_horizon_claim_age_clips():
    """A claim age past end_age is clipped (dropped), not crashed."""
    scenario = replace(default_scenario(), person_b_ss_start_age=120)
    assert glidepath_boundaries(scenario) == [50, 60, 65, 67]


def test_inflection_at_or_below_start_collapses():
    """An inflection at/below start_age collapses into the first phase.

    A pension that starts at 50 (== start_age) does not add a boundary before the
    start; one below start_age (e.g. 45) is likewise absorbed.
    """
    scenario = replace(
        default_scenario(), person_a_pension_start_age=50, person_b_pension_start_age=45
    )
    # 401k 60, SS 67 and 70 remain; the two pensions at/below 50 collapse into phase 0.
    assert glidepath_boundaries(scenario) == [50, 60, 67, 70]


# ---------- Off-by-one at each boundary (lower-bound inclusive) ----------


def test_phase_index_off_by_one_at_each_boundary():
    """Phases are lower-bound INCLUSIVE: a boundary age belongs to the LATER phase.

    For the default boundaries [50, 60, 65, 67, 70]:
      50–59 → 0,  60–64 → 1,  65–66 → 2,  67–69 → 3,  70–95 → 4.
    Age 65 belongs to the second-pension phase (2), NOT the prior phase (1).
    """
    scenario = default_scenario()
    expected = {
        50: 0, 59: 0,   # bridge
        60: 1, 64: 1,   # 401(k) access + Person A pension
        65: 2, 66: 2,   # Person B pension (65 is the boundary — later phase wins)
        67: 3, 69: 3,   # Person A Social Security
        70: 4, 95: 4,   # Person B Social Security
    }
    for age, phase in expected.items():
        assert phase_index_for_age(scenario, age) == phase, f"age {age}"


def test_blended_return_at_age_constant_is_flat():
    """Constant allocation_mode: the per-age return is the constant blend at EVERY age.

    This is what keeps the deterministic canary byte-for-byte — the per-age seam
    returns exactly ``blended_real_return`` for every age under constant mode.
    """
    scenario = default_scenario()
    constant = blended_real_return_at_age(scenario, scenario.start_age)
    # The deterministic r is the REBALANCED compound return (review 2026-10-11),
    # above the plain weighted knob average by the diversification return.
    assert constant > blended_real_return(scenario)
    for age in range(scenario.start_age, scenario.end_age + 1):
        assert blended_real_return_at_age(scenario, age) == constant


def test_blended_return_at_age_steps_at_boundaries():
    """Glidepath: the per-age return is the active phase's blend, stepping at boundaries.

    The bond tent holds 2% real through 50–59 (the bridge), then the 60/40 blend
    from 60 (first pension) onward. The step lands exactly at age 60
    (lower-bound inclusive).
    """
    tent = _bond_tent(default_scenario())
    for age in (50, 55, 59):
        assert blended_real_return_at_age(tent, age) == pytest.approx(0.02), f"age {age}"
    sixty_forty = blended_real_return_at_age(default_scenario(), 50)  # rebalanced 60/40
    for age in (60, 64, 65, 67, 70, 95):
        assert blended_real_return_at_age(tent, age) == pytest.approx(sixty_forty), f"age {age}"


# ---------- Degenerate-glide identity (all three engines) ----------


def test_degenerate_glide_deterministic_identity():
    """Glidepath ON with every phase == default → deterministic canary byte-for-byte."""
    scenario = default_scenario()
    const = simulate(scenario)
    glide = simulate(_degenerate_glide(scenario))
    assert glide.final_portfolio == const.final_portfolio == DET_FINAL_CANARY
    assert glide.depleted_age == const.depleted_age
    assert [p.end_portfolio for p in glide.points] == [
        p.end_portfolio for p in const.points
    ]


def test_degenerate_glide_historical_identity():
    """Glidepath ON with every phase == default → Historical fan byte-for-byte."""
    scenario = default_scenario()
    const = simulate_historical(scenario)
    glide = simulate_historical(_degenerate_glide(scenario))
    assert glide.cohort_count == const.cohort_count == 110
    assert glide.start_year == const.start_year == 1871
    assert glide.median == const.median
    assert glide.p10 == const.p10
    assert glide.p25 == const.p25
    assert glide.p75 == const.p75
    assert glide.p90 == const.p90
    assert glide.depletion_count == const.depletion_count


def test_degenerate_glide_monte_carlo_identity():
    """Glidepath ON with every phase == default → Monte Carlo fan byte-for-byte.

    Same seed + 10k trials. The glidepath blend reduces to the constant
    ``returns @ w`` matmul when every phase shares the single allocation, so the
    sampled draws AND the blend are bit-identical.
    """
    scenario = default_scenario()
    const = simulate_monte_carlo(scenario)
    glide = simulate_monte_carlo(_degenerate_glide(scenario))
    assert glide.trial_count == const.trial_count == 10_000
    assert glide.median == const.median
    assert glide.p10 == const.p10
    assert glide.p90 == const.p90
    assert glide.depletion_count == const.depletion_count


# ---------- Directional sanity ----------


def test_bond_tent_lowers_deterministic_terminal():
    """A conservative bond bridge ends lower than constant 60/40 (deterministic).

    The bridge years (50–59) compound at 2% instead of 5%, so less growth before
    the income floor arrives — the terminal moves DOWN, the direction the
    allocations imply (glidepath design gate #3).
    """
    scenario = default_scenario()
    const_terminal = simulate(scenario).final_portfolio
    tent_terminal = simulate(_bond_tent(scenario)).final_portfolio
    assert tent_terminal < const_terminal


def test_glidepath_off_by_default():
    """The default scenario is constant allocation_mode (glidepath off)."""
    assert default_scenario().allocation_mode == ALLOCATION_MODE_CONSTANT
    assert default_scenario().phase_allocations is None


# ---------- Per-class-assumption consistency (v1 contract) ----------


def _conflicting_return_glide(scenario):
    """Glidepath where the SAME class id carries DIFFERENT real_return across phases.

    The bridge holds bonds @2% real; the post-income phases hold the SAME bond id
    but @5% real (an illegal per-phase return assumption). The deterministic engine
    would read each phase's own real_return while MC dedups the bond series by id,
    so the two engines would silently describe different portfolios. The contract
    rejects this.
    """
    boundaries = glidepath_boundaries(scenario)
    bonds_low = (AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 1.0, 0.02),)
    bonds_high = (AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 1.0, 0.05),)
    phases = tuple(
        bonds_low if start < scenario.person_a_pension_start_age else bonds_high
        for start in boundaries
    )
    return replace(
        scenario,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=phases,
    )


def test_validate_rejects_conflicting_class_params():
    """Same id with differing real_return across phases raises a clear ValueError."""
    scenario = _conflicting_return_glide(default_scenario())
    with pytest.raises(ValueError, match="conflicting real_return/volatility"):
        validate_glidepath_class_consistency(scenario)


def test_monte_carlo_rejects_conflicting_class_params():
    """The MC engine (via the union builder) raises rather than silently blend.

    This is the defense-in-depth guard for direct engine callers: without it, MC
    would dedup the bond series by id (first occurrence wins) and produce a number
    that silently disagrees with the deterministic engine.
    """
    scenario = _conflicting_return_glide(default_scenario())
    with pytest.raises(ValueError, match="conflicting real_return/volatility"):
        simulate_monte_carlo(scenario)


def test_validate_allows_same_params_different_weights():
    """The LEGAL case: same id, same params, different WEIGHTS across phases.

    This is what the UI actually produces (a glidepath varies weights, not returns).
    It must pass validation and, when every phase equals the single allocation, still
    reproduce the degenerate-glide MC identity byte-for-byte.
    """
    scenario = default_scenario()
    eq = scenario.allocation[0]  # us_total_market @ 0.07 / 0.174
    bd = scenario.allocation[1]  # us_agg_bonds   @ 0.02 / 0.088
    boundaries = glidepath_boundaries(scenario)
    # Same ids + same return params every phase; only the WEIGHTS vary by phase.
    conservative = (
        AssetClass(eq.id, eq.label, 0.3, eq.real_return, eq.volatility),
        AssetClass(bd.id, bd.label, 0.7, bd.real_return, bd.volatility),
    )
    aggressive = (
        AssetClass(eq.id, eq.label, 0.8, eq.real_return, eq.volatility),
        AssetClass(bd.id, bd.label, 0.2, bd.real_return, bd.volatility),
    )
    legal = replace(
        scenario,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=tuple(
            conservative if start < scenario.person_a_pension_start_age else aggressive
            for start in boundaries
        ),
    )
    # No raise — the contract permits varying weights.
    validate_glidepath_class_consistency(legal)
    # And it runs in MC (a real number, not a crash).
    assert simulate_monte_carlo(legal).trial_count == 10_000

    # The degenerate glide (every phase == the single allocation) still holds the
    # MC identity byte-for-byte after the guard was added.
    const = simulate_monte_carlo(scenario)
    glide = simulate_monte_carlo(_degenerate_glide(scenario))
    assert glide.median == const.median
    assert glide.p10 == const.p10
    assert glide.p90 == const.p90
    assert glide.depletion_count == const.depletion_count
