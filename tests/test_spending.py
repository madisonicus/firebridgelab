"""Tests for the pluggable SpendingStrategy + the Guyton-Klinger guardrail.

Variable-spending design slice 1. Anchors:
  1. Strategy factory dispatch (constant/lifecycle → PureAgeStrategy wrapper;
     guardrails → GuardrailStrategy).
  2. The PureAgeStrategy wrapper is byte-identical to ``spending_for_age`` (the
     canary depends on this — proven here at the unit level, and at the engine
     level in test_simulate / test_historical / test_monte_carlo).
  3. Hand-computed micro-cohort: a cut fires once and a raise fires once; the
     exact spend path through nudge→taper→clamp is asserted by hand.
  4. Degenerate checks: ADJUST→0 ⇒ spend never moves; initial_rate==0 ⇒ guardrail
     disabled; ADJUST→1.0 ⇒ first trip jumps the base by the full adjustment.
  5. Direction checks: monotonically rising returns ⇒ only raises; falling ⇒ only
     cuts.
  6. The rails: floor wins, ceiling is activity-tapered, floor is flat.

The hand cases run through ``historical._simulate_cohort`` (the SAME shared
per-year loop Historical/MC/solver use), so they pin the guardrail as the engines
actually execute it — not a re-implementation in the test.
"""

import numpy as np
import pytest

from dataclasses import replace

from firebridgelab import default_scenario, simulate
from firebridgelab.sim_core import _simulate_cohort
from firebridgelab.scenario import (
    AssetClass,
    Scenario,
    SPENDING_MODE_CONSTANT,
    SPENDING_MODE_FR_GUARDRAIL,
    SPENDING_MODE_FR_SMOOTH,
    SPENDING_MODE_GUARDRAILS,
    SPENDING_MODE_LIFECYCLE,
    activity_multiplier,
    spending_for_age,
)
from firebridgelab.spending import (
    _FR_GR_LOWER,
    _FR_GR_STEP,
    _FR_GR_UPPER,
    _FR_SMOOTH_ALPHA,
    FundedRatioGuardrailStrategy,
    FundedRatioSmoothStrategy,
    FundedRatioStrategyBase,
    GuardrailStrategy,
    PureAgeStrategy,
    make_strategy,
)


def _guardrail_scenario(**overrides) -> Scenario:
    """A small, GI-free guardrail scenario for hand-computed micro-cohorts.

    4-year horizon (ages 50..56, all go-go so the activity taper is ×1.0), $1M
    start, $50k base spend, no GI → initial_rate = 50000 / 1,000,000 = 0.05, so
    the default ±20% bands are [0.04, 0.06]. The rails are widened off
    (floor 0 / ceiling 1e12) by default so the NUDGE arithmetic is tested clean;
    the clamp tests set them explicitly.
    """
    base = dict(
        start_age=50,
        end_age=53,
        initial_portfolio=1_000_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.0),),
        annual_spending=50_000.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=70,
        spending_mode=SPENDING_MODE_GUARDRAILS,
        spending_floor=0.0,
        spending_ceiling=1e12,
    )
    base.update(overrides)
    return Scenario(**base)


# --------------------------------------------------------------------------- #
# Factory dispatch + the byte-stable pure-age wrapper                         #
# --------------------------------------------------------------------------- #


def test_make_strategy_dispatch():
    """The factory maps spending_mode to the right strategy type."""
    constant = replace(default_scenario(), spending_mode=SPENDING_MODE_CONSTANT)
    lifecycle = replace(default_scenario(), spending_mode=SPENDING_MODE_LIFECYCLE)
    guardrails = replace(default_scenario(), spending_mode=SPENDING_MODE_GUARDRAILS)
    assert isinstance(make_strategy(constant), PureAgeStrategy)
    assert isinstance(make_strategy(lifecycle), PureAgeStrategy)
    assert isinstance(make_strategy(guardrails), GuardrailStrategy)


def test_pure_age_strategy_matches_spending_for_age():
    """PureAgeStrategy.spend_for_year == spending_for_age at every age (byte-stable).

    The wrapper ignores begin_year_portfolio + gi and carries no state, so a run
    under it is byte-for-byte identical to the pre-strategy engines for constant
    and lifecycle (the canary's foundation).
    """
    for mode in (SPENDING_MODE_CONSTANT, SPENDING_MODE_LIFECYCLE):
        scenario = replace(default_scenario(), spending_mode=mode)
        strat = make_strategy(scenario)
        for age in range(scenario.start_age, scenario.end_age + 1):
            # Pass nonsense for begin_year_portfolio/gi — must be ignored.
            assert strat.spend_for_year(scenario, age, 1.234, 5.678) == \
                spending_for_age(scenario, age)


def test_activity_multiplier_matches_lifecycle_curve():
    """activity_multiplier reproduces the lifecycle phase multipliers exactly."""
    scenario = replace(default_scenario(), spending_mode=SPENDING_MODE_LIFECYCLE)
    assert activity_multiplier(scenario, 50) == 1.0
    assert activity_multiplier(scenario, 74) == 1.0
    assert activity_multiplier(scenario, 75) == pytest.approx(0.85)
    assert activity_multiplier(scenario, 84) == pytest.approx(0.85)
    assert activity_multiplier(scenario, 85) == pytest.approx(0.75)
    assert activity_multiplier(scenario, 95) == pytest.approx(0.75)


# --------------------------------------------------------------------------- #
# Hand-computed micro-cohort: one cut + one raise, exact spend path           #
# --------------------------------------------------------------------------- #


