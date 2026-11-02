"""Aggregate per-cohort Historical runs into a ``HistoricalResult``.

Builds the per-age percentile fans (portfolio, and spend for strategies that
collect spends), depletion stats, the cost-of-flexibility readout, the worst-cohort
drill-downs, and, when a tax-location split is supplied, the split-mode per-cohort
step (dragged decomposition plus pre-tax counterfactual) and its aggregates. Shared
by every ``engine.historical`` path so the readouts can't drift between them.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from firebridgelab.engine.scenario import Scenario
from firebridgelab.engine.sim_core import (
    AdjustmentStats,
    BufferSurvivalStats,
    WorstCohort,
    WorstCohortYear,
    _compute_adjustment_stats,
    _simulate_cohort,
)
from firebridgelab.spending.base import AdjustmentEvent, SpendingStrategy
from firebridgelab.spending.registry import make_strategy
from firebridgelab.tax.location_path import TaxLocationPath, simulate_path_decomposition
from firebridgelab.tax.location_risk import aggregate_estate_cost, aggregate_tax_location_risk
from firebridgelab.tax.overlay import aggregate_tax_overlay, compute_tax_overlay


@dataclass(frozen=True)
class HistoricalResult:
    """Aggregate of a Historical-mode run across all cohorts.

    ``median`` is one ``(age, portfolio)`` point per age in the horizon, the
    median end-of-year portfolio across cohorts at that age. ``depletion_count``
    is how many cohorts reached zero before ``end_age``; ``earliest_depletion_age``
    is the earliest age any cohort hit zero, or ``None`` if none did.

    The **percentile fan**: ``p10``/``p25``/``p75``/``p90`` are
    per-age percentiles of the end-of-year portfolio across cohorts (the median
    is the p50 already carried in ``median``), so at every age
    ``p10 <= p25 <= median <= p75 <= p90``. ``ending_range`` is the
    ``(p10, p90)`` of the FINAL-age portfolio across cohorts — the spread of
    where the plan ends — for the readout.
    """

    cohort_count: int
    start_year: int          # earliest cohort start year used (= testable_since)
    ages: list[int]          # the horizon's ages (start_age .. end_age)
    median: list[float]      # median (p50) end-of-year portfolio at each age
    p10: list[float]         # 10th-percentile portfolio at each age
    p25: list[float]         # 25th-percentile portfolio at each age
    p75: list[float]         # 75th-percentile portfolio at each age
    p90: list[float]         # 90th-percentile portfolio at each age
    ending_range: tuple[float, float]  # (p10, p90) of the final-age portfolio
    depletion_count: int
    earliest_depletion_age: int | None

    # Spending fan. Under constant/lifecycle spending is identical across every
    # cohort, so there is no spending distribution to plot and these stay None
    # (the payload omits the spending fan). Under a dynamic strategy (e.g.
    # ``guardrails``) spending VARIES across cohorts
    # (a bad sequence cuts, a good one raises), so these carry the per-age
    # percentile fan of the WITHDRAWN spend, mirroring the portfolio fan
    # field-for-field: ``spend_*[k]`` is the percentile spend at ``ages[k]``, so at
    # every age ``spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90``.
    spend_median: list[float] | None = None
    spend_p10: list[float] | None = None
    spend_p25: list[float] | None = None
    spend_p75: list[float] | None = None
    spend_p90: list[float] | None = None

    # Adjustment-event visibility. Dynamic strategies only — the
    # cost-of-flexibility readout (``adjustment_stats``) and the worst-cohort
    # drill-down (``worst_cohort``) are None under constant/lifecycle (no
    # adjustments to surface); the payload omits them then.
    adjustment_stats: "AdjustmentStats | None" = None
    worst_cohort: "WorstCohort | None" = None

    # Buffer-survival stats. Buckets sourcing only — how often a sustained
    # drawdown exhausted the cash + income buffer and FORCED a growth sale at a
    # loss, across all cohorts. None under single sourcing (the payload omits them).
    buffer_survival: "BufferSurvivalStats | None" = None

    # Worst-cohort 3-bucket balance series (the bucket-balance visualization for
    # Historical mode). Buckets sourcing only: the
    # per-year cash/income/growth balances of the worst cohort (earliest forced
    # growth sale; see ``WorstBucketCohort``), so the UI can draw "cash drains
    # through the crash while growth rides". None under single sourcing (and under
    # buckets if there were no cohorts).
    worst_bucket_cohort: "WorstBucketCohort | None" = None

    # Tax-location risk. The headline frequency metric:
    # across cohorts, how often does the freely-reachable money run dry before 59½.
    # Populated ONLY when a tax-location split is supplied AND sourcing is single
    # (the shadow decomposition ran); None otherwise, so the payload omits the
    # tax_location_risk block. Held as the ``tax.location_risk.TaxLocationRisk``
    # aggregate.
    tax_location_risk: "object | None" = None

    # Federal tax overlay. The Historical twin of the deterministic per-age series: median annual
    # tax, gross-outflow and forced/voluntary medians across cohorts, plus the
    # median-of-path-scalars readout
    # (avg annual tax / effective rate / worst tax year). Populated under the
    # SAME gate as ``tax_location_risk`` (split supplied + single sourcing) —
    # the overlay rides the shadow decomposition, so it can only exist where
    # that decomposition ran. None otherwise (payload omits the block). Held as
    # the ``tax.overlay.FederalTaxOverlayAggregate``.
    federal_tax: "object | None" = None

    # Estate cost of taxes. Populated under the
    # SAME gate as ``tax_location_risk`` / ``federal_tax``: per cohort, the
    # INDEPENDENT pre-tax counterfactual's terminal balance minus the dragged
    # decomposition's terminal balance ("how much smaller the estate ends
    # because the portfolio paid the IRS"), aggregated as the MEDIAN of
    # per-cohort differences (never a difference of medians) and paired with
    # the dragged-vs-pretax success-rate delta — see
    # ``tax.location_risk.EstateCostOfTaxes``'s docstring for why the pairing
    # matters (a $0 median cost when both worlds deplete would otherwise read
    # as "taxes are free"). None otherwise (payload omits the block). Held as
    # the ``tax.location_risk.EstateCostOfTaxes``.
    estate_cost: "object | None" = None

    @property
    def testable_since(self) -> int:
        """Earliest valid cohort start year — the "testable since YYYY" label.

        Identical to ``start_year`` (the earliest cohort start used); exposed
        under this name so the API payload / UI can read the policy label
        without coupling to the internal field name. For the default 2-class
        portfolio (equity + bonds, both 1871+) this is 1871; a short-history
        class in the mix (e.g. ``us_reit`` 1972+) pushes it later.
        """
        return self.start_year


@dataclass(frozen=True)
class WorstBucketCohort:
    """The worst-case cohort's per-year 3-bucket balances (the bucket-balance
    visualization).

    Shows the worst cohort's bucket dynamics so the "cash drains through the
    crash while growth rides" behaviour is visible. The worst cohort is the one
    whose buffer broke EARLIEST (the smallest first-forced-growth-sale age — the
    same metric the ``worst_forced_sale_age`` headline reports), with a
    deterministic tie-break to
    the lowest terminal portfolio then the earliest start year. When NO cohort ever
    forced a growth sale (full defeasance — the buffer always held), we fall back to
    the lowest-terminal cohort so the chart always has a worst case to show (and the
    caption notes the buffer was never breached).

      - ``start_year``  — the cohort's start year (its label, e.g. 1928).
      - ``ages``        — the horizon ages (start_age .. end_age), x-axis.
      - ``cash`` / ``income`` / ``growth`` — per-age END-of-year bucket balances
        (the three lines the chart draws).
      - ``first_forced_sale_age`` — the age this cohort first sold growth at a
        loss, or None if its buffer held the whole horizon.
      - ``depleted_age`` — the age this cohort hit zero, or None.
    """

    start_year: int
    ages: list[int]
    cash: list[float]
    income: list[float]
    growth: list[float]
    first_forced_sale_age: int | None
    depleted_age: int | None


def _decompose_cohort(
    scenario: Scenario,
    returns: np.ndarray,
    strategy: SpendingStrategy,
    year_context: dict | None = None,
) -> TaxLocationPath:
    """Run the tax-location dragged decomposition for ONE cohort.

    ``strategy`` is built by the CALLER (a fresh instance per cohort) so the
    caller can read the strategy's ``events`` log AFTER the run (``_events_of``),
    as for the non-split path: for split scenarios the portfolio fan ITSELF is
    the dragged total, and this collects the dragged-consistent adjustment-event
    log without a second, possibly-drifting strategy instance. The SAME
    ``returns`` array the pre-tax-counterfactual scalar cohort uses is passed in,
    so the two runs see the identical per-year market path — only their spending
    strategies' REACTIONS can differ (the dragged strategy reads the dragged,
    taxed balance). ``year_context`` is the OPTIONAL precomputed
    ``tax.location_path.year_tax_context_table`` — see that function's docstring.
    """
    return simulate_path_decomposition(
        scenario, returns, strategy, year_context=year_context
    )


def _split_cohort_step(
    scenario: Scenario,
    returns: np.ndarray,
    tax_paths: list[TaxLocationPath],
    cohort_trajectories: list[list[float]],
    cohort_spends: list[list[float]],
    cohort_events: list[list[AdjustmentEvent]],
    depletion_ages: list[int],
    pretax_terminals: list[float],
    dragged_terminals: list[float],
    pretax_success: list[bool],
    dragged_success: list[bool],
    year_context: dict | None = None,
) -> None:
    """One cohort's FULL split-mode step: the dragged fan contribution + the
    estate-cost pairing, in one place so the three Historical split call sites
    (legacy Shiller, multi-class, glidepath) can't drift.

    Appends to every one of the ten collector lists (mutation, mirroring the
    existing per-cohort loop shape at each call site so this drops into their
    ``if split_on:`` body unchanged). Runs TWO INDEPENDENT per-cohort sims over
    the SAME ``returns`` sequence — each is the honest run of its own world:

      1. The DRAGGED decomposition (``_decompose_cohort``, a fresh strategy
         reading the dragged, TAXED balance) — becomes this cohort's
         contribution to the portfolio fan / spend fan / adjustment-event log
         / depletion count (the portfolio fan ITSELF is the dragged total for
         split scenarios). ``tax_paths`` also feeds
         ``aggregate_tax_location_risk`` / ``aggregate_tax_overlay`` downstream.
      2. The INDEPENDENT pre-tax counterfactual (``_simulate_cohort``, a
         SEPARATE fresh strategy reading the UNTAXED balance) — feeds ONLY the
         estate-cost pairing (``aggregate_estate_cost``), never the fan. This
         is deliberately NOT the same-spend ``TaxLocationPath.pretax_total``
         (that series is paired against the DRAGGED run's OWN spend decisions,
         kept for the drag-direction invariant) — a
         dynamic strategy's two independent runs can legitimately spend, and
         therefore deplete, at different times.

    ``year_context`` is the OPTIONAL precomputed per-age
    ``(penalty_free, n_65_plus, pension_ss_ordinary)`` table (see
    ``tax.location_path.year_tax_context_table``): the caller builds it ONCE
    per scenario (outside the cohort loop) and passes it to every cohort, since
    none of the three values depend on the cohort's return path.
    """
    n_steps = len(returns)
    strategy = make_strategy(scenario)
    tax_path = _decompose_cohort(scenario, returns, strategy, year_context)
    tax_paths.append(tax_path)
    dragged_total = [
        tax_path.taxable[i] + tax_path.tax_deferred[i] + tax_path.roth[i]
        for i in range(n_steps)
    ]
    cohort_trajectories.append(dragged_total)
    # The spend fan / worst-cohort drill / adjustment stats read the REALIZED
    # (fundable-capped) consumption, mirroring the non-split loop's
    # realized_spend discipline — recording the strategy's INTENDED spend here
    # would credit a depleted cohort with its full target (e.g. the $85k
    # floor) on a $0 portfolio, overstating after-tax consumption and
    # inverting the worst-cohort ranking (see TaxLocationPath.realized_spend).
    cohort_spends.append(list(tax_path.realized_spend))
    cohort_events.append(_events_of(strategy))
    if tax_path.dragged_depleted_age is not None:
        depletion_ages.append(tax_path.dragged_depleted_age)

    pretax_strategy = make_strategy(scenario)
    pretax_ends, pretax_depleted_age, _pretax_spends = _simulate_cohort(
        scenario, returns, pretax_strategy
    )
    pretax_terminals.append(pretax_ends[-1])
    dragged_terminals.append(dragged_total[-1])
    pretax_success.append(pretax_depleted_age is None)
    dragged_success.append(tax_path.dragged_depleted_age is None)


def _split_aggregates(
    scenario: Scenario,
    tax_paths: list[TaxLocationPath],
    pretax_terminals: list[float],
    dragged_terminals: list[float],
    pretax_success: list[bool],
    dragged_success: list[bool],
) -> dict:
    """The three split-mode aggregate blocks, built off the SAME ``tax_paths`` /
    pretax-pairing lists ``_split_cohort_step`` collected — a ``dataclasses.
    replace(result, **_split_aggregates(...))`` one-liner at each of the three
    Historical split call sites, so they can't drift from each other.
    """
    federal_tax_paths = [compute_tax_overlay(scenario, p) for p in tax_paths]
    return {
        "tax_location_risk": aggregate_tax_location_risk(tax_paths),
        "federal_tax": aggregate_tax_overlay(federal_tax_paths),
        "estate_cost": aggregate_estate_cost(
            pretax_terminals, dragged_terminals, pretax_success, dragged_success
        ),
    }


def _select_worst_bucket_cohort(
    scenario: Scenario,
    start_years: list[int],
    trajectories: list,
) -> "WorstBucketCohort | None":
    """Pick the worst cohort + extract its 3-bucket balance series (the drill-down).

    "Worst" = the cohort whose buffer broke EARLIEST (smallest first-forced-growth-sale age —
    the same signal ``worst_forced_sale_age`` reports), with a deterministic
    tie-break: lower terminal portfolio, then earlier start year. When NO cohort
    ever forced a sale (full defeasance), fall back to the LOWEST-terminal cohort so
    the chart always has a worst case to render (the buffer-never-breached caption
    covers that case). Returns None only when there are no cohorts at all.

    ``trajectories[i]`` is cohort ``i``'s ``BucketTrajectory`` (aligned to
    ``start_years``). The selected cohort's per-age cash/income/growth balances are
    sliced straight off its ``BucketYear`` list — the SAME end-of-year balances the
    aggregate fan summed, so the drill-down can't drift from the fan.
    """
    if not trajectories:
        return None
    ages = list(range(scenario.start_age, scenario.end_age + 1))

    # Sort key per cohort (ascending = worse first):
    #   forced sale earlier is worse → use the first-forced-sale age, with None
    #   (never forced) sorted LAST (a large sentinel); ties broken by lower terminal
    #   portfolio, then earlier start year.
    SENTINEL = scenario.end_age + 1
    def _key(i: int):
        ffs = trajectories[i].first_forced_sale_age
        forced_age = ffs if ffs is not None else SENTINEL
        return (forced_age, trajectories[i].final_portfolio, start_years[i])

    worst_idx = min(range(len(trajectories)), key=_key)
    traj = trajectories[worst_idx]
    return WorstBucketCohort(
        start_year=start_years[worst_idx],
        ages=ages,
        cash=[y.cash for y in traj.years],
        income=[y.income for y in traj.years],
        growth=[y.growth for y in traj.years],
        first_forced_sale_age=traj.first_forced_sale_age,
        depleted_age=traj.depleted_age,
    )


def _events_of(strategy: SpendingStrategy) -> list[AdjustmentEvent]:
    """The strategy's adjustment-event log, or [] for a non-guardrail strategy.

    Strategies that adjust spending (e.g. ``GuardrailStrategy``) record events;
    the pure-age wrapper for constant/lifecycle has none, so this returns an
    empty list there. Keeps the cohort loops uniform — they always collect a
    list, and ``_aggregate_cohorts`` simply finds it empty under constant/lifecycle.
    """
    return list(getattr(strategy, "events", []))


def _gi_step_label(action: str, gi_driven: bool) -> str:
    """Human action tag for a worst-cohort year (GI-driven raises labeled as such).

    A raise in a year where GI stepped up is GI-driven (the portfolio-funded rate
    dropped because a pension / SS stream landed) — NOT market outperformance, so
    it reads "raise — guaranteed income started". A raise in a non-GI-step year is
    "raise — strong returns". Cuts and holds read plainly.
    """
    if action == "raise":
        return "raise — guaranteed income started" if gi_driven else "raise — strong returns"
    if action == "cut":
        return "cut — portfolio under pressure"
    return "hold"


def _compute_worst_cohort(
    start_years: list[int],
    cohort_spends: list[list[float]],
    cohort_events: list[list[AdjustmentEvent]],
    depletion_ages_by_index: dict[int, int],
) -> WorstCohort:
    """Pick + drill the most-squeezed cohort (LOWEST TOTAL real spend).

    Selection key = the cohort's summed real spend over the horizon (the
    most-squeezed retirement). Deterministic tie-break: the EARLIEST start year
    (cohorts are ascending, so the first index with the min total wins). The
    drill-down path is the cohort's AdjustmentEvent log mapped to display-ready
    ``WorstCohortYear``s (dollars + a human label, GI-driven raises distinguished).
    """
    totals = [sum(spends) for spends in cohort_spends]
    # argmin with earliest-start tie-break: enumerate keeps the first (lowest
    # index = earliest start year) on ties.
    worst_idx = min(range(len(totals)), key=lambda i: (totals[i], i))

    events = cohort_events[worst_idx]
    realized = cohort_spends[worst_idx]
    # The drill path's action LABELS stay the strategy's decisions (cut/raise/hold
    # off its carried intent), but the dollar ``spend`` shown is the REALIZED spend
    # (``cohort_spends``, already capped at what was fundable in _simulate_cohort) —
    # so a depleting cohort's post-depletion years read the affordable spend (≈ GI),
    # not the strategy's clamped intent. ``events`` and ``realized`` are both per-age
    # in age order (one event per spend_for_year call), so index k aligns.
    path = [
        WorstCohortYear(
            age=ev.age,
            action=ev.action,
            gi_driven=(ev.action == "raise" and ev.gi_stepped_up),
            spend=realized[k],
            begin_portfolio=ev.begin_portfolio,
            gi=ev.gi,
            label=_gi_step_label(ev.action, ev.gi_stepped_up),
        )
        for k, ev in enumerate(events)
    ]
    return WorstCohort(
        start_year=start_years[worst_idx],
        total_spend=totals[worst_idx],
        min_spend=min(cohort_spends[worst_idx]),
        depleted_age=depletion_ages_by_index.get(worst_idx),
        path=path,
    )


def _aggregate_cohorts(
    scenario: Scenario,
    start_years: list[int],
    cohort_trajectories: list[list[float]],
    depletion_ages: list[int],
    cohort_spends: list[list[float]] | None = None,
    cohort_events: list[list[AdjustmentEvent]] | None = None,
) -> HistoricalResult:
    """Build the HistoricalResult from per-cohort trajectories (shared aggregator).

    The percentile-fan + depletion aggregation common to the legacy Shiller path
    and the multi-class path, so the two can't drift. ``start_years`` is the
    ascending list of valid cohort starts (``start_years[0]`` is the earliest =
    ``testable_since``); ``cohort_trajectories[c][k]`` is cohort c's end-of-year
    portfolio at age step k; ``depletion_ages`` is the per-cohort first-depletion
    ages (only for cohorts that depleted).

    ``cohort_spends`` is the parallel per-cohort WITHDRAWN-spend matrix. The
    spending fan is built ONLY for strategies that collect spends
    (``collects_spends``; spending varies across cohorts); under constant/lifecycle
    spending is identical across cohorts, so a fan would be a flat degenerate band
    — we leave the spend fields None and the payload omits the spending fan.

    ``cohort_events`` is the parallel per-cohort AdjustmentEvent log.
    For those strategies it drives the aggregate ``adjustment_stats`` + the
    ``worst_cohort`` drill-down; under constant/lifecycle it's empty and both
    stay None (the payload omits them).
    """
    matrix = np.array(cohort_trajectories, dtype=float)  # shape (cohorts, n_steps)
    PERCENTILES = (10, 25, 50, 75, 90)
    pctiles = np.percentile(matrix, PERCENTILES, axis=0)  # shape (5, n_steps)
    p10, p25, median, p75, p90 = (row.tolist() for row in pctiles)
    ages = list(range(scenario.start_age, scenario.end_age + 1))
    ending_range = (p10[-1], p90[-1])

    # Spending fan — collecting strategies only. Same percentile machinery as the
    # portfolio fan, over the (cohorts, n_steps) spend matrix, so the two fans are
    # built identically and stay monotone (spend_p10 <= ... <= spend_p90 per age).
    spend_fan: dict[str, list[float] | None] = {
        "spend_median": None,
        "spend_p10": None,
        "spend_p25": None,
        "spend_p75": None,
        "spend_p90": None,
    }
    adjustment_stats: AdjustmentStats | None = None
    worst_cohort: WorstCohort | None = None
    # Strategy-level predicate: collect the spend fan + adjustment stats for any
    # strategy whose ``collects_spends`` is set. ``make_strategy`` is cheap (no
    # per-cohort cost); built once here.
    if make_strategy(scenario).collects_spends and cohort_spends:
        spend_matrix = np.array(cohort_spends, dtype=float)  # (cohorts, n_steps)
        s_pctiles = np.percentile(spend_matrix, PERCENTILES, axis=0)  # (5, n_steps)
        s_p10, s_p25, s_median, s_p75, s_p90 = (row.tolist() for row in s_pctiles)
        spend_fan = {
            "spend_median": s_median,
            "spend_p10": s_p10,
            "spend_p25": s_p25,
            "spend_p75": s_p75,
            "spend_p90": s_p90,
        }

        # Adjustment-event visibility. Per-cohort depletion-age map for
        # the worst-cohort drill-down: the first age step whose end-of-year balance
        # is 0 → its age (start_age + k). Recovered from the trajectory matrix so
        # the loops don't need to thread per-index depletion separately.
        events = cohort_events or [[] for _ in cohort_spends]
        depletion_by_index: dict[int, int] = {}
        for c, traj in enumerate(cohort_trajectories):
            for k, end in enumerate(traj):
                if end <= 0.0:
                    depletion_by_index[c] = scenario.start_age + k
                    break
        adjustment_stats = _compute_adjustment_stats(scenario, cohort_spends, events)
        worst_cohort = _compute_worst_cohort(
            start_years, cohort_spends, events, depletion_by_index
        )

    return HistoricalResult(
        cohort_count=len(start_years),
        start_year=start_years[0],
        ages=ages,
        median=median,
        p10=p10,
        p25=p25,
        p75=p75,
        p90=p90,
        ending_range=ending_range,
        depletion_count=len(depletion_ages),
        earliest_depletion_age=min(depletion_ages) if depletion_ages else None,
        adjustment_stats=adjustment_stats,
        worst_cohort=worst_cohort,
        **spend_fan,
    )
