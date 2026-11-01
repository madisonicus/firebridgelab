"""Historical-sequence simulation (Historical mode).

Where Deterministic mode compounds one constant blended real return, Historical
mode replays actual history: it runs the plan over **every** historical cohort
(start year) with a full N-year window, drawing that cohort's year-by-year real
returns, then aggregates across cohorts. This is what makes the
sequence-of-returns / volatility risk of an allocation visible — the point of
the feature (see the design notes).

Build 1 reported the **median** portfolio trajectory by age plus descriptive
**depletion** stats (how many cohorts hit zero, and the earliest age). Build 2
adds the **percentile fan**: per-age p10/p25/p75/p90 alongside the median (=p50),
plus the (p10, p90) ending range. No comparison yet (Build 3).

Series mapping + renormalization (Decision 3)
---------------------------------------------
A small registry (``SERIES_REGISTRY``) lists the historical series we have, v1:
``us_equity`` (S&P real total return) and ``us_bonds`` (10yr Treasury real total
return). Each ``AssetClass`` maps to a series id by its **class id** via the
class-id → series-id map ``SERIES_FOR_CLASS`` (asset-class design: the two id
namespaces — class id is the allocation key, series id is the data key; they
differ for the legacy pair, where class ``us_total_market`` binds series
``us_equity`` and class ``us_agg_bonds`` binds series ``us_bonds``).

When building a cohort's blended return, we read weights **only** from classes
that map to a series we have, and **renormalize those weights to sum to 1** at
computation time. In v1 both default classes map and already sum to 100%, so
this is a no-op — but it's a cheap correctness invariant: a future unmapped
class (e.g. "crypto 5%") can never silently corrupt a Historical result by
leaking weight the series can't fill. If no class maps, that's an error.

Everything else matches Deterministic mode exactly: the midpoint withdrawal
convention, the four per-person GI streams, constant-real spending, and the
portfolio floored at zero (depletion = flatline at zero thereafter).

Multi-class cohort replay + window policy (slice 3, D3)
-------------------------------------------------------
Slice 1 staged the new asset-class annual REAL series; slice 2 used them in
Monte Carlo. Slice 3 makes **Historical cohort replay** work across the loaded
multi-class menu (the seven classes that have real series today), implementing
the no-backfill common-overlap window policy (asset-class design D3):

  - **Selected = weight > ε** (``HISTORICAL_SELECTION_EPSILON``). A 0%-weight
    (but mapped) class does NOT constrain the window — otherwise a preset that
    lists many classes with some at 0% would silently collapse every window to
    the shortest history.
  - **No-backfill common-overlap window.** The valid cohort start years are
    exactly the years Y where EVERY *selected* class has real data for the FULL
    horizon (Y .. Y+horizon-1). A short-history class (e.g. ``us_reit`` 1972+)
    sharply restricts the valid cohorts — that's correct and honest; we never
    fabricate / backfill the missing pre-inception years.
  - **"testable since YYYY".** The result payload surfaces the earliest valid
    cohort start year (``start_year`` / its alias ``testable_since``) and the
    cohort count, so the UI can display "testable since YYYY".
  - **Hide / renormalize.** A class with NO loaded series at all (the five
    not-yet-loaded: intl / EM / TIPS / gold / commodities) is still hidden and
    the remaining weights renormalized (the existing rule). A *loaded-but-short-
    history* class is NOT hidden — it constrains the window instead.

Calendar-phase reconciliation (slice-3 carry-forward)
-----------------------------------------------------
The loaded series sit on three sub-annual phases, all labeled by an integer
calendar year y (see ``firebridgelab/data_build.py``):

  * Jan(y)→Jan(y+1): the Shiller pair (``us_equity`` / ``us_bonds``) and
    ``it_treasury`` (built from January 5-yr yields).
  * Jan(y)→Dec(y) calendar-year: the Damodaran/French series
    (``st_treasury``, ``lt_treasury``, ``us_small_value``).
  * Dec(y-1)→Dec(y): ``us_reit`` (Nareit December index levels).

Convention chosen: **align all series to one annual phase by their shared
integer year label** — i.e. every series' "year y" return is the cohort's
year-y return, blended cell-by-cell. The residual is at most a ~6-month phase
offset between the Jan-anchored and the Dec/calendar-anchored series. We accept
and label that residual rather than re-fetch sub-annual data, because:

  1. The engine is annual by construction (D1) — there is no monthly axis to
     re-phase onto, and the free sources are only published annually.
  2. The offset is a *fraction of one* of the ~42 compounding years in a
     cohort, so it cannot accumulate or drift a trajectory (it shifts which
     12-month window a single cell measures, not the count or ordering of
     cells). Slice-1 per-series stats are unaffected — each series is internally
     self-consistent; only the cross-class *blend* of a single year's cell mixes
     the phase, and only for portfolios that actually mix a Jan-anchored with a
     calendar/Dec-anchored class.

This is the same annual-label convention the existing Shiller two-series blend
already uses (stock and bond both Jan→Jan, so it was phase-clean before; adding
the calendar/Dec series introduces the residual, which is why it's documented
here). A future reader who notices the ~6-month mismatch should NOT chase it as
an off-by-one — it is the deliberate, accepted annual-alignment residual.

Horizon convention
------------------
The horizon is ``end_age - start_age + 1`` annual returns — one return per
age-point, inclusive of both endpoints — matching the deterministic engine's
age-points (default 50→95 = 46 return-years). Note this differs from how a
classic "30-year retirement" SWR study counts: there, a 30-year horizon spans
``end_age - start_age = 30`` (30 return-years over 31 age-points), so our
cohort counts will read one higher than such a table for the same span. Keep
this in mind before chasing a phantom off-by-one against a published 30-year
SWR result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from firebridgelab._step import step_year
from firebridgelab.class_series import NEW_CLASS_SERIES, load_class_series
from firebridgelab.scenario import (
    ALLOCATION_MODE_GLIDEPATH,
    SOURCING_MODE_BUCKETS,
    Scenario,
    allocation_at_age,
    gi_at_age,
)
from firebridgelab.shiller import ShillerData, load_shiller
from firebridgelab.sim_core import (
    AdjustmentStats,
    BufferSurvivalStats,
    WorstCohort,
    WorstCohortYear,
    _compute_adjustment_stats,
    _simulate_cohort,
    compute_buffer_survival,
)
from firebridgelab.spending import (
    AdjustmentEvent,
    SpendingStrategy,
    make_strategy,
)
from firebridgelab.federal_tax import aggregate_tax_overlay, compute_tax_overlay
from firebridgelab.tax_location import (
    TaxLocationPath,
    aggregate_estate_cost,
    aggregate_tax_location_risk,
    has_tax_split,
    simulate_path_decomposition,
    year_tax_context_table,
)


class NoReplayableWindow(ValueError):
    """Raised when a Historical run's selected classes share no common stretch of
    history long enough to replay the requested horizon (every "no cohort has a
    full N-year window" condition: the multi-class / glidepath / bucket common
    overlap, the legacy Shiller-only span, and the comparison-window floor).

    A ``ValueError`` subclass (so the API's existing ``except ValueError``
    fallbacks still catch it) but typed — the same pattern as
    ``SpendingStrategyNotSolvable`` — so the API can translate it to a friendly
    422 by class, not by string-matching the cohort-span message text. The
    carried message is the technical cohort-window detail (n-year span, the
    per-series year ranges) — correct for a log, mapped to plain language at the
    API seam.
    """


# Registry of historical series the SHILLER cohort replay can serve (Decision 3,
# v1: exactly these two). Maps a series id to the attribute on ShillerData
# holding its annual real returns. The Shiller-backed Historical replay
# (returns_for_period) can only serve these two; adding a Shiller-backed series
# later is one entry here plus the data on ShillerData. The slice-1 NEW classes
# come from a SEPARATE staged source (firebridgelab.class_series) and are NOT
# Shiller-replayable yet — their multi-class cohort replay is slice 3 — so they
# live in SERIES_REGISTRY_NEW below, not here.
SERIES_REGISTRY: dict[str, str] = {
    "us_equity": "stock_real_return",
    "us_bonds": "bond_real_return",
}

# Registry of the slice-1 NEW asset-class series (asset-class design D5), staged
# as annual REAL returns in firebridgelab.class_series. Maps series id → its loaded
# ClassSeries. Kept distinct from SERIES_REGISTRY (which is Shiller-backed and
# drives cohort replay) because these series come from a different source and
# are seeded into the Deterministic class menu in slice 1 but NOT wired into the
# multi-class Historical replay until slice 3. class id == series id for all of
# these (D5), so they double as the SERIES_FOR_CLASS values below.
SERIES_REGISTRY_NEW: dict[str, str] = {sid: sid for sid in NEW_CLASS_SERIES}

# Class-id → series-id map (asset-class design: the data-binding namespace). The
# KEY is the stable **class id** (AssetClass.id), the VALUE is the **series id**.
# Seeded with the legacy pair so the default scenario's Historical/MC path keeps
# binding to the EXISTING Shiller data: class ``us_total_market`` → series
# ``us_equity``, class ``us_agg_bonds`` → series ``us_bonds`` (the two namespaces
# differ for the legacy classes — see AssetClass). The slice-1 NEW classes are
# registered here too, where class id == series id (D5): they bind to the staged
# class_series data, NOT to ShillerData. A class id absent here is treated as
# UNMAPPED (its weight is renormalized out; see renormalized_weights). Exact-
# match on the class id (a stable token, not a display string — no case-folding).
SERIES_FOR_CLASS: dict[str, str] = {
    "us_total_market": "us_equity",
    "us_agg_bonds": "us_bonds",
    **{sid: sid for sid in NEW_CLASS_SERIES},
}


@dataclass(frozen=True)
class HistoricalResult:
    """Aggregate of a Historical-mode run across all cohorts.

    ``median`` is one ``(age, portfolio)`` point per age in the horizon, the
    median end-of-year portfolio across cohorts at that age. ``depletion_count``
    is how many cohorts reached zero before ``end_age``; ``earliest_depletion_age``
    is the earliest age any cohort hit zero, or ``None`` if none did.

    Build 2 adds the **percentile fan**: ``p10``/``p25``/``p75``/``p90`` are
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

    # Spending fan (variable-spending design slice 1). Under constant/lifecycle
    # spending is identical across every cohort, so there is no spending
    # distribution to plot and these stay None (the payload omits the spending
    # fan — back-compatible). Under ``guardrails`` spending VARIES across cohorts
    # (a bad sequence cuts, a good one raises), so these carry the per-age
    # percentile fan of the WITHDRAWN spend, mirroring the portfolio fan
    # field-for-field: ``spend_*[k]`` is the percentile spend at ``ages[k]``, so at
    # every age ``spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90``.
    spend_median: list[float] | None = None
    spend_p10: list[float] | None = None
    spend_p25: list[float] | None = None
    spend_p75: list[float] | None = None
    spend_p90: list[float] | None = None

    # Adjustment-event visibility (variable-spending design slice 2). Guardrails
    # only — the cost-of-flexibility readout (``adjustment_stats``) and the
    # worst-cohort drill-down (``worst_cohort``) are None under constant/lifecycle
    # (no adjustments to surface). Both additive; the payload omits them otherwise.
    adjustment_stats: "AdjustmentStats | None" = None
    worst_cohort: "WorstCohort | None" = None

    # Buffer-survival stats (bucket-strategy design "Output"). Buckets sourcing only
    # — the thesis payoff metric: how often a sustained drawdown exhausted the
    # cash + income buffer and FORCED a growth sale at a loss, across all cohorts.
    # None under single sourcing (the payload omits them). Additive defaulted field.
    buffer_survival: "BufferSurvivalStats | None" = None

    # Worst-cohort 3-bucket balance series (bucket-strategy design — the
    # bucket-balance visualization for Historical mode). Buckets sourcing only: the
    # per-year cash/income/growth balances of the worst cohort (earliest forced
    # growth sale; see ``WorstBucketCohort``), so the UI can draw "cash drains
    # through the crash while growth rides". None under single sourcing (and under
    # buckets if there were no cohorts). Additive defaulted field.
    worst_bucket_cohort: "WorstBucketCohort | None" = None

    # Tax-location risk (tax-buckets design tb1). The headline frequency metric:
    # across cohorts, how often does the freely-reachable money run dry before 59½.
    # Populated ONLY when a tax-location split is supplied AND sourcing is single
    # (the shadow decomposition ran); None otherwise, so the payload omits the
    # tax_location_risk block (back-compatible). Held as the firebridgelab.tax_location
    # ``TaxLocationRisk`` aggregate. Additive defaulted field.
    tax_location_risk: "object | None" = None

    # Federal tax (federal-tax overlay design ft1; dragged under TAX_DRAG td1).
    # The Historical twin of the deterministic per-age series: median annual
    # tax, gross-outflow and forced/voluntary medians across cohorts, plus the
    # median-of-path-scalars readout
    # (avg annual tax / effective rate / worst tax year). Populated under the
    # SAME gate as ``tax_location_risk`` (split supplied + single sourcing) —
    # the overlay rides tb1's shadow decomposition, so it can only exist where
    # that decomposition ran. None otherwise (payload omits the block).
    # Additive defaulted field; held as the firebridgelab.federal_tax
    # ``FederalTaxOverlayAggregate``.
    federal_tax: "object | None" = None

    # Estate cost of taxes (tax-drag design td1, DECISION 9). Populated under the
    # SAME gate as ``tax_location_risk`` / ``federal_tax``: per cohort, the
    # INDEPENDENT pre-tax counterfactual's terminal balance minus the dragged
    # decomposition's terminal balance ("how much smaller the estate ends
    # because the portfolio paid the IRS"), aggregated as the MEDIAN of
    # per-cohort differences (never a difference of medians) and paired with
    # the dragged-vs-pretax success-rate delta — see
    # ``firebridgelab.tax_location.EstateCostOfTaxes``'s docstring for why the pairing
    # matters (a $0 median cost when both worlds deplete would otherwise read
    # as "taxes are free"). None otherwise (payload omits the block). Additive
    # defaulted field; held as the firebridgelab.tax_location ``EstateCostOfTaxes``.
    estate_cost: "object | None" = None

    @property
    def testable_since(self) -> int:
        """Earliest valid cohort start year — the "testable since YYYY" label (D3).

        Identical to ``start_year`` (the earliest cohort start used); exposed
        under the brief's name so the API payload / UI can read the policy label
        without coupling to the internal field name. For the default 2-class
        portfolio (equity + bonds, both 1871+) this is 1871; a short-history
        class in the mix (e.g. ``us_reit`` 1972+) pushes it later.
        """
        return self.start_year


