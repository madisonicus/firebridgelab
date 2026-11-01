"""Monte Carlo simulation (Monte Carlo mode) — Historical's parametric twin.

Where Historical mode *replays* actual 1871-2025 return sequences, Monte Carlo
mode *samples* them: each asset class draws its annual real return from a
``Normal(mean, vol)``, classes are correlated via a correlation matrix, and the
blended sequence runs through the *identical* midpoint-withdrawal sim
(``_simulate_cohort``) Historical and Deterministic use. The only thing that
changes vs Historical is how the year-by-year returns are generated; GI streams,
spending shape, and the floor-at-zero are all unchanged. See
the design notes.

This returns the SAME aggregate shape as ``HistoricalResult`` (a Monte Carlo
**trial** is the analogue of a Historical **cohort**), so the API's
Historical-shaped fan payload and the front-end fan renderer are reused verbatim.

Compound -> arithmetic uplift (Decision #2)
-------------------------------------------
The editable return knob is the **compound** (geometric) real return — what the
Deterministic engine compounds. A Normal sampler needs the **arithmetic** mean,
so we convert per class: ``mean = compound + vol**2 / 2`` (the standard
variance-drag adjustment). We sample arithmetic-Normal returns with the displayed
stdev (vol). Because terminal wealth is right-skewed, this makes the MC **median**
sit a few percent ABOVE the Deterministic terminal value — that is expected and
correct. A median *below* Deterministic is the failure signature of a skipped
uplift (gate #1 in the brief asserts the sign).

Correlation -> nearest-PSD -> Cholesky (Decision #1, #4; asset-class design)
---------------------------------------------------------------------------
The correlation matrix over the mapped series is assembled, projected to the
nearest positive-semidefinite matrix, then Cholesky-factored; the factor ``L``
correlates independent standard normals.

Two assembly paths (asset-class design, round-2 fix N1):
  - **N <= 2**: the scalar ``scenario.correlation`` fast-path — the single
    equity-bond scalar broadcast onto the 1x1 / 2x2 matrix. This is the ONLY
    place the scalar is used.
  - **N > 2**: either an explicit ``scenario.correlations`` matrix (ordered,
    aligned to allocation order) is used after validation, OR — when none is
    supplied — the per-pair/max-overlap **estimated default** matrix over the
    mapped series is built by ``firebridgelab.correlation`` (slice 2 D6). The scalar is
    NEVER broadcast onto a >2 matrix (a uniform off-diagonal is a silent wrong
    default). A malformed explicit matrix raises ValueError (→ 422 at the API).

Every matrix — scalar-built or explicit — passes a nearest-PSD projection
(``_nearest_psd``: clip negative eigenvalues to a pinned 1e-8 floor, renormalize
to unit diagonal) BEFORE Cholesky. For a valid 2x2 it's a no-op. The valid
scalar-correlation range is [-0.99, +0.99] (exactly +/-1 is rank-deficient —
guarded at the API).

Shared sampler (Decision #4)
----------------------------
``_sample_class_returns`` builds the ``(n_trials, n_years, n_classes)`` draw in
ONE fixed order/shape from ``np.random.default_rng(seed)``. Both the display path
(``simulate_monte_carlo``) and build 2's lean solver evaluator
(``_monte_carlo_evaluator``) call it with the same seed/count/shape, so their
draws are bit-identical — the same "can't drift" discipline as the shared
``_simulate_cohort``.

Series mapping + renormalization (mirrors Historical's Decision 3)
------------------------------------------------------------------
We sample one return series per *mapped* asset class (a class whose name binds to
a known historical series via ``SERIES_FOR_CLASS``), using that class's vol and
compound return, then blend with ``renormalized_weights`` — the exact same
renormalized per-series weights Historical blends. Unmapped classes' weight is
renormalized out, so a future unmapped class can't leak into the blend. If no
class maps, that's an error (same as Historical).
"""

from __future__ import annotations

import functools

import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace

import numpy as np

from firebridgelab.historical import (
    MC_SERIES_REGISTRY,
    mapped_series_for,
    renormalized_weights,
)
from firebridgelab.sim_core import (
    AdjustmentStats,
    BufferSurvivalStats,
    _compute_adjustment_stats,
    _simulate_cohort,
    compute_buffer_survival,
)
from firebridgelab.scenario import (
    ALLOCATION_MODE_GLIDEPATH,
    SOURCING_MODE_BUCKETS,
    MC_DISTRIBUTION_STUDENT_T,
    Scenario,
    allocation_at_age,
)
from firebridgelab.shiller import ShillerData
from firebridgelab.spending import AdjustmentEvent, SpendingStrategy, make_strategy
from firebridgelab.federal_tax import aggregate_tax_overlay, compute_tax_overlay
from firebridgelab.tax_location import (
    TaxLocationPath,
    aggregate_estate_cost,
    aggregate_tax_location_risk,
    has_tax_split,
    simulate_path_decomposition,
    year_tax_context_table,
)

