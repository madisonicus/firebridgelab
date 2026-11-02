"""Historical-sequence simulation (Historical mode): ``simulate_historical``.

Where Deterministic mode compounds one constant return, Historical mode replays
actual history: it runs the plan over **every** historical cohort (start year) with
a full N-year window, drawing that cohort's year-by-year real returns, then
aggregates across cohorts to the per-age percentile fan (p10/p25/p50/p75/p90) and
depletion stats. This makes the sequence-of-returns risk of an allocation visible.
The per-year mechanic is the shared ``engine.sim_core._simulate_cohort`` (or
``engine.buckets.simulate_buckets_cohort``); window selection lives in
``engine.cohort_windows`` and aggregation in ``engine.cohort_aggregate``.

Series mapping and renormalization
----------------------------------
Each ``AssetClass`` maps to a historical series by its class id
(``returns.series.SERIES_FOR_CLASS``). A cohort's blended return reads weights
**only** from classes that map to a loaded series and **renormalizes** them to sum
to 1, so an unmapped class can never silently leak weight the series can't fill.
If no class maps, that's an error. Mixes that select any non-Shiller series use
the no-backfill common-overlap window (see ``engine.cohort_windows``), and the
result reports the earliest valid start as ``testable_since``.

Calendar-phase reconciliation
-----------------------------
The loaded series sit on three sub-annual phases, all labeled by an integer
calendar year y (see ``returns.data_build``):

  * Jan(y)→Jan(y+1): the Shiller pair (``us_equity`` / ``us_bonds``) and
    ``it_treasury`` (built from January 5-yr yields).
  * Jan(y)→Dec(y) calendar-year: the Damodaran/French series
    (``st_treasury``, ``lt_treasury``, ``us_small_value``).
  * Dec(y-1)→Dec(y): ``us_reit`` (Nareit December index levels).

Convention: **align all series by their shared integer year label** and blend
cell-by-cell. The residual is at most a ~6-month phase offset between the
Jan-anchored and the Dec/calendar-anchored series. We accept it rather than
re-fetch sub-annual data because the engine is annual by construction and the
free sources are published annually, and because the offset shifts which
12-month window a single cell measures, not the count or ordering of cells, so it
cannot accumulate along a trajectory. It only affects portfolios that mix a
Jan-anchored with a Dec-anchored class. Do NOT chase it as an off-by-one.

Horizon convention
------------------
The horizon is ``end_age - start_age + 1`` annual returns — one return per
age-point, inclusive of both endpoints — matching the deterministic engine's
age-points (default 50→95 = 46 return-years). A classic "30-year retirement" SWR
study counts ``end_age - start_age = 30`` return-years, so our cohort counts read
one higher than such a table for the same span; that is not an off-by-one.
"""

from __future__ import annotations

import logging

from firebridgelab.engine.allocation import ALLOCATION_MODE_GLIDEPATH
from firebridgelab.engine.cohort_aggregate import (
    HistoricalResult,
    _aggregate_cohorts,
    _events_of,
    _select_worst_bucket_cohort,
    _split_aggregates,
    _split_cohort_step,
)
from firebridgelab.engine.cohort_windows import (
    NoReplayableWindow,
    _apply_window_floor,
    _bucket_window,
    _cohort_blended_returns,
    _glidepath_cohort_blended_returns,
    _glidepath_window,
    _is_legacy_shiller_only,
    _multiclass_window,
    _period_from_series,
)
from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS, Scenario
from firebridgelab.engine.sim_core import _simulate_cohort, compute_buffer_survival
from firebridgelab.returns.series import HIST_SERIES_REGISTRY, renormalized_weights
from firebridgelab.returns.shiller import ShillerData, load_shiller
from firebridgelab.spending.base import AdjustmentEvent
from firebridgelab.spending.registry import make_strategy
from firebridgelab.tax.location import has_tax_split
from firebridgelab.tax.location_path import TaxLocationPath, year_tax_context_table