def test_hand_case_cut_then_raise():
    """A 4-year cohort firing a hold, a cut, a raise, then a hold — by hand.

    Scenario: $1M, $50k base, no GI, ages 50-53 (go-go ×1.0), wide rails.
    initial_rate = 0.05, bands [0.04, 0.06], ADJUST 0.10. Return sequence
    r = [-0.30, +1.00, 0.00, 0.00]. Midpoint withdrawal:
    end = begin*(1+r) - spend*(1 + r/2).

      yr1 (50): begin 1,000,000; rate 50000/1e6 = 0.05 ∈ band → HOLD, spend 50000;
                end = 1e6*0.70 - 50000*0.85 = 700000 - 42500 = 657500.
      yr2 (51): begin 657500; rate 50000/657500 = 0.07605 > 0.06 → CUT to 45000;
                end = 657500*2.0 - 45000*1.5 = 1,315,000 - 67500 = 1,247,500.
      yr3 (52): begin 1,247,500; rate 45000/1,247,500 = 0.03607 < 0.04 → RAISE to
                49500; end = 1,247,500*1.0 - 49500*1.0 = 1,198,000.
      yr4 (53): begin 1,198,000; rate 49500/1,198,000 = 0.04132 ∈ band → HOLD,
                spend 49500; end = 1,198,000 - 49500 = 1,148,500.
    """
    scenario = _guardrail_scenario()
    returns = np.array([-0.30, 1.00, 0.00, 0.00])
    ends, depleted_age, spends = _simulate_cohort(scenario, returns)

    assert spends == pytest.approx([50_000.0, 45_000.0, 49_500.0, 49_500.0])
    assert ends == pytest.approx([657_500.0, 1_247_500.0, 1_198_000.0, 1_148_500.0])
    assert depleted_age is None


# --------------------------------------------------------------------------- #
# Degenerate checks                                                           #
# --------------------------------------------------------------------------- #


def test_adjust_zero_spend_never_moves():
    """ADJUST → 0 ⇒ the nudge is ×1.0 ⇒ spend is exactly tapered/clamped constant.

    The clean "constant" proof (design: lead with this). With guard_adjust=0, no
    band trip can change the base, so every year spends the base $50k regardless
    of the wild return swings.
    """
    scenario = _guardrail_scenario(guard_adjust=0.0)
    returns = np.array([-0.50, 1.50, -0.30, 0.80])  # wild swings, both bands tripped
    _ends, _dep, spends = _simulate_cohort(scenario, returns)
    assert spends == pytest.approx([50_000.0] * 4)


def test_initial_rate_zero_disables_guardrail():
    """initial_rate == 0 (start spend ≤ start GI) ⇒ guardrail disabled.

    With $50k base and $60k GI active from age 50, the portfolio-funded rate at
    start is max(0, 50000-60000)/1e6 = 0 → the run behaves as tapered/clamped
    constant (no nudging), so spend stays at the base every year despite swings.
    """
    scenario = _guardrail_scenario(
        person_a_pension_amount=60_000.0, person_a_pension_start_age=50,
    )
    strat = make_strategy(scenario)
    assert isinstance(strat, GuardrailStrategy)
    assert strat.initial_rate == 0.0
    assert strat.enabled is False

    returns = np.array([-0.50, 1.50, -0.30, 0.80])
    _ends, _dep, spends = _simulate_cohort(scenario, returns)
    # No nudging → spend is the flat base (GI doesn't reduce the spend, it reduces
    # the portfolio-funded draw; the dollar spend stays at the base).
    assert spends == pytest.approx([50_000.0] * 4)


def test_adjust_one_first_trip_jumps_base():
    """ADJUST → 1.0 ⇒ the first cut multiplies the base by (1-1.0)=0 (a full jump).

    With guard_adjust=1.0, a cut sends base_spend to 0 (then the floor clamp would
    catch it in a real scenario; here the floor is 0 so it lands at 0). This pins
    the "crank the dial to jump-to-rail" behavior: ADJUST is a plain %-multiply, so
    1.0 is the extreme.
    """
    scenario = _guardrail_scenario(guard_adjust=1.0)
    # yr1 holds (rate .05); force a cut in yr2 with a big drop.
    returns = np.array([-0.50, 0.0, 0.0, 0.0])
    _ends, _dep, spends = _simulate_cohort(scenario, returns)
    # yr1 hold 50000; yr2 begin = 1e6*0.5 - 50000*0.75 = 462500; rate
    # 50000/462500 = 0.108 > 0.06 → cut: base *= (1-1.0) = 0 → spend 0 thereafter.
    assert spends[0] == pytest.approx(50_000.0)
    assert spends[1] == pytest.approx(0.0)
    assert spends[2] == pytest.approx(0.0)
    assert spends[3] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Direction checks                                                            #
# --------------------------------------------------------------------------- #


def test_rising_returns_only_raises():
    """Monotonically strong-positive returns ⇒ the base only ever rises (or holds).

    A strongly rising portfolio drives the funded rate down, tripping prosperity
    raises; no cut can fire. The base spend is non-decreasing across the horizon.
    """
    scenario = _guardrail_scenario(end_age=59)  # 10 years, ages 50-59 (still go-go)
    returns = np.full(10, 0.40)  # +40%/yr, portfolio balloons
    _ends, _dep, spends = _simulate_cohort(scenario, returns)
    # Spend is non-decreasing (only holds + raises). Pin the direction, not the
    # exact path (the exact path is the hand case's job).
    for prev, cur in zip(spends, spends[1:]):
        assert cur >= prev - 1e-9
    assert spends[-1] > spends[0]  # at least one raise actually fired


def test_falling_returns_only_cuts():
    """Monotonically negative returns ⇒ the base only ever falls (or holds).

    A shrinking portfolio drives the funded rate up, tripping capital-preservation
    cuts; no raise can fire. The base spend is non-increasing across the horizon.
    """
    scenario = _guardrail_scenario(end_age=59)
    returns = np.full(10, -0.20)  # -20%/yr, portfolio shrinks
    _ends, _dep, spends = _simulate_cohort(scenario, returns)
    for prev, cur in zip(spends, spends[1:]):
        assert cur <= prev + 1e-9
    assert spends[-1] < spends[0]  # at least one cut actually fired


# --------------------------------------------------------------------------- #
# The rails — floor flat, ceiling activity-tapered, floor wins                #
# --------------------------------------------------------------------------- #


