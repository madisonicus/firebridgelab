"""Tests for the Historical-mode engine (Return Variability, Build 1).

Each anchors one property of the historical simulation:
  1. Cohort count for a known horizon matches all_starting_years.
  2. Renormalization is a no-op for the default 60/40 (the v1 invariant).
  3. Renormalization drops an unmapped class's weight (the correctness invariant).
  4. Median trajectory is sensible (right length, ages, positive for the default).
  5. Depletion logic: none for the default; all cohorts under stress, earliest set.
  6. No-mapped-class allocation raises rather than silently producing garbage.
  7. Cross-engine equivalence: a constant-return cohort reproduces simulate()
     exactly, pinning that both engines apply the same midpoint withdrawal math.
"""

import numpy as np
import pytest

from dataclasses import replace

from firebridgelab import (
    AssetClass,
    blended_real_return,
    blended_real_return_at_age,
    default_scenario,
    renormalized_weights,
    simulate,
    simulate_historical,
)
from firebridgelab.returns.series import (
    HIST_SERIES_REGISTRY,
    HISTORICAL_SELECTION_EPSILON,
    common_overlap_start_years,
    historical_series_by_id,
    selected_series_for,
)
from firebridgelab.engine.cohort_windows import (
    _apply_window_floor,
    _is_legacy_shiller_only,
    cohort_return_sequences,
)
from firebridgelab.engine.sim_core import _simulate_cohort
from firebridgelab.returns.shiller import load_shiller


def _reit_scenario(reit_weight=0.20, equity=0.50, bonds=0.30):
    """A multi-class portfolio with a short-history class (us_reit, 1972+).

    Used by the slice-3 window-policy tests. Default 50/30/20 equity/bonds/REIT.
    """
    return replace(
        default_scenario(),
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", equity, 0.07),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", bonds, 0.02),
            AssetClass("us_reit", "US REITs", reit_weight, 0.0666),
        ),
    )


def test_cohort_count_matches_full_windows():
    """Cohort count == number of start years with a complete N-year window.

    The default horizon is ages 50..95 inclusive = 46 year-steps, so the number
    of cohorts is all_starting_years(46) on the loaded data.
    """
    data = load_shiller()
    scenario = default_scenario()
    n_steps = scenario.end_age - scenario.start_age + 1  # 46
    result = simulate_historical(scenario, data)

    assert result.cohort_count == len(data.all_starting_years(n_steps))
    assert result.start_year == data.all_starting_years(n_steps)[0]
    assert result.start_year == 1871  # full history per the brief


def test_renormalization_noop_for_default_allocation():
    """Default 60/40 maps both classes and already sums to 100 → weights unchanged."""
    weights = renormalized_weights(default_scenario())
    assert weights == pytest.approx({"us_equity": 0.60, "us_bonds": 0.40})


def test_renormalization_drops_unmapped_class():
    """An unmapped class's weight is renormalized out (data-can't-lie invariant).

    57% Equity / 38% Bonds / 5% unmapped Crypto → the mapped 57/38 renormalize to
    the same 60/40 the default produces. The Crypto weight never leaks in.
    """
    scenario = replace(
        default_scenario(),
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", 0.57, 0.07),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.38, 0.02),
            AssetClass("crypto", "Crypto", 0.05, 0.0),
        ),
    )
    weights = renormalized_weights(scenario)
    assert weights == pytest.approx({"us_equity": 0.60, "us_bonds": 0.40})


def test_median_trajectory_is_sensible():
    """Median has one point per age, spans the horizon, and is positive for default.

    The default scenario (a 6.1% initial draw that guaranteed income later
    relieves) has a median path that never depletes, so every median age value is
    > 0; the first step should be near the one-year deterministic-ish
    neighbourhood (>$1.7M).
    """
    scenario = default_scenario()
    result = simulate_historical(scenario)

    assert len(result.median) == scenario.end_age - scenario.start_age + 1
    assert result.ages[0] == scenario.start_age
    assert result.ages[-1] == scenario.end_age
    assert all(v > 0 for v in result.median)
    # First-year median is one cohort-year of growth/withdrawal on $1.8M.
    assert result.median[0] > 1_700_000
    # Ending median for the default 60/40 lands in the multi-million range
    # (cFIREsim cross-check is the supervisor's; this is just a sanity band).
    assert 2_000_000 < result.median[-1] < 15_000_000


def test_percentile_fan_is_ordered_at_every_age():
    """Build 2: p10 <= p25 <= median <= p75 <= p90 at every age, all same length.

    The percentile fan is the deliverable; this pins that the bands are
    monotonic in the percentile at each age (so the shaded 10–90 band never
    inverts) and that each band has one value per age in the horizon.
    """
    scenario = default_scenario()
    result = simulate_historical(scenario)
    n = scenario.end_age - scenario.start_age + 1

    for band in (result.p10, result.p25, result.median, result.p75, result.p90):
        assert len(band) == n

    for i in range(n):
        assert (
            result.p10[i]
            <= result.p25[i]
            <= result.median[i]
            <= result.p75[i]
            <= result.p90[i]
        )
        # All percentiles are non-negative (portfolio floored at zero).
        assert result.p10[i] >= 0


def test_ending_range_matches_final_age_percentiles():
    """The (p10, p90) ending range equals the final-age column's p10/p90.

    The readout's "10th–90th percentile" sentence reads from ending_range, so it
    must agree with the last point of the p10/p90 bands. Also pins p10 <= p90.
    """
    result = simulate_historical(default_scenario())
    low, high = result.ending_range
    assert low == result.p10[-1]
    assert high == result.p90[-1]
    assert low <= high


