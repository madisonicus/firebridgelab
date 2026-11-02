"""Tests for the spending solver (max sustainable spend).

Each anchors one property of the solver (see the design notes):
  1. Constant + lifecycle + deterministic modes produce sane solved numbers.
  2. Monotonicity: survival fraction is weakly non-increasing across a spend
     sweep (the property that makes bisection valid).
  3. Self-consistency: achieved >= threshold at the reported number, and a probe
     above the boundary fails (boundary correctness, allowing for round-down).
  4. Lean-path equivalence: the depletion-only loop returns the same
     depletion_count as the full simulate_historical for the same scenario.
  5. Confidence ordering: a higher confidence target solves a lower (or equal)
     spend; deterministic solves >= historical-100% (the sequence-risk gap).
  6. Ceiling guard: a GI-covers-everything scenario reports hit_ceiling.
  7. Display: the solved number is rounded down to the nearest $1,000.

Tax-drag (design td1, DECISION 8, td-b slice): deterministic + historical ride
the DRAGGED engine when a split is supplied (1-8), Monte Carlo + a split is
fenced (9), and the monotonicity re-derivation is probed adversarially at the
two named kinks — the lever's bracket-fill crossing and the RMD onset
(ages 74->75, varying prior tax-deferred) (10).
"""

import pytest

from dataclasses import replace

import numpy as np

from firebridgelab import (
    AssetClass,
    default_scenario,
    simulate_historical,
    solve_max_spend,
)
from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS, SPENDING_MODE_LIFECYCLE
from firebridgelab.analysis.solver import (
    MODE_DETERMINISTIC,
    MODE_HISTORICAL,
    MODE_MONTE_CARLO,
    TAX_DRAG_MC_FENCE_MESSAGE,
    _historical_evaluator,
    _historical_evaluator_dragged,
    evaluate,
)
from firebridgelab.engine.monte_carlo import _monte_carlo_evaluator
from firebridgelab.spending.base import TaxDragMonteCarloNotSolvable
from firebridgelab.spending.registry import make_strategy
from firebridgelab.tax.location_path import (
    deterministic_blended_returns,
    simulate_dragged_depletion_only,
    simulate_tax_location_deterministic,
)

# The split fixture reused across the td-b solver tests: sums exactly to
# default_scenario().initial_portfolio ($1.8M) in the default 25/60/15 split,
# heavy enough in tax-deferred to guarantee a real tax bite.
_SPLIT_KWARGS = dict(
    taxable_balance=450_000.0,
    tax_deferred_balance=1_080_000.0,
    roth_balance=270_000.0,
)


def test_constant_mode_solves_a_sane_number():
    """Historical constant-mode solve on the default lands in a sane band.

    The default 60/40 with heavy GI sustains a six-figure spend; at 80%
    confidence the solved number should exceed $100k and stay well under the
    portfolio (a draw-everything ceiling).
    """
    result = solve_max_spend(default_scenario(), threshold=0.80, mode=MODE_HISTORICAL)
    assert result.mode == MODE_HISTORICAL
    assert result.cohort_count is not None and result.cohort_count > 100
    assert not result.hit_ceiling
    assert 100_000 < result.solved_spend < 400_000
    assert result.solved_spend % 1_000 == 0  # rounded to $1k


def test_lifecycle_mode_solves_higher_peak_than_constant():
    """Lifecycle solves a HIGHER go-go peak than constant at the same confidence.

    The solved scalar is the go-go peak; lifecycle tapers later-life spending
    (x0.85 then x0.75), so the same portfolio supports a higher peak than a flat
    spend held forever. Sanity sign-check that the shape flows through.
    """
    constant = solve_max_spend(default_scenario(), 0.90, MODE_HISTORICAL)
    lifecycle = solve_max_spend(
        replace(default_scenario(), spending_mode=SPENDING_MODE_LIFECYCLE),
        0.90,
        MODE_HISTORICAL,
    )
    assert lifecycle.solved_spend > constant.solved_spend