def test_floor_clamps_spend():
    """The hard floor clamps WITHDRAWN spend — UNTIL the portfolio can't fund it.

    $50k base with a $40k floor: a cut would take the base below $40k over enough
    falling years, but the clamp holds the recorded spend at the floor while the
    portfolio can still fund it. Once a relentless decline depletes the portfolio
    (here at age 58, no GI), the plan genuinely cannot fund the floor — the
    RECORDED spend is the realized-spend correctness fix: it drops to what the
    dying portfolio can fund (≈$13.1k in the depletion year) and then to $0 once
    the portfolio is gone, instead of falsely reporting a $40k floor it can't pay.
    (Pre-fix this asserted ``min(spends) >= 40k``; that was the overstatement bug.)
    """
    scenario = _guardrail_scenario(end_age=67, spending_floor=40_000.0)
    returns = np.full(18, -0.20)  # relentless decline → many cuts, then depletion
    ends, dep, spends = _simulate_cohort(scenario, returns)
    assert dep == 58  # the portfolio depletes (age 50 + step 8)
    # Pre-depletion the floor holds: every funded year is at or above $40k.
    pre_depletion = [s for e, s in zip(ends, spends) if e > 0.0]
    assert min(pre_depletion) >= 40_000.0 - 1e-6
    # Depletion year: the realized spend is what the dying portfolio could fund —
    # below the floor (the plan can't pay $40k once the money is gone).
    assert spends[8] == pytest.approx(13_116.9849, abs=1e-3)
    # Fully depleted, no GI → the realized spend is $0 (not a phantom $40k floor).
    assert all(s == 0.0 for s in spends[9:])


def test_ceiling_is_activity_tapered():
    """The ceiling is shaped by the activity taper; the floor is flat.

    A guardrail run held at the ceiling: the GuardrailStrategy.spend_for_year
    clamps to ceiling × activity_multiplier(age). At a slow-go age (75) the
    effective ceiling is 150000 × 0.85 = 127500; at go-go (60) it's the full
    150000. The floor is NOT tapered — it stays flat at the hard floor.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        spending_floor=60_000.0,
        spending_ceiling=150_000.0,
    )
    strat = make_strategy(scenario)
    # Drive the base far above the ceiling so the clamp binds, with a huge begin
    # portfolio so no cut fires (rate ~0). The clamp is the only thing acting.
    strat.base_spend = 1_000_000.0
    huge = 1e12
    spend_go_go = strat.spend_for_year(scenario, 60, huge, 0.0)
    spend_slow_go = strat.spend_for_year(scenario, 75, huge, 0.0)
    spend_no_go = strat.spend_for_year(scenario, 85, huge, 0.0)
    assert spend_go_go == pytest.approx(150_000.0)          # full ceiling
    assert spend_slow_go == pytest.approx(150_000.0 * 0.85)  # tapered
    assert spend_no_go == pytest.approx(150_000.0 * 0.75)    # tapered


def test_floor_wins_when_tapered_ceiling_falls_below_floor():
    """Floor wins if a tapered ceiling ever falls to/below the floor (step 4).

    With a ceiling of $70k and a no-go multiplier so deep that 70000 × m < the
    $60k floor, the clamp returns the floor (not the lower tapered ceiling).
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        spending_floor=60_000.0,
        spending_ceiling=70_000.0,
        no_go_multiplier=0.5,  # 70000 * 0.5 = 35000 < 60000 floor
    )
    strat = make_strategy(scenario)
    strat.base_spend = 1_000_000.0
    spend = strat.spend_for_year(scenario, 90, 1e12, 0.0)  # no-go age
    assert spend == pytest.approx(60_000.0)  # floor wins over the 35k tapered ceiling


# --------------------------------------------------------------------------- #
# Begin-of-year denominator + deterministic guardrail run                     #
# --------------------------------------------------------------------------- #


def test_begin_year_zero_skips_nudge():
    """begin_year_portfolio <= 0 skips the nudge (the floor clamp governs).

    Directly exercise the strategy with a zero begin balance: no nudge fires, and
    the spend is the floor-clamped tapered base.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        spending_floor=60_000.0,
        spending_ceiling=150_000.0,
        annual_spending=110_000.0,
    )
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend = strat.spend_for_year(scenario, 70, 0.0, 0.0)
    # No nudge → base unchanged; spend is the clamped tapered base (110k in band).
    assert strat.base_spend == before
    assert spend == pytest.approx(110_000.0)


def test_deterministic_guardrail_runs_and_is_near_degenerate():
    """A deterministic guardrail run executes (offered for consistency).

    A single smooth return path rarely trips a band, so the deterministic guardrail
    is near-degenerate (design: offered but labeled). This just pins that the
    deterministic engine accepts + runs guardrail mode without error and produces a
    full trajectory.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=110_000.0,
    )
    traj = simulate(scenario)
    assert len(traj.points) == scenario.end_age - scenario.start_age + 1
    # Every withdrawn spend respects the rails (default operational floor $85k).
    for p in traj.points:
        assert p.spending >= scenario.spending_floor - 1e-6 or p.end_portfolio == 0.0