def test_default_scenario_has_tail_depletion_under_constant_mode():
    """Default ($110k constant) depletes in a historical tail.

    The default scenario sits between the $85k guardrail floor and the
    $150k ceiling to demonstrate two-sided GK flex. Constant mode (no
    guardrails) takes the full $110k from the most adverse cohorts and a
    tail depletes — by design, since the guardrails would otherwise
    cut to the floor in those cohorts. Pins the count + earliest age so any
    silent engine drift is caught.
    """
    result = simulate_historical(default_scenario())
    assert result.depletion_count == 17
    assert result.earliest_depletion_age == 63


def test_depletion_logic_under_stress():
    """High spend, no GI, all-equity → every cohort depletes; earliest age is set.

    Confirms the floor-at-zero + earliest-depletion-age aggregation fires.
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
    result = simulate_historical(scenario)

    assert result.depletion_count == result.cohort_count  # all cohorts depleted
    assert result.earliest_depletion_age is not None
    assert scenario.start_age < result.earliest_depletion_age <= scenario.end_age
    # Once depleted the floor holds: median ending value is zero.
    assert result.median[-1] == 0.0


def test_no_mapped_class_raises():
    """An allocation with no class mapping to a known series is an error.

    Renormalizing over zero mapped classes can't produce an honest blend, so we
    raise rather than silently returning garbage.
    """
    scenario = replace(
        default_scenario(),
        allocation=(AssetClass("crypto", "Crypto", 1.0, 0.0),),
    )
    with pytest.raises(ValueError):
        simulate_historical(scenario)


def test_cohort_returns_floor_at_zero_stay_zero():
    """A cohort that depletes flatlines at zero for all later ages (no rebound).

    Drive a guaranteed depletion (huge spend, no GI, all-equity) and assert the
    median, once it hits zero, never recovers above zero at any later age.
    """
    scenario = replace(
        default_scenario(),
        annual_spending=500_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07),),
    )
    result = simulate_historical(scenario)
    series = np.array(result.median)
    if (series == 0).any():
        first_zero = int(np.argmax(series == 0))
        assert (series[first_zero:] == 0).all()


@pytest.mark.parametrize(
    "scenario",
    [
        default_scenario(),
        # Alternate allocation + heavier spend, so the equivalence isn't only
        # checked on the no-stress default path (exercises GI activation too).
        replace(
            default_scenario(),
            allocation=(AssetClass("us_total_market", "US Total Stock Market", 0.80, 0.07), AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.20, 0.02)),
            annual_spending=160_000.0,
        ),
    ],
)
def test_historical_constant_return_matches_deterministic(scenario):
    """A constant-return cohort reproduces simulate()'s per-age trajectory.

    Pins the invariant that the Historical and Deterministic engines apply the
    SAME midpoint withdrawal math. We feed a single cohort a constant return
    series equal to the scenario's blended real return — the exact return the
    deterministic engine compounds — and assert the per-age end-of-year
    portfolio matches simulate() across the full horizon to abs diff < 1e-6.

    If a future change touches one engine's withdrawal/GI formula and not the
    other, the two trajectories diverge and this test fails.
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    r = blended_real_return_at_age(scenario, scenario.start_age)
    constant_returns = np.full(n_steps, r)

    cohort_ends, _, _ = _simulate_cohort(scenario, constant_returns)
    deterministic = [p.end_portfolio for p in simulate(scenario).points]

    assert len(cohort_ends) == len(deterministic) == n_steps
    for hist_v, det_v in zip(cohort_ends, deterministic):
        assert abs(hist_v - det_v) < 1e-6


# --------------------------------------------------------------------------- #
# Slice 3 — multi-class cohort replay + D3 window policy                      #
# --------------------------------------------------------------------------- #


@pytest.mark.requires_optional_classes
def test_default_two_class_takes_legacy_shiller_path():
    """The byte-stability gate: the default 2-class portfolio routes to the
    legacy Shiller path (window policy is a no-op for the 1871+ pair)."""
    assert _is_legacy_shiller_only(default_scenario()) is True
    # A REIT in the mix routes to the multi-class path instead.
    assert _is_legacy_shiller_only(_reit_scenario()) is False


def test_default_historical_byte_stable():
    """Hard constraint: the default 2-class Historical run is unchanged.

    Same 110 cohorts and 1871 testable-since the legacy path produced before
    slice 3 (equity + bonds both 1871+, so the window policy is a no-op). This
    is the regression anchor for the byte-stability requirement.
    """
    data = load_shiller()
    scenario = default_scenario()
    n_steps = scenario.end_age - scenario.start_age + 1  # 46
    result = simulate_historical(scenario, data)

    assert result.cohort_count == len(data.all_starting_years(n_steps))
    assert result.cohort_count == 110
    assert result.start_year == 1871
    assert result.testable_since == 1871


def test_testable_since_aliases_start_year():
    """``testable_since`` is the earliest valid cohort start (== ``start_year``)."""
    result = simulate_historical(default_scenario())
    assert result.testable_since == result.start_year == 1871

    reit = simulate_historical(_reit_scenario())
    assert reit.testable_since == reit.start_year


