"""Monte Carlo simulation (Monte Carlo mode) — Historical's parametric twin.

Where Historical mode *replays* actual 1871-2025 return sequences, Monte Carlo
mode *samples* them, then runs each sampled sequence (a **trial**, the analogue of
a Historical **cohort**) through the *identical* per-year sim
(``engine.sim_core._simulate_cohort``, or ``engine.buckets.simulate_buckets_cohort``
in buckets mode). GI streams, spending shape and the floor-at-zero are unchanged;
only the return generation differs. ``simulate_monte_carlo`` returns the SAME
aggregate shape as ``HistoricalResult``, so the API's fan payload and the
front-end fan renderer are reused.

Sampling lives in ``returns.sampling``:

- Each class draws an annual real return around an **arithmetic** mean
  ``compound + vol**2 / 2`` (the standard variance-drag uplift from the editable
  compound knob), so the MC **median** terminal sits a few percent ABOVE the
  Deterministic one. A median *below* Deterministic is the failure signature of a
  skipped uplift.
- Classes are correlated through a nearest-PSD-projected, Cholesky-factored
  correlation matrix: the scalar ``scenario.correlation`` for N <= 2, else an
  explicit ``scenario.correlations`` or an estimated default. Draws are Normal,
  or Student-t with ``scenario.mc_t_df`` (same mean, vol and correlation; fatter
  tails) when ``mc_distribution == "student_t"``.
- Mapped classes blend with the same renormalized per-series weights Historical
  uses; if no class maps, that's an error.

The display path and the solver's evaluator (``_monte_carlo_evaluator``) share one
sampler with the same seed and trial count, so their draws are bit-identical.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
import numpy as np

from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS, Scenario
from firebridgelab.engine.sim_core import (
    AdjustmentStats,
    BufferSurvivalStats,
    _compute_adjustment_stats,
    _simulate_cohort,
    compute_buffer_survival,
)
from firebridgelab.returns.sampling import _blended_trial_returns, _bucket_trial_returns
from firebridgelab.returns.shiller import ShillerData
from firebridgelab.spending.base import AdjustmentEvent
from firebridgelab.spending.registry import make_strategy
from firebridgelab.tax.location import has_tax_split
from firebridgelab.tax.location_path import (
    TaxLocationPath,
    simulate_path_decomposition,
    year_tax_context_table,
)
from firebridgelab.tax.location_risk import aggregate_estate_cost, aggregate_tax_location_risk
from firebridgelab.tax.overlay import aggregate_tax_overlay, compute_tax_overlay


# Fixed default seed so a given scenario samples the SAME draws every run (no
# flicker between a display run and a later solve). The display
# path and the solver pass this same seed so their draws are bit-identical.
DEFAULT_SEED = 12345

# Default trial count: 10k is advisor-grade and smooths the
# solver's confidence dial. Same count + seed for display and solver.
DEFAULT_N_TRIALS = 10_000

# --- split-MC process parallelism ---------------------------------------
#
# The dragged per-trial decomposition (the split branch's hot loop) is PURE
# given the pre-sampled return matrix: sampling stays centralized in the parent
# (one seeded ``_blended_trial_returns`` call, byte-identical to the serial
# run), and only the per-trial *consumption* of those rows is distributed. Each
# worker runs the IDENTICAL per-trial code (``_split_trials_chunk`` — the same
# function the serial path calls) on a contiguous chunk of trials, and the
# parent reassembles results in trial order (``ProcessPoolExecutor.map``
# preserves submission order), so every downstream aggregate is byte-identical
# to the serial run — equivalence is structural, and pinned by
# ``tests/test_monte_carlo.py``'s parallel-vs-serial equality test.
#
# Worker count: cap at ``os.cpu_count() - 1`` so the API server keeps a core.
# Spawn-safety: the code runs inside uvicorn, so the pool uses the ``spawn`` start method explicitly
# (fork under a threaded server is unsafe), the worker is a MODULE-LEVEL
# function with picklable args (Scenario is a frozen dataclass of plain
# fields; the chunk is an ndarray slice), and the pool is created per-request
# inside a ``with`` block — deterministic teardown, no fork-at-import side
# effects, no leaked processes.
SPLIT_PARALLEL_MAX_WORKERS = max(1, (os.cpu_count() or 1) - 1)

# Below this trial count the serial path runs inline: spawned interpreters
# each re-import the engine (~1s of fixed overhead) — a loss for the small
# fixture-sized runs the test suite does by the hundreds, a rounding error for
# the 10k display run the parallelism exists for.
SPLIT_PARALLEL_MIN_TRIALS = 512


@dataclass(frozen=True)
class MonteCarloResult:
    """Aggregate of a Monte Carlo run across all trials.

    Mirrors ``engine.cohort_aggregate.HistoricalResult`` field-for-field so the API's
    Historical-shaped fan payload and the front-end fan renderer are reused
    unchanged — only the cohort/trial naming differs. ``trial_count`` is the
    number of sampled return paths (the MC analogue of Historical's
    ``cohort_count``).

    ``median``/``p10``/``p25``/``p75``/``p90`` are per-age percentiles of the
    end-of-year portfolio across trials (so at every age
    ``p10 <= p25 <= median <= p75 <= p90``). ``ending_range`` is the (p10, p90)
    of the FINAL-age portfolio. ``depletion_count`` is how many trials reached
    zero before ``end_age``; ``earliest_depletion_age`` is the earliest age any
    trial hit zero, or ``None``.
    """

    trial_count: int
    ages: list[int]
    median: list[float]
    p10: list[float]
    p25: list[float]
    p75: list[float]
    p90: list[float]
    ending_range: tuple[float, float]
    depletion_count: int
    earliest_depletion_age: int | None

    # Spending fan (the MC twin of the Historical spending fan). Under
    # constant/lifecycle spending is identical across every trial, so there is no
    # spending distribution to plot and these stay None (the payload omits the
    # spending fan). Under a dynamic strategy (e.g. ``guardrails``) spending VARIES
    # across trials (a bad sampled sequence cuts, a good one raises), so these carry the
    # per-age percentile fan of the WITHDRAWN spend, mirroring the portfolio fan
    # field-for-field: ``spend_*[k]`` is the percentile spend at ``ages[k]``, so at
    # every age ``spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90``.
    spend_median: list[float] | None = None
    spend_p10: list[float] | None = None
    spend_p25: list[float] | None = None
    spend_p75: list[float] | None = None
    spend_p90: list[float] | None = None

    # Aggregate adjustment-event stats. Dynamic strategies only — the
    # cost-of-flexibility readout (``adjustment_stats``), aggregated
    # across the synthetic trials with the SAME ``_compute_adjustment_stats`` the
    # Historical engine uses (so the two stat readouts can't drift). None under
    # constant/lifecycle (no adjustments to surface; the payload omits it).
    #
    # NOTE: there is NO ``worst_cohort`` analogue for MC. MC trials are synthetic
    # (no calendar year), so the Historical worst-cohort "1969 cohort" narrative
    # does NOT translate — MC shows the fan + aggregate stats and omits the
    # worst-trial drill-down to keep the read clean.
    adjustment_stats: "AdjustmentStats | None" = None

    # Buffer-survival stats. Buckets sourcing only
    # — the MC twin of Historical's buffer-survival readout, aggregated across the
    # synthetic trials with the SAME ``compute_buffer_survival`` the Historical
    # engine uses (so the two readouts can't drift). None under single sourcing.
    buffer_survival: "BufferSurvivalStats | None" = None

    # Tax-location risk — the MC twin of Historical's
    # tax_location_risk. Populated ONLY when a tax-location split is supplied AND
    # sourcing is single (the shadow decomposition ran across trials, aggregated
    # with the SAME ``aggregate_tax_location_risk`` Historical uses); None
    # otherwise, so the payload omits the tax_location_risk block.
    tax_location_risk: "object | None" = None

    # Federal-tax overlay — the MC twin of Historical's ``federal_tax``. Populated
    # under the SAME gate as ``tax_location_risk`` (split supplied + single
    # sourcing); None otherwise (payload omits the block). Held as the
    # ``tax.overlay.FederalTaxOverlayAggregate``.
    federal_tax: "object | None" = None

    # Estate cost of taxes — the MC twin of Historical's ``estate_cost``.
    # Populated under the SAME gate; None otherwise. Held as the
    # ``tax.location_risk.EstateCostOfTaxes``. See the
    # ``HistoricalResult.estate_cost`` comment for the pairing discipline (median of
    # per-trial differences, paired with the dragged-vs-pretax success-rate delta).
    estate_cost: "object | None" = None


def _trial_trajectories(
    scenario: Scenario, blended: np.ndarray, collect_spends: bool = False
) -> tuple[np.ndarray, list[list[float]] | None, list[list[AdjustmentEvent]] | None]:
    """Run each trial's blended-return sequence through the shared cohort sim.

    ``blended`` is ``(n_trials, n_years)``. Returns
    ``(trajectories, spends, events)`` where ``trajectories`` is the
    ``(n_trials, n_years)`` end-of-year-portfolio matrix, using the SAME
    ``_simulate_cohort`` math Historical and the solver use (midpoint withdrawal,
    four GI streams, strategy-shaped spending, floor at zero). One Python loop over
    trials, each calling the vectorized-enough per-trial sim — 10k x 43 x 2 is
    trivial.

    When ``collect_spends`` is True (a strategy that collects spends) a
    ``SpendingStrategy`` is built explicitly per trial (BEFORE its year loop, so
    each trial resets the guardrail's carried state), and its per-year withdrawn
    spend + ``AdjustmentEvent`` log are captured: ``spends[t][k]`` is the spend at
    age step ``k`` of trial ``t`` (feeds the spend fan) and ``events[t]`` is that
    trial's adjustment-event log (feeds ``adjustment_stats``). Otherwise both are
    None and the per-trial spend/events are discarded (the trajectory is identical
    whether or not events are collected).
    """
    trajectories = np.empty(blended.shape, dtype=float)
    spends: list[list[float]] | None = [] if collect_spends else None
    events: list[list[AdjustmentEvent]] | None = [] if collect_spends else None
    for t in range(blended.shape[0]):
        if collect_spends:
            # Build the strategy explicitly so its per-trial adjustment events can
            # be read after the run. _simulate_cohort would build an identical one
            # internally for None — passing it in changes no math.
            strategy = make_strategy(scenario)
            ends, _depleted_age, trial_spends = _simulate_cohort(
                scenario, blended[t], strategy
            )
            spends.append(trial_spends)
            events.append(list(getattr(strategy, "events", [])))
        else:
            ends, _depleted_age, _spends = _simulate_cohort(scenario, blended[t])
        trajectories[t] = ends
    return trajectories, spends, events


def _split_trials_chunk(
    scenario: Scenario, blended_chunk: np.ndarray
) -> list[tuple[TaxLocationPath, list[AdjustmentEvent], float, bool]]:
    """Run the per-trial split-mode pair of sims for a CONTIGUOUS chunk of
    trials — the worker function, and ALSO the serial path's inner loop
    (``_split_trial_trajectories`` calls it on the whole matrix when the run
    is too small to parallelize), so the parallel and serial runs execute the
    IDENTICAL per-trial code by construction.

    MODULE-LEVEL and picklable-args-only on purpose (spawn-safety in uvicorn —
    see the SPLIT_PARALLEL_* constants' comment). Per trial:

      1. The DRAGGED decomposition (a fresh strategy reading the dragged, TAXED
         balance) — the trial's fan/spend/attribution contribution.
      2. The INDEPENDENT pre-tax counterfactual (``_simulate_cohort``, a
         SEPARATE fresh strategy reading the UNTAXED balance) — the estate-cost
         pairing only.

    Returns one ``(tax_path, events, pretax_terminal, pretax_success)`` tuple
    per trial, in chunk order. Everything else the caller needs (the dragged
    matrix row, realized-spend list, dragged terminal/success) derives from
    ``tax_path`` and is assembled parent-side — keeping the pickled return
    small and single-sourced.

    ``year_tax_context_table`` is rebuilt per chunk rather than shipped from
    the parent: it is a pure function of ``scenario`` (never of the return
    path), so every rebuild is identical — same values the serial run uses.
    """
    year_context = year_tax_context_table(scenario)
    out: list[tuple[TaxLocationPath, list[AdjustmentEvent], float, bool]] = []
    for t in range(blended_chunk.shape[0]):
        strategy = make_strategy(scenario)
        tax_path = simulate_path_decomposition(
            scenario, blended_chunk[t], strategy, year_context=year_context
        )
        events = list(getattr(strategy, "events", []))

        pretax_strategy = make_strategy(scenario)
        pretax_ends, pretax_depleted_age, _pretax_spends = _simulate_cohort(
            scenario, blended_chunk[t], pretax_strategy
        )
        out.append(
            (tax_path, events, float(pretax_ends[-1]), pretax_depleted_age is None)
        )
    return out


def _split_trial_trajectories(scenario: Scenario, blended: np.ndarray) -> tuple[
    np.ndarray,
    list[list[float]],
    list[list[AdjustmentEvent]],
    list[TaxLocationPath],
    list[float],
    list[float],
    list[bool],
    list[bool],
]:
    """The split-mode analogue of ``_trial_trajectories``: each trial runs TWO
    independent sims over the SAME ``blended[t]`` return sequence — each is the
    honest run of its own world. See ``_split_trials_chunk`` for the per-trial pair.

    For large runs the per-trial work is chunked across a spawn-safe
    process pool (the SAME ``_split_trials_chunk`` the serial path runs,
    on contiguous chunks, reassembled in trial order — byte-identical
    aggregates by construction; see the SPLIT_PARALLEL_* constants' comment
    for the full worker-pool design argument). Small runs (<
    ``SPLIT_PARALLEL_MIN_TRIALS``) stay serial inline — interpreter spawn
    overhead would dominate.

    Returns ``(matrix, spends, events, tax_paths, pretax_terminals,
    dragged_terminals, pretax_success, dragged_success)`` — ``matrix`` is the
    ``(n_trials, n_years)`` DRAGGED end-of-year-total, drop-in for the ``matrix``
    ``_trial_trajectories`` returns (so the existing percentile / depletion /
    spend-fan aggregation downstream needs no change).
    """
    n_trials, n_years = blended.shape

    workers = min(SPLIT_PARALLEL_MAX_WORKERS, max(1, n_trials))
    if n_trials < SPLIT_PARALLEL_MIN_TRIALS or workers <= 1:
        per_trial = _split_trials_chunk(scenario, blended)
    else:
        # Contiguous chunks (first chunks get the remainder), one per worker —
        # a few fat submissions instead of 10k tiny ones keeps IPC negligible.
        base, rem = divmod(n_trials, workers)
        bounds = [0]
        for w in range(workers):
            bounds.append(bounds[-1] + base + (1 if w < rem else 0))
        chunks = [blended[bounds[w] : bounds[w + 1]] for w in range(workers)]
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            # executor.map preserves submission order, so concatenating the
            # chunk results reassembles per-trial results in EXACT trial
            # order — every downstream aggregate is order-identical to serial.
            chunk_results = list(
                pool.map(_split_trials_chunk, [scenario] * workers, chunks)
            )
        per_trial = [item for chunk in chunk_results for item in chunk]

    matrix = np.empty((n_trials, n_years), dtype=float)
    spends: list[list[float]] = []
    events: list[list[AdjustmentEvent]] = []
    tax_paths: list[TaxLocationPath] = []
    pretax_terminals: list[float] = []
    dragged_terminals: list[float] = []
    pretax_success: list[bool] = []
    dragged_success: list[bool] = []
    for t, (tax_path, trial_events, pretax_terminal, pretax_ok) in enumerate(
        per_trial
    ):
        tax_paths.append(tax_path)
        dragged_row = [
            tax_path.taxable[i] + tax_path.tax_deferred[i] + tax_path.roth[i]
            for i in range(n_years)
        ]
        matrix[t] = dragged_row
        # Spend fan / adjustment stats read the REALIZED (fundable-capped)
        # consumption — same discipline as cohort_aggregate._split_cohort_step and
        # the non-split loops (see TaxLocationPath.realized_spend; recording
        # the intended spend overstated post-depletion consumption).
        spends.append(list(tax_path.realized_spend))
        events.append(trial_events)
        pretax_terminals.append(pretax_terminal)
        dragged_terminals.append(dragged_row[-1])
        pretax_success.append(pretax_ok)
        dragged_success.append(tax_path.dragged_depleted_age is None)

    return (
        matrix,
        spends,
        events,
        tax_paths,
        pretax_terminals,
        dragged_terminals,
        pretax_success,
        dragged_success,
    )


def _simulate_monte_carlo_buckets(
    scenario: Scenario, n_trials: int, seed: int
) -> MonteCarloResult:
    """3-bucket Monte Carlo — sample once, run the shared bucket sim per trial.

    Sample the union of bucket classes (correlated, seeded), blend each bucket per
    trial, then run each trial's per-bucket return sequence through the SAME
    ``engine.buckets.simulate_buckets_cohort`` Historical uses (so the
    mechanic can't drift). Aggregates to the portfolio fan + depletion exactly like
    the single-balance MC path, plus the buffer-survival readout.

    Spending is constant / lifecycle only (the dynamic modes are fenced), so there
    is no spend fan / adjustment stats — the bucket value is the buffer-survival
    metric.
    """
    from firebridgelab.engine.buckets import simulate_buckets_cohort

    n_years = scenario.end_age - scenario.start_age + 1
    bucket_returns = _bucket_trial_returns(scenario, n_trials, n_years, seed)

    matrix = np.empty((n_trials, n_years), dtype=float)
    first_forced_sale_ages: list[int | None] = []
    # Depletion is taken from each trial's per-cohort flag (step_year semantics: the
    # year the PRE-floor summed balance went strictly < 0), NOT re-derived from the
    # post-floor summed ``matrix == 0.0`` like the single-balance MC path. The two
    # agree for every trial EXCEPT one whose first zero-crossing lands EXACTLY on
    # 0.0: the post-floor ``== 0.0`` test would count it, but step_year (and the
    # bucket per-cohort flag) does not. Reading depleted_age here keeps the bucket MC
    # depletion count consistent with the per-cohort rule at that boundary (the
    # single-bucket identity). (The matrix is still summed for the fan.)
    depletion_ages: list[int] = []
    for t in range(n_trials):
        per_trial = {bid: arr[t] for bid, arr in bucket_returns.items()}
        traj = simulate_buckets_cohort(scenario, per_trial)
        matrix[t] = [y.cash + y.income + y.growth for y in traj.years]
        first_forced_sale_ages.append(traj.first_forced_sale_age)
        if traj.depleted_age is not None:
            depletion_ages.append(traj.depleted_age)

    PERCENTILES = (10, 25, 50, 75, 90)
    pctiles = np.percentile(matrix, PERCENTILES, axis=0)
    p10, p25, median, p75, p90 = (row.tolist() for row in pctiles)
    ages = list(range(scenario.start_age, scenario.end_age + 1))
    ending_range = (p10[-1], p90[-1])
    depletion_count = len(depletion_ages)
    earliest_depletion_age = min(depletion_ages) if depletion_ages else None

    return MonteCarloResult(
        trial_count=n_trials,
        ages=ages,
        median=median,
        p10=p10,
        p25=p25,
        p75=p75,
        p90=p90,
        ending_range=ending_range,
        depletion_count=depletion_count,
        earliest_depletion_age=earliest_depletion_age,
        buffer_survival=compute_buffer_survival(first_forced_sale_ages),
    )


def simulate_monte_carlo(
    scenario: Scenario,
    n_trials: int = DEFAULT_N_TRIALS,
    seed: int = DEFAULT_SEED,
    data: ShillerData | None = None,
) -> MonteCarloResult:
    """Run ``n_trials`` sampled return paths and aggregate to the fan + depletion.

    Horizon spans ages ``start_age .. end_age`` inclusive, so each trial samples
    that many years of returns (``end_age - start_age + 1``). Aggregates to the
    per-age percentile fan and depletion stats exactly like ``simulate_historical``
    — same percentile interpolation, same (p10, p90) ending range, same
    depletion-count / earliest-age logic — so the result drops into the
    Historical-shaped payload unchanged.

    ``data`` is accepted for API symmetry with ``simulate_historical`` (the MC
    engine samples parametrically and does not read the Shiller series), so the
    API can call both engines through the same signature.

    Buckets: when ``sourcing_mode == "buckets"`` the run
    routes to the dedicated 3-bucket trial path (``_simulate_monte_carlo_buckets``)
    — same fan + depletion aggregation, plus the buffer-survival readout. The
    bucket allocations supersede the single/glidepath allocation, so the bucket
    branch is checked FIRST.
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        # The tax-location split is SINGLE-mode only — ignored under buckets
        # sourcing. Logged so the mode interaction is visible.
        if has_tax_split(scenario):
            logging.getLogger(__name__).info(
                "tax-location split supplied with sourcing_mode=buckets; "
                "ignoring the split (tb1 is single-mode only)"
            )
        return _simulate_monte_carlo_buckets(scenario, n_trials, seed)

    n_years = scenario.end_age - scenario.start_age + 1

    blended = _blended_trial_returns(scenario, n_trials, n_years, seed)
    # Collect per-trial spends + adjustment events ONLY for strategies whose
    # ``collects_spends`` is set, where spending varies across trials and a spend
    # fan / adjustment stats exist. Constant/lifecycle take the lean path (no
    # collection).
    collects_spends = make_strategy(scenario).collects_spends

    tax_location_risk = None
    federal_tax = None
    estate_cost = None
    if has_tax_split(scenario):
        # The DRAGGED decomposition drives the fan / spend fan / adjustment
        # stats / depletion for split scenarios (the ``matrix`` this returns IS
        # the dragged total, a drop-in for ``_trial_trajectories``'s pre-tax
        # matrix below). ``tax_paths`` also feeds ``tax_location_risk`` /
        # ``federal_tax``, so the dragged decomposition is computed ONCE per
        # trial. The pre-tax counterfactual rides inside the same per-trial loop
        # and feeds ``estate_cost`` only.
        (
            matrix,
            trial_spends,
            trial_events,
            tax_paths,
            pretax_terminals,
            dragged_terminals,
            pretax_success,
            dragged_success,
        ) = _split_trial_trajectories(scenario, blended)
        tax_location_risk = aggregate_tax_location_risk(tax_paths)
        federal_tax_paths = [compute_tax_overlay(scenario, p) for p in tax_paths]
        federal_tax = aggregate_tax_overlay(federal_tax_paths)
        estate_cost = aggregate_estate_cost(
            pretax_terminals, dragged_terminals, pretax_success, dragged_success
        )
    else:
        matrix, trial_spends, trial_events = _trial_trajectories(
            scenario, blended, collect_spends=collects_spends
        )  # matrix: (n_trials, n_years)

    # Per-age percentiles across trials. One np.percentile over the
    # (trials, n_years) matrix yields all five at once; linear interpolation is
    # monotonic in the percentile, so p10 <= p25 <= p50 <= p75 <= p90 holds by
    # construction at every age (mirrors simulate_historical exactly).
    PERCENTILES = (10, 25, 50, 75, 90)
    pctiles = np.percentile(matrix, PERCENTILES, axis=0)  # (5, n_years)
    p10, p25, median, p75, p90 = (row.tolist() for row in pctiles)
    ages = list(range(scenario.start_age, scenario.end_age + 1))
    ending_range = (p10[-1], p90[-1])

    # Depletion: a trial deplete iff its end-of-year balance ever hits zero before
    # end_age. The matrix is floored at zero by _simulate_cohort and stays zero
    # thereafter, so the first zero-age per trial is its depletion age. We recover
    # depletion from the trajectory matrix (no second sim pass): a trial depleted
    # iff any age before end_age is zero; earliest age is the smallest such age
    # across trials.
    depletion_count, earliest_depletion_age = _depletion_stats(matrix, ages)

    # Spending fan + adjustment stats — collecting strategies only. The SAME percentile
    # machinery as the portfolio fan over the (trials, n_years) spend matrix, and the
    # SAME _compute_adjustment_stats the Historical engine uses (so the two stat
    # readouts can't drift). NO worst-trial drill-down: MC trials are synthetic (no
    # calendar year), so the worst-cohort narrative does not translate — MC shows the
    # fan + aggregate stats only.
    spend_fan: dict[str, list[float] | None] = {
        "spend_median": None,
        "spend_p10": None,
        "spend_p25": None,
        "spend_p75": None,
        "spend_p90": None,
    }
    adjustment_stats: AdjustmentStats | None = None
    if collects_spends and trial_spends:
        spend_matrix = np.array(trial_spends, dtype=float)  # (trials, n_years)
        s_pctiles = np.percentile(spend_matrix, PERCENTILES, axis=0)  # (5, n_years)
        s_p10, s_p25, s_median, s_p75, s_p90 = (row.tolist() for row in s_pctiles)
        spend_fan = {
            "spend_median": s_median,
            "spend_p10": s_p10,
            "spend_p25": s_p25,
            "spend_p75": s_p75,
            "spend_p90": s_p90,
        }
        adjustment_stats = _compute_adjustment_stats(
            scenario, trial_spends, trial_events or [[] for _ in trial_spends]
        )

    return MonteCarloResult(
        trial_count=n_trials,
        ages=ages,
        median=median,
        p10=p10,
        p25=p25,
        p75=p75,
        p90=p90,
        ending_range=ending_range,
        depletion_count=depletion_count,
        earliest_depletion_age=earliest_depletion_age,
        adjustment_stats=adjustment_stats,
        tax_location_risk=tax_location_risk,
        federal_tax=federal_tax,
        estate_cost=estate_cost,
        **spend_fan,
    )


def _depletion_stats(
    matrix: np.ndarray, ages: list[int]
) -> tuple[int, int | None]:
    """Depletion count + earliest depletion age from the trajectory matrix.

    ``matrix`` is ``(n_trials, n_years)`` of end-of-year balances (floored at zero
    and staying zero once depleted). A trial depleted iff any of its end-of-year
    balances is zero. The earliest depletion age is the smallest age whose column
    has any zero. Returns ``(count, earliest_age_or_None)``. (Pinned to the same
    "first balance == 0" definition the per-cohort ``_simulate_cohort`` uses.)
    """
    zero_mask = matrix == 0.0  # (n_trials, n_years)
    depleted_per_trial = zero_mask.any(axis=1)
    depletion_count = int(depleted_per_trial.sum())
    if depletion_count == 0:
        return 0, None
    # Earliest age with any zero: smallest age-index whose column has a zero.
    ages_with_zero = np.where(zero_mask.any(axis=0))[0]
    earliest_idx = int(ages_with_zero[0])
    return depletion_count, ages[earliest_idx]


def _monte_carlo_evaluator(
    scenario: Scenario,
    n_trials: int = DEFAULT_N_TRIALS,
    seed: int = DEFAULT_SEED,
    data: ShillerData | None = None,
):
    """Build a fast survival-fraction function over a spend (the solver hook).

    Mirrors ``analysis.solver._historical_evaluator``: the trials' blended-return arrays
    are spend-independent, so we sample them ONCE here (via the SAME shared
    sampler the display path uses, with the SAME seed/count — bit-identical
    draws), then the returned ``evaluate(spend)`` re-runs only the lean
    depletion-only sim per probe. Returns ``(evaluate, trial_count)``.

    Short-circuits before any percentile/fan work — depletion count only — same
    lean discipline as the solver's historical evaluator. The display path does
    not use it.

    Raises ``ValueError`` (via the shared sampler) on a scenario whose allocation
    maps no known series — the API routes that to a 422.
    """
    n_years = scenario.end_age - scenario.start_age + 1
    blended = _blended_trial_returns(scenario, n_trials, n_years, seed)
    trial_count = blended.shape[0]

    def evaluate(spend: float) -> float:
        probe = replace(scenario, annual_spending=spend)
        depletion_count = 0
        for t in range(blended.shape[0]):
            # Reuse the SAME per-trial math as simulate_monte_carlo (which reuses
            # _simulate_cohort); take only the depletion flag — no trajectory
            # retained, no percentile fan.
            _ends, depleted_age, _spends = _simulate_cohort(probe, blended[t])
            if depleted_age is not None:
                depletion_count += 1
        return 1.0 - depletion_count / trial_count

    return evaluate, trial_count