@dataclass(frozen=True)
class WorstBucketCohort:
    """The worst-case cohort's per-year 3-bucket balances (bucket-strategy design
    "Output / what the user sees" — the bucket-balance visualization).

    bucket-strategy design: "render the **worst cohort's** bucket dynamics (reuse
    the worst-cohort selection the guardrails feature already does) so the 'cash
    drains through the crash while growth rides' behaviour is visible — this is
    where the protection shows." We reuse the buffer-survival signal already
    tracked per cohort: the worst cohort is the one whose buffer broke EARLIEST
    (the smallest first-forced-growth-sale age — the same metric the
    ``worst_forced_sale_age`` headline reports), with a deterministic tie-break to
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


# The MC-mappable registry (asset-class design slice 2): the union of the
# Shiller-replayable series (SERIES_REGISTRY) and the slice-1 staged classes
# (SERIES_REGISTRY_NEW). Monte Carlo is PARAMETRIC — it samples each class from a
# Normal(mean, vol) using the class's own ``real_return`` / ``volatility``, so it
# does NOT need a Shiller cohort-replayable series; any class whose series id has
# loaded real-return data (Shiller OR class_series) is samplable, and its
# cross-class correlation comes from the per-pair estimator (firebridgelab.correlation).
MC_SERIES_REGISTRY: dict[str, str] = {**SERIES_REGISTRY, **SERIES_REGISTRY_NEW}

# The Historical cohort-replay registry (asset-class design slice 3). Now that the
# slice-1 staged classes carry real ANNUAL return series (loaded via
# firebridgelab.class_series, surfaced year-keyed by firebridgelab.correlation), they ARE
# cohort-replayable — Historical replays whatever class has a real series the
# multi-source accessor (``historical_series_by_id``) can serve. So Historical's
# registry is the SAME union MC uses: the two Shiller series ∪ the staged classes.
# A class whose series id is NOT in this registry (the five not-yet-loaded:
# intl/EM/TIPS/gold/commodities) is still UNMAPPED → hidden + renormalized out.
HIST_SERIES_REGISTRY: dict[str, str] = {**SERIES_REGISTRY, **SERIES_REGISTRY_NEW}

# Selection epsilon for the window policy (asset-class design D3: "selected =
# weight > ε"). A mapped class whose weight is at or below this floor is mapped
# (its weight still rides the renormalized blend) but does NOT constrain the
# common-overlap window — so a preset listing many classes with some at exactly
# 0% can't silently collapse every window to the shortest-history class. Pinned,
# not a parameter; well below any meaningful allocation weight.
HISTORICAL_SELECTION_EPSILON: float = 1e-9


def mapped_series_for(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[int, str]:
    """Indices of allocation classes that map to a known series → its series id.

    A class maps iff its **class id** (``ac.id``) is in SERIES_FOR_CLASS and the
    mapped series id is in ``registry`` (defaults to ``SERIES_REGISTRY`` — the
    Shiller-replayable series the Historical engine uses; Monte Carlo passes the
    wider ``MC_SERIES_REGISTRY`` so the parametrically-samplable slice-1 classes
    also map). Keys off the stable class id (not the display label), so the
    binding can't drift with presentation. Unmapped classes are omitted, so their
    weight is excluded from the renormalized blend.
    """
    reg = SERIES_REGISTRY if registry is None else registry
    mapped: dict[int, str] = {}
    for i, ac in enumerate(scenario.allocation):
        series_id = SERIES_FOR_CLASS.get(ac.id)
        if series_id is not None and series_id in reg:
            mapped[i] = series_id
    return mapped


def renormalized_weights(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[str, float]:
    """Per-series weights, renormalized over mapped classes to sum to 1.

    Reads weights ONLY from classes that map to a known series (per ``registry``,
    defaulting to the Historical ``SERIES_REGISTRY``; MC passes
    ``MC_SERIES_REGISTRY``), then divides by their total so the kept weights sum
    to 1 (the cheap correctness invariant in Decision 3 — a stale/unmapped weight
    can never leak into a blend). Sums weights per series id (so two classes
    mapping to the same series add). Raises ``ValueError`` if no class maps.
    """
    reg = SERIES_REGISTRY if registry is None else registry
    mapped = mapped_series_for(scenario, reg)
    if not mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(reg)})"
        )
    total = sum(scenario.allocation[i].weight for i in mapped)
    if total <= 0:
        raise ValueError("mapped allocation weights sum to zero")
    weights: dict[str, float] = {}
    for i, series_id in mapped.items():
        weights[series_id] = weights.get(series_id, 0.0) + scenario.allocation[i].weight / total
    return weights


def historical_series_by_id() -> dict[str, dict[int, float]]:
    """Unified ``{series_id: {year: real_return}}`` view for cohort replay.

    The single multi-source accessor the Historical multi-class replay reads.
    Reuses ``firebridgelab.correlation.real_return_series_by_series_id`` — the SAME
    year-keyed view the per-pair correlation estimator uses — so Historical and
    the MC correlation matrix draw from one canonical source of historical real
    returns (the Shiller pair from ``shiller.py``; the staged classes from
    ``class_series.py``). Year-keyed so a class's data window is just the key
    set, and a cohort's per-year cell is a direct lookup.

    Imported lazily to avoid a module-level import cycle (``correlation`` does
    not import this module today, but keeping the import local matches the
    pattern the rest of the engine uses for the cross-module loaders).
    """
    from firebridgelab.correlation import real_return_series_by_series_id

    return real_return_series_by_series_id()


def selected_series_for(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[str, float]:
    """Mapped series whose summed weight clears the selection epsilon (D3).

    Maps each allocation class to its series id (via ``mapped_series_for`` over
    ``registry``, defaulting to the Historical ``HIST_SERIES_REGISTRY``), sums
    raw (un-renormalized) weights per series id, and keeps only series whose
    summed weight exceeds ``HISTORICAL_SELECTION_EPSILON``. These — and ONLY
    these — constrain the common-overlap window (a 0%-weight mapped class does
    not bind the window, per D3). Returns ``{series_id: summed_raw_weight}``.
    """
    reg = HIST_SERIES_REGISTRY if registry is None else registry
    mapped = mapped_series_for(scenario, reg)
    summed: dict[str, float] = {}
    for i, series_id in mapped.items():
        summed[series_id] = summed.get(series_id, 0.0) + scenario.allocation[i].weight
    return {
        sid: w for sid, w in summed.items() if w > HISTORICAL_SELECTION_EPSILON
    }


def common_overlap_start_years(
    selected_series: list[str],
    n_steps: int,
    series_data: dict[str, dict[int, float]] | None = None,
) -> list[int]:
    """Valid cohort start years = full-horizon common overlap of selected series.

    The no-backfill window policy (D3): a start year Y is valid iff EVERY series
    in ``selected_series`` has real data for the FULL horizon Y .. Y+n_steps-1.
    Computed from each series' actual year set (no fabricated / backfilled
    years). The intersection of the per-series valid-start sets, sorted ascending.

    ``n_steps`` is the horizon length (``end_age - start_age + 1``). A short-
    history class restricts the set (its earliest full-window start is later);
    if the selected series have no common full-horizon year, the result is empty
    (the caller raises). ``series_data`` defaults to ``historical_series_by_id``.
    """
    data = historical_series_by_id() if series_data is None else series_data

    valid: set[int] | None = None
    for sid in selected_series:
        years = data[sid].keys()
        present = set(years)
        # A start year Y is full-horizon-valid for this series iff every year in
        # Y .. Y+n_steps-1 is present (no gaps, no backfill). Built directly from
        # the present-year set so a hole inside a window correctly disqualifies Y.
        series_starts = {
            y for y in present
            if all((y + k) in present for k in range(n_steps))
        }
        valid = series_starts if valid is None else (valid & series_starts)

    return sorted(valid) if valid else []


def _apply_window_floor(
    start_years: list[int], window_start_year: int | None
) -> list[int]:
    """Restrict valid cohort starts to those at or after ``window_start_year``.

    The comparison-window alignment hook (option 3): when two scenarios with
    DIFFERENT native testable windows are compared, the web layer passes the
    COMMON (latest) floor = ``max(testable_since_A, testable_since_B)`` to BOTH so
    the head-to-head scores them on the SAME cohort set (otherwise a buckets
    scenario testable since 1954 is measured only on the brutal post-1954 cohorts
    while a 60/40 since 1871 is measured on all of history — misleading).

    This ONLY narrows: it drops cohort starts earlier than the floor. It never
    fabricates / expands cohorts (every kept year was already a valid full-horizon
    start), so a floor at or below the native earliest start is a no-op. ``None``
    (single, non-comparison runs) is the identity — the native window is kept.

    Raises ``ValueError`` (the same "no cohort has a full window" shape the window
    builders raise) if the floor removes EVERY cohort, so the API surfaces it as a
    422 rather than aggregating an empty cohort set.
    """
    if window_start_year is None:
        return start_years
    floored = [y for y in start_years if y >= window_start_year]
    if not floored:
        raise NoReplayableWindow(
            "no cohort has a full window at or after the requested "
            f"window_start_year {window_start_year} "
            f"(valid starts span {start_years[0]}-{start_years[-1]})"
            if start_years
            else f"no cohort has a full window at or after {window_start_year}"
        )
    return floored


def _cohort_blended_returns(
    weights: dict[str, float], period: dict[str, np.ndarray]
) -> np.ndarray:
    """Year-by-year blended real returns for one cohort.

    ``weights`` are the renormalized per-series weights; ``period`` maps each
    series id to its array of real returns for the cohort's window. The blend is
    the weight-weighted sum across series, computed per year.
    """
    blended = None
    for series_id, w in weights.items():
        contribution = w * period[series_id]
        blended = contribution if blended is None else blended + contribution
    return blended


def glidepath_phase_weights(
    scenario: Scenario, registry: dict[str, str]
) -> list[dict[str, float]]:
    """Per-phase renormalized per-series weights for a glidepath scenario.

    Glidepath design "Engine hook": under glidepath the per-year blend uses the
    weights of the phase in force that year, so the cohort/MC blend must vary its
    weights per phase (not just the constant-blend ``r``). Returns one
    ``{series_id: renormalized_weight}`` map per glidepath phase, in phase order
    (aligned to ``scenario.glidepath_boundaries``).

    Each phase's weights are computed by ``renormalized_weights`` over a scenario
    swapped to that phase's allocation — the SAME renormalization the constant
    path uses, so a glidepath whose every phase equals the single allocation
    blends byte-for-byte like the constant path (the degenerate-glide identity).
    """
    from dataclasses import replace

    from firebridgelab.scenario import glidepath_boundaries

    boundaries = glidepath_boundaries(scenario)
    phase_weights: list[dict[str, float]] = []
    for start_age in boundaries:
        # The allocation in force at this phase's start age IS this phase's
        # allocation (phases are lower-bound inclusive). Renormalize it over the
        # mapped series exactly as the constant path does.
        phase_alloc = allocation_at_age(scenario, start_age)
        phase_scenario = replace(scenario, allocation=phase_alloc)
        phase_weights.append(renormalized_weights(phase_scenario, registry))
    return phase_weights


def _glidepath_cohort_blended_returns(
    scenario: Scenario,
    period: dict[str, np.ndarray],
    registry: dict[str, str],
) -> np.ndarray:
    """Year-by-year blended real returns under glidepath (phase-varying weights).

    The glidepath analogue of ``_cohort_blended_returns``: instead of one constant
    weight set, each age-step uses the renormalized per-series weights of the phase
    in force at that age (``phase_index_for_age``). ``period`` maps each series id
    to its cohort-window return array (length ``n_steps``).

    Built per-step so the weights track the phase boundaries exactly the
    deterministic engine does. A series a given phase doesn't weight contributes
    0 that year (``weights.get(sid, 0.0)``), so phases that select different
    classes compose cleanly. Reduces to ``_cohort_blended_returns`` when every
    phase shares the same weights (the degenerate-glide identity).
    """
    from firebridgelab.scenario import phase_index_for_age

    phase_weights = glidepath_phase_weights(scenario, registry)
    n_steps = len(next(iter(period.values())))
    blended = np.zeros(n_steps, dtype=float)
    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        index = phase_index_for_age(scenario, age)
        if index >= len(phase_weights):
            index = len(phase_weights) - 1
        weights = phase_weights[index]
        total = 0.0
        for series_id, arr in period.items():
            total += weights.get(series_id, 0.0) * arr[step]
        blended[step] = total
    return blended


def _period_from_series(
    series_data: dict[str, dict[int, float]],
    series_ids: list[str],
    start_year: int,
    n_steps: int,
) -> dict[str, np.ndarray]:
    """One cohort's per-series real-return arrays from the year-keyed accessor.

    The multi-class analogue of ``ShillerData.returns_for_period``: for each
    series id, slices years ``start_year .. start_year+n_steps-1`` out of the
    year-keyed map into a parallel array. Aligns all series by their shared
    integer year label (the calendar-phase convention documented in the module
    docstring). Assumes the window is valid for every series (the caller built
    ``start_year`` from ``common_overlap_start_years``, so every cell exists).
    """
    period: dict[str, np.ndarray] = {}
    for sid in series_ids:
        by_year = series_data[sid]
        period[sid] = np.array(
            [by_year[start_year + k] for k in range(n_steps)], dtype=float
        )
    return period


def _decompose_cohort(
    scenario: Scenario,
    returns: np.ndarray,
    strategy: SpendingStrategy,
    year_context: dict | None = None,
) -> TaxLocationPath:
    """Run the tax-location dragged decomposition for ONE cohort.

    td-b: ``strategy`` is now built by the CALLER (a fresh instance per cohort,
    same discipline the non-split scalar loop already used) rather than
    internally — so the caller can read the strategy's ``events`` log AFTER the
    run (``_events_of``) the same way it already does for the non-split path.
    This is what lets the Historical/MC fan re-point (DECISION 1: for split
    scenarios the portfolio fan ITSELF becomes the dragged total) collect the
    dragged-consistent adjustment-event log without a second, possibly-drifting
    strategy instance. The SAME ``returns`` array the (now pre-tax-counterfactual)
    scalar cohort uses is passed in, so the two runs see the identical per-year
    market path — only their spending strategies' REACTIONS can differ (DECISION
    2: the dragged strategy reads the dragged, taxed balance). ``year_context``
    is the OPTIONAL precomputed ``tax_location.year_tax_context_table`` (td-b
    MC-timing-gate performance) — see that function's docstring.
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
    """One cohort's FULL split-mode step (td-b): the fan re-point + the
    DECISION 9 estate-cost pairing, in one place so the three Historical
    split call sites (legacy Shiller, multi-class, glidepath) can't drift.

    Appends to every one of the ten collector lists (mutation, mirroring the
    existing per-cohort loop shape at each call site so this drops into their
    ``if split_on:`` body unchanged). Runs TWO INDEPENDENT per-cohort sims over
    the SAME ``returns`` sequence — "each is the honest run of its own world"
    (DECISION 9):

      1. The DRAGGED decomposition (``_decompose_cohort``, a fresh strategy
         reading the dragged, TAXED balance) — becomes this cohort's
         contribution to the portfolio fan / spend fan / adjustment-event log
         / depletion count (DECISION 1: the portfolio fan ITSELF is the
         dragged total for split scenarios). ``tax_paths`` also feeds
         ``aggregate_tax_location_risk`` / ``aggregate_tax_overlay`` downstream
         (unchanged from td-a).
      2. The INDEPENDENT pre-tax counterfactual (``_simulate_cohort``, a
         SEPARATE fresh strategy reading the UNTAXED balance) — feeds ONLY the
         estate-cost pairing (``aggregate_estate_cost``), never the fan. This
         is deliberately NOT the same-spend ``TaxLocationPath.pretax_total``
         td-a recorded (that series is paired against the DRAGGED run's OWN
         spend decisions, kept for the drag-direction BINDS invariant) — a
         dynamic strategy's two independent runs can legitimately spend, and
         therefore deplete, at different times.

    ``year_context`` is the OPTIONAL precomputed per-age
    ``(penalty_free, n_65_plus, pension_ss_ordinary)`` table (td-b performance
    — see ``tax_location.year_tax_context_table``): the caller builds it ONCE
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


def _is_legacy_shiller_only(scenario: Scenario) -> bool:
    """True iff the mapped allocation is exactly the two Shiller series.

    The byte-stability gate (asset-class design hard constraint): when the only
    mapped+selected classes resolve to the Shiller pair (``us_equity`` /
    ``us_bonds``), the window policy is a no-op (both 1871+), and we run the
    EXISTING Shiller cohort path verbatim so the default 2-class Historical run
    stays byte-stable (same ~113 cohorts, same fan, same 4%-rule). Any class that
    maps to a NON-Shiller series (a slice-1 staged class) routes to the
    multi-class path instead. A 0%-weight non-Shiller class still counts as
    mapped here, but it is dropped by ``selected_series_for`` in the multi-class
    path, so the window stays correct either way; we route on *mapped* (not
    selected) so even a 0%-weight new class takes the multi-class accessor (whose
    blend is identical for the Shiller pair, since the year-keyed view is built
    from the same Shiller data).
    """
    mapped = mapped_series_for(scenario, HIST_SERIES_REGISTRY)
    series = set(mapped.values())
    return bool(series) and series <= set(SERIES_REGISTRY)


def _glidepath_phase_scenarios(scenario: Scenario) -> list[Scenario]:
    """One single-allocation scenario per glidepath phase (for series selection).

    Each phase's allocation as its OWN constant-allocation scenario, so the
    existing series-selection helpers (``mapped_series_for`` / ``selected_series_for``)
    can be reused per phase without teaching them about glidepath. Used only to
    derive which series each phase touches and the window union — the actual blend
    uses ``_glidepath_cohort_blended_returns`` over the real (glidepath) scenario.
    """
    from dataclasses import replace

    from firebridgelab.scenario import glidepath_boundaries

    boundaries = glidepath_boundaries(scenario)
    return [
        replace(scenario, allocation=allocation_at_age(scenario, start_age))
        for start_age in boundaries
    ]


def _glidepath_window(
    scenario: Scenario,
) -> tuple[list[str], list[int], dict[str, dict[int, float]]]:
    """Series union + common-overlap window for a glidepath scenario (Historical).

    Glidepath generalizes the D3 window policy across phases: the cohort window
    must cover EVERY series ANY phase selects, so the selected-series set is the
    UNION of each phase's selected series (a series only one phase uses still
    needs full-horizon data wherever that phase fires). Computes the no-backfill
    common-overlap window over that union.

    Returns ``(selected_series, start_years, series_data)``. Raises ``ValueError``
    (no mapped class anywhere / no class clears epsilon / no common window) with
    the same messages the constant multi-class path uses, so the API's 422
    mapping is unchanged.
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    phase_scenarios = _glidepath_phase_scenarios(scenario)

    union: set[str] = set()
    any_mapped = False
    for ps in phase_scenarios:
        if mapped_series_for(ps, HIST_SERIES_REGISTRY):
            any_mapped = True
        union |= set(selected_series_for(ps, HIST_SERIES_REGISTRY))
    if not any_mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )
    if not union:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    series_data = historical_series_by_id()
    selected_series = sorted(union)
    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )
    return selected_series, start_years, series_data


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
    byte-for-byte when every phase shares the single allocation (the
    degenerate-glide identity).
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
    # td-b MC/Historical timing-gate perf: precompute the per-age
    # (penalty_free, n_65_plus, pension_ss_ordinary) table ONCE per scenario
    # (none of the three depend on the cohort's return path) instead of every
    # cohort recomputing it fresh — see tax_location.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = _period_from_series(series_data, selected_series, start_year, n_steps)
        returns = _glidepath_cohort_blended_returns(
            scenario, period, HIST_SERIES_REGISTRY
        )
        if split_on:
            # td-b fan re-point (DECISION 1): the dragged decomposition drives
            # the fan/spend-fan/events/depletion; the scalar pass moves INSIDE
            # this helper as the DECISION 9 pre-tax counterfactual only.
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