@pytest.mark.requires_optional_classes
def test_short_history_class_restricts_window_to_1972():
    """A 20% us_reit (1972+) restricts the window to the post-1972 overlap (D3).

    testable_since ≈ 1972 (REIT's first year), and the cohort count matches the
    full-horizon common-overlap span: starts 1972 .. 2025-n_steps+1, inclusive.
    """
    scenario = _reit_scenario(reit_weight=0.20)
    n_steps = scenario.end_age - scenario.start_age + 1  # 46
    result = simulate_historical(scenario)

    assert result.testable_since == 1972
    # us_equity/us_bonds run to 2025, us_reit 1972-2025, so the binding window is
    # REIT's: starts 1972 .. (2025 - 46 + 1) = 1980 inclusive → 9 cohorts.
    expected_last_start = 2025 - n_steps + 1
    expected_count = expected_last_start - 1972 + 1
    assert result.cohort_count == expected_count == 9
    # Sanity: a real fan came out (not a crash / empty result).
    assert len(result.median) == n_steps
    assert all(v >= 0 for v in result.p10)


def test_zero_weight_short_history_class_does_not_bind_window():
    """A 0%-weight us_reit does NOT constrain the window (selected = weight > ε).

    With the REIT at exactly 0%, the window stays the default 1871 (the REIT's
    short history is ignored because it isn't *selected*), and the run reproduces
    the default 2-class cohort count.
    """
    scenario = _reit_scenario(reit_weight=0.0, equity=0.60, bonds=0.40)
    result = simulate_historical(scenario)
    assert result.testable_since == 1871
    assert result.cohort_count == 110


def test_sub_epsilon_weight_does_not_bind_window():
    """A weight at/below the epsilon is not selected (boundary of the ε rule)."""
    tiny = HISTORICAL_SELECTION_EPSILON  # exactly the floor → NOT selected (> ε)
    scenario = _reit_scenario(reit_weight=tiny, equity=0.60, bonds=0.40 - tiny)
    selected = selected_series_for(scenario, HIST_SERIES_REGISTRY)
    assert "us_reit" not in selected
    result = simulate_historical(scenario)
    assert result.testable_since == 1871


def test_unloaded_class_hidden_and_renormalized():
    """A class with NO loaded series (an unmapped class id) is hidden + renormalized.

    60% equity / 30% bonds / 10% ``crypto`` (an unmapped class id with no series —
    the brief's own example of a future unmapped class) → the crypto weight is
    dropped and the equity/bonds renormalize, so the window stays 1871 and the
    result matches a pure 60/40 of the kept weights (66.7% equity / 33.3% bonds
    renormalized).

    Note: slice 4a loaded the last of the canonical 12 (gold/intl/EM/TIPS/
    commodities), so all 12 menu classes now MAP — the hide/renormalize path only
    fires for a class id absent from SERIES_FOR_CLASS entirely (a stale/future
    class), which is what this asserts. Pre-4a this test used ``gold`` as the
    unloaded example; gold is now loaded, so it would (correctly) bind the window
    to 1972 instead of being dropped.
    """
    scenario = replace(
        default_scenario(),
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", 0.60, 0.07),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.30, 0.02),
            AssetClass("crypto", "Crypto", 0.10, 0.01),  # not in SERIES_FOR_CLASS
        ),
    )
    # Crypto maps to nothing → not in the selected set; only equity/bonds remain.
    selected = selected_series_for(scenario, HIST_SERIES_REGISTRY)
    assert set(selected) == {"us_equity", "us_bonds"}
    # Both kept series are Shiller → legacy path → window stays 1871.
    result = simulate_historical(scenario)
    assert result.testable_since == 1871
    assert result.cohort_count == 110


def test_it_treasury_mixed_with_damodaran_phase_classes_runs():
    """it_treasury (Jan-Jan phase) blended with Damodaran calendar-year classes.

    The calendar-phase reconciliation (slice-3 carry-forward): it_treasury is on
    the Jan→Jan phase while st/lt-treasury and small-value are calendar-year. The
    convention aligns all by integer year label; this asserts the mixed-phase
    blend runs and the window is the common overlap (it_treasury 1954-2024 binds
    the start to 1954 and the end to 2024).
    """
    scenario = replace(
        default_scenario(),
        allocation=(
            AssetClass("us_small_value", "SV", 0.40, 0.10),   # French calendar-year
            AssetClass("lt_treasury", "LT", 0.30, 0.014),      # Damodaran calendar-year
            AssetClass("it_treasury", "IT", 0.30, 0.012),      # Jan-Jan phase
        ),
    )
    n_steps = scenario.end_age - scenario.start_age + 1
    result = simulate_historical(scenario)

    # it_treasury (1954-2024) is the binding class on both ends:
    #   start = 1954, last full-window start = 2024 - n_steps + 1.
    assert result.testable_since == 1954
    expected_count = (2024 - n_steps + 1) - 1954 + 1
    assert result.cohort_count == expected_count
    assert len(result.median) == n_steps


@pytest.mark.requires_optional_classes
def test_common_overlap_intersection_logic():
    """The window is the intersection of the selected series' full-horizon starts.

    Directly exercises ``common_overlap_start_years``: with us_equity (1871+) and
    us_reit (1972+) over a short 10-year horizon, the intersection is bounded
    below by REIT's first year (1972) and above by the latest full 10-year window.
    """
    series_data = historical_series_by_id()
    starts = common_overlap_start_years(["us_equity", "us_reit"], 10, series_data)
    assert starts[0] == 1972  # bounded below by REIT's start
    # The accessor's REIT data ends 2025, so the last full 10-year start is 2016.
    assert starts[-1] == max(series_data["us_reit"]) - 10 + 1
    # Equity-only over the same horizon reaches back to 1871.
    eq_only = common_overlap_start_years(["us_equity"], 10, series_data)
    assert eq_only[0] == 1871