def test_guardrail_spend_clamps_to_floor_until_depletion_across_engines():
    """Guardrail spending clamps to ``spending_floor`` — until a cohort depletes.

    The floor is the OPERATIONAL clamp ("we don't dip below this") WHILE the
    portfolio can fund it; a per-year withdrawn spend never goes below the floor
    unless that year is depleted (end_portfolio == 0). Realized-spend correctness
    fix: a depleted cohort genuinely CANNOT fund the floor, so its recorded spend
    drops to what's affordable (≈ GI, or $0 in the no-GI bridge) — which can pull a
    fan percentile below the floor once enough cohorts deplete. That is the
    descriptive depletion outcome the fix surfaces, NOT a clamp violation. (Pre-fix
    this test asserted ``min(spend_p10) >= floor`` unconditionally; that masked the
    overstatement bug by recording the unfundable floor through depletions.)
    """
    from firebridgelab.historical import simulate_historical
    from firebridgelab.monte_carlo import simulate_monte_carlo

    for floor in (85_000.0, 110_000.0):
        scenario = replace(
            default_scenario(),
            spending_mode=SPENDING_MODE_GUARDRAILS,
            annual_spending=145_000.0,
            spending_floor=floor,
            spending_ceiling=190_000.0,
        )

        # Deterministic (smooth path, no depletion here): every per-year withdrawn
        # spend >= floor (the clamp holds; the realized cap is a no-op).
        traj = simulate(scenario)
        for p in traj.points:
            assert p.spending >= floor - 1e-6 or p.end_portfolio == 0.0

    # At the $85k floor few cohorts deplete (none historically, <2% in MC), so the
    # fan's p10 never reaches a depleting cohort and the clamp holds the whole p10
    # at the floor.
    sc_85 = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=145_000.0,
        spending_floor=85_000.0,
        spending_ceiling=190_000.0,
    )
    assert min(simulate_historical(sc_85).spend_p10) >= 85_000.0 - 1e-6
    assert min(simulate_monte_carlo(sc_85).spend_p10) >= 85_000.0 - 1e-6

    # At the higher $110k floor (above every default GI level) enough cohorts
    # deplete that the realized spend (correctly below the unfundable floor in
    # those cohorts) pulls the p10 BELOW the floor — the realized-spend
    # correctness fix made visible. Pinned deliberately.
    sc_110 = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=145_000.0,
        spending_floor=110_000.0,
        spending_ceiling=190_000.0,
    )
    # With GI credited mid-year like spending, a depleted cohort funds exactly its
    # GI, so each p10 floor-breach lands on a default GI level: the Historical p10
    # on the $66,000 GI of ages 67-69, the MC p10 on the $98,240 GI from 70.
    assert min(simulate_historical(sc_110).spend_p10) == pytest.approx(66_000.0, abs=0.5)
    assert min(simulate_monte_carlo(sc_110).spend_p10) == pytest.approx(98_240.0, abs=0.5)


# --------------------------------------------------------------------------- #
# Published-figure cross-check (offline TODO for the supervisor)              #
# --------------------------------------------------------------------------- #


def test_guyton_klinger_cross_check_TODO():
    """TODO (supervisor): validate against a published Guyton-Klinger figure.

    The design's validation plan calls for reproducing a published GK result
    (matched inputs, GI stripped) within tolerance — the reason GK was chosen over
    a bespoke rule. Doing this faithfully needs the original GK paper's assumptions
    (nominal COLA rule, terminal-suspension rule, their specific guard widths) which
    our v1 deliberately does NOT implement (real-dollar engine, two core nudges
    only — see design "v1 = the two core nudges only"). A clean offline closed-form
    GK figure to assert against is therefore not available without re-introducing
    the deferred rules, and cFIREsim's guardrail mode is an online reference.

    LEFT FOR THE SUPERVISOR (correctness guarantor): run the matched-input
    comparison against cFIREsim's guardrail mode / the GK literature as part of the
    live numerical-validation gate. The hand cases + direction + degenerate checks
    above pin the mechanics exactly; this is the external-reference tie-out.
    """
    pytest.skip("supervisor validates against cFIREsim / GK literature (online)")


# --------------------------------------------------------------------------- #
# FundedRatioStrategyBase — the carried-state spine (FR feature slice 1)       #
# --------------------------------------------------------------------------- #


class _EchoFRStrategy(FundedRatioStrategyBase):
    """Trivial FR subclass whose ``_decide`` echoes the funded ratio as the spend
    and tags the state with the FR, so the spine's carried state can be asserted
    in isolation (no tier ladder, no clamps). It also records the per-call FR +
    smoothed value it observed for the test to inspect.
    """

    def __init__(self, scenario, npv_target_real):
        super().__init__(scenario, npv_target_real)
        self.seen_fr: list[float] = []
        self.seen_smoothed: list[float] = []
        self.seen_crash: list[float] = []
        self.seen_prev_portfolio: list[float | None] = []

    def _decide(self, scenario, age, fr, begin_year_portfolio, gi):
        # Capture what the spine handed us THIS call (prev_* is still last year's).
        self.seen_fr.append(fr)
        self.seen_smoothed.append(self.smoothed)
        self.seen_crash.append(self.crash_drop)
        self.seen_prev_portfolio.append(self.prev_portfolio)
        # Echo: spend the FR (as a dollar number), tag the state with the age.
        return fr, f"echo@{age}"


def _fr_scenario(**overrides) -> Scenario:
    """A small GI-free scenario for FR-spine tests (ages 50..57, $1M, $100k base).

    GI is zero so the NPV gaps are a clean target-perpetuity; the spine's carried
    state (smoothing, prev-portfolio, crash fraction, FR lookups) is what's under
    test here, not the spend policy.
    """
    base = dict(
        start_age=50,
        end_age=54,
        initial_portfolio=1_000_000.0,
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


def test_fr_spine_fences_solver_and_collects():
    """The spine declares itself path-dependent (solver fenced) and collecting."""
    scenario = _fr_scenario()
    strat = _EchoFRStrategy(scenario, npv_target_real=scenario.annual_spending)
    assert strat.is_solvable is False
    assert strat.reason_not_solvable  # non-empty user-facing reason
    assert strat.collects_spends is True
    # NPV table is built once over the full horizon.
    assert set(strat.npv_gaps_by_age) == set(range(50, 55))


def test_fr_spine_first_call_smoothing_and_no_crash():
    """First call: smoothed == current portfolio, prev_portfolio None, crash 0."""
    scenario = _fr_scenario()
    strat = _EchoFRStrategy(scenario, npv_target_real=scenario.annual_spending)
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1_000_000.0, gi=0.0)
    # On the first _decide call the spine had updated smoothing (= current value)
    # but prev_portfolio was still None and crash 0 (no prior year).
    assert strat.seen_smoothed[0] == pytest.approx(1_000_000.0)
    assert strat.seen_prev_portfolio[0] is None
    assert strat.seen_crash[0] == pytest.approx(0.0)
    # After the call, carried state advanced for next year.
    assert strat.prev_portfolio == pytest.approx(1_000_000.0)
    assert strat.prev_state == "echo@50"


