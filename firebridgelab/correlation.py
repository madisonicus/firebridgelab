"""Cross-class real-return correlation estimator (asset-class design D6).

Slice 2 of the asset-class substrate. The Monte Carlo sampler needs a cross-class
correlation matrix whenever a scenario runs more than two asset classes (the
scalar ``scenario.correlation`` is a fast-path for N <= 2 only). When the caller
supplies no explicit matrix, this module produces the **estimated default**: each
pair's correlation estimated over THAT pair's own longest honest overlap of real
annual returns (the standard CMA construction), NOT a single all-class common
window.

Why per-pair max-overlap (D6)
-----------------------------
The loaded classes have wildly asymmetric histories (us_equity/us_bonds from
1871, the Treasuries from ~1928/1954, REITs from 1972). A single common window
would collapse to the shortest series' start (~1972 for the 7 loaded classes)
and bake the post-1972 regime into every pair — discarding a century of
equity/bond data. Estimating each pair over its own maximum overlap keeps every
pair as honest as its data allows.

Two namespaces (asset-class design)
----------------------------------
The estimator works in the **series-id** namespace (the data key), not the class
id. Callers pass series ids (resolved from a scenario's class ids via
``historical.SERIES_FOR_CLASS``). The unified accessor
(``real_return_series_by_series_id``) pulls the legacy pair (``us_equity`` /
``us_bonds``) from Shiller and the slice-1 classes from ``class_series``, so a
single ``{year: real_return}`` view spans both sources.

Min-overlap floor + prior (D6)
------------------------------
A pair with fewer than ``MIN_OVERLAP`` (10) overlapping annual observations is too
thin for a trustworthy sample estimate, so it falls back to a documented **prior**
rather than a noisy correlation. The default prior is ``DEFAULT_PRIOR`` = 0.0
(treat the pair as independent) unless a more specific prior is registered for the
unordered series-id pair in ``PAIR_PRIORS``. The floor and the prior are explicit
(named constants, tested) so the fallback is visible, not a silent zero.

PSD projection + Frobenius gate (D6)
------------------------------------
The raw per-pair matrix need not be positive-semidefinite (independently
estimated cells can violate the joint constraint). To keep the per-pair honesty
from being silently undone by a large projection, ``estimate_with_gate`` first
asserts the **Frobenius distance** between the estimated matrix and its
nearest-PSD projection is within ``FROBENIUS_TOLERANCE`` (and reports the max
per-cell delta), raising loudly past it so a large correction surfaces rather
than hides.

This gate runs ON THE LIVE MC PATH: the sampler's N>2-no-explicit-matrix branch
(``monte_carlo._assemble_correlation_matrix``) calls ``estimate_with_gate`` and
uses its already-projected matrix, so a badly non-PSD estimate raises inside
``simulate_monte_carlo`` itself (→ 422 at the API) — not only when a test calls
``estimate_with_gate`` directly. (The N<=2 scalar fast-path and an explicit
caller-supplied matrix do NOT reach this estimator/gate; they pass through
``monte_carlo._nearest_psd`` only.)
"""

from __future__ import annotations

import numpy as np

from firebridgelab.class_series import load_class_series
from firebridgelab.shiller import load_shiller

# Minimum overlapping annual observations for a sample correlation estimate
# (asset-class design D6). A pair below this falls back to the documented prior
# rather than a noisy sample value. Pinned, not a parameter.
MIN_OVERLAP = 10

# Documented fallback prior for a pair below the min-overlap floor: 0.0 means
# "treat the pair as independent" (asset-class design D6). This is a deliberate,
# conservative default — not a sample estimate — and is the value used unless a
# more specific prior is registered in PAIR_PRIORS for the pair.
DEFAULT_PRIOR = 0.0

# Pair-specific priors for thin-overlap pairs (asset-class design D6: "unless a
# more specific prior is documented for the pair. Name the prior in the data
# file."). Keyed by the FROZENSET of the two SERIES ids so order doesn't matter.
# Empty in slice 2 — every loaded pair has >= MIN_OVERLAP overlap, so the floor
# never fires on the real data; the mechanism is here (and tested) for the
# shorter-history classes (TIPS/EM) that land in slice 4. Add an entry as
# {frozenset({"a", "b"}): value} with a one-line rationale when a specific prior
# is justified.
PAIR_PRIORS: dict[frozenset[str], float] = {}

