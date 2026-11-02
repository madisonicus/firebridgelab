"""Monte Carlo return sampling: correlated per-class draws and their blends.

``_sample_class_returns`` is the single shared sampler: per-class Normal (or
multivariate Student-t) real returns, correlated through a Cholesky factor of
the nearest-PSD correlation matrix. ``_blended_trial_returns``,
``_glidepath_blended_trial_returns`` and ``_bucket_trial_returns`` reduce the
draws to per-trial portfolio returns for constant, glidepath and bucket
allocations. ``rebalanced_compound_return`` gives the Deterministic engine the
long-run compound return of the same moments.
"""

from __future__ import annotations

import functools
from dataclasses import replace
import numpy as np

from firebridgelab.engine.allocation import ALLOCATION_MODE_GLIDEPATH, allocation_at_age
from firebridgelab.engine.scenario import MC_DISTRIBUTION_STUDENT_T, Scenario
from firebridgelab.returns.series import (
    MC_SERIES_REGISTRY,
    mapped_series_for,
    renormalized_weights,
)


# Floor on any sampled simple return (see _sample_class_returns).
MIN_SIMPLE_RETURN = -0.99

# Valid correlation range. Exactly +/-1 is positive-SEMIdefinite (rank-deficient)
# so np.linalg.cholesky raises; clamp to [-CORRELATION_LIMIT, +CORRELATION_LIMIT]
# and 422 outside it at the API.
CORRELATION_LIMIT = 0.99

# Eigenvalue floor for the nearest-PSD projection: a PINNED 1e-8 floor. After
# clipping negative eigenvalues to this floor and renormalizing to unit diagonal,
# the matrix is strictly positive-definite, so np.linalg.cholesky always succeeds.
# For an already-valid matrix (e.g. any valid 2x2) the projection is a no-op within
# fp noise. The value is pinned (not a parameter) so the projection is reproducible
# across runs.
EIGENVALUE_FLOOR = 1e-8


def _nearest_psd(matrix: np.ndarray) -> np.ndarray:
    """Project a symmetric matrix to the nearest positive-(semi)definite one.

    The acceptance gate every correlation matrix passes before Cholesky:
    symmetrize, eigendecompose, clip eigenvalues to the
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

    Three paths:
      - ``n <= 2``: the scalar ``scenario.correlation`` fast-path — 1.0 on the
        diagonal, the scalar on the single off-diagonal pair. The ONLY place the
        scalar is used. This is exactly the 2x2 equity-bond case (or a trivial
        1x1 identity for a single mapped class).
      - ``n > 2`` with an explicit ``scenario.correlations``: validate and use it.
        The scalar is NEVER broadcast onto a >2 matrix (a uniform off-diagonal is
        a silent wrong default).
      - ``n > 2`` with NO explicit matrix: fall back to the per-pair/max-overlap
        **estimated default** matrix over the mapped ``series_ids`` (in allocation
        order), via ``returns.correlation.estimate_with_gate``. ``series_ids`` MUST
        be supplied
        for this branch (the estimator needs to know which series, in which
        order); the sampler always passes them.

    The explicit matrix is validated for shape (n x n) and structure (symmetric,
    unit diagonal within tolerance, |cell| <= 1) and a malformed one raises
    ``ValueError`` (the API routes it to 422). The
    assembled matrix is the INPUT to the nearest-PSD projection — it need not be
    PSD here (the per-pair estimate especially need not be).
    """
    if n <= 2:
        matrix = np.full((n, n), float(scenario.correlation))
        np.fill_diagonal(matrix, 1.0)
        return matrix

    # n > 2 with no explicit matrix: the estimated default. The
    # scalar is still NEVER broadcast onto a >2 matrix.
    if scenario.correlations is None:
        if series_ids is None:
            raise ValueError(
                "a >2-class Monte Carlo run with no explicit correlation matrix "
                "needs the mapped series ids to estimate the default matrix"
            )
        # Lazy import to avoid a module-level cycle (correlation imports back into
        # this module for _nearest_psd).
        from firebridgelab.returns.correlation import estimate_with_gate

        # estimate_with_gate runs the Frobenius-distance acceptance gate on the
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
    # ALIGNMENT HAZARD: ``n`` here is the count of the MAPPED series (the subset
    # of the allocation that binds to a known series, in allocation order). The
    # explicit ``scenario.correlations`` matrix is supplied by the USER in full
    # ALLOCATION order (one row/col per allocation entry). When every class maps,
    # the two orders coincide and the shape check below passes. An allocation with
    # an unmapped class makes the user's matrix LARGER than the mapped subset; it
    # is not reindexed here, so it fails SAFE (an oversized matrix mismatches
    # (n, n) → ValueError → 422). Accepting it would mean reindexing to the mapped
    # subset (drop the unmapped rows/cols, preserving mapped order) BEFORE these
    # checks — otherwise the wrong cells would align to the wrong series.
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
    return an annually-rebalanced mix earns (~0.4%/yr below the Monte Carlo
    median for a 60/40). Using the SAME moments the MC sampler draws from:

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
    # class_series), letting every staged class sample.
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
    # Compound knob -> arithmetic mean: the variance-drag uplift.
    means = np.array(
        [ac.real_return + ac.volatility**2 / 2.0 for ac in classes], dtype=float
    )

    # Assemble (scalar fast-path for N<=2; explicit matrix or the per-pair
    # estimated default for N>2), then project to the nearest PSD matrix
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
    # Wider MC registry (matches _sample_class_returns) so the staged classes'
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
    union is exactly that single allocation in its original order, so the draws
    are bit-identical to the constant path.
    """
    from firebridgelab.engine.allocation import glidepath_boundaries
    from firebridgelab.engine.validation import validate_glidepath_class_consistency

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

    MC honors the glide by sampling the union of
    phase classes ONCE (so the correlated draws are shared across phases) and then
    blending each YEAR with the renormalized per-series weights of the phase in
    force that year (the same per-phase weights Historical uses, via
    ``glidepath_phase_weights``). Shape ``(n_trials, n_years)``.

    Reduces to ``_blended_trial_returns`` bit-for-bit when every phase equals
    the single allocation: the union is that single allocation (identical draws),
    and every phase's weights are identical, so the per-year blend equals the
    constant ``returns @ w``.
    """
    from firebridgelab.returns.series import glidepath_phase_weights
    from firebridgelab.engine.allocation import phase_index_for_age

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
    # glide (one effective weight across all years) reproduces the constant
    # path bit-for-bit. Phases are contiguous by construction, so a single left-to-
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
    from firebridgelab.returns.series import renormalized_weights as _renorm

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