def _simulate_historical_glidepath(
    scenario: Scenario, window_start_year: int | None = None
) -> HistoricalResult:
    """Glidepath cohort replay — per-phase weights over the union-series window.

    The glidepath analogue of ``_simulate_historical_multiclass``: resolve the
    union-of-phases series window (``_glidepath_window``), then replay each cohort
    blending the per-series returns with the PHASE-VARYING weights
    (``_glidepath_cohort_blended_returns``) instead of one constant weight set.
    Everything downstream (the per-cohort sim, the fan/depletion aggregation) is
    the SAME shared machinery the constant paths use, so the spending fan +
    depletion stats can't drift. Reduces to the constant Historical result
    exactly when every phase shares the single allocation (the degenerate-glide
    identity).
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    selected_series, start_years, series_data = _glidepath_window(scenario)
    start_years = _apply_window_floor(start_years, window_start_year)

    cohort_trajectories: list[list[float]] = []
    cohort_spends: list[list[float]] = []
    cohort_events: list[list[AdjustmentEvent]] = []
    depletion_ages: list[int] = []
    split_on = has_tax_split(scenario)
    tax_paths: list[TaxLocationPath] = []
    pretax_terminals: list[float] = []
    dragged_terminals: list[float] = []
    pretax_success: list[bool] = []
    dragged_success: list[bool] = []
    # Precompute the per-age (penalty_free, n_65_plus, pension_ss_ordinary)
    # table ONCE per scenario (none of the three depend on the cohort's return
    # path) instead of every cohort recomputing it — see
    # tax.location_path.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = _period_from_series(series_data, selected_series, start_year, n_steps)
        returns = _glidepath_cohort_blended_returns(
            scenario, period, HIST_SERIES_REGISTRY
        )
        if split_on:
            # With a tax split, the dragged (after-tax) decomposition drives the
            # fan / spend fan / events / depletion; the scalar pass runs inside
            # this helper only as the pre-tax counterfactual.
            _split_cohort_step(
                scenario, returns, tax_paths, cohort_trajectories, cohort_spends,
                cohort_events, depletion_ages, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success, year_context,
            )
        else:
            strategy = make_strategy(scenario)
            ends, depleted_age, spends = _simulate_cohort(scenario, returns, strategy)
            cohort_trajectories.append(ends)
            cohort_spends.append(spends)
            cohort_events.append(_events_of(strategy))
            if depleted_age is not None:
                depletion_ages.append(depleted_age)

    result = _aggregate_cohorts(
        scenario, start_years, cohort_trajectories, depletion_ages,
        cohort_spends, cohort_events,
    )
    if split_on:
        from dataclasses import replace as _replace
        result = _replace(
            result,
            **_split_aggregates(
                scenario, tax_paths, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success,
            ),
        )
    return result


def _simulate_historical_buckets(
    scenario: Scenario, window_start_year: int | None = None
) -> HistoricalResult:
    """3-bucket cohort replay over the no-backfill common-overlap window.

    Replay the plan over every historical cohort, sourcing cash-first with the
    one-sided refill (``engine.buckets.simulate_buckets_cohort``).
    Each bucket's per-year real return is its allocation's cohort blend — the SAME
    ``_cohort_blended_returns`` the single-balance path uses, applied per bucket
    over the union-series window. Aggregates to the portfolio fan + depletion
    (the shared ``_aggregate_cohorts``, so the fan can't drift) and attaches the
    buffer-survival readout (the per-cohort first-forced-growth-sale ages).

    Spending is constant / lifecycle only (the dynamic modes are fenced), so the
    cohort spends are identical across cohorts and the spending fan stays None —
    the bucket value is the buffer-survival metric, not a spend fan.
    """
    from dataclasses import replace

    from firebridgelab.engine.buckets import simulate_buckets_cohort

    n_steps = scenario.end_age - scenario.start_age + 1
    bc = scenario.buckets
    bucket_weights, selected_series, start_years, series_data = _bucket_window(scenario)
    start_years = _apply_window_floor(start_years, window_start_year)

    cohort_trajectories: list[list[float]] = []
    depletion_ages: list[int] = []
    first_forced_sale_ages: list[int | None] = []
    cohort_bucket_trajs: list = []  # the full BucketTrajectory per cohort (for the worst-cohort drill-down)
    for start_year in start_years:
        period = _period_from_series(series_data, selected_series, start_year, n_steps)
        # Each bucket's per-year blended real return = its renormalized weights over
        # the same per-series period (the SAME blend the single path uses).
        bucket_returns = {
            bucket.id: _cohort_blended_returns(bucket_weights[bucket.id], period)
            for bucket in (bc.cash, bc.income, bc.growth)
        }
        traj = simulate_buckets_cohort(scenario, bucket_returns)
        cohort_trajectories.append([y.cash + y.income + y.growth for y in traj.years])
        if traj.depleted_age is not None:
            depletion_ages.append(traj.depleted_age)
        first_forced_sale_ages.append(traj.first_forced_sale_age)
        cohort_bucket_trajs.append(traj)

    result = _aggregate_cohorts(
        scenario, start_years, cohort_trajectories, depletion_ages,
    )
    return replace(
        result,
        buffer_survival=compute_buffer_survival(first_forced_sale_ages),
        worst_bucket_cohort=_select_worst_bucket_cohort(
            scenario, start_years, cohort_bucket_trajs
        ),
    )


def simulate_historical(
    scenario: Scenario,
    data: ShillerData | None = None,
    window_start_year: int | None = None,
) -> HistoricalResult:
    """Run the plan over every historical cohort and aggregate.

    Horizon spans ages ``start_age .. end_age`` inclusive, so each cohort needs
    that many years of returns (``end_age - start_age + 1``). Aggregates to the
    per-age **percentile fan** and depletion stats across cohorts.

    Two paths, same math:

    - **Legacy Shiller path** (``_is_legacy_shiller_only``): when the only mapped
      classes are the Shiller pair, use the ``ShillerData`` cohort path — both
      series are 1871+, so the window policy is a no-op.
    - **Multi-class path**: any non-Shiller class in the mix routes here. It
      applies the window policy — selected = weight > ε, no-backfill common-
      overlap window, hide/renormalize unmapped — and replays cohorts over the
      restricted window, surfacing the earliest valid start (``testable_since``).

    Note: the ``data`` arg (injected ``ShillerData``) ONLY applies to the legacy
    Shiller-only path. The multi-class path always reads
    ``historical_series_by_id()`` and ignores ``data`` — injecting it will not
    reach the multi-class blend.

    Glidepath: when ``allocation_mode == "glidepath"`` the blend
    weights vary by phase, so it ALWAYS routes through the dedicated glidepath path
    (``_simulate_historical_glidepath``) — which reads the year-keyed accessor and
    blends per-phase. The year-keyed accessor returns exactly ``ShillerData``'s values
    for the Shiller pair, so a degenerate glide (every phase = the single
    allocation) reproduces the constant Historical result exactly.

    Buckets: when ``sourcing_mode == "buckets"`` the run routes to the dedicated
    3-bucket cohort path (``_simulate_historical_buckets``) — the portfolio fan +
    depletion stats use the SAME aggregator, plus the buffer-survival readout. The
    bucket allocations supersede the single/glidepath allocation (glidepath is
    ignored in buckets mode), so the bucket branch is checked FIRST.

    Comparison-window alignment (``window_start_year``): an optional COHORT
    START-YEAR FLOOR. When set, every path drops cohort starts earlier than it
    (``_apply_window_floor``) so an A/B comparison can score two scenarios on the
    SAME cohort set (the common = latest testable window). ``None`` (single,
    non-comparison runs) keeps each scenario's native window. The floor only
    NARROWS (it can never expand a window past the data).
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        # The tax-location split is SINGLE-mode only. If a split is ALSO supplied
        # here it is intentionally ignored (the buckets path never decomposes by
        # tax location). Logged (not silently ignored) so the mode interaction is
        # visible.
        if has_tax_split(scenario):
            logging.getLogger(__name__).info(
                "tax-location split supplied with sourcing_mode=buckets; "
                "ignoring the split (tb1 is single-mode only — the tax-location × "
                "cash/income/growth product is a later card)"
            )
        return _simulate_historical_buckets(scenario, window_start_year)

    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        return _simulate_historical_glidepath(scenario, window_start_year)

    if not _is_legacy_shiller_only(scenario):
        return _simulate_historical_multiclass(scenario, window_start_year)

    if data is None:
        data = load_shiller()

    n_steps = scenario.end_age - scenario.start_age + 1
    weights = renormalized_weights(scenario)
    start_years = data.all_starting_years(n_steps)
    if not start_years:
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window in the historical data "
            f"(data spans {data.first_year}-{data.last_year})"
        )
    start_years = _apply_window_floor(start_years, window_start_year)

    # cohorts[c][k] = end-of-year portfolio for cohort c at age step k;
    # cohort_spends[c][k] = the spend WITHDRAWN at that age (feeds the spend fan);
    # cohort_events[c] = that cohort's guardrail AdjustmentEvent log.
    cohort_trajectories: list[list[float]] = []
    cohort_spends: list[list[float]] = []
    cohort_events: list[list[AdjustmentEvent]] = []
    depletion_ages: list[int] = []
    # Tax-location shadow decomposition: collect one path
    # per cohort when a split is supplied, then aggregate to the risk metric. No-op
    # (the list stays empty → tax_location_risk None → payload omits it) otherwise.
    split_on = has_tax_split(scenario)
    tax_paths: list[TaxLocationPath] = []
    pretax_terminals: list[float] = []
    dragged_terminals: list[float] = []
    pretax_success: list[bool] = []
    dragged_success: list[bool] = []
    # Precompute the per-age (penalty_free, n_65_plus, pension_ss_ordinary)
    # table ONCE per scenario (none of the three depend on the cohort's return
    # path) instead of every cohort recomputing it — see
    # tax.location_path.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = data.returns_for_period(start_year, n_steps)
        returns = _cohort_blended_returns(weights, period)
        if split_on:
            # With a tax split, the dragged (after-tax) decomposition drives the
            # fan / spend fan / events / depletion; the scalar pass runs inside
            # this helper only as the pre-tax counterfactual.
            _split_cohort_step(
                scenario, returns, tax_paths, cohort_trajectories, cohort_spends,
                cohort_events, depletion_ages, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success, year_context,
            )
        else:
            # Build the strategy explicitly so we can read its per-year adjustment
            # events after the run (guardrails only). _simulate_cohort would build an
            # identical one internally for None — passing it in changes no math.
            strategy = make_strategy(scenario)
            ends, depleted_age, spends = _simulate_cohort(scenario, returns, strategy)
            cohort_trajectories.append(ends)
            cohort_spends.append(spends)
            cohort_events.append(_events_of(strategy))
            if depleted_age is not None:
                depletion_ages.append(depleted_age)

    # Shared aggregator (same code the multi-class path uses) so the fan + spend
    # fan + depletion math can't drift between the two paths.
    result = _aggregate_cohorts(
        scenario, start_years, cohort_trajectories, depletion_ages,
        cohort_spends, cohort_events,
    )
    if split_on:
        from dataclasses import replace as _replace
        result = _replace(
            result,
            **_split_aggregates(
                scenario, tax_paths, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success,
            ),
        )
    return result