def test_fr_spine_smoothing_buffer_is_trailing_three():
    """Smoothed is the mean of the last ≤3 begin-of-year portfolios."""
    scenario = _fr_scenario()
    strat = _EchoFRStrategy(scenario, npv_target_real=scenario.annual_spending)
    seq = [1_000_000.0, 1_300_000.0, 700_000.0, 1_500_000.0]
    ages = [50, 51, 52, 53]
    for age, p in zip(ages, seq):
        strat.spend_for_year(scenario, age=age, begin_year_portfolio=p, gi=0.0)
    # Year 0: mean([1.0M]) ; year 1: mean([1.0M, 1.3M]) ; year 2: mean of all
    # three; year 3: mean of the trailing three (drops the first).
    assert strat.seen_smoothed[0] == pytest.approx(1_000_000.0)
    assert strat.seen_smoothed[1] == pytest.approx((1_000_000.0 + 1_300_000.0) / 2)
    assert strat.seen_smoothed[2] == pytest.approx(
        (1_000_000.0 + 1_300_000.0 + 700_000.0) / 3
    )
    assert strat.seen_smoothed[3] == pytest.approx(
        (1_300_000.0 + 700_000.0 + 1_500_000.0) / 3
    )


def test_fr_spine_crash_fraction_off_prior_portfolio():
    """Crash YoY drop fraction = (prev - cur)/prev, off the PRIOR begin balance."""
    scenario = _fr_scenario()
    strat = _EchoFRStrategy(scenario, npv_target_real=scenario.annual_spending)
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1_000_000.0, gi=0.0)
    strat.spend_for_year(scenario, age=51, begin_year_portfolio=700_000.0, gi=0.0)
    # Second call saw a 30% drop off the prior $1.0M.
    assert strat.seen_crash[1] == pytest.approx(0.30)
    # A rebound year yields a negative "drop" (i.e. a gain), not clamped.
    strat.spend_for_year(scenario, age=52, begin_year_portfolio=1_400_000.0, gi=0.0)
    assert strat.seen_crash[2] == pytest.approx((700_000.0 - 1_400_000.0) / 700_000.0)


def test_fr_spine_funded_ratio_uses_npv_table_and_smoothed():
    """FR(age) = smoothed / npv[age], with the safe sentinel on npv<=0."""
    scenario = _fr_scenario()
    target = scenario.annual_spending
    strat = _EchoFRStrategy(scenario, npv_target_real=target)
    # Drive one year so smoothing is set; FR observed must equal smoothed/npv.
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1_000_000.0, gi=0.0)
    npv50 = strat.npv_gaps_by_age[50]
    assert npv50 > 0
    assert strat.seen_fr[0] == pytest.approx(1_000_000.0 / npv50)
    # Sentinel path: a zero NPV (GI fully covers the target) → sentinel FR.
    covered = _fr_scenario(
        person_a_ss_amount=500_000.0, person_a_ss_start_age=50,
    )
    strat2 = _EchoFRStrategy(covered, npv_target_real=target)
    assert strat2.npv_gaps_by_age[50] == pytest.approx(0.0)
    strat2.spend_for_year(covered, age=50, begin_year_portfolio=1_000_000.0, gi=500_000.0)
    assert strat2.seen_fr[0] == pytest.approx(FundedRatioStrategyBase._FR_SENTINEL)


def test_fr_spine_base_decide_is_abstract():
    """The base ``_decide`` must be overridden — calling it raises."""
    scenario = _fr_scenario()
    base = FundedRatioStrategyBase(scenario, npv_target_real=scenario.annual_spending)
    with pytest.raises(NotImplementedError):
        base.spend_for_year(scenario, age=50, begin_year_portfolio=1_000_000.0, gi=0.0)


# --------------------------------------------------------------------------- #
# FundedRatioSmoothStrategy — partial-adjustment FR spending (FR slice 2)      #
# --------------------------------------------------------------------------- #
#
# fr-smooth mirrors the guardrail nudge→taper→clamp order, swapping the discrete
# band trip for a damped partial-adjustment on the funded ratio. These pin the
# recurrence (FR=1 holds / FR>1 raises / FR<1 cuts, damped by alpha), the rails
# (floor flat, ceiling activity-tapered, floor wins), the degenerate skip, the
# single-taper composition, and a cross-mode smoke run.


def _fr_smooth_scenario(**overrides) -> Scenario:
    """A GI-free fr-smooth scenario: ages 50..57, $1M, $100k baseline, all go-go
    (activity ×1.0) unless an age outside go-go is probed. Floor/ceiling default
    wide-open so the recurrence is tested clean; clamp tests set them explicitly.
    """
    base = dict(
        start_age=50,
        end_age=54,
        initial_portfolio=1_000_000.0,
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
        spending_mode=SPENDING_MODE_FR_SMOOTH,
        spending_floor=0.0,
        spending_ceiling=1e12,
    )
    base.update(overrides)
    return Scenario(**base)


def test_fr_smooth_dispatch_and_npv_target():
    """The factory builds the smooth strategy with the baseline as the NPV target."""
    scenario = _fr_smooth_scenario(annual_spending=110_000.0)
    strat = make_strategy(scenario)
    assert isinstance(strat, FundedRatioSmoothStrategy)
    # NPV target == baseline annual_spending → FR=1 funds the baseline.
    assert strat.npv_target_real == pytest.approx(110_000.0)
    assert strat.base_spend == pytest.approx(110_000.0)
    assert strat.is_solvable is False
    assert strat.collects_spends is True