def test_no_common_window_raises():
    """Selected classes whose data never share a full-horizon year raise (honest).

    A 200-year horizon exceeds any series' length, so no start year has a full
    common window — surfaced as a ValueError (the API routes it to a 422), never
    a silent empty result.
    """
    scenario = replace(_reit_scenario(), start_age=0, end_age=199)  # 200-year horizon
    with pytest.raises(ValueError):
        simulate_historical(scenario)


def test_solver_uses_same_multiclass_window_as_display():
    """The solver's cohort construction matches simulate_historical's (no drift).

    Both go through ``cohort_return_sequences``, so the solver evaluates the SAME
    cohorts (same restricted window) the display engine replays. Pins that a
    multi-class solve can't silently use a different window than the fan.
    """
    scenario = _reit_scenario(reit_weight=0.20)
    display = simulate_historical(scenario)
    start_years, cohort_returns = cohort_return_sequences(scenario)

    assert len(cohort_returns) == display.cohort_count
    assert start_years[0] == display.testable_since
    n_steps = scenario.end_age - scenario.start_age + 1
    assert all(len(arr) == n_steps for arr in cohort_returns)


@pytest.mark.requires_optional_classes
def test_multiclass_weights_renormalize_over_selected():
    """The blend weights renormalize over the SELECTED series (sub-ε excluded).

    50/30/20 equity/bonds/REIT all clear ε, so the blend weights are exactly the
    raw weights (they already sum to 1); this pins that the selected weights drive
    the blend (the first cohort's blended return is the weight-weighted mix).
    """
    scenario = _reit_scenario(reit_weight=0.20, equity=0.50, bonds=0.30)
    selected = selected_series_for(scenario, HIST_SERIES_REGISTRY)
    assert selected == pytest.approx(
        {"us_equity": 0.50, "us_bonds": 0.30, "us_reit": 0.20}
    )
    # The first cohort's year-0 blended return = weighted mix of the three
    # series' year-0 (1972) returns.
    series_data = historical_series_by_id()
    _starts, cohort_returns = cohort_return_sequences(scenario)
    expected_year0 = (
        0.50 * series_data["us_equity"][1972]
        + 0.30 * series_data["us_bonds"][1972]
        + 0.20 * series_data["us_reit"][1972]
    )
    assert cohort_returns[0][0] == pytest.approx(expected_year0)


# --------------------------------------------------------------------------- #
# Adjustment-event visibility (variable-spending design slice 2)               #
# --------------------------------------------------------------------------- #

from firebridgelab.engine.cohort_aggregate import _compute_worst_cohort, _gi_step_label  # noqa: E402
from firebridgelab.spending.base import AdjustmentEvent  # noqa: E402
from firebridgelab.engine.sim_core import _compute_adjustment_stats  # noqa: E402
from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS  # noqa: E402


def _ev(age, action, spend, begin=1_000_000.0, gi=0.0, gi_stepped_up=False):
    """A terse AdjustmentEvent for the hand-constructed cohort sets."""
    return AdjustmentEvent(
        age=age, action=action, spend=spend,
        begin_portfolio=begin, gi=gi, gi_stepped_up=gi_stepped_up,
    )


def test_adjustment_stats_hand_constructed_counts_and_medians():
    """Exact counts/medians on a hand-built 4-cohort set.

    Start spend $130k, operational floor $60k (explicitly set, not the default).
    Four cohorts with deliberately chosen spend paths so every stat is
    hand-verifiable:

      A: [130, 117, 110] — cut below start (min 110); never raised; 0 floor years.
         First cut at age 51.
      B: [130, 145, 160] — raised above start; never cut (min 130 == start, NOT
         below); 0 floor years. No cut event.
      C: [130, 100, 60]  — cut; min 60 == floor; 1 floor year. First cut at age 51.
      D: [100, 80, 60]   — cut; min 60 floor; 1 floor year. First cut at age 50.

    So: ever_cut = A,C,D = 3/4; ever_raised = B = 1/4; first-cut ages = [51,51,50]
    → median 51; years_at_floor per cohort = [0,0,1,1] → median 0.5; per-cohort
    minima = [110,130,60,60] → p10 ≈ 60 (computed); lowest single-year = 60;
    ever_forced_to_floor = C,D = 2/4.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=130_000.0,
        spending_floor=60_000.0,
        spending_ceiling=300_000.0,
    )
    cohort_spends = [
        [130_000.0, 117_000.0, 110_000.0],   # A
        [130_000.0, 145_000.0, 160_000.0],   # B
        [130_000.0, 100_000.0, 60_000.0],    # C
        [100_000.0, 80_000.0, 60_000.0],     # D
    ]
    cohort_events = [
        [_ev(50, "hold", 130_000.0), _ev(51, "cut", 117_000.0), _ev(52, "cut", 110_000.0)],
        [_ev(50, "hold", 130_000.0), _ev(51, "raise", 145_000.0), _ev(52, "raise", 160_000.0)],
        [_ev(50, "hold", 130_000.0), _ev(51, "cut", 100_000.0), _ev(52, "cut", 60_000.0)],
        [_ev(50, "cut", 100_000.0), _ev(51, "cut", 80_000.0), _ev(52, "cut", 60_000.0)],
    ]
    stats = _compute_adjustment_stats(scenario, cohort_spends, cohort_events)

    assert stats.cohort_count == 4
    assert stats.pct_ever_cut == pytest.approx(3 / 4)
    assert stats.pct_ever_raised == pytest.approx(1 / 4)
    assert stats.median_first_cut_age == 51  # median of [51, 51, 50]
    assert stats.median_years_at_floor == pytest.approx(0.5)   # median [0,0,1,1]
    assert stats.lowest_single_year_spend == pytest.approx(60_000.0)
    assert stats.pct_ever_forced_to_floor == pytest.approx(2 / 4)
    # deepest_cut = p10 of per-cohort minima [110k, 130k, 60k, 60k] (sorted
    # [60k, 60k, 110k, 130k]); numpy linear-interp p10 = 60000 + 0.3*(60000-60000)
    # = 60000 (the 10th percentile sits within the two 60k minima).
    assert stats.deepest_cut == pytest.approx(60_000.0)


def test_adjustment_stats_empty_cohort_set_is_guarded():
    """An empty cohort set returns zeroed stats, not a ZeroDivisionError / min([]).

    Defensive guard (slice-3 insurance): short-window multi-class cohorts under
    guardrails could combine into a small/empty cohort set. The compute must not
    divide by n=0 or call min()/np.percentile() on an empty list.
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
    )
    stats = _compute_adjustment_stats(scenario, [], [])
    assert stats.cohort_count == 0
    assert stats.pct_ever_cut == 0.0
    assert stats.pct_ever_raised == 0.0
    assert stats.median_first_cut_age is None
    assert stats.median_years_at_floor == 0.0
    assert stats.deepest_cut == 0.0
    assert stats.lowest_single_year_spend == 0.0
    assert stats.pct_ever_forced_to_floor == 0.0