# Fixed default seed so a given scenario samples the SAME draws every run (no
# flicker between a display run and a later solve — see Decision #4). The display
# path and the solver pass this same seed so their draws are bit-identical.
DEFAULT_SEED = 12345

# Floor on any sampled simple return (see _sample_class_returns).
MIN_SIMPLE_RETURN = -0.99

# Default trial count (Decision #4): 10k is advisor-grade and smooths the
# solver's confidence dial. Same count + seed for display and solver.
DEFAULT_N_TRIALS = 10_000

# Valid correlation range. Exactly +/-1 is positive-SEMIdefinite (rank-deficient)
# so np.linalg.cholesky raises; clamp to [-CORRELATION_LIMIT, +CORRELATION_LIMIT]
# and 422 outside it at the API (Decision: correlation guard).
CORRELATION_LIMIT = 0.99

# Eigenvalue floor for the nearest-PSD projection (asset-class design D6 / round-2):
# a PINNED 1e-8 floor. After clipping negative eigenvalues to this floor and
# renormalizing to unit diagonal, the matrix is strictly positive-definite, so
# np.linalg.cholesky always succeeds. For an already-valid matrix (e.g. any valid
# 2x2) the projection is a no-op within fp noise. The value is pinned (not a
# parameter) so the projection is reproducible across runs.
EIGENVALUE_FLOOR = 1e-8

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
# Worker count: the box has 4 cores and runs live services — cap at
# ``os.cpu_count() - 1`` (never saturate the box). Spawn-safety: the code runs
# inside uvicorn, so the pool uses the ``spawn`` start method explicitly
# (fork under a threaded server is unsafe), the worker is a MODULE-LEVEL
# function with picklable args (Scenario is a frozen dataclass of plain
# fields; the chunk is an ndarray slice), and the pool is created per-request
# inside a ``with`` block — deterministic teardown, no fork-at-import side
# effects, no leaked processes.
SPLIT_PARALLEL_MAX_WORKERS = max(1, (os.cpu_count() or 1) - 1)

# Below this trial count the serial path runs inline: 3 spawned interpreters
# each re-import the engine (~1s of fixed overhead) — a loss for the small
# fixture-sized runs the test suite does by the hundreds, a rounding error for
# the 10k display run the parallelism exists for.
SPLIT_PARALLEL_MIN_TRIALS = 512