def test_deterministic_mode_solves_a_sane_number():
    """Deterministic solve returns a single-path boundary with no cohort count.

    Deterministic survival is binary, so threshold is 1.0 and cohort_count is
    None. The solved spend should be a positive, $1k-rounded number above the
    default spend (the default never depletes deterministically).
    """
    result = solve_max_spend(default_scenario(), threshold=1.0, mode=MODE_DETERMINISTIC)
    assert result.mode == MODE_DETERMINISTIC
    assert result.cohort_count is None
    assert result.solved_spend > default_scenario().annual_spending
    assert result.solved_spend % 1_000 == 0


@pytest.mark.parametrize("spending_mode", ["constant", SPENDING_MODE_LIFECYCLE])
def test_survival_fraction_monotonic_non_increasing(spending_mode):
    """Survival fraction is weakly non-increasing across an ascending spend sweep.

    This is the property that makes bisection valid (dend/dspend <= 0 for every
    cohort-year). We sweep spend across a wide range on the historical cohorts
    (an adversarial all-equity, low-GI scenario to exercise many depletions) and
    assert no spend INCREASE ever raises the survival fraction. Checked in both
    spending shapes — the lifecycle multipliers must preserve monotonicity too.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=spending_mode,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 0.80, 0.07), AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.20, 0.02)),
    )
    survives, _ = _historical_evaluator(scenario)
    spends = list(range(0, 400_001, 20_000))
    fractions = [survives(s) for s in spends]
    for earlier, later in zip(fractions, fractions[1:]):
        assert later <= earlier + 1e-12  # non-increasing (allow fp noise)


@pytest.mark.parametrize(
    "threshold,mode",
    [
        (0.80, MODE_HISTORICAL),
        (0.90, MODE_HISTORICAL),
        (0.95, MODE_HISTORICAL),
        (1.00, MODE_HISTORICAL),
        (1.00, MODE_DETERMINISTIC),
    ],
)
def test_self_consistency_achieved_clears_and_boundary_holds(threshold, mode):
    """achieved >= threshold at the reported number, and +$2k drops below it.

    The hard gate: the displayed (round-down) number must clear the target, and
    a probe at solved_spend + $2,000 must fail OR the result hit the ceiling
    (boundary correctness — the +$2k headroom absorbs the <=$999 round-down so
    this isn't fragile against a +$1k re-probe).
    """
    result = solve_max_spend(default_scenario(), threshold, mode)
    scenario_at_displayed = replace(
        default_scenario(), annual_spending=result.solved_spend
    )
    assert evaluate(scenario_at_displayed, mode) == pytest.approx(
        result.achieved_fraction
    )
    assert result.achieved_fraction >= threshold

    if not result.hit_ceiling:
        above = replace(
            default_scenario(), annual_spending=result.solved_spend + 2_000.0
        )
        assert evaluate(above, mode) < threshold


def test_lean_path_matches_full_engine_depletion_count():
    """The depletion-only loop counts the same depletions as simulate_historical.

    Pins that evaluate()'s lean inner loop didn't drift from the full engine: for
    the same (stressed, so depletions actually happen) scenario, the survival
    fraction implies exactly simulate_historical's depletion_count.
    """
    scenario = replace(
        default_scenario(),
        annual_spending=300_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07),),
    )
    full = simulate_historical(scenario)
    survives, cohort_count = _historical_evaluator(scenario)
    fraction = survives(scenario.annual_spending)

    assert cohort_count == full.cohort_count
    implied_depletions = round((1.0 - fraction) * cohort_count)
    assert implied_depletions == full.depletion_count


def test_higher_confidence_solves_lower_or_equal_spend():
    """A stricter confidence target never solves MORE spend (monotone in the dial).

    80% should solve at least as much as 90/95/100% (the dial trades spend for
    safety). Equalities are allowed — with ~130-150 cohorts adjacent stops can
    coincide — but the sequence must be non-increasing.
    """
    spends = [
        solve_max_spend(default_scenario(), t, MODE_HISTORICAL).solved_spend
        for t in (0.80, 0.90, 0.95, 1.00)
    ]
    for earlier, later in zip(spends, spends[1:]):
        assert later <= earlier


def test_deterministic_solves_at_least_historical_100():
    """Deterministic solved spend >= historical-100% solved spend.

    The deterministic average hides sequence-of-returns risk, so its single path
    sustains at least as much as the worst historical cohort requires at 100%
    confidence. The gap is the sequence risk the deterministic view conceals.
    """
    det = solve_max_spend(default_scenario(), 1.0, MODE_DETERMINISTIC).solved_spend
    hist_100 = solve_max_spend(default_scenario(), 1.0, MODE_HISTORICAL).solved_spend
    assert det >= hist_100


def test_ceiling_guard_when_gi_covers_everything():
    """A tiny portfolio with huge GI survives even at the search cap → hit_ceiling.

    With GI alone far exceeding any plausible spend up to 10x the (tiny)
    portfolio, no probe in the bracket fails, so the solver caps out and flags
    hit_ceiling with solved_spend at the cap.
    """
    scenario = replace(
        default_scenario(),
        initial_portfolio=1_000.0,
        person_a_pension_amount=500_000.0,
        person_a_pension_start_age=50,
        person_b_pension_amount=500_000.0,
        person_b_pension_start_age=50,
    )
    result = solve_max_spend(scenario, 1.0, MODE_DETERMINISTIC)
    assert result.hit_ceiling
    assert result.solved_spend == pytest.approx(10.0 * scenario.initial_portfolio)


def test_evaluate_rejects_unknown_mode():
    """evaluate() raises ValueError on an unknown mode (API routes it to 422)."""
    with pytest.raises(ValueError):
        evaluate(default_scenario(), "bootstrap")


@pytest.mark.parametrize(
    "mode",
    [MODE_DETERMINISTIC, MODE_HISTORICAL, MODE_MONTE_CARLO],
)
def test_solve_rejects_guardrails_mode(mode):
    """solve_max_spend raises ValueError in guardrails spending mode (view-only).

    Guardrail spend is portfolio-dependent (the floor clamp drops spend to the
    floor in bad sequences), so survival is non-monotonic in the spend dial and
    never decays to 0 — the bisection's monotonicity proof does not hold and the
    solver would return a confidently-wrong number. The guard is mode-agnostic:
    it fences off deterministic, historical, AND monte_carlo before any bisection
    runs. The API routes this ValueError to a 422.
    """
    scenario = replace(default_scenario(), spending_mode=SPENDING_MODE_GUARDRAILS)
    with pytest.raises(ValueError, match="guardrails mode"):
        solve_max_spend(scenario, 0.80, mode)


def test_evaluate_rejects_guardrails_mode():
    """evaluate() also fences off guardrails mode (same view-only invariant).

    ``evaluate`` is the solver's other public entry point; the guard sits before
    any mode dispatch, so it rejects guardrails regardless of engine mode.
    """
    scenario = replace(default_scenario(), spending_mode=SPENDING_MODE_GUARDRAILS)
    with pytest.raises(ValueError, match="guardrails mode"):
        evaluate(scenario, MODE_HISTORICAL)


def test_solver_raises_on_no_mapped_class_historical():
    """A historical solve with no mapped allocation class raises ValueError.

    The same data-can't-lie invariant simulate_historical enforces; the API
    routes this to a 422.
    """
    scenario = replace(
        default_scenario(),
        allocation=(AssetClass("crypto", "Crypto", 1.0, 0.0),),
    )
    with pytest.raises(ValueError):
        solve_max_spend(scenario, 0.80, MODE_HISTORICAL)


# ---------- Monte Carlo solver (Build 2) ----------
#
# The MC solver reuses the SAME shared sampler the display path uses (fixed
# seed/count → bit-identical draws), precomputes the trials ONCE, then bisects on
# spend over those FIXED draws (so the solve is reproducible + monotonic). These
# anchor the build-2 gate: monotonicity (bisection validity), threshold + repro,
# the deterministic-≥-MC sequence-risk gap, and the dial behaviour (vol /
# correlation move the success fraction the expected direction).


def _adversarial_mc_scenario():
    """An all-equity, no-GI scenario so MC depletions actually happen across spend.

    Mirrors the historical monotonicity test's adversarial setup, with an explicit
    equity vol so the sampler has dispersion to deplete on.
    """
    return replace(
        default_scenario(),
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07, 0.174),),
    )


def test_mc_survival_fraction_monotonic_non_increasing():
    """MC success fraction is weakly non-increasing across an ascending spend sweep.

    The property that makes bisection valid: with the seeded draws FIXED across
    all probes, each trial's survival flips at most once in spend (spend
    coefficient (1+r/2) > 0; GI spend-independent), so the aggregate success
    fraction can only fall as spend rises. We sweep a wide range over the FIXED
    sampled trials and assert no spend increase ever raises the fraction.
    """
    survives, _ = _monte_carlo_evaluator(_adversarial_mc_scenario())
    spends = list(range(0, 400_001, 20_000))
    fractions = [survives(s) for s in spends]
    for earlier, later in zip(fractions, fractions[1:]):
        assert later <= earlier + 1e-12  # non-increasing (allow fp noise)


def test_mc_solve_achieves_threshold_and_is_reproducible():
    """MC solved spend clears the confidence target, and solving twice matches.

    Fixed seed/count ⇒ the same scenario solves to the EXACT same number both
    times (no flicker), and the achieved fraction at the displayed number clears
    the chosen 80% target by construction (round-down keeps it on the safe side).
    """
    first = solve_max_spend(default_scenario(), 0.80, MODE_MONTE_CARLO)
    second = solve_max_spend(default_scenario(), 0.80, MODE_MONTE_CARLO)

    assert first.mode == MODE_MONTE_CARLO
    assert first.cohort_count == 10_000  # trial count rides in cohort_count
    assert first.solved_spend % 1_000 == 0
    assert first.achieved_fraction >= 0.80
    # Reproducible: identical solved number + achieved fraction.
    assert first.solved_spend == second.solved_spend
    assert first.achieved_fraction == second.achieved_fraction


def test_mc_solve_self_consistency_boundary_holds():
    """achieved >= threshold at the reported number; +$2k drops below it.

    Boundary correctness for the MC bisection (the +$2k headroom absorbs the
    <=$999 round-down), unless the result hit the ceiling.
    """
    result = solve_max_spend(default_scenario(), 0.80, MODE_MONTE_CARLO)
    at_displayed = replace(default_scenario(), annual_spending=result.solved_spend)
    assert evaluate(at_displayed, MODE_MONTE_CARLO) == pytest.approx(
        result.achieved_fraction
    )
    assert result.achieved_fraction >= 0.80
    if not result.hit_ceiling:
        above = replace(
            default_scenario(), annual_spending=result.solved_spend + 2_000.0
        )
        assert evaluate(above, MODE_MONTE_CARLO) < 0.80


def test_deterministic_solves_at_least_monte_carlo():
    """Deterministic solved spend >= MC solved spend at the same confidence.

    The deterministic single path hides the dispersion the MC trials expose, so it
    sustains at least as much as MC requires at any fixed confidence — the
    sequence/volatility-risk gap. (Compared at 80%, a well-defined band; not at
    MC-100%, where thin tails may push the 100% spend below the GI floor / ceiling
    guard.)
    """
    det = solve_max_spend(default_scenario(), 1.0, MODE_DETERMINISTIC).solved_spend
    mc_80 = solve_max_spend(default_scenario(), 0.80, MODE_MONTE_CARLO).solved_spend
    assert det >= mc_80


def test_higher_confidence_solves_lower_or_equal_spend_mc():
    """A stricter MC confidence never solves MORE spend (monotone in the dial).

    The 10k trials make the dial land cleanly, so the sequence is non-increasing
    across 80/90/95.
    """
    spends = [
        solve_max_spend(default_scenario(), t, MODE_MONTE_CARLO).solved_spend
        for t in (0.80, 0.90, 0.95)
    ]
    for earlier, later in zip(spends, spends[1:]):
        assert later <= earlier


def test_higher_equity_vol_lowers_success_fraction():
    """Dial behaviour: higher equity vol → lower success fraction at a fixed spend.

    At a fixed (stressed) spend, cranking equity vol widens the fan and pushes more
    trials below zero, so the success fraction drops. Compared at the same fixed
    spend on the SAME seed so only the vol differs.
    """
    base = replace(_adversarial_mc_scenario(), annual_spending=100_000.0)
    lo_vol = replace(base, allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07, 0.10),))
    hi_vol = replace(base, allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07, 0.30),))
    lo_survives, _ = _monte_carlo_evaluator(lo_vol)
    hi_survives, _ = _monte_carlo_evaluator(hi_vol)
    assert hi_survives(base.annual_spending) < lo_survives(base.annual_spending)


def test_higher_correlation_lowers_success_fraction():
    """Dial behaviour: higher equity-bond correlation → lower success at fixed spend.

    Raising correlation removes diversification (the blended-return variance rises),
    so more trials deplete at a fixed spend. A 60/40 stressed scenario where both
    classes carry vol, compared low vs high correlation on the same seed.
    """
    base = replace(
        default_scenario(),
        annual_spending=80_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", 0.60, 0.07, 0.174),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.40, 0.02, 0.088),
        ),
    )
    lo_corr = replace(base, correlation=-0.50)
    hi_corr = replace(base, correlation=0.90)
    lo_survives, _ = _monte_carlo_evaluator(lo_corr)
    hi_survives, _ = _monte_carlo_evaluator(hi_corr)
    assert hi_survives(base.annual_spending) < lo_survives(base.annual_spending)


def test_mc_solver_raises_on_no_mapped_class():
    """An MC solve with no mapped allocation class raises ValueError (API → 422)."""
    scenario = replace(
        default_scenario(),
        allocation=(AssetClass("crypto", "Crypto", 1.0, 0.0),),
    )
    with pytest.raises(ValueError):
        solve_max_spend(scenario, 0.80, MODE_MONTE_CARLO)


# ============================================================================
# Tax-drag design td1 (td-b slice): solver rides drag in deterministic +
# historical (DECISION 8), the Monte Carlo fence, and the adversarial
# monotonicity property test at the two named kinks.
# ============================================================================


def test_solve_max_spend_dragged_deterministic_lands_at_or_below_no_split():
    """With a split supplied, the deterministic solve rides the DRAGGED engine
    — the solved number is at or below the no-split (pre-tax) solved number on
    the SAME scenario, since the portfolio now also funds a real tax bill."""
    no_split = solve_max_spend(default_scenario(), 1.0, MODE_DETERMINISTIC)
    split = solve_max_spend(
        replace(default_scenario(), **_SPLIT_KWARGS), 1.0, MODE_DETERMINISTIC
    )
    assert split.solved_spend <= no_split.solved_spend
    assert split.solved_spend > 0


def test_solve_max_spend_dragged_historical_lands_at_or_below_no_split():
    """Same property, Historical mode: the dragged solve never exceeds the
    no-split solve at the same confidence target."""
    no_split = solve_max_spend(default_scenario(), 0.90, MODE_HISTORICAL)
    split = solve_max_spend(
        replace(default_scenario(), **_SPLIT_KWARGS), 0.90, MODE_HISTORICAL
    )
    assert split.solved_spend <= no_split.solved_spend
    assert split.solved_spend > 0
    assert split.cohort_count == no_split.cohort_count  # same cohort set


def test_evaluate_dragged_deterministic_matches_full_engine():
    """``evaluate()`` for a split deterministic scenario agrees with the FULL
    dragged engine's own depletion bit (``simulate_tax_location_deterministic``
    — never a second, possibly-drifting derivation of "did it deplete")."""
    for spend in (95_000.0, 160_000.0, 240_000.0):
        scenario = replace(
            default_scenario(), **_SPLIT_KWARGS, annual_spending=spend
        )
        expected = (
            1.0
            if simulate_tax_location_deterministic(scenario).dragged_depleted_age
            is None
            else 0.0
        )
        assert evaluate(scenario, MODE_DETERMINISTIC) == expected


def test_lean_dragged_historical_matches_full_engine_depletion_count():
    """The dragged lean evaluator's implied depletion count matches
    ``simulate_historical``'s ``depletion_count`` for the SAME split scenario
    — pins that ``_historical_evaluator_dragged`` (the solver's inner loop)
    didn't drift from the (td-b re-pointed, now DRAGGED) display fan. Mirrors
    ``test_lean_path_matches_full_engine_depletion_count`` for the no-split
    path."""
    scenario = replace(
        default_scenario(),
        annual_spending=280_000.0,
        **_SPLIT_KWARGS,
    )
    full = simulate_historical(scenario)
    survives, cohort_count = _historical_evaluator_dragged(scenario)
    fraction = survives(scenario.annual_spending)

    assert cohort_count == full.cohort_count
    implied_depletions = round((1.0 - fraction) * cohort_count)
    assert implied_depletions == full.depletion_count


def test_self_consistency_dragged_deterministic_boundary_holds():
    """achieved >= threshold at the reported number for the DRAGGED
    deterministic solve, and a +$2k probe drops below it (mirrors the
    no-split self-consistency gate, DECISION 8's after-tax-honest number)."""
    scenario = replace(default_scenario(), **_SPLIT_KWARGS)
    result = solve_max_spend(scenario, 1.0, MODE_DETERMINISTIC)
    at_displayed = replace(scenario, annual_spending=result.solved_spend)
    assert evaluate(at_displayed, MODE_DETERMINISTIC) == pytest.approx(
        result.achieved_fraction
    )
    assert result.achieved_fraction >= 1.0
    if not result.hit_ceiling:
        above = replace(scenario, annual_spending=result.solved_spend + 2_000.0)
        assert evaluate(above, MODE_DETERMINISTIC) < 1.0


def test_solve_max_spend_monte_carlo_with_split_raises_tax_drag_fence():
    """A split + Monte Carlo raises ``TaxDragMonteCarloNotSolvable`` BEFORE any
    bisection (DECISION 8's MC fence — the same idiom as
    ``BucketsNotSolvable``/``SpendingStrategyNotSolvable``)."""
    scenario = replace(default_scenario(), **_SPLIT_KWARGS)
    with pytest.raises(TaxDragMonteCarloNotSolvable, match="Monte Carlo"):
        solve_max_spend(scenario, 0.80, MODE_MONTE_CARLO)
    # It's also a plain ValueError (back-compat with existing except clauses).
    with pytest.raises(ValueError):
        solve_max_spend(scenario, 0.80, MODE_MONTE_CARLO)


def test_evaluate_monte_carlo_with_split_raises_tax_drag_fence():
    """``evaluate()``'s other public entry point fences the same way, with the
    EXACT pinned message (so the API's 422 text is stable)."""
    scenario = replace(default_scenario(), **_SPLIT_KWARGS)
    with pytest.raises(TaxDragMonteCarloNotSolvable) as exc_info:
        evaluate(scenario, MODE_MONTE_CARLO)
    assert str(exc_info.value) == TAX_DRAG_MC_FENCE_MESSAGE


def test_no_split_solver_behavior_byte_stable_after_td_b():
    """Sanity: a plain no-split solve is completely untouched by the td-b
    changes — same solved number as the pre-existing behavior this module's
    other tests already pin (the dragged branch is unreachable without a
    split, mirroring the engine's own no-split byte-stability discipline)."""
    result = solve_max_spend(default_scenario(), 0.80, MODE_HISTORICAL)
    assert not result.hit_ceiling
    assert 100_000 < result.solved_spend < 400_000


# ---------- Property test: adversarial monotonicity at the two named kinks ----------
#
# DECISION 8's monotonicity re-derivation argues survival stays weakly
# non-increasing in spend even under drag (the lever kink only re-labels which
# draw fills the bracket; the RMD-channel tax savings on an early-removed
# dollar are bounded below the direct spend effect). This is NOT a generic
# sweep — it probes the two named kinks. A violation here is a ship-blocker by
# design (no tolerance-widening).


def test_property_survival_monotonic_at_lever_bracket_fill_kink():
    """Adversarial probe at the LEVER kink: with the bracket-fill lever active,
    sweep spend densely across the range that crosses the point where
    spending-driven tax-deferred draws alone fill the bracket target (the
    lever's own draw shrinks toward zero exactly there — the re-labeling kink
    the monotonicity argument calls out). Survival must never rise as spend
    rises."""
    scenario = replace(
        default_scenario(),
        taxable_balance=300_000.0,
        tax_deferred_balance=1_500_000.0,
        roth_balance=200_000.0,
        initial_portfolio=2_000_000.0,
        bracket_fill_target=100_800.0,  # the 12% bracket top preset
    )
    survives, _ = _historical_evaluator_dragged(scenario)
    spends = list(range(60_000, 300_001, 10_000))
    fractions = [survives(s) for s in spends]
    for earlier, later in zip(fractions, fractions[1:]):
        assert later <= earlier + 1e-12  # non-increasing (allow fp noise)


@pytest.mark.parametrize("prior_td", [500_000.0, 1_500_000.0, 3_000_000.0, 6_000_000.0])
def test_property_survival_monotonic_at_rmd_onset_kink(prior_td):
    """Adversarial probe at the RMD-ONSET kink: vary the prior tax-deferred
    balance (which sets the RMD magnitude at the 74->75 transition the
    horizon crosses) and sweep spend densely, asserting survival never rises
    as spend rises for ANY of the varied prior-tax-deferred levels."""
    scenario = replace(
        default_scenario(),
        taxable_balance=300_000.0,
        tax_deferred_balance=prior_td,
        roth_balance=200_000.0,
        initial_portfolio=300_000.0 + prior_td + 200_000.0,
    )
    survives, _ = _historical_evaluator_dragged(scenario)
    spends = list(range(80_000, 400_001, 20_000))
    fractions = [survives(s) for s in spends]
    for earlier, later in zip(fractions, fractions[1:]):
        assert later <= earlier + 1e-12  # non-increasing (allow fp noise)


def test_property_survival_monotonic_deterministic_dragged_dense_sweep():
    """Deterministic-mode companion: a dense spend sweep on the split fixture,
    checked via the lean depletion-only evaluator directly (the same one the
    deterministic dragged solve bisects against)."""
    scenario = replace(default_scenario(), **_SPLIT_KWARGS, bracket_fill_target=211_400.0)
    returns = deterministic_blended_returns(scenario)
    spends = list(range(60_000, 400_001, 10_000))
    fractions = []
    for spend in spends:
        probe = replace(scenario, annual_spending=spend)
        strategy = make_strategy(probe)
        depleted_age = simulate_dragged_depletion_only(probe, returns, strategy)
        fractions.append(1.0 if depleted_age is None else 0.0)
    for earlier, later in zip(fractions, fractions[1:]):
        assert later <= earlier + 1e-12