def test_default_spending_floor_is_85k():
    """The operational floor default is $85k (the guardrail clamp).

    $85k is the operational floor (the clamp); the $45k ABSOLUTE_FLOOR is the
    survivable context, not the default clamp.
    """
    assert default_scenario().spending_floor == 85_000.0


def test_worst_cohort_selection_lowest_total_spend():
    """Worst cohort = lowest TOTAL real spend; tie-break to earliest start year.

    Three cohorts (start years 1900/1901/1902). Totals: 1900 → 420k, 1901 → 285k
    (lowest), 1902 → 420k. The worst is 1901. (And a tie-break check: if 1900 and
    1902 tie for lowest, the earlier start wins.)
    """
    start_years = [1900, 1901, 1902]
    cohort_spends = [
        [140_000.0, 140_000.0, 140_000.0],   # 1900 total 420k
        [95_000.0, 95_000.0, 95_000.0],      # 1901 total 285k  ← lowest
        [140_000.0, 140_000.0, 140_000.0],   # 1902 total 420k
    ]
    cohort_events = [
        [_ev(50, "hold", s) for s in cohort_spends[c]] for c in range(3)
    ]
    worst = _compute_worst_cohort(start_years, cohort_spends, cohort_events, {})
    assert worst.start_year == 1901
    assert worst.total_spend == pytest.approx(285_000.0)
    assert worst.min_spend == pytest.approx(95_000.0)
    assert worst.depleted_age is None
    assert [y.age for y in worst.path] == [50, 50, 50]  # one per event

    # Tie-break: make 1900 and 1902 both the (tied) lowest; the earlier wins.
    tied = [
        [70_000.0, 70_000.0, 70_000.0],      # 1900 total 210k
        [95_000.0, 95_000.0, 95_000.0],      # 1901 total 285k
        [70_000.0, 70_000.0, 70_000.0],      # 1902 total 210k (ties 1900)
    ]
    tied_events = [[_ev(50, "hold", s) for s in tied[c]] for c in range(3)]
    worst_tie = _compute_worst_cohort(start_years, tied, tied_events, {})
    assert worst_tie.start_year == 1900  # earliest of the tied pair


def test_worst_cohort_carries_depletion_age():
    """The worst cohort's depleted_age comes through from the index→age map."""
    start_years = [1965, 1966]
    cohort_spends = [
        [100_000.0, 100_000.0],   # 1965 total 200k
        [80_000.0, 60_000.0],     # 1966 total 140k ← worst
    ]
    cohort_events = [[_ev(50, "hold", s) for s in cs] for cs in cohort_spends]
    # Cohort index 1 (1966) depleted at age 80.
    worst = _compute_worst_cohort(start_years, cohort_spends, cohort_events, {1: 80})
    assert worst.start_year == 1966
    assert worst.depleted_age == 80


def test_gi_driven_vs_market_driven_raise_labeling():
    """A raise in a GI-step year reads GI-driven; a raise in a non-GI year reads
    market-driven (design resolved decision)."""
    # Direct label helper.
    assert _gi_step_label("raise", gi_driven=True) == "raise — guaranteed income started"
    assert _gi_step_label("raise", gi_driven=False) == "raise — strong returns"
    assert _gi_step_label("cut", gi_driven=False) == "cut — portfolio under pressure"
    assert _gi_step_label("hold", gi_driven=False) == "hold"

    # And through the worst-cohort path: a raise where GI stepped up is gi_driven,
    # a raise where it didn't is not.
    start_years = [1970]
    cohort_spends = [[130_000.0, 110_000.0, 121_000.0, 133_100.0]]
    cohort_events = [[
        _ev(50, "hold", 130_000.0),
        _ev(51, "cut", 110_000.0),
        # a pension lands → GI stepped up → GI-driven raise.
        _ev(52, "raise", 121_000.0, gi=40_000.0, gi_stepped_up=True),
        # next year: GI flat, another raise → market-driven.
        _ev(53, "raise", 133_100.0, gi=40_000.0, gi_stepped_up=False),
    ]]
    worst = _compute_worst_cohort(start_years, cohort_spends, cohort_events, {})
    by_age = {y.age: y for y in worst.path}
    assert by_age[52].gi_driven is True
    assert by_age[52].label == "raise — guaranteed income started"
    assert by_age[53].gi_driven is False
    assert by_age[53].label == "raise — strong returns"