@dataclass(frozen=True)
class MonteCarloResult:
    """Aggregate of a Monte Carlo run across all trials.

    Mirrors ``historical.HistoricalResult`` field-for-field so the API's
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

    # Spending fan (variable-spending design slice 3 — the MC twin of the Historical
    # spending fan). Under constant/lifecycle spending is identical across every
    # trial, so there is no spending distribution to plot and these stay None (the
    # payload omits the spending fan — back-compatible). Under ``guardrails``
    # spending VARIES across trials (a bad sampled sequence cuts, a good one
    # raises), so these carry the per-age percentile fan of the WITHDRAWN spend,
    # mirroring the portfolio fan field-for-field: ``spend_*[k]`` is the percentile
    # spend at ``ages[k]``, so at every age
    # ``spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90``.
    spend_median: list[float] | None = None
    spend_p10: list[float] | None = None
    spend_p25: list[float] | None = None
    spend_p75: list[float] | None = None
    spend_p90: list[float] | None = None

    # Aggregate adjustment-event stats (variable-spending design slice 3). Guardrails
    # only — the cost-of-flexibility readout (``adjustment_stats``), aggregated
    # across the synthetic trials with the SAME ``_compute_adjustment_stats`` the
    # Historical engine uses (so the two stat readouts can't drift). None under
    # constant/lifecycle (no adjustments to surface; the payload omits it).
    #
    # NOTE: there is NO ``worst_cohort`` analogue for MC. MC trials are synthetic
    # (no calendar year), so the Historical worst-cohort "1969 cohort" narrative
    # does NOT translate — the design pins MC to the fan + aggregate stats and omits
    # the worst-trial drill-down to keep the read clean.
    adjustment_stats: "AdjustmentStats | None" = None

    # Buffer-survival stats (bucket-strategy design "Output"). Buckets sourcing only
    # — the MC twin of Historical's buffer-survival readout, aggregated across the
    # synthetic trials with the SAME ``compute_buffer_survival`` the Historical
    # engine uses (so the two readouts can't drift). None under single sourcing.
    buffer_survival: "BufferSurvivalStats | None" = None

    # Tax-location risk (tax-buckets design tb1) — the MC twin of Historical's
    # tax_location_risk. Populated ONLY when a tax-location split is supplied AND
    # sourcing is single (the shadow decomposition ran across trials, aggregated
    # with the SAME ``aggregate_tax_location_risk`` Historical uses); None
    # otherwise, so the payload omits the tax_location_risk block (back-compatible).
    tax_location_risk: "object | None" = None

    # Federal-tax overlay (federal-tax overlay design ft1) — the MC twin of
    # Historical's ``federal_tax``. Populated under the SAME gate as
    # ``tax_location_risk`` (split supplied + single sourcing); None otherwise
    # (payload omits the block). Additive defaulted field; held as the
    # firebridgelab.federal_tax ``FederalTaxOverlayAggregate``.
    federal_tax: "object | None" = None

    # Estate cost of taxes (tax-drag design td1, DECISION 9) — the MC twin of
    # Historical's ``estate_cost``. Populated under the SAME gate; None
    # otherwise. Additive defaulted field; held as the firebridgelab.tax_location
    # ``EstateCostOfTaxes``. See ``historical.HistoricalResult.estate_cost``'s
    # docstring for the pairing discipline (median of per-trial differences,
    # paired with the dragged-vs-pretax success-rate delta).
    estate_cost: "object | None" = None


def _nearest_psd(matrix: np.ndarray) -> np.ndarray:
    """Project a symmetric matrix to the nearest positive-(semi)definite one.

    The acceptance gate every correlation matrix passes before Cholesky
    (asset-class design D6): symmetrize, eigendecompose, clip eigenvalues to the
    pinned ``EIGENVALUE_FLOOR`` (1e-8), reassemble, then renormalize to unit
    diagonal so it's a valid *correlation* matrix again. The result is strictly
    positive-definite (eigenvalues >= 1e-8), so ``np.linalg.cholesky`` always
    succeeds. For an already-valid matrix (e.g. any valid 2x2) this is a no-op
    within fp noise.

    Eigendecomposition via ``eigh`` (symmetric solver) — the input is symmetric
    by construction (built symmetric, or symmetrized here defensively).
    """
    # Symmetrize defensively (an explicit matrix may carry tiny asymmetry).
    sym = (matrix + matrix.T) / 2.0
    eigvals, eigvecs = np.linalg.eigh(sym)
    clipped = np.clip(eigvals, EIGENVALUE_FLOOR, None)
    rebuilt = (eigvecs * clipped) @ eigvecs.T
    # Renormalize to unit diagonal: D^-1/2 . rebuilt . D^-1/2.
    d = np.sqrt(np.diag(rebuilt))
    rebuilt = rebuilt / np.outer(d, d)
    # Force exact unit diagonal + symmetry (kill residual fp drift).
    rebuilt = (rebuilt + rebuilt.T) / 2.0
    np.fill_diagonal(rebuilt, 1.0)
    return rebuilt


def _assemble_correlation_matrix(
    scenario: Scenario, n: int, series_ids: list[str] | None = None
) -> np.ndarray:
    """Assemble the N x N correlation matrix for the mapped classes (pre-PSD).

    Three paths (asset-class design, round-2 fix N1 + slice 2 D6):
      - ``n <= 2``: the scalar ``scenario.correlation`` fast-path — 1.0 on the
        diagonal, the scalar on the single off-diagonal pair. The ONLY place the
        scalar is used. This is exactly the 2x2 equity-bond case (or a trivial
        1x1 identity for a single mapped class). UNCHANGED by slice 2.
      - ``n > 2`` with an explicit ``scenario.correlations``: validate and use it.
        The scalar is NEVER broadcast onto a >2 matrix (a uniform off-diagonal is
        a silent wrong default).
      - ``n > 2`` with NO explicit matrix (slice 2): fall back to the
        per-pair/max-overlap **estimated default** matrix over the mapped
        ``series_ids`` (in allocation order) — ``correlation.estimate_correlation_matrix``.
        This replaces slice 0's placeholder raise. ``series_ids`` MUST be supplied
        for this branch (the estimator needs to know which series, in which
        order); the sampler always passes them.

    The explicit matrix is validated for shape (n x n) and structure (symmetric,
    unit diagonal within tolerance, |cell| <= 1) and a malformed one raises
    ``ValueError`` (the API routes it to 422 — slice-2 carry-forward). The
    assembled matrix is the INPUT to the nearest-PSD projection — it need not be
    PSD here (the per-pair estimate especially need not be).
    """
    if n <= 2:
        matrix = np.full((n, n), float(scenario.correlation))
        np.fill_diagonal(matrix, 1.0)
        return matrix

    # n > 2 with no explicit matrix: the estimated default (slice 2 D6). The
    # scalar is still NEVER broadcast onto a >2 matrix.
    if scenario.correlations is None:
        if series_ids is None:
            raise ValueError(
                "a >2-class Monte Carlo run with no explicit correlation matrix "
                "needs the mapped series ids to estimate the default matrix"
            )
        # Lazy import to avoid a module-level cycle (correlation imports back into
        # monte_carlo for _nearest_psd / FROBENIUS gate).
        from firebridgelab.correlation import estimate_with_gate

        # estimate_with_gate runs the D6 Frobenius-distance acceptance gate on the
        # LIVE path: it asserts the raw per-pair estimate is within
        # FROBENIUS_TOLERANCE of its nearest-PSD projection, raising loudly past it
        # (→ 422 at the API) so a large silent correction surfaces here in
        # production — NOT just in tests. It returns the ALREADY-PSD-projected
        # matrix; the subsequent _nearest_psd in _sample_class_returns is then a
        # no-op on this PSD, unit-diagonal matrix (no double-projection of any
        # consequence — see _nearest_psd's idempotence on a valid matrix).
        projected, _overlaps, _frobenius = estimate_with_gate(series_ids)
        return projected

    # n > 2 with an explicit matrix: validate structure, then use it.
    #
    # SLICE-4 ALIGNMENT HAZARD: ``n`` here is the count of the MAPPED series (the
    # subset of the allocation that binds to a known series, in allocation order).
    # The explicit ``scenario.correlations`` matrix is supplied by the USER in full
    # ALLOCATION order (one row/col per allocation entry). In slice 2 every loaded
    # class maps, so mapped == allocation and the two orders coincide — the shape
    # check below passes. But slice 4 wires currently-UNMAPPED classes
    # (intl/EM/TIPS/gold/commodities) into a UI that emits an allocation-order
    # matrix; an allocation with any unmapped class makes the user's matrix LARGER
    # than the mapped subset, so it MUST be reindexed to the mapped subset (drop the
    # unmapped rows/cols, preserving mapped order) BEFORE these shape/structure
    # checks — otherwise the wrong cells align to the wrong series. In slice 2 this
    # fails SAFE (an oversized matrix mismatches (n, n) → ValueError → 422), so this
    # is a forward-looking note, not a bug today.
    matrix = np.array(scenario.correlations, dtype=float)
    if matrix.shape != (n, n):
        raise ValueError(
            f"correlation matrix shape {matrix.shape} does not match the "
            f"{n} mapped asset classes (expected {(n, n)})"
        )
    if not np.allclose(matrix, matrix.T, atol=1e-9):
        raise ValueError("correlation matrix must be symmetric")
    if not np.allclose(np.diag(matrix), 1.0, atol=1e-9):
        raise ValueError("correlation matrix must have a unit diagonal")
    if np.any(np.abs(matrix) > 1.0 + 1e-9):
        raise ValueError("correlation matrix cells must be in [-1, 1]")
    return matrix


@functools.lru_cache(maxsize=256)
def rebalanced_compound_return(scenario: Scenario) -> float:
    """Long-run compound real return of the scenario's allocation, rebalanced yearly.

    The Deterministic engine compounds ONE constant return. A plain weighted
    average of class compound returns ignores the rebalancing (diversification)
    return an annually-rebalanced mix earns, so it sat ~0.4%/yr below the Monte
    Carlo median for a 60/40 (review 2026-10-11). Using the SAME moments the MC
    sampler draws from:

        arith_p = sum_i w_i * (g_i + vol_i**2 / 2)
        var_p   = w' S w            (S = vol_i vol_j corr_ij, same corr matrix as MC)
        g_p     = arith_p - var_p / 2

    Weights renormalize over the mapped classes exactly as MC does. Zero-vol
    allocations reduce to the weighted average, so closed-form checks still hold.
    """
    mapped = mapped_series_for(scenario, MC_SERIES_REGISTRY)
    if not mapped:
        return sum(ac.weight * ac.real_return for ac in scenario.allocation)
    indices = sorted(mapped)
    classes = [scenario.allocation[i] for i in indices]
    total = sum(ac.weight for ac in classes)
    if total <= 0:
        return 0.0
    w = np.array([ac.weight / total for ac in classes], dtype=float)
    vols = np.array([ac.volatility for ac in classes], dtype=float)
    if not np.any(vols):
        return float(np.dot(w, [ac.real_return for ac in classes]))
    arith = np.array([ac.real_return + ac.volatility**2 / 2.0 for ac in classes])
    corr = _nearest_psd(
        _assemble_correlation_matrix(scenario, len(indices), [mapped[i] for i in indices])
    )
    cov = np.outer(vols, vols) * corr
    return float(w @ arith - (w @ cov @ w) / 2.0)


def _sample_class_returns(
    scenario: Scenario,
    n_trials: int,
    n_years: int,
    seed: int,
) -> tuple[list[str], dict[int, str], np.ndarray]:
    """The SINGLE shared sampler — correlated per-class Normal real returns.

    Returns ``(series_ids, mapped, returns)`` where ``series_ids`` is the ordered
    list of mapped series, ``mapped`` is ``{allocation_index: series_id}`` (from
    ``mapped_series_for``), and ``returns`` is the
    ``(n_trials, n_years, n_classes)`` array of per-mapped-class real returns
    (``n_classes == len(series_ids)``, in ``series_ids`` order).

    Build (in this fixed order/shape so display + solver draws are bit-identical
    for a given seed):
      1. Per-class arithmetic mean = compound knob + vol**2 / 2 (the uplift).
      2. Per-class vol (the displayed stdev).
      3. Correlation matrix over the mapped series (scalar fast-path for N<=2,
         else the explicit matrix) -> nearest-PSD projection -> Cholesky factor L.
      4. Draw (n_trials, n_years, n_classes) standard normals via
         ``np.random.default_rng(seed)``, correlate via L, scale by vol, shift by
         mean.

    Raises ``ValueError`` (via ``mapped_series_for`` being empty) if no class maps
    to a known series — the API routes that to a 422.
    """
    # MC is parametric, so it maps over the WIDER MC registry (Shiller +
    # class_series), letting the slice-1 classes sample (slice 2). Historical's
    # narrower SERIES_REGISTRY mapping is unchanged.
    mapped = mapped_series_for(scenario, MC_SERIES_REGISTRY)
    if not mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(MC_SERIES_REGISTRY)})"
        )

    # Stable order: by allocation index, so the draw shape is deterministic.
    indices = sorted(mapped)
    series_ids = [mapped[i] for i in indices]
    classes = [scenario.allocation[i] for i in indices]

    vols = np.array([ac.volatility for ac in classes], dtype=float)
    # Compound knob -> arithmetic mean: the variance-drag uplift (Decision #2).
    means = np.array(
        [ac.real_return + ac.volatility**2 / 2.0 for ac in classes], dtype=float
    )

    # Assemble (scalar fast-path for N<=2; explicit matrix or the per-pair
    # estimated default for N>2 — slice 2), then project to the nearest PSD matrix
    # BEFORE Cholesky (a no-op for a valid 2x2 or an already-PSD estimate).
    raw_matrix = _assemble_correlation_matrix(scenario, len(series_ids), series_ids)
    matrix = _nearest_psd(raw_matrix)
    chol = np.linalg.cholesky(matrix)  # lower-triangular L, matrix == L @ L.T

    rng = np.random.default_rng(seed)
    # Standard normals in the fixed (trials, years, classes) shape.
    z = rng.standard_normal((n_trials, n_years, len(series_ids)))
    # Correlate: for each (trial, year) vector of independent normals, apply L.
    # z @ L.T gives correlated standard normals (Cov == L @ L.T == matrix).
    correlated = z @ chol.T
    if scenario.mc_distribution == MC_DISTRIBUTION_STUDENT_T:
        # Multivariate Student-t: one chi-square mixing draw per (trial, year),
        # shared across classes so the correlation structure is kept, scaled by
        # sqrt((df-2)/chi2) so each class keeps unit variance before the vol
        # scaling below. Drawn AFTER ``z`` so the Normal stream is unchanged.
        df = float(scenario.mc_t_df)
        chi2 = rng.chisquare(df, size=(n_trials, n_years, 1))
        correlated = correlated * np.sqrt((df - 2.0) / chi2)
    # Scale by per-class vol and shift by per-class arithmetic mean.
    returns = correlated * vols + means
    # A simple-return draw below -100% is impossible (it would make a balance
    # negative); floor at -99%. Rare under Normal (about 1 class-year in 60,000
    # for a heavy EM/small-value mix), more common under fat tails.
    np.maximum(returns, MIN_SIMPLE_RETURN, out=returns)
    return series_ids, mapped, returns


def _blended_trial_returns(
    scenario: Scenario,
    n_trials: int,
    n_years: int,
    seed: int,
) -> np.ndarray:
    """Per-trial blended real-return sequences: shape ``(n_trials, n_years)``.

    Samples per-class returns via the shared sampler, then blends them with the
    SAME renormalized per-series weights Historical uses (so the blend math can't
    drift between the two stochastic engines). Spend-independent, so the solver's
    evaluator computes this once and re-runs only the cheap depletion loop.
    """
    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        return _glidepath_blended_trial_returns(scenario, n_trials, n_years, seed)

    series_ids, _mapped, returns = _sample_class_returns(
        scenario, n_trials, n_years, seed
    )
    # Wider MC registry (matches _sample_class_returns) so the slice-1 classes'
    # weights are kept and renormalized over the same mapped set.
    weights = renormalized_weights(scenario, MC_SERIES_REGISTRY)  # {series_id: weight}, sums to 1
    # returns[:, :, k] is the k-th mapped series; weight it and sum across series.
    w = np.array([weights[sid] for sid in series_ids], dtype=float)
    return returns @ w  # (n_trials, n_years, n_classes) @ (n_classes,) -> (n_trials, n_years)


def _glidepath_union_allocation(scenario: Scenario) -> tuple:
    """The distinct asset classes across all glidepath phases (sampling set).

    Monte Carlo samples ONE return path per distinct class, then the per-year
    blend selects each phase's weights over those draws. So the sampler needs the
    UNION of classes any phase uses, deduped by class id (first occurrence wins —
    a class id carries one vol, so a later phase reusing it samples the same
    series). For a degenerate glide (every phase == the single allocation) the
    union is exactly that single allocation in its original order, so the draws —
    and thus the MC canary — are byte-identical to the constant path.
    """
    from firebridgelab.scenario import (
        glidepath_boundaries,
        validate_glidepath_class_consistency,
    )

    # Defense in depth: same id with differing return params across phases would be
    # silently swallowed by the first-occurrence-wins dedup below (deterministic
    # would meanwhile read each phase's own real_return) — different portfolios, no
    # error. Enforce the contract (per-class assumptions are global) loudly instead.
    validate_glidepath_class_consistency(scenario)

    boundaries = glidepath_boundaries(scenario)
    union: list = []
    seen: set[str] = set()
    for start_age in boundaries:
        for ac in allocation_at_age(scenario, start_age):
            if ac.id not in seen:
                seen.add(ac.id)
                union.append(ac)
    return tuple(union)


def _glidepath_blended_trial_returns(
    scenario: Scenario, n_trials: int, n_years: int, seed: int
) -> np.ndarray:
    """Per-trial blended real-return sequences under glidepath (phase weights).

    Glidepath design "Engine hook": MC honors the glide by sampling the union of
    phase classes ONCE (so the correlated draws are shared across phases) and then
    blending each YEAR with the renormalized per-series weights of the phase in
    force that year (the same per-phase weights Historical uses, via
    ``glidepath_phase_weights``). Shape ``(n_trials, n_years)``.

    Reduces to ``_blended_trial_returns`` byte-for-byte when every phase equals
    the single allocation: the union is that single allocation (identical draws),
    and every phase's weights are identical, so the per-year blend equals the
    constant ``returns @ w``.
    """
    from firebridgelab.historical import glidepath_phase_weights
    from firebridgelab.scenario import phase_index_for_age

    # Sample over the union of phase classes (deduped by id) — one draw per class,
    # shared across phases. Build a constant-mode scenario carrying the union as
    # its single allocation so the SHARED sampler is reused unchanged.
    union = _glidepath_union_allocation(scenario)
    sampling_scenario = replace(
        scenario, allocation=union, allocation_mode="constant", phase_allocations=None
    )
    series_ids, _mapped, returns = _sample_class_returns(
        sampling_scenario, n_trials, n_years, seed
    )

    # Per-phase renormalized per-series weights (same helper Historical uses).
    phase_weights = glidepath_phase_weights(scenario, MC_SERIES_REGISTRY)
    series_index = {sid: k for k, sid in enumerate(series_ids)}

    # The phase index in force at each age-step. ``n_years`` is the parameter the
    # sampler was called with (== ``returns.shape[1]`` by construction); use it
    # directly rather than rebinding from the array shape.
    phase_at_step = []
    for age in range(scenario.start_age, scenario.end_age + 1):
        index = phase_index_for_age(scenario, age)
        if index >= len(phase_weights):
            index = len(phase_weights) - 1
        phase_at_step.append(index)

    # Blend each CONTIGUOUS same-phase run with a single ``slice @ w`` matmul —
    # the IDENTICAL reduction the constant path uses (``returns @ w``), just over
    # a year-slice. Doing it per-run (not per-year via einsum) keeps the float
    # summation order bit-for-bit identical to the constant blend, so a degenerate
    # glide (one effective weight across all years) reproduces the MC canary
    # byte-for-byte. Phases are contiguous by construction, so a single left-to-
    # right pass covers every year exactly once.
    blended = np.empty((returns.shape[0], n_years), dtype=float)
    step = 0
    while step < n_years:
        index = phase_at_step[step]
        end = step
        while end < n_years and phase_at_step[end] == index:
            end += 1
        w = np.zeros(len(series_ids), dtype=float)
        for sid, weight in phase_weights[index].items():
            w[series_index[sid]] = weight
        blended[:, step:end] = returns[:, step:end, :] @ w
        step = end
    return blended


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

    When ``collect_spends`` is True (guardrails — variable-spending design slice 3)
    a ``SpendingStrategy`` is built explicitly per trial (BEFORE its year loop, so
    each trial resets the guardrail's carried state), and its per-year withdrawn
    spend + ``AdjustmentEvent`` log are captured: ``spends[t][k]`` is the spend at
    age step ``k`` of trial ``t`` (feeds the spend fan) and ``events[t]`` is that
    trial's adjustment-event log (feeds ``adjustment_stats``). Otherwise both are
    None and the per-trial spend/events are discarded — the constant/lifecycle path
    stays byte-stable (it builds the same byte-stable ``PureAgeStrategy`` either
    way, so the trajectory is identical whether or not events are collected).
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
    see the SPLIT_PARALLEL_* constants' comment). Per trial, exactly what the
    pre-memoization inline loop did:

      1. The DRAGGED decomposition (a fresh strategy reading the dragged, TAXED
         balance) — the trial's fan/spend/attribution contribution.
      2. The INDEPENDENT pre-tax counterfactual (``_simulate_cohort``, a
         SEPARATE fresh strategy reading the UNTAXED balance) — the DECISION 9
         estate-cost pairing only.

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
    """The split-mode analogue of ``_trial_trajectories`` (td-b fan re-point,
    DECISION 1 + DECISION 9): each trial runs TWO independent sims over the
    SAME ``blended[t]`` return sequence — "each is the honest run of its own
    world" (DECISION 9). See ``_split_trials_chunk`` for the per-trial pair.

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
        # 3 fat submissions instead of 10k tiny ones keeps IPC negligible.
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
        # consumption — same discipline as historical._split_cohort_step and
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