def _bucket_scenarios(scenario: Scenario) -> dict[str, Scenario]:
    """One single-allocation scenario per bucket (for series selection / blending).

    Each bucket's allocation as its OWN constant-allocation, single-sourcing
    scenario, so the existing series-selection + blend helpers
    (``mapped_series_for`` / ``selected_series_for`` / ``renormalized_weights`` /
    ``_cohort_blended_returns``) can be reused per bucket WITHOUT teaching them
    about buckets. ``sourcing_mode`` is reset to single + ``allocation_mode`` to
    constant on each so those helpers see a plain allocation. Keyed by bucket id.
    """
    from dataclasses import replace

    bc = scenario.buckets
    out: dict[str, Scenario] = {}
    for bucket in (bc.cash, bc.income, bc.growth):
        out[bucket.id] = replace(
            scenario,
            allocation=bucket.allocation,
            sourcing_mode="single",
            allocation_mode="constant",
            phase_allocations=None,
            buckets=None,
        )
    return out


def _bucket_window(
    scenario: Scenario,
) -> tuple[dict[str, dict[str, float]], list[str], list[int], dict[str, dict[int, float]]]:
    """Per-bucket renormalized weights + common-overlap window for a bucket scenario.

    The buckets analogue of ``_multiclass_window``: the cohort window must cover
    EVERY series ANY bucket selects, so the selected-series set is the UNION of
    each bucket's selected series, and the no-backfill common-overlap window is
    computed over that union (a series only the income bucket uses still needs
    full-horizon data). Each bucket's blend weights are its OWN renormalized
    per-series weights (``renormalized_weights`` over the bucket's allocation).

    Returns ``(bucket_weights, selected_series, start_years, series_data)`` where
    ``bucket_weights[bucket_id]`` is ``{series_id: renormalized_weight}`` for that
    bucket. Raises ``ValueError`` (no mapped class anywhere / no class clears the
    epsilon / no common window) with the same messages the multi-class path uses,
    so the API's 422 mapping is unchanged.
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    bucket_scenarios = _bucket_scenarios(scenario)

    bucket_weights: dict[str, dict[str, float]] = {}
    union: set[str] = set()
    any_mapped = False
    for bid, bs in bucket_scenarios.items():
        if mapped_series_for(bs, HIST_SERIES_REGISTRY):
            any_mapped = True
        bucket_weights[bid] = renormalized_weights(bs, HIST_SERIES_REGISTRY)
        union |= set(selected_series_for(bs, HIST_SERIES_REGISTRY))
    if not any_mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )
    if not union:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    series_data = historical_series_by_id()
    selected_series = sorted(union)
    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )
    return bucket_weights, selected_series, start_years, series_data


def _simulate_historical_buckets(
    scenario: Scenario, window_start_year: int | None = None
) -> HistoricalResult:
    """3-bucket cohort replay over the D3 no-backfill common-overlap window.

    bucket-strategy design: replay the plan over every historical cohort, sourcing
    cash-first with the one-sided refill (``buckets.simulate_buckets_cohort``).
    Each bucket's per-year real return is its allocation's cohort blend — the SAME
    ``_cohort_blended_returns`` the single-balance path uses, applied per bucket
    over the union-series window. Aggregates to the portfolio fan + depletion
    (the shared ``_aggregate_cohorts``, so the fan can't drift) and attaches the
    buffer-survival readout (the per-cohort first-forced-growth-sale ages).

    Spending is constant / lifecycle only (guardrails is fenced at the API), so the
    cohort spends are identical across cohorts and the spending fan stays None —
    the bucket value is the buffer-survival metric, not a spend fan.
    """
    from dataclasses import replace

    from firebridgelab.buckets import simulate_buckets_cohort

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


def _select_worst_bucket_cohort(
    scenario: Scenario,
    start_years: list[int],
    trajectories: list,
) -> "WorstBucketCohort | None":
    """Pick the worst cohort + extract its 3-bucket balance series (the drill-down).

    bucket-strategy design: render the WORST cohort's bucket dynamics, reusing the
    worst-cohort selection the guardrails feature uses — and "if buffer-survival
    already tracks a worst/earliest-forced-sale cohort, use that". So "worst" =
    the cohort whose buffer broke EARLIEST (smallest first-forced-growth-sale age —
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


def simulate_historical(
    scenario: Scenario,
    data: ShillerData | None = None,
    window_start_year: int | None = None,
) -> HistoricalResult:
    """Run the plan over every historical cohort and aggregate.

    Horizon spans ages ``start_age .. end_age`` inclusive, so each cohort needs
    that many years of returns (``end_age - start_age + 1``). Aggregates to the
    per-age **percentile fan** and depletion stats across cohorts.

    Two paths, same math (asset-class design slice 3):

    - **Legacy Shiller path** (``_is_legacy_shiller_only``): when the only mapped
      classes are the Shiller pair, use the original ``ShillerData`` cohort path
      verbatim — both series are 1871+, so the window policy is a no-op and the
      default 2-class run stays byte-stable.
    - **Multi-class path**: any slice-1 staged class in the mix routes here. It
      applies the D3 window policy — selected = weight > ε, no-backfill common-
      overlap window, hide/renormalize unmapped — and replays cohorts over the
      restricted window, surfacing the earliest valid start (``testable_since``).

    Note: the ``data`` arg (injected ``ShillerData``) ONLY applies to the legacy
    Shiller-only path. The multi-class path always reads
    ``historical_series_by_id()`` and ignores ``data`` — injecting it will not
    reach the multi-class blend.

    Glidepath (glidepath design): when ``allocation_mode == "glidepath"`` the blend
    weights vary by phase, so it ALWAYS routes through the dedicated glidepath path
    (``_simulate_historical_glidepath``) — which reads the year-keyed accessor and
    blends per-phase. The year-keyed accessor is byte-identical to ``ShillerData``
    for the Shiller pair, so a degenerate glide (every phase = the single
    allocation) reproduces the constant Historical result exactly.

    Buckets (bucket-strategy design): when ``sourcing_mode == "buckets"`` the run
    routes to the dedicated 3-bucket cohort path (``_simulate_historical_buckets``)
    — the portfolio fan + depletion stats use the SAME aggregator, plus the new
    buffer-survival readout. The bucket allocations supersede the single/glidepath
    allocation (glidepath is ignored in buckets mode, per the design), so the bucket
    branch is checked FIRST.

    Comparison-window alignment (``window_start_year``): an optional COHORT
    START-YEAR FLOOR. When set, every path drops cohort starts earlier than it
    (``_apply_window_floor``) so an A/B comparison can score two scenarios on the
    SAME cohort set (the common = latest testable window). ``None`` (single,
    non-comparison runs) keeps each scenario's native window — single-run behavior
    is byte-for-byte unchanged. The floor only NARROWS (it can never expand a
    window past the data), so the canary is untouched.
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        # Tax-location split (tax-buckets design tb1) is SINGLE-mode only. If a split
        # is ALSO supplied here it is intentionally NO-OP'd (the buckets path never
        # decomposes by tax location — the 3×3 tax-location × cash/income/growth
        # product is a deferred later card). Logged (not silently ignored) so the
        # mode interaction is visible; the buckets internals are untouched.
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
    # cohort_events[c] = that cohort's guardrail AdjustmentEvent log (slice 2).
    cohort_trajectories: list[list[float]] = []
    cohort_spends: list[list[float]] = []
    cohort_events: list[list[AdjustmentEvent]] = []
    depletion_ages: list[int] = []
    # Tax-location shadow decomposition (tax-buckets design tb1): collect one path
    # per cohort when a split is supplied, then aggregate to the risk metric. No-op
    # (the list stays empty → tax_location_risk None → payload omits it) otherwise.
    split_on = has_tax_split(scenario)
    tax_paths: list[TaxLocationPath] = []
    pretax_terminals: list[float] = []
    dragged_terminals: list[float] = []
    pretax_success: list[bool] = []
    dragged_success: list[bool] = []
    # td-b MC/Historical timing-gate perf: precompute the per-age
    # (penalty_free, n_65_plus, pension_ss_ordinary) table ONCE per scenario
    # (none of the three depend on the cohort's return path) instead of every
    # cohort recomputing it fresh — see tax_location.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = data.returns_for_period(start_year, n_steps)
        returns = _cohort_blended_returns(weights, period)
        if split_on:
            # td-b fan re-point (DECISION 1): the dragged decomposition drives
            # the fan/spend-fan/events/depletion; the scalar pass moves INSIDE
            # this helper as the DECISION 9 pre-tax counterfactual only.
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


def _events_of(strategy: SpendingStrategy) -> list[AdjustmentEvent]:
    """The strategy's adjustment-event log, or [] for a non-guardrail strategy.

    Only ``GuardrailStrategy`` records events (slice 2); the byte-stable
    ``PureAgeStrategy`` wrapper for constant/lifecycle has none, so this returns
    an empty list there. Keeps the cohort loops uniform — they always collect a
    list, and ``_aggregate_cohorts`` simply finds it empty under constant/lifecycle.
    """
    return list(getattr(strategy, "events", []))


def _gi_step_label(action: str, gi_driven: bool) -> str:
    """Human action tag for a worst-cohort year (design: label GI-driven raises).

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
    """Pick + drill the most-squeezed cohort (LOWEST TOTAL real spend; slice 2).

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

    ``cohort_spends`` (variable-spending design slice 1) is the parallel per-cohort
    WITHDRAWN-spend matrix. The spending fan is built ONLY for ``guardrails`` mode
    (where spending varies across cohorts); under constant/lifecycle spending is
    identical across cohorts, so a fan would be a flat degenerate band — we leave
    the spend fields None and the payload omits the spending fan (back-compatible).

    ``cohort_events`` (slice 2) is the parallel per-cohort AdjustmentEvent log.
    Under guardrails it drives the aggregate ``adjustment_stats`` + the
    ``worst_cohort`` drill-down; under constant/lifecycle it's empty and both
    stay None (the payload omits them).
    """
    matrix = np.array(cohort_trajectories, dtype=float)  # shape (cohorts, n_steps)
    PERCENTILES = (10, 25, 50, 75, 90)
    pctiles = np.percentile(matrix, PERCENTILES, axis=0)  # shape (5, n_steps)
    p10, p25, median, p75, p90 = (row.tolist() for row in pctiles)
    ages = list(range(scenario.start_age, scenario.end_age + 1))
    ending_range = (p10[-1], p90[-1])

    # Spending fan — guardrails only (design). Same percentile machinery as the
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
    # Strategy-level predicate (replaces the old ``spending_mode == guardrails``
    # gate): collect the spend fan + adjustment stats for any collecting strategy
    # (guardrails today, the funded-ratio variants when they land). Same value
    # today — only GuardrailStrategy collects among reachable modes — so byte-
    # stable. ``make_strategy`` is cheap (no per-cohort cost); built once here.
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

        # Adjustment-event visibility (slice 2). Per-cohort depletion-age map for
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


def cohort_return_sequences(
    scenario: Scenario, data: ShillerData | None = None
) -> tuple[list[int], list[np.ndarray]]:
    """Per-cohort blended real-return arrays + their start years (spend-free).

    The single source of cohort construction shared by ``simulate_historical``
    and the solver's lean depletion-only loop, so the two can't drift in which
    cohorts they run or how returns blend. Dispatches on the SAME byte-stability
    gate (``_is_legacy_shiller_only``):

    - Legacy Shiller path: ``ShillerData.all_starting_years`` +
      ``returns_for_period`` (window policy is a no-op for the 1871+ pair).
    - Multi-class path: the D3 window policy (selected = weight > ε, no-backfill
      common-overlap window, hide/renormalize) over the year-keyed accessor.

    Returns ``(start_years, cohort_returns)`` where ``start_years[c]`` is cohort
    c's start year (ascending; ``start_years[0]`` = testable_since) and
    ``cohort_returns[c]`` is its length-``n_steps`` blended real-return array.
    Raises ``ValueError`` (no mapped class / no common window) like the engines.

    Note: the ``data`` arg (injected ``ShillerData``) ONLY applies to the legacy
    Shiller-only path. The multi-class path always reads
    ``historical_series_by_id()`` and ignores ``data``.
    """
    n_steps = scenario.end_age - scenario.start_age + 1

    # Glidepath (glidepath design): phase-varying weights, so always take the
    # glidepath window + per-phase blend (mirrors simulate_historical's routing,
    # so the solver runs the SAME cohorts/returns the display path does).
    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        selected_series, start_years, series_data = _glidepath_window(scenario)
        cohort_returns = [
            _glidepath_cohort_blended_returns(
                scenario,
                _period_from_series(series_data, selected_series, sy, n_steps),
                HIST_SERIES_REGISTRY,
            )
            for sy in start_years
        ]
        return start_years, cohort_returns

    if _is_legacy_shiller_only(scenario):
        if data is None:
            data = load_shiller()
        weights = renormalized_weights(scenario)
        start_years = data.all_starting_years(n_steps)
        if not start_years:
            raise NoReplayableWindow(
                f"no cohort has a full {n_steps}-year window in the historical "
                f"data (data spans {data.first_year}-{data.last_year})"
            )
        cohort_returns = [
            _cohort_blended_returns(weights, data.returns_for_period(sy, n_steps))
            for sy in start_years
        ]
        return start_years, cohort_returns

    weights, selected_series, start_years, series_data = _multiclass_window(scenario)
    cohort_returns = [
        _cohort_blended_returns(
            weights, _period_from_series(series_data, selected_series, sy, n_steps)
        )
        for sy in start_years
    ]
    return start_years, cohort_returns


def _multiclass_window(
    scenario: Scenario,
) -> tuple[dict[str, float], list[str], list[int], dict[str, dict[int, float]]]:
    """Resolve the multi-class blend weights + D3 common-overlap window.

    Shared by ``_simulate_historical_multiclass`` and ``cohort_return_sequences``.
    Returns ``(weights, selected_series, start_years, series_data)``:

    - ``weights``: ``{series_id: renormalized_weight}`` over the SELECTED series.
    - ``selected_series``: sorted series ids with weight > ε (the window binders
      and the blend members).
    - ``start_years``: ascending valid cohort starts (no-backfill common overlap).
    - ``series_data``: the year-keyed real-return accessor (returned so callers
      reuse the one load).

    Raises ``ValueError`` if no class maps, no class clears the epsilon, or the
    selected classes share no full-horizon overlap year.
    """
    n_steps = scenario.end_age - scenario.start_age + 1

    if not mapped_series_for(scenario, HIST_SERIES_REGISTRY):
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )

    selected = selected_series_for(scenario, HIST_SERIES_REGISTRY)
    if not selected:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    total = sum(selected.values())
    weights = {sid: w / total for sid, w in selected.items()}

    series_data = historical_series_by_id()
    selected_series = sorted(selected)

    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )

    return weights, selected_series, start_years, series_data


def _simulate_historical_multiclass(
    scenario: Scenario, window_start_year: int | None = None
) -> HistoricalResult:
    """Multi-class cohort replay over the D3 no-backfill common-overlap window.

    The slice-3 path (taken whenever a slice-1 staged class is in the mix). Steps:

    1. **Hide / renormalize** unmapped classes: ``renormalized_weights`` over
       ``HIST_SERIES_REGISTRY`` drops any class with no loaded series (the five
       not-yet-loaded) and renormalizes the rest to sum to 1.
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
    # td-b MC/Historical timing-gate perf: precompute the per-age
    # (penalty_free, n_65_plus, pension_ss_ordinary) table ONCE per scenario
    # (none of the three depend on the cohort's return path) instead of every
    # cohort recomputing it fresh — see tax_location.year_tax_context_table.
    year_context = year_tax_context_table(scenario) if split_on else None
    for start_year in start_years:
        period = _period_from_series(series_data, selected_series, start_year, n_steps)
        returns = _cohort_blended_returns(weights, period)
        if split_on:
            # td-b fan re-point (DECISION 1): the dragged decomposition drives
            # the fan/spend-fan/events/depletion; the scalar pass moves INSIDE
            # this helper as the DECISION 9 pre-tax counterfactual only.
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