# Frobenius-distance acceptance tolerance between the estimated matrix and its
# nearest-PSD projection (asset-class design D6 gate). The behavior is a function
# of WHICH series the caller mixes:
#   - A small, long-overlap subset (e.g. the legacy equity/bond pair, or the
#     1926/28+ US-core classes) estimates to an already-PSD matrix, so the
#     distance is at machine-epsilon scale (~1e-15) and the gate is a no-op.
#   - The FULL 12-class estimate is NOT PSD (its min eigenvalue is negative,
#     ~ -0.17): the short-history classes (TIPS 2001+, EM 1995+, commodities
#     1980+) are each estimated over their own thin per-pair overlaps, which need
#     not be jointly consistent. The nearest-PSD projection then makes a
#     *structural* correction (Frobenius distance ~0.2, max per-cell delta ~0.07),
#     which EXCEEDS this tolerance and trips the gate on purpose — a broad MC mix
#     raises ValueError (→ a user-facing 422), telling the user to narrow the mix
#     or use Historical/Deterministic instead of silently trusting a bent matrix.
# Generous enough to pass a genuinely-PSD estimate's fp noise, tight enough to
# catch a structural projection.
FROBENIUS_TOLERANCE = 1e-6


def real_return_series_by_series_id() -> dict[str, dict[int, float]]:
    """Unified ``{series_id: {year: real_return}}`` view across both sources.

    The legacy pair (``us_equity`` / ``us_bonds``) comes from Shiller
    (``shiller.py``); the slice-1 classes (``us_small_value``, the Treasury
    ladder, ``us_reit``) come from the staged ``class_series`` CSV. Both are
    annual REAL returns, so a single year-keyed dict per series spans the two
    sources. Loaders are memoized, so this is cheap to call repeatedly.

    Returns a per-series ``{year: real_return}`` map (year-keyed so a pair's
    overlap is just the intersection of two years' key sets) for exactly the
    series that have a real-return series loaded today (the 7 of the 12 canonical
    classes whose data is staged — intl/EM/TIPS/gold/commodities land later).
    """
    shiller = load_shiller()
    series: dict[str, dict[int, float]] = {
        "us_equity": dict(
            zip(shiller.years.tolist(), shiller.stock_real_return.tolist())
        ),
        "us_bonds": dict(
            zip(shiller.years.tolist(), shiller.bond_real_return.tolist())
        ),
    }
    for series_id, cs in load_class_series().items():
        series[series_id] = dict(zip(cs.years.tolist(), cs.real_return.tolist()))
    return series


def pairwise_correlation(
    a_by_year: dict[int, float], b_by_year: dict[int, float]
) -> tuple[float, int]:
    """Pearson correlation of two annual real-return series over their overlap.

    The overlap is the set of years present in BOTH series (per-pair max-overlap
    — D6); the correlation is estimated over exactly those years. Returns
    ``(rho, overlap_n)``.

    Below the min-overlap floor (``overlap_n < MIN_OVERLAP``) the sample estimate
    is too noisy to trust, so the documented prior is returned instead (a
    pair-specific ``PAIR_PRIORS`` value if registered, else ``DEFAULT_PRIOR`` =
    0.0 / independent). The returned ``overlap_n`` is the TRUE overlap count
    (so a caller can see the floor fired), even when the returned rho is the
    prior.
    """
    common_years = sorted(set(a_by_year) & set(b_by_year))
    overlap_n = len(common_years)
    if overlap_n < MIN_OVERLAP:
        # Floor: documented prior, not a noisy sample estimate (D6).
        return DEFAULT_PRIOR, overlap_n
    x = np.array([a_by_year[y] for y in common_years], dtype=float)
    y = np.array([b_by_year[y] for y in common_years], dtype=float)
    rho = float(np.corrcoef(x, y)[0, 1])
    return rho, overlap_n


def _prior_for(series_a: str, series_b: str) -> float:
    """The documented prior for a thin-overlap pair (pair-specific else default)."""
    return PAIR_PRIORS.get(frozenset({series_a, series_b}), DEFAULT_PRIOR)