def _bucket_union_allocation(scenario: Scenario) -> tuple:
    """The distinct asset classes across all three buckets (the MC sampling set).

    Monte Carlo samples ONE correlated return path per distinct class, then each
    bucket's per-year blend selects its own weights over those shared draws (same
    pattern as the glidepath union — ``_glidepath_union_allocation``). Deduped by
    class id, first occurrence wins (a class id carries one vol, so a bucket reusing
    it samples the same series). Order: cash → income → growth.
    """
    bc = scenario.buckets
    union: list = []
    seen: set[str] = set()
    for bucket in (bc.cash, bc.income, bc.growth):
        for ac in bucket.allocation:
            if ac.id not in seen:
                seen.add(ac.id)
                union.append(ac)
    return tuple(union)


def _bucket_trial_returns(
    scenario: Scenario, n_trials: int, n_years: int, seed: int
) -> dict[str, np.ndarray]:
    """Per-bucket per-trial blended real returns: ``{bucket_id: (n_trials, n_years)}``.

    Samples the UNION of all bucket classes ONCE via the SHARED sampler (so the
    correlated draws are shared across buckets and the run is reproducible), then
    blends each bucket from those draws with its OWN renormalized per-series weights
    (the SAME ``renormalized_weights`` Historical/MC use). Mirrors
    ``_glidepath_blended_trial_returns``'s sample-once-then-reblend structure.
    """
    from firebridgelab.historical import renormalized_weights as _renorm

    bc = scenario.buckets
    union = _bucket_union_allocation(scenario)
    # Sample over the union as a single constant-allocation scenario so the SHARED
    # sampler is reused unchanged (single sourcing, constant allocation_mode).
    sampling_scenario = replace(
        scenario,
        allocation=union,
        sourcing_mode="single",
        allocation_mode="constant",
        phase_allocations=None,
        buckets=None,
    )
    series_ids, _mapped, returns = _sample_class_returns(
        sampling_scenario, n_trials, n_years, seed
    )  # returns: (n_trials, n_years, n_union_classes)
    series_index = {sid: k for k, sid in enumerate(series_ids)}

    out: dict[str, np.ndarray] = {}
    for bucket in (bc.cash, bc.income, bc.growth):
        # This bucket's renormalized per-series weights over the SAME mapped set.
        bucket_scenario = replace(
            sampling_scenario, allocation=bucket.allocation
        )
        weights = _renorm(bucket_scenario, MC_SERIES_REGISTRY)
        w = np.zeros(len(series_ids), dtype=float)
        for sid, weight in weights.items():
            w[series_index[sid]] = weight
        out[bucket.id] = returns @ w  # (n_trials, n_years)
    return out