def test_fr_smooth_fr_one_holds_base():
    """FR == 1 → the partial-adjustment factor is ×1.0 (the base is held)."""
    scenario = _fr_smooth_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=50, fr=1.0, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend == pytest.approx(before)  # held
    assert action == "hold"
    assert spend == pytest.approx(before)  # go-go taper ×1, within wide rails


def test_fr_smooth_fr_above_one_raises_damped_by_alpha():
    """FR > 1 raises the base by alpha*(fr-1) of itself (the damped recurrence)."""
    scenario = _fr_smooth_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    fr = 1.4
    spend, action = strat._decide(scenario, age=50, fr=fr, begin_year_portfolio=1e6, gi=0.0)
    expected = before * (1.0 + _FR_SMOOTH_ALPHA * (fr - 1.0))
    assert strat.base_spend == pytest.approx(expected)
    assert action == "raise"
    assert spend == pytest.approx(expected)  # ×1 taper, wide rails
    # Damped: the base did NOT jump the full (fr-1) — only half of it (alpha=0.5).
    full_jump = before * fr
    assert strat.base_spend < full_jump


def test_fr_smooth_fr_below_one_cuts_damped_by_alpha():
    """FR < 1 cuts the base by alpha*(1-fr) of itself."""
    scenario = _fr_smooth_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    fr = 0.6
    spend, action = strat._decide(scenario, age=50, fr=fr, begin_year_portfolio=1e6, gi=0.0)
    expected = before * (1.0 + _FR_SMOOTH_ALPHA * (fr - 1.0))
    assert strat.base_spend == pytest.approx(expected)
    assert action == "cut"
    assert spend == pytest.approx(expected)
    # Damped: a 40%-underfunded year cuts only 20% (alpha=0.5), not the full 40%.
    assert strat.base_spend == pytest.approx(before * 0.8)


def test_fr_smooth_alpha_default_is_half():
    """The baked smoothness preset is 0.5 (half-speed adjustment)."""
    assert _FR_SMOOTH_ALPHA == pytest.approx(0.5)