def estimate_correlation_matrix(
    series_ids: list[str],
) -> tuple[np.ndarray, dict[frozenset[str], int]]:
    """Estimate the N x N cross-class correlation matrix (per-pair max-overlap).

    ``series_ids`` are the data-key (series id) names IN THE ORDER the caller
    wants the matrix laid out (the sampler aligns row/col i to allocation slot
    i). Each off-diagonal cell ``(i, j)`` is the Pearson correlation of series i
    and series j estimated over THAT pair's own longest overlap of real annual
    returns; the diagonal is exactly 1.0. A pair below ``MIN_OVERLAP`` overlapping
    observations gets its documented prior (``_prior_for``) instead of a sample
    estimate.

    Returns ``(matrix, overlaps)`` where ``matrix`` is the symmetric estimated
    correlation matrix (pre-PSD — it need not be positive-semidefinite) and
    ``overlaps`` maps each unordered series-id pair (a ``frozenset``) to its
    overlap window length, so a caller can report / sanity-check the per-pair
    window depth.

    Raises ``ValueError`` if a series id has no loaded real-return series (the
    accessor doesn't carry it) — the same data-can't-lie discipline the sampler
    and Historical replay use.
    """
    all_series = real_return_series_by_series_id()
    missing = [sid for sid in series_ids if sid not in all_series]
    if missing:
        raise ValueError(
            f"no real-return series loaded for {missing} "
            f"(have {sorted(all_series)})"
        )

    n = len(series_ids)
    matrix = np.eye(n, dtype=float)
    overlaps: dict[frozenset[str], int] = {}
    for i in range(n):
        for j in range(i + 1, n):
            rho, overlap_n = pairwise_correlation(
                all_series[series_ids[i]], all_series[series_ids[j]]
            )
            if overlap_n < MIN_OVERLAP:
                rho = _prior_for(series_ids[i], series_ids[j])
            matrix[i, j] = matrix[j, i] = rho
            overlaps[frozenset({series_ids[i], series_ids[j]})] = overlap_n
    return matrix, overlaps


class CorrelationNotEstimable(ValueError):
    """Raised when the D6 Frobenius gate in ``estimate_with_gate`` fails: the
    per-pair correlation estimate is too non-PSD to trust, so projecting it onto
    the nearest PSD matrix would require a large structural correction.

    A ``ValueError`` subclass (so the API's existing ``except ValueError``
    fallbacks still catch it) but typed — the same pattern as
    ``SpendingStrategyNotSolvable`` — so the API can translate it to a friendly
    422 by class, not by string-matching the Frobenius/PSD message text. The
    carried message is the technical gate detail (Frobenius distance, per-cell
    delta) — correct for a log, mapped to plain language at the API seam.
    """


def estimate_with_gate(
    series_ids: list[str],
) -> tuple[np.ndarray, dict[frozenset[str], int], float]:
    """Estimate the matrix, PSD-project it, and assert the Frobenius gate (D6).

    Wires the per-pair estimate through ``monte_carlo._nearest_psd`` (eigenvalue
    clip to the pinned floor + unit-diagonal renormalize) and checks the
    Frobenius distance between the estimated and projected matrices is within
    ``FROBENIUS_TOLERANCE`` — so a large silent correction (a badly non-PSD
    estimate bent into shape) surfaces rather than hides, per D6.

    Returns ``(projected, overlaps, frobenius_distance)``. ``projected`` is the
    PSD matrix ready for Cholesky; ``overlaps`` is the per-pair window map;
    ``frobenius_distance`` is the estimated-vs-projected distance (reported so a
    caller can log it / cross-check the gate).

    Raises ``ValueError`` if the Frobenius distance exceeds the tolerance — the
    acceptance gate failing loudly is the whole point (an estimate that needs a
    structural correction is a data problem to investigate, not to paper over).

    Imports ``_nearest_psd`` lazily to avoid a module-level import cycle
    (``monte_carlo`` imports this module for the default-matrix path).
    """
    from firebridgelab.monte_carlo import _nearest_psd

    estimated, overlaps = estimate_correlation_matrix(series_ids)
    projected = _nearest_psd(estimated)
    frobenius_distance = float(np.linalg.norm(estimated - projected, ord="fro"))
    if frobenius_distance > FROBENIUS_TOLERANCE:
        max_cell_delta = float(np.max(np.abs(estimated - projected)))
        raise CorrelationNotEstimable(
            "estimated correlation matrix required a large PSD correction "
            f"(Frobenius distance {frobenius_distance:.3e} exceeds tolerance "
            f"{FROBENIUS_TOLERANCE:.1e}; max per-cell delta {max_cell_delta:.3e}) "
            "— inspect the per-pair estimates before trusting the projection"
        )
    return projected, overlaps, frobenius_distance