def _simulate_monte_carlo_buckets(
    scenario: Scenario, n_trials: int, seed: int
) -> MonteCarloResult:
    """3-bucket Monte Carlo — sample once, run the shared bucket sim per trial.

    bucket-strategy design: sample the union of bucket classes (correlated, seeded),
    blend each bucket per trial, then run each trial's per-bucket return sequence
    through the SAME ``buckets.simulate_buckets_cohort`` Historical uses (so the
    mechanic can't drift). Aggregates to the portfolio fan + depletion exactly like
    the single-balance MC path, plus the buffer-survival readout.

    Spending is constant / lifecycle only (guardrails fenced at the API), so there
    is no spend fan / adjustment stats — the bucket value is the buffer-survival
    metric.
    """
    from firebridgelab.buckets import simulate_buckets_cohort

    n_years = scenario.end_age - scenario.start_age + 1
    bucket_returns = _bucket_trial_returns(scenario, n_trials, n_years, seed)

    matrix = np.empty((n_trials, n_years), dtype=float)
    first_forced_sale_ages: list[int | None] = []
    # Depletion is taken from each trial's per-cohort flag (step_year semantics: the
    # year the PRE-floor summed balance went strictly < 0), NOT re-derived from the
    # post-floor summed ``matrix == 0.0`` like the single-balance MC path. The two
    # agree for every trial EXCEPT one whose first zero-crossing lands EXACTLY on
    # 0.0: the post-floor ``== 0.0`` test would count it, but step_year (and now the
    # bucket per-cohort flag) does not. Reading depleted_age here keeps the bucket MC
    # depletion count consistent with the corrected per-cohort rule at that boundary
    # — the structural Gate-B identity. (The matrix is still summed for the fan.)
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

    Buckets (bucket-strategy design): when ``sourcing_mode == "buckets"`` the run
    routes to the dedicated 3-bucket trial path (``_simulate_monte_carlo_buckets``)
    — same fan + depletion aggregation, plus the buffer-survival readout. The
    bucket allocations supersede the single/glidepath allocation, so the bucket
    branch is checked FIRST.
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        # Tax-location split (tax-buckets design tb1) is SINGLE-mode only — NO-OP'd
        # under buckets sourcing (the 3×3 tax-location × cash/income/growth product
        # is a deferred later card). Logged so the mode interaction is visible.
        if has_tax_split(scenario):
            logging.getLogger(__name__).info(
                "tax-location split supplied with sourcing_mode=buckets; "
                "ignoring the split (tb1 is single-mode only)"
            )
        return _simulate_monte_carlo_buckets(scenario, n_trials, seed)

    n_years = scenario.end_age - scenario.start_age + 1

    blended = _blended_trial_returns(scenario, n_trials, n_years, seed)
    # Collect per-trial spends + adjustment events ONLY for collecting strategies
    # (guardrails today, the funded-ratio variants when they land), where spending
    # varies across trials and a spend fan / adjustment stats exist (variable-
    # spending design slice 3). The strategy-level ``collects_spends`` predicate
    # replaces the old ``spending_mode == guardrails`` gate — same value today
    # (only GuardrailStrategy collects among reachable modes), so byte-stable.
    # Constant/lifecycle take the lean path (no collection) and stay byte-stable.
    collects_spends = make_strategy(scenario).collects_spends

    tax_location_risk = None
    federal_tax = None
    estate_cost = None
    if has_tax_split(scenario):
        # td-b fan re-point (DECISION 1): the DRAGGED decomposition drives the
        # fan / spend fan / adjustment stats / depletion for split scenarios
        # (the ``matrix`` this returns IS the dragged total, a drop-in for
        # ``_trial_trajectories``'s pre-tax matrix below). ``tax_paths`` also
        # feeds ``tax_location_risk`` / ``federal_tax`` (unchanged from td-a —
        # this REPLACES the old separate per-trial list comprehension, so the
        # dragged decomposition is computed ONCE per trial, not twice). The
        # pre-tax counterfactual (DECISION 9) rides inside the same per-trial
        # loop and feeds ``estate_cost`` only.
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

    # Spending fan + adjustment stats — guardrails only (variable-spending design
    # slice 3). The SAME percentile machinery as the portfolio fan over the
    # (trials, n_years) spend matrix, and the SAME _compute_adjustment_stats the
    # Historical engine uses (so the two stat readouts can't drift). NO worst-trial
    # drill-down: MC trials are synthetic (no calendar year), so the worst-cohort
    # narrative does not translate — the design pins MC to fan + aggregate stats.
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
    """Build a fast survival-fraction function over a spend (build-2 solver hook).

    Mirrors ``solver._historical_evaluator``: the trials' blended-return arrays
    are spend-independent, so we sample them ONCE here (via the SAME shared
    sampler the display path uses, with the SAME seed/count — bit-identical
    draws), then the returned ``evaluate(spend)`` re-runs only the lean
    depletion-only sim per probe. Returns ``(evaluate, trial_count)``.

    Short-circuits before any percentile/fan work — depletion count only — same
    lean discipline as the solver's historical evaluator. Build 1 ships this so
    build 2's solver can call it unchanged; the display path does not use it.

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