def _simulate_historical_multiclass(
    scenario: Scenario, window_start_year: int | None = None
) -> HistoricalResult:
    """Multi-class cohort replay over the no-backfill common-overlap window.

    Taken whenever a non-Shiller class is in the mix. Steps:

    1. **Hide / renormalize** unmapped classes: ``renormalized_weights`` over
       ``HIST_SERIES_REGISTRY`` drops any class with no loaded series and
       renormalizes the rest to sum to 1.
    2. **Selected = weight > ε** (``selected_series_for``): only series whose
       summed raw weight clears the epsilon constrain the window — a 0%-weight
       mapped class does not.
    3. **Common-overlap window** (``common_overlap_start_years``): valid cohort
       starts = years where EVERY selected series has data for the full horizon.
       No backfill — a short-history class restricts the set honestly.
    4. Replay each cohort over that window (aligning series by integer year label
       — the calendar-phase convention in the module docstring), aggregate to the
       fan + depletion. ``start_year`` is the earliest valid start = the
       "testable since YYYY" label.

    Raises ``ValueError`` if no class maps, no class clears the epsilon, or the
    selected classes share no full-horizon overlap year (an honest "this
    portfolio has no replayable window" — the API routes it to a 422).
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    weights, selected_series, start_years, series_data = _multiclass_window(scenario)
    start_years = _apply_window_floor(start_years, window_start_year)

    # Replay each cohort over the restricted window, blending only the selected
    # series (aligned by integer year label — the calendar-phase convention).
    cohort_trajectories: list[list[float]] = []
    cohort_spends: list[list[float]] = []
    cohort_events: list[list[AdjustmentEvent]] = []
    depletion_ages: list[int] = []
    split_on = has_tax_split(scenario)
    tax_paths: list[TaxLocationPath] = []
    pretax_terminals: list[float] = []
    dragged_terminals: list[float] = []
    pretax_success: list[bool] = []
    dragged_success: list[bool] = []
    # Precompute the per-age (penalty_free, n_65_plus, pension_ss_ordinary)
    # table ONCE per scenario (none of the three depend on the cohort's return
    # path) instead of every cohort recomputing it — see
    # tax.location_path.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = _period_from_series(series_data, selected_series, start_year, n_steps)
        returns = _cohort_blended_returns(weights, period)
        if split_on:
            # With a tax split, the dragged (after-tax) decomposition drives the
            # fan / spend fan / events / depletion; the scalar pass runs inside
            # this helper only as the pre-tax counterfactual.
            _split_cohort_step(
                scenario, returns, tax_paths, cohort_trajectories, cohort_spends,
                cohort_events, depletion_ages, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success, year_context,
            )
        else:
            strategy = make_strategy(scenario)
            ends, depleted_age, spends = _simulate_cohort(scenario, returns, strategy)
            cohort_trajectories.append(ends)
            cohort_spends.append(spends)
            cohort_events.append(_events_of(strategy))
            if depleted_age is not None:
                depletion_ages.append(depleted_age)

    result = _aggregate_cohorts(
        scenario, start_years, cohort_trajectories, depletion_ages,
        cohort_spends, cohort_events,
    )
    if split_on:
        from dataclasses import replace as _replace
        result = _replace(
            result,
            **_split_aggregates(
                scenario, tax_paths, pretax_terminals, dragged_terminals,
                pretax_success, dragged_success,
            ),
        )
    return result