def test_fr_smooth_floor_clamps_spend():
    """The flat floor clamps the withdrawn spend even as the base is driven down."""
    scenario = _fr_smooth_scenario(spending_floor=80_000.0)
    strat = make_strategy(scenario)
    # Drive the base far below the floor via repeated deep-underfunded years.
    spend = None
    for age in range(50, 55):
        spend, _ = strat._decide(scenario, age=age, fr=0.1, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend < 80_000.0   # base sank below the floor
    assert spend == pytest.approx(80_000.0)  # but the withdrawn spend is floored


def test_fr_smooth_ceiling_is_activity_tapered():
    """The ceiling is activity-tapered; the floor is flat (same rails as guardrails)."""
    scenario = _fr_smooth_scenario(spending_floor=60_000.0, spending_ceiling=150_000.0)
    strat = make_strategy(scenario)
    strat.base_spend = 1_000_000.0  # far above the ceiling so the clamp binds
    go_go, _ = strat._decide(scenario, age=60, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    strat.base_spend = 1_000_000.0
    slow_go, _ = strat._decide(scenario, age=75, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    strat.base_spend = 1_000_000.0
    no_go, _ = strat._decide(scenario, age=85, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    assert go_go == pytest.approx(150_000.0)
    assert slow_go == pytest.approx(150_000.0 * activity_multiplier(scenario, 75))
    assert no_go == pytest.approx(150_000.0 * activity_multiplier(scenario, 85))


def test_fr_smooth_floor_wins_over_collapsed_tapered_ceiling():
    """Floor wins if a tapered ceiling falls to/below the floor."""
    scenario = _fr_smooth_scenario(
        spending_floor=60_000.0, spending_ceiling=70_000.0, no_go_multiplier=0.5,
    )
    strat = make_strategy(scenario)
    strat.base_spend = 1_000_000.0
    spend, _ = strat._decide(scenario, age=90, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    assert spend == pytest.approx(60_000.0)  # 70k*0.5=35k tapered ceiling < 60k floor


def test_fr_smooth_taper_composes_once():
    """The activity taper is applied ONCE (no double-taper).

    Carrying the UNtapered base is the invariant: at a slow-go age the spend is the
    untapered base times the multiplier exactly once, not the multiplier squared.
    """
    scenario = _fr_smooth_scenario(spending_floor=0.0, spending_ceiling=1e12)
    strat = make_strategy(scenario)
    base = strat.base_spend  # 100k, untapered
    m = activity_multiplier(scenario, 80)  # a slow/no-go multiplier < 1
    assert m < 1.0
    spend, _ = strat._decide(scenario, age=80, fr=1.0, begin_year_portfolio=1e6, gi=0.0)
    # Single taper: base * m. A double-taper would give base * m * m (strictly less).
    assert spend == pytest.approx(base * m)
    # The carried base itself is still untapered (taper never folded back in).
    assert strat.base_spend == pytest.approx(base)


def test_fr_smooth_degenerate_begin_zero_skips_adjustment():
    """begin_year_portfolio <= 0 skips the FR adjustment (floor governs)."""
    scenario = _fr_smooth_scenario(spending_floor=60_000.0)
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=70, fr=0.01, begin_year_portfolio=0.0, gi=0.0)
    assert strat.base_spend == pytest.approx(before)  # base held — no adjustment
    assert action == "hold"
    assert spend == pytest.approx(before)  # 100k base in band; not floored here


def test_fr_smooth_base_stays_positive_under_extreme_underfunding():
    """The base never goes non-positive, even across many deep-underfunded years."""
    scenario = _fr_smooth_scenario()
    strat = make_strategy(scenario)
    for age in range(50, 55):
        strat._decide(scenario, age=age, fr=0.0, begin_year_portfolio=1e6, gi=0.0)
        assert strat.base_spend > 0.0  # (1 + 0.5*(0-1)) = 0.5 factor each year


def test_fr_smooth_emits_per_year_events():
    """Option (a): fr-smooth emits one AdjustmentEvent per year with a direction.

    The action is the SIGN of the FR-driven change (raise/cut/hold), and a raise in
    a GI-step year is flagged gi_stepped_up — so the existing adjustment readout +
    worst-cohort drill work unchanged.
    """
    scenario = _fr_smooth_scenario()
    strat = make_strategy(scenario)
    # year 0: fr>1 raise; year 1: fr<1 cut; year 2: fr=1 hold.
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1e6, gi=0.0)
    strat.base_spend  # carried
    strat.spend_for_year(scenario, age=51, begin_year_portfolio=1e6, gi=0.0)
    assert len(strat.events) == 2
    assert {e.age for e in strat.events} == {50, 51}
    assert all(e.action in ("raise", "cut", "hold") for e in strat.events)


def test_fr_smooth_runs_across_all_three_engines():
    """Cross-mode smoke: fr-smooth runs in deterministic / historical / MC and
    returns a spending series (the minimum-bar fan requirement)."""
    from firebridgelab.historical import simulate_historical
    from firebridgelab.monte_carlo import simulate_monte_carlo

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_FR_SMOOTH,
        annual_spending=110_000.0,
    )
    # Deterministic.
    det = simulate(scenario)
    assert len(det.points) == scenario.end_age - scenario.start_age + 1
    assert all(p.spending >= 0.0 for p in det.points)
    # Historical: the spending fan must be present (collects_spends → fan built).
    hist = simulate_historical(scenario)
    assert hist.spend_median is not None and len(hist.spend_median) > 0
    # Monte Carlo: same — a spending fan across synthetic trials.
    mc = simulate_monte_carlo(scenario, n_trials=200, seed=7)
    assert mc.spend_median is not None and len(mc.spend_median) > 0


def test_fr_smooth_spend_never_below_floor_across_engines():
    """fr-smooth spending respects the operational floor in every engine (except a
    fully depleted year), mirroring the guardrail invariant."""
    from firebridgelab.historical import simulate_historical
    from firebridgelab.monte_carlo import simulate_monte_carlo

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_FR_SMOOTH,
        annual_spending=145_000.0,   # well above the floor → the rule cuts hard
        spending_floor=75_000.0,
    )
    det = simulate(scenario)
    for p in det.points:
        assert p.spending >= scenario.spending_floor - 1e-6 or p.end_portfolio == 0.0
    hist = simulate_historical(scenario)
    if hist.spend_p10:
        for v in hist.spend_p10:
            assert v >= scenario.spending_floor - 1e-6 or v == 0.0


# --------------------------------------------------------------------------- #
# FundedRatioGuardrailStrategy — funded-ratio BAND spending (FR slice 3)       #
# --------------------------------------------------------------------------- #
#
# fr-guardrail is a close sibling of fr-smooth: same spine, same NPV target, same
# rails, same nudge→taper→clamp order — the ONLY difference is the per-year rule.
# fr-smooth moves the base a damped fraction toward full funding every year;
# fr-guardrail trips a DISCRETE band on the funded ratio: hold inside [0.80, 1.20],
# raise +10% above 1.20, cut −10% below 0.80. These pin the band rule, the rails,
# the degenerate skip, the single-taper composition, and a cross-mode smoke run.


def _fr_guardrail_scenario(**overrides) -> Scenario:
    """A GI-free fr-guardrail scenario: ages 50..57, $1M, $100k baseline, all
    go-go (activity ×1.0) unless an age outside go-go is probed. Floor/ceiling
    default wide-open so the band rule is tested clean; clamp tests set them.
    """
    base = dict(
        start_age=50,
        end_age=54,
        initial_portfolio=1_000_000.0,
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
        spending_mode=SPENDING_MODE_FR_GUARDRAIL,
        spending_floor=0.0,
        spending_ceiling=1e12,
    )
    base.update(overrides)
    return Scenario(**base)


def test_fr_guardrail_dispatch_and_npv_target():
    """The factory builds the band strategy with the baseline as the NPV target."""
    scenario = _fr_guardrail_scenario(annual_spending=110_000.0)
    strat = make_strategy(scenario)
    assert isinstance(strat, FundedRatioGuardrailStrategy)
    assert strat.npv_target_real == pytest.approx(110_000.0)
    assert strat.base_spend == pytest.approx(110_000.0)
    assert strat.is_solvable is False
    assert strat.collects_spends is True


def test_fr_guardrail_baked_constants():
    """The baked band/step preset is ±20% bands with a ±10% step."""
    assert _FR_GR_UPPER == pytest.approx(1.20)
    assert _FR_GR_LOWER == pytest.approx(0.80)
    assert _FR_GR_STEP == pytest.approx(0.10)


@pytest.mark.parametrize("fr", [0.80, 0.95, 1.0, 1.05, 1.20])
def test_fr_guardrail_inside_band_holds(fr):
    """FR within [0.80, 1.20] (inclusive at the edges) → hold, base unchanged."""
    scenario = _fr_guardrail_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=50, fr=fr, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend == pytest.approx(before)  # held
    assert action == "hold"
    assert spend == pytest.approx(before)  # ×1 taper, wide rails


def test_fr_guardrail_above_upper_raises_exactly_ten_percent():
    """FR > 1.20 → raise the base by exactly +10% (the baked step)."""
    scenario = _fr_guardrail_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=50, fr=1.5, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend == pytest.approx(before * 1.10)
    assert action == "raise"
    assert spend == pytest.approx(before * 1.10)  # ×1 taper, wide rails


def test_fr_guardrail_below_lower_cuts_exactly_ten_percent():
    """FR < 0.80 → cut the base by exactly −10% (the baked step)."""
    scenario = _fr_guardrail_scenario()
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=50, fr=0.5, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend == pytest.approx(before * 0.90)
    assert action == "cut"
    assert spend == pytest.approx(before * 0.90)


def test_fr_guardrail_floor_clamps_spend():
    """The flat floor clamps the withdrawn spend even as the base is driven down."""
    scenario = _fr_guardrail_scenario(spending_floor=80_000.0)
    strat = make_strategy(scenario)
    spend = None
    for age in range(50, 55):
        spend, _ = strat._decide(scenario, age=age, fr=0.1, begin_year_portfolio=1e6, gi=0.0)
    assert strat.base_spend < 80_000.0   # base sank below the floor (5×−10%)
    assert spend == pytest.approx(80_000.0)  # but the withdrawn spend is floored


def test_fr_guardrail_ceiling_is_activity_tapered():
    """The ceiling is activity-tapered; the floor is flat (same rails as siblings)."""
    scenario = _fr_guardrail_scenario(spending_floor=60_000.0, spending_ceiling=150_000.0)
    strat = make_strategy(scenario)
    strat.base_spend = 1_000_000.0  # far above the ceiling so the clamp binds
    go_go, _ = strat._decide(scenario, age=60, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    strat.base_spend = 1_000_000.0
    slow_go, _ = strat._decide(scenario, age=75, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    strat.base_spend = 1_000_000.0
    no_go, _ = strat._decide(scenario, age=85, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    assert go_go == pytest.approx(150_000.0)
    assert slow_go == pytest.approx(150_000.0 * activity_multiplier(scenario, 75))
    assert no_go == pytest.approx(150_000.0 * activity_multiplier(scenario, 85))


def test_fr_guardrail_floor_wins_over_collapsed_tapered_ceiling():
    """Floor wins if a tapered ceiling falls to/below the floor."""
    scenario = _fr_guardrail_scenario(
        spending_floor=60_000.0, spending_ceiling=70_000.0, no_go_multiplier=0.5,
    )
    strat = make_strategy(scenario)
    strat.base_spend = 1_000_000.0
    spend, _ = strat._decide(scenario, age=90, fr=1.0, begin_year_portfolio=1e12, gi=0.0)
    assert spend == pytest.approx(60_000.0)  # 70k*0.5=35k tapered ceiling < 60k floor


def test_fr_guardrail_taper_composes_once():
    """The activity taper is applied ONCE (no double-taper).

    Carrying the UNtapered base is the invariant: at a slow-go age the spend is the
    untapered base times the multiplier exactly once, not the multiplier squared.
    """
    scenario = _fr_guardrail_scenario(spending_floor=0.0, spending_ceiling=1e12)
    strat = make_strategy(scenario)
    base = strat.base_spend  # 100k, untapered
    m = activity_multiplier(scenario, 80)  # a slow/no-go multiplier < 1
    assert m < 1.0
    spend, _ = strat._decide(scenario, age=80, fr=1.0, begin_year_portfolio=1e6, gi=0.0)
    assert spend == pytest.approx(base * m)
    assert strat.base_spend == pytest.approx(base)  # carried base still untapered


def test_fr_guardrail_degenerate_begin_zero_skips_adjustment():
    """begin_year_portfolio <= 0 skips the band trip (floor governs)."""
    scenario = _fr_guardrail_scenario(spending_floor=60_000.0)
    strat = make_strategy(scenario)
    before = strat.base_spend
    spend, action = strat._decide(scenario, age=70, fr=0.01, begin_year_portfolio=0.0, gi=0.0)
    assert strat.base_spend == pytest.approx(before)  # base held — no adjustment
    assert action == "hold"
    assert spend == pytest.approx(before)  # 100k base in band; not floored here


def test_fr_guardrail_base_stays_positive_under_extreme_underfunding():
    """The base never goes non-positive, even across many deep-underfunded years."""
    scenario = _fr_guardrail_scenario()
    strat = make_strategy(scenario)
    for age in range(50, 55):
        strat._decide(scenario, age=age, fr=0.0, begin_year_portfolio=1e6, gi=0.0)
        assert strat.base_spend > 0.0  # ×0.90 each year stays strictly positive


def test_fr_guardrail_emits_per_year_events():
    """fr-guardrail emits one AdjustmentEvent per year with a real cut/raise/hold."""
    scenario = _fr_guardrail_scenario()
    strat = make_strategy(scenario)
    strat.spend_for_year(scenario, age=50, begin_year_portfolio=1e6, gi=0.0)
    strat.spend_for_year(scenario, age=51, begin_year_portfolio=1e6, gi=0.0)
    assert len(strat.events) == 2
    assert {e.age for e in strat.events} == {50, 51}
    assert all(e.action in ("raise", "cut", "hold") for e in strat.events)


def test_fr_guardrail_runs_across_all_three_engines():
    """Cross-mode smoke: fr-guardrail runs in deterministic / historical / MC and
    returns a spending series (the minimum-bar fan requirement)."""
    from firebridgelab.historical import simulate_historical
    from firebridgelab.monte_carlo import simulate_monte_carlo

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_FR_GUARDRAIL,
        annual_spending=110_000.0,
    )
    det = simulate(scenario)
    assert len(det.points) == scenario.end_age - scenario.start_age + 1
    assert all(p.spending >= 0.0 for p in det.points)
    hist = simulate_historical(scenario)
    assert hist.spend_median is not None and len(hist.spend_median) > 0
    mc = simulate_monte_carlo(scenario, n_trials=200, seed=7)
    assert mc.spend_median is not None and len(mc.spend_median) > 0


def test_fr_guardrail_spend_never_below_floor_across_engines():
    """fr-guardrail spending respects the operational floor in every engine (except
    a fully depleted year), mirroring the sibling invariant."""
    from firebridgelab.historical import simulate_historical

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_FR_GUARDRAIL,
        annual_spending=145_000.0,
        spending_floor=75_000.0,
    )
    det = simulate(scenario)
    for p in det.points:
        assert p.spending >= scenario.spending_floor - 1e-6 or p.end_portfolio == 0.0
    hist = simulate_historical(scenario)
    if hist.spend_p10:
        for v in hist.spend_p10:
            assert v >= scenario.spending_floor - 1e-6 or v == 0.0