def test_adjustment_fields_none_for_constant_and_lifecycle():
    """Constant/lifecycle leave adjustment_stats + worst_cohort None (additive).

    The slice-2 fields are guardrails-only; under constant/lifecycle there are no
    adjustments, so both stay None and the payload omits them (back-compatible —
    part of the byte-stability guarantee for the non-guardrail modes).
    """
    for mode in ("constant", "lifecycle"):
        result = simulate_historical(replace(default_scenario(), spending_mode=mode))
        assert result.adjustment_stats is None
        assert result.worst_cohort is None


def test_guardrails_historical_populates_adjustment_fields():
    """End-to-end: a guardrails Historical run carries the slice-2 fields.

    The default 2-class legacy Shiller path (110 cohorts, 1871+) at a $130k start
    spend flexes enough to produce real adjustments. Pins that the fields populate
    with sane shapes, the worst cohort is labelled by a real start year with a
    full-horizon path, and the GI-driven labelling fires at the GI step ages (the
    default GI lands at 60, 65, 67 and 70).
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=130_000.0,
    )
    result = simulate_historical(scenario)
    stats = result.adjustment_stats
    wc = result.worst_cohort
    assert stats is not None and wc is not None
    assert stats.cohort_count == result.cohort_count == 110
    assert 0.0 <= stats.pct_ever_cut <= 1.0
    assert 0.0 <= stats.pct_ever_forced_to_floor <= 1.0
    # The worst cohort is a real start year with a full-horizon annotated path.
    n_steps = scenario.end_age - scenario.start_age + 1
    assert 1871 <= wc.start_year <= 2025
    assert len(wc.path) == n_steps
    # At least one GI-driven raise should appear in a squeezed cohort (GI landing
    # at 60/65/67/70 drops the funded rate and trips a prosperity raise — the resolved
    # design decision). It must be labelled distinctly from a market raise.
    gi_raises = [y for y in wc.path if y.action == "raise" and y.gi_driven]
    for y in gi_raises:
        assert y.label == "raise — guaranteed income started"
        assert y.age in (scenario.person_a_pension_start_age, scenario.person_b_pension_start_age,
                         scenario.person_a_ss_start_age, scenario.person_b_ss_start_age)


# --------------------------------------------------------------------------- #
# Variable-spending design slice 1 — guardrail spending fan                    #
# --------------------------------------------------------------------------- #


def test_constant_lifecycle_carry_no_spending_fan():
    """Under constant/lifecycle the spending fan is absent (None).

    Spending is identical across cohorts in those modes, so there is no spending
    distribution to plot — the spend_* fields stay None (the API omits the
    spending fan; back-compatible).
    """
    from firebridgelab.spending.modes import SPENDING_MODE_LIFECYCLE

    const = simulate_historical(default_scenario())
    assert const.spend_median is None
    assert const.spend_p10 is None and const.spend_p90 is None

    lifecycle = simulate_historical(
        replace(default_scenario(), spending_mode=SPENDING_MODE_LIFECYCLE)
    )
    assert lifecycle.spend_median is None


def test_guardrail_spending_fan_present_and_monotone():
    """Guardrails populates a per-age spending fan, monotone p10<=...<=p90.

    With a start spend well above the floor ($130k > $85k), the
    guardrail nudges across cohorts so a genuine spending distribution exists.
    Mirrors the portfolio fan: same length as ``ages``, and at every age
    spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90.
    """
    from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=130_000.0,
    )
    result = simulate_historical(scenario)
    n_steps = scenario.end_age - scenario.start_age + 1

    assert result.cohort_count == 110  # same cohorts as the default historical run
    assert result.start_year == 1871
    for arr in (result.spend_p10, result.spend_p25, result.spend_median,
                result.spend_p75, result.spend_p90):
        assert arr is not None
        assert len(arr) == n_steps
    for k in range(n_steps):
        assert (result.spend_p10[k] <= result.spend_p25[k] <= result.spend_median[k]
                <= result.spend_p75[k] <= result.spend_p90[k] + 1e-9)
    # Every spend respects the default rails ($85k operational floor, $150k
    # ceiling tapered).
    assert min(result.spend_p10) >= 85_000.0 - 1e-6
    assert max(result.spend_p90) <= 150_000.0 + 1e-6
    # The fan actually spreads somewhere in the bridge years (spending varies).
    spread = [result.spend_p90[k] - result.spend_p10[k] for k in range(n_steps)]
    assert max(spread) > 0.0


def test_guardrail_year_one_no_trip():
    """Year-1 spend == start spend for every cohort (begin balance = initial).

    At the start age the begin-of-year portfolio is exactly the initial portfolio,
    so the funded rate equals initial_rate (in-band) → no trip → every cohort
    spends the start spend. So the spending fan is a point at age 50.
    """
    from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        annual_spending=130_000.0,
    )
    result = simulate_historical(scenario)
    assert result.spend_p10[0] == pytest.approx(130_000.0)
    assert result.spend_p90[0] == pytest.approx(130_000.0)


def test_realized_spend_capped_at_fundable_after_depletion():
    """Realized-spend correctness fix: a depleting cohort RECORDS what it could
    fund, NOT the strategy's clamped intent (the overstatement bug).

    Diagnosis scenario: $1.2M / $110k / guardrails with the operational floor set
    explicitly to $100k (above every default GI level, so the post-depletion cap
    always binds), default GI ($24,000 at 60-64, $33,000 at 65-66, $66,000 at
    67-69, $98,240 at 70+). The 1906 cohort depletes at 63; pre-fix it would
    record the $100k floor for ages 63-95 even though the portfolio was gone and
    GI was far lower. Post-fix the recorded spend is ``max_fundable_spend`` —
    ``(start*(1+r) + gi)/(1+r/2)`` — which collapses to the GI once the
    portfolio is 0. Pinned against a hand-computed value.

    Also asserts each fully-depleted year's spend reflects that year's GI level
    (24k, 33k, 66k, 98,240); a no-GI bridge depletion (next test) drives the
    recorded spend to ~$0.
    """
    from firebridgelab.income.guaranteed import gi_at_age
    from firebridgelab.engine.step import max_fundable_spend

    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        initial_portfolio=1_200_000.0,
        annual_spending=110_000.0,
        spending_floor=100_000.0,
    )
    start_years, cohort_returns = cohort_return_sequences(scenario)
    idx = start_years.index(1906)
    returns = cohort_returns[idx]
    ends, depleted_age, spends = _simulate_cohort(scenario, returns)
    assert depleted_age == 63  # the 1906 cohort depletes at 63

    # Reconstruct each year's begin-of-year portfolio to hand-compute max_fundable.
    begins = [scenario.initial_portfolio] + ends[:-1]

    def k_of(age):
        return age - scenario.start_age

    # Pre-depletion (age 62, portfolio still alive): intended < max_fundable, so the
    # recorded spend is the unchanged $100k floor (the cap is a no-op).
    assert spends[k_of(62)] == pytest.approx(100_000.0)

    # Depletion year (63): recorded spend == max_fundable, NOT $100k.
    k63 = k_of(63)
    r63 = float(returns[k63])
    mf63 = max_fundable_spend(begins[k63], r63, gi_at_age(scenario, 63))
    assert spends[k63] == pytest.approx(mf63, abs=1e-4)
    assert spends[k63] < 100_000.0  # genuinely below the floor — can't fund it
    assert ends[k63] == 0.0

    # Fully-depleted years: with GI credited at mid-year like spending, a $0
    # balance funds exactly that year's GI.
    for age, gi in ((64, 24_000.0), (66, 33_000.0), (68, 66_000.0), (90, 98_240.0)):
        assert gi_at_age(scenario, age) == pytest.approx(gi)
        assert spends[k_of(age)] == pytest.approx(gi, abs=1e-4), f"age {age}"

    # Pre-fix every one of ages 63-95 recorded the $100k floor; post-fix none do.
    assert all(spends[k_of(a)] < 100_000.0 - 1e-6 for a in (63, 64, 66, 90, 95))


def test_realized_spend_zero_in_no_gi_bridge_depletion():
    """A no-GI bridge depletion records ~$0 spend — the plan funds nothing.

    Tiny portfolio ($400k) + high spend ($160k) → an early cohort depletes during
    the GI-free bridge (ages 50-59, before the first pension at 60). Once the
    portfolio is gone with GI == 0, the realized spend is exactly $0 — the
    realized-spend correctness fix (pre-fix it recorded the floor through the
    depletion).
    """
    scenario = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_GUARDRAILS,
        initial_portfolio=400_000.0,
        annual_spending=160_000.0,
    )
    start_years, cohort_returns = cohort_return_sequences(scenario)
    found = False
    for idx in range(len(start_years)):
        ends, depleted_age, spends = _simulate_cohort(
            scenario, cohort_returns[idx]
        )
        if depleted_age is not None and depleted_age < 59:  # depleted in the bridge
            found = True
            # The years strictly after depletion (still in the bridge, GI == 0) are
            # fully depleted with no income → recorded spend exactly $0.
            for age in range(depleted_age + 1, 60):
                k = age - scenario.start_age
                assert ends[k] == 0.0
                assert spends[k] == 0.0
            break
    assert found, "expected at least one cohort to deplete during the GI-free bridge"


def test_default_historical_canary_unchanged_by_strategy_refactor():
    """The strategy refactor did not move the default Historical canary.

    110 cohorts, testable-since 1871 — byte-stable structurally through the
    SpendingStrategy generalization (the per-year loop now goes through the
    PureAgeStrategy wrapper for constant, which is identical to spending_for_age).
    The default at $110k constant-mode shows a historical tail (17 cohorts
    deplete, earliest at age 63) — by design, since this default is meant to
    showcase two-sided GK flex; guardrails (off in constant) would cut toward the
    floor in those cohorts. Cohort/year are structural; depletion count is the
    canonical value at $110k.
    """
    result = simulate_historical(default_scenario())
    assert result.cohort_count == 110
    assert result.start_year == 1871
    assert result.depletion_count == 17
    assert result.earliest_depletion_age == 63


# ---------- comparison-window alignment (option 3) ----------

def _it_treasury_scenario(equity=0.6, it=0.4):
    """A 60/40 with INTERMEDIATE TREASURY (it_treasury, data starts 1954) for bonds.

    Native testable_since = 1954 (it_treasury is the shortest-history selected
    series). Used to exercise the comparison-window floor against the default 60/40
    (us_equity/us_bonds, both 1871+). it_treasury data ends 2024, so over the
    46-year horizon the last valid start is 1979 → 26 cohorts (1954..1979).
    """
    return replace(
        default_scenario(),
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", equity, 0.07),
            AssetClass("it_treasury", "Intermediate Treasury", it, 0.02),
        ),
    )


def test_apply_window_floor_restricts_to_floor_year():
    """The cohort-start floor drops every start earlier than it, keeps the rest.

    Floor=1954 over 1871..1979 keeps 1954..1979 = 26 cohorts. None (single runs)
    is the identity. A floor at/below the earliest start is a no-op (never expands).
    """
    starts = list(range(1871, 1980))  # 1871..1979 inclusive
    floored = _apply_window_floor(starts, 1954)
    assert floored[0] == 1954
    assert floored[-1] == 1979
    assert len(floored) == 26
    # None → identity (single, non-comparison runs unchanged).
    assert _apply_window_floor(starts, None) == starts
    # Floor at/below the earliest start is a no-op (only narrows, never expands).
    assert _apply_window_floor(starts, 1800) == starts
    assert _apply_window_floor(starts, 1871) == starts


def test_apply_window_floor_above_all_starts_raises():
    """A floor past every valid start removes all cohorts → ValueError (→ 422)."""
    with pytest.raises(ValueError, match="window_start_year"):
        _apply_window_floor([1871, 1900, 1950], 2000)


def test_window_floor_restricts_cohorts_to_floor():
    """simulate_historical(window_start_year=1954) scores only 1954+ cohorts.

    The default 60/40 (native 1871, 110 cohorts) clamped to 1954 starts its
    earliest cohort at 1954 and reports testable_since=1954. Its native data runs
    to 2025, so the last valid start is 1980 → 27 cohorts (1954..1980). The clamp
    aligns the START year (the dominant, misleading bias); each scenario keeps its
    own data tail.
    """
    clamped = simulate_historical(default_scenario(), window_start_year=1954)
    assert clamped.testable_since == 1954
    assert clamped.start_year == 1954
    assert clamped.cohort_count == 27  # 1954..1980 inclusive (us_bonds data → 2025)


def test_comparison_1871_vs_1954_scores_both_since_1954():
    """A 1871 scenario vs a 1954 scenario: clamping BOTH to 1954 aligns their start.

    The misleading default: the it_treasury 60/40 (native 1954, 26 cohorts) scored
    only on the brutal post-1954 cohorts while the default 60/40 (native 1871, 110
    cohorts) is scored on all of history. The common floor = max(1871, 1954) = 1954;
    requesting BOTH with window_start_year=1954 makes them both start at 1954, so the
    head-to-head is measured from the same earliest cohort.
    """
    it_native = simulate_historical(_it_treasury_scenario())
    default_native = simulate_historical(default_scenario())
    # Native windows DIFFER — this is the misleading state the clamp fixes.
    assert it_native.testable_since == 1954
    assert default_native.testable_since == 1871

    floor = max(it_native.testable_since, default_native.testable_since)
    assert floor == 1954

    it_clamped = simulate_historical(_it_treasury_scenario(), window_start_year=floor)
    default_clamped = simulate_historical(default_scenario(), window_start_year=floor)
    # BOTH now start their earliest cohort at the common floor.
    assert it_clamped.testable_since == 1954
    assert default_clamped.testable_since == 1954
    # The it_treasury side is already at its native window (floor == native), so the
    # clamp is a no-op there — its cohort set is unchanged.
    assert it_clamped.cohort_count == it_native.cohort_count == 26
    # The default 60/40 is genuinely narrowed: from 110 (1871+) down to its 1954+ set.
    assert default_clamped.cohort_count < default_native.cohort_count
    assert default_clamped.start_year == 1954


def test_same_window_comparison_floor_is_noop():
    """Two scenarios with the SAME native window: the floor is a no-op (unchanged).

    The web layer only clamps when the native windows DIFFER (commonComparisonFloor
    returns null on a tie), but even if the common floor (== the shared native
    testable_since) is passed, it must be a no-op: same cohorts, same fan. Here both
    sides are the default 60/40 (native 1871), common floor 1871.
    """
    native = simulate_historical(default_scenario())
    floored = simulate_historical(default_scenario(), window_start_year=native.testable_since)
    assert floored.cohort_count == native.cohort_count == 110
    assert floored.start_year == native.start_year == 1871
    # Byte-identical fan — the floor at the native start changes nothing.
    assert floored.median == native.median
    assert floored.p10 == native.p10
    assert floored.p90 == native.p90


def test_window_floor_none_is_byte_identical_canary():
    """window_start_year=None leaves the default Historical run byte-for-byte.

    Single (non-comparison) runs must be unchanged by the additive floor param —
    the canary regression anchor for option 3.
    """
    native = simulate_historical(default_scenario())
    explicit_none = simulate_historical(default_scenario(), window_start_year=None)
    assert explicit_none.cohort_count == native.cohort_count == 110
    assert explicit_none.start_year == native.start_year == 1871
    assert explicit_none.median == native.median
    assert explicit_none.depletion_count == native.depletion_count


def test_window_floor_applies_in_buckets_mode():
    """The floor narrows a BUCKETS-mode Historical run too (the design's case).

    The user hit this with a buckets scenario vs a single 60/40. A bucket scenario
    using a short-history class is clamped exactly like the single path.
    """
    from firebridgelab.engine.scenario import default_bucket_config

    bsc = replace(
        default_scenario(),
        sourcing_mode="buckets",
        buckets=default_bucket_config(),
    )
    native = simulate_historical(bsc)
    clamped = simulate_historical(bsc, window_start_year=native.start_year + 10)
    assert clamped.start_year == native.start_year + 10
    assert clamped.cohort_count < native.cohort_count
    # Buffer-survival readout still attaches (the buckets payoff metric).
    assert clamped.buffer_survival is not None
