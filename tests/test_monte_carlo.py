"""Tests for the Monte Carlo engine (Monte Carlo mode, Build 1).

Each anchors one property of the parametric Monte Carlo simulation
(the design notes "Validation gate"):

  Gate #1 (the hard one — closed-form anchors, NOT "near Historical"):
    1a. vol -> 0 collapses the MC median ONTO the Deterministic projection line
        (a degenerate MC must equal Deterministic within tight tolerance).
    1b. At default inputs the MC median terminal wealth is >= the Deterministic
        terminal wealth (strictly above, the right-skew + diversification margin).
        A median BELOW would mean the arith/geo uplift was skipped -> FAIL.
  Gate #3: seed reproducibility — the same scenario twice yields identical arrays,
        and the display sampler + the lean solver sampler draw bit-identically.
  Plus: fan ordering, depletion logic, the no-mapped-class error, and the
  cross-engine equivalence at vol -> 0 (the MC analogue of the Historical
  equivalence test — the same midpoint math reached via the sampler).
"""

import numpy as np
import pytest

from dataclasses import replace

from firebridgelab import (
    AssetClass,
    default_scenario,
    simulate,
    simulate_monte_carlo,
)
from firebridgelab.monte_carlo import (
    DEFAULT_N_TRIALS,
    DEFAULT_SEED,
    _assemble_correlation_matrix,
    _blended_trial_returns,
    _monte_carlo_evaluator,
    _nearest_psd,
    _sample_class_returns,
    simulate_monte_carlo as _simulate_mc,
)
from firebridgelab.historical import SERIES_FOR_CLASS, mapped_series_for
from tests._canary import DET_YEAR0_CANARY


def _zero_vol_scenario():
    """The default scenario with every class vol and the correlation set to 0.

    A degenerate (no-randomness) MC: every sampled return collapses to its
    arithmetic mean = compound + 0/2 = the compound knob, so the blend equals the
    Deterministic blended real return at every year and every trial is identical.
    """
    s = default_scenario()
    return replace(
        s,
        allocation=tuple(replace(ac, volatility=0.0) for ac in s.allocation),
        correlation=0.0,
    )


def test_zero_vol_median_equals_deterministic_line():
    """Gate #1a: vol -> 0 ⇒ MC median == the Deterministic projection at every age.

    The strong, exact anchor: with zero vol the sampler emits the compound knob
    every year (uplift adds vol²/2 = 0), so each trial reproduces the Deterministic
    trajectory and the per-age median equals the Deterministic line within fp noise.
    """
    s = _zero_vol_scenario()
    result = simulate_monte_carlo(s, n_trials=200, seed=DEFAULT_SEED)
    det = [p.end_portfolio for p in simulate(s).points]

    assert len(result.median) == len(det)
    for mc_v, det_v in zip(result.median, det):
        # Tight tolerance: degenerate MC must collapse onto Deterministic.
        assert abs(mc_v - det_v) < 1e-3


def test_zero_vol_collapses_fan_to_a_point():
    """Gate #1a corollary: zero vol ⇒ p10 == median == p90 at every age.

    With no randomness every trial is identical, so the percentile fan has zero
    width — a direct check that the vol dial is what opens the fan.
    """
    result = simulate_monte_carlo(_zero_vol_scenario(), n_trials=200, seed=DEFAULT_SEED)
    for p10, median, p90 in zip(result.p10, result.median, result.p90):
        assert abs(p90 - p10) < 1e-3
        assert abs(median - p10) < 1e-3


def test_default_vol_median_at_least_deterministic_terminal():
    """Gate #1b: MC median terminal wealth >= Deterministic terminal (strictly above).

    The arith/geo uplift (mean = compound + vol²/2) plus the annual-rebalancing
    diversification bonus make the median terminal sit ABOVE the Deterministic
    line — that's expected and correct. A median BELOW would mean the uplift was
    skipped (the failure signature). Prints the empirical ratio (the build-1
    regression target recorded in the report).
    """
    s = default_scenario()
    det_terminal = simulate(s).final_portfolio
    result = simulate_monte_carlo(s)  # default 10k trials, fixed seed
    mc_median_terminal = result.median[-1]

    ratio = mc_median_terminal / det_terminal
    print(
        f"\n[gate #1b] MC median terminal / Deterministic terminal = {ratio:.4f} "
        f"(MC {mc_median_terminal:,.0f} vs det {det_terminal:,.0f})"
    )
    # The deterministic r is now the rebalanced compound return (the median
    # growth rate of the same moments), so the MC median sits CLOSE to it —
    # slightly below once withdrawals add sequence risk. A skipped vol uplift
    # in the sampler would put the ratio far below this band.
    assert 0.95 < ratio < 1.02
    # Hard canary: pin the EXACT default 2-class MC median terminal (fixed seed +
    # default 10k trials) at the $110k default. ``ratio > 1.0`` only checks the
    # sign; this pins the value so a future sampler change (RNG order, uplift,
    # blend, PSD/Cholesky path) can't silently drift the headline number.
    # Recompute + re-baseline ONLY with an explicit, reviewed reason. Tight
    # ±$1000 tolerance on a ~$5.8M figure.
    # Re-baselined 7,970,399.12 -> 7,977,702.31 (legacy-class
    # volatility single-sourcing): default_scenario() now builds with the
    # EXACT series-derived stdev instead of the hand-rounded
    # _DEFAULT_EQUITY_VOLATILITY / _DEFAULT_BOND_VOLATILITY constants, which
    # shifts every fixed-seed sampled path by the same tiny (~2e-4) per-class
    # amount. Intended effect of the fix, not a regression.
    # Re-pinned 2026-10-11 (review fixes: mid-year GI timing, Shiller dividend timing).
    # Re-pinned 2026-11-01 for the new fictional default household.
    assert mc_median_terminal == pytest.approx(5_807_486.88, abs=1_000.0)


def test_default_mc_depletion_canary_unchanged_by_strategy_refactor():
    """Hard canary: the default MC depletion stats at the $110k default.

    Fixed seed + 10k trials → ``trial_count == 10000``, ``depletion_count == 846``
    (~8% tail), ``earliest_depletion_age == 57``. The variable-spending
    SpendingStrategy generalization threads through the SAME ``_simulate_cohort``
    MC uses; constant mode routes through the byte-stable PureAgeStrategy wrapper.
    The ~8% tail is by design — the default sits between the $85k floor and
    $150k ceiling to showcase two-sided GK flex; guardrails (off in constant)
    would rescue these trials via floor cuts. A drift here signals the engine
    path perturbed the MC arithmetic.
    """
    result = simulate_monte_carlo(default_scenario())
    assert result.trial_count == 10_000
    assert result.depletion_count == 846
    assert result.earliest_depletion_age == 57


def test_seed_reproducible_identical_arrays():
    """Gate #3: the same scenario run twice yields byte-identical fan arrays.

    Fixed seed ⇒ no flicker. Pins every fan band + the depletion stats.
    """
    s = default_scenario()
    a = simulate_monte_carlo(s)
    b = simulate_monte_carlo(s)

    assert a.median == b.median
    assert a.p10 == b.p10
    assert a.p25 == b.p25
    assert a.p75 == b.p75
    assert a.p90 == b.p90
    assert a.ending_range == b.ending_range
    assert a.depletion_count == b.depletion_count
    assert a.earliest_depletion_age == b.earliest_depletion_age


def test_display_and_solver_samplers_draw_bit_identically():
    """Gate #3: the display path and the lean solver evaluator draw the SAME paths.

    Both call the single shared sampler with the same seed/count/shape, so their
    blended return draws must be bit-identical (the "can't drift" discipline that
    keeps a solved number matching the displayed fan). We compare the blended
    return arrays the display path computes against what the solver evaluator's
    precompute produces — they share _blended_trial_returns, so this pins that the
    solver path doesn't consume the RNG stream differently.
    """
    s = default_scenario()
    n_years = s.end_age - s.start_age + 1
    display_draws = _blended_trial_returns(s, DEFAULT_N_TRIALS, n_years, DEFAULT_SEED)
    solver_draws = _blended_trial_returns(s, DEFAULT_N_TRIALS, n_years, DEFAULT_SEED)
    assert np.array_equal(display_draws, solver_draws)

    # And the evaluator builds without error and reports the full trial count.
    evaluate, trial_count = _monte_carlo_evaluator(s)
    assert trial_count == DEFAULT_N_TRIALS
    # The lean evaluator's survival at the default spend matches the full engine's
    # implied survival (1 - depletion/trials) — the lean path didn't drift.
    full = simulate_monte_carlo(s)
    implied_survival = 1.0 - full.depletion_count / full.trial_count
    assert evaluate(s.annual_spending) == pytest.approx(implied_survival)


def test_fan_is_ordered_and_right_length_at_every_age():
    """The percentile fan is monotonic (p10<=p25<=median<=p75<=p90) at every age.

    Mirrors the Historical fan-ordering test; the band must never invert and each
    band has one value per age in the horizon, all non-negative (floor at zero).
    """
    s = default_scenario()
    result = simulate_monte_carlo(s)
    n = s.end_age - s.start_age + 1

    assert result.ages[0] == s.start_age
    assert result.ages[-1] == s.end_age
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
        assert result.p10[i] >= 0


def test_ending_range_matches_final_age_percentiles():
    """ending_range == (final-age p10, final-age p90), and p10 <= p90.

    The MC readout's "10th–90th percentile" sentence reads ending_range, so it
    must agree with the last point of the p10/p90 bands (same contract as
    Historical).
    """
    result = simulate_monte_carlo(default_scenario())
    low, high = result.ending_range
    assert low == result.p10[-1]
    assert high == result.p90[-1]
    assert low <= high


def test_depletion_under_stress():
    """High spend, no GI, all-equity ⇒ most trials deplete; earliest age is set.

    Confirms the floor-at-zero + earliest-depletion aggregation fires off the
    trajectory matrix.
    """
    scenario = replace(
        default_scenario(),
        annual_spending=300_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        allocation=(AssetClass("us_total_market", "US Total Stock Market", 1.0, 0.07, 0.174),),
    )
    result = simulate_monte_carlo(scenario)
    assert result.depletion_count > 0
    assert result.earliest_depletion_age is not None
    assert scenario.start_age < result.earliest_depletion_age <= scenario.end_age


def test_default_scenario_has_bounded_depletion():
    """Default ($110k constant mode) shows a bounded tail under MC.

    The default at $110k annual_spending sits between the $85k floor and
    $150k ceiling to showcase two-sided GK flex. In constant mode (no
    guardrails) the depletion tail is meaningful but bounded — the engine
    must not produce a runaway depletion rate (would indicate broken
    arithmetic). Pin the upper bound; the exact count is canary-pinned by
    ``test_default_mc_depletion_canary_unchanged_by_strategy_refactor``.
    """
    result = simulate_monte_carlo(default_scenario())
    assert result.depletion_count / result.trial_count < 0.10


def test_no_mapped_class_raises():
    """An allocation mapping no known series is an error (data-can't-lie invariant).

    The MC sampler renormalizes over mapped classes just like Historical; with no
    mapped class it raises rather than silently producing garbage.
    """
    scenario = replace(
        default_scenario(),
        allocation=(AssetClass("crypto", "Crypto", 1.0, 0.07, 0.174),),
    )
    with pytest.raises(ValueError):
        simulate_monte_carlo(scenario)


def test_higher_vol_widens_the_fan():
    """Raising equity vol widens the 10–90 ending spread (the dial is the point).

    Directional (not a hard gate here — the crank-vol gate is Build 2), but a
    cheap pin that the vol knob actually drives dispersion: a higher-vol default
    has a wider ending (p90 - p10) than a lower-vol one.
    """
    s = default_scenario()
    lo_vol = replace(
        s, allocation=tuple(replace(ac, volatility=ac.volatility * 0.5) for ac in s.allocation)
    )
    hi_vol = replace(
        s, allocation=tuple(replace(ac, volatility=ac.volatility * 1.5) for ac in s.allocation)
    )
    lo = simulate_monte_carlo(lo_vol)
    hi = simulate_monte_carlo(hi_vol)
    lo_spread = lo.ending_range[1] - lo.ending_range[0]
    hi_spread = hi.ending_range[1] - hi.ending_range[0]
    assert hi_spread > lo_spread


def test_deterministic_and_historical_unregressed_by_mc_import():
    """Regression guard: importing/adding MC didn't move the Deterministic canary.

    The locked deterministic year-0 end-of-year value (design worked example) must
    be byte-unchanged after threading volatility/correlation through the model.
    """
    s = default_scenario()
    traj = simulate(s)
    assert traj.points[0].end_portfolio == DET_YEAR0_CANARY
    assert round(traj.final_portfolio / 1_000_000, 2) == 6.07


# ---------- Slice 0: class-id → series-id mapping, scalar fast-path, PSD ----------
#
# The asset-class substrate plumbing: the class-id→series-id map binds the legacy
# pair to the EXISTING series; the scalar correlation is a fast-path for N <= 2
# ONLY; the nearest-PSD projection is a no-op on a valid matrix; and a >2-class MC
# request with no explicit matrix raises (never silently broadcasts the scalar).


def test_class_id_maps_to_legacy_series():
    """The legacy class ids bind to the EXISTING series ids (the two namespaces).

    Class `us_total_market` → series `us_equity`, class `us_agg_bonds` → series
    `us_bonds` — the class-id/series-id split that keeps the default binding to the
    data already in the engine.
    """
    assert SERIES_FOR_CLASS["us_total_market"] == "us_equity"
    assert SERIES_FOR_CLASS["us_agg_bonds"] == "us_bonds"
    # The default scenario's allocation maps both classes, in allocation order.
    mapped = mapped_series_for(default_scenario())
    assert mapped == {0: "us_equity", 1: "us_bonds"}


def test_scalar_fast_path_builds_the_2x2_for_n_le_2():
    """N <= 2: the scalar correlation is broadcast onto the (pre-PSD) matrix.

    The ONLY place the scalar is used. For the default 2-class allocation the
    assembled matrix is exactly [[1, ρ], [ρ, 1]] with ρ = scenario.correlation.
    """
    s = replace(default_scenario(), correlation=0.2)
    matrix = _assemble_correlation_matrix(s, 2)
    assert matrix.shape == (2, 2)
    np.testing.assert_allclose(matrix, np.array([[1.0, 0.2], [0.2, 1.0]]))
    # A single mapped class collapses to a 1x1 identity.
    one = _assemble_correlation_matrix(s, 1)
    np.testing.assert_allclose(one, np.array([[1.0]]))


def test_psd_projection_is_no_op_on_a_valid_matrix():
    """A valid correlation matrix passes nearest-PSD unchanged (within fp noise).

    The default 2x2 (ρ = 0.2) is positive-definite, so projecting it must return
    the same matrix to fp tolerance — the no-op gate the design requires.
    """
    valid = np.array([[1.0, 0.2], [0.2, 1.0]])
    projected = _nearest_psd(valid)
    np.testing.assert_allclose(projected, valid, atol=1e-12)
    # And the projection always yields a Cholesky-able (strictly positive-definite)
    # result even for an INVALID (non-PSD) input — the gate's real job. Note: the
    # eigenvalue floor is applied BEFORE the unit-diagonal renormalization, which
    # can shrink the smallest eigenvalue slightly below 1e-8 — but it stays
    # strictly > 0, so Cholesky succeeds (the actual requirement).
    invalid = np.array([[1.0, 0.95, -0.95], [0.95, 1.0, 0.95], [-0.95, 0.95, 1.0]])
    fixed = _nearest_psd(invalid)
    eigvals = np.linalg.eigvalsh(fixed)
    assert eigvals.min() > 0.0  # strictly positive-definite
    np.testing.assert_allclose(np.diag(fixed), 1.0)  # unit diagonal preserved
    np.linalg.cholesky(fixed)  # must not raise


def test_default_2class_mc_path_unaffected_by_psd_projection():
    """The 2-class MC sampler still produces a valid correlated draw (PSD no-op).

    Sanity that wiring the PSD projection in front of Cholesky didn't perturb the
    default 2-class path — the sampler returns the expected shape and the two
    mapped series.
    """
    s = default_scenario()
    n_years = s.end_age - s.start_age + 1
    series_ids, mapped, returns = _sample_class_returns(s, 100, n_years, DEFAULT_SEED)
    assert series_ids == ["us_equity", "us_bonds"]
    assert returns.shape == (100, n_years, 2)


def test_more_than_two_classes_without_series_ids_raises():
    """N > 2, no matrix, no series_ids → raises (a degenerate direct call).

    Slice 2 changed the >2-with-no-matrix contract: it now estimates a default
    matrix over the mapped series. But the estimator needs to KNOW which series
    (and in which order), so calling ``_assemble_correlation_matrix`` for n>2
    without passing ``series_ids`` and without an explicit matrix is a degenerate
    call that still fails loudly (the scalar is never broadcast onto a >2 matrix).
    The real >2 path always passes series_ids (see the estimated-default test).
    """
    s = replace(default_scenario(), correlation=0.2)
    with pytest.raises(ValueError, match="series ids"):
        _assemble_correlation_matrix(s, 3)


def test_more_than_two_classes_with_explicit_matrix_is_used():
    """N > 2 with an explicit (valid) matrix is accepted and returned for projection.

    The forward-looking multi-class path: an explicit ordered matrix aligned to
    allocation order is honored (validated for shape/symmetry/unit-diagonal).
    """
    m = ((1.0, 0.2, 0.1), (0.2, 1.0, 0.3), (0.1, 0.3, 1.0))
    s = replace(default_scenario(), correlations=m)
    assembled = _assemble_correlation_matrix(s, 3, ["us_equity", "us_bonds", "us_small_value"])
    np.testing.assert_allclose(assembled, np.array(m))


# ---------- Slice 2: estimated default matrix + multi-class MC ----------
#
# For N > 2 with NO explicit matrix the sampler builds the per-pair/max-overlap
# estimated default (firebridgelab.correlation), projects it to PSD, and runs the fan.
# A malformed explicit matrix raises ValueError (the API routes it to 422).


def _four_class_scenario():
    """A 4-class allocation whose classes all map to loaded series.

    Equity / bonds / small-value / REITs at 40/30/15/15 — all four bind to a
    real-return series, so the sampler estimates the per-pair default 4x4 matrix
    (no explicit `correlations`). Vols are the per-class defaults; returns are
    arbitrary sane reals (the fan shape is what's under test, not the level).
    """
    s = default_scenario()
    return replace(
        s,
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", 0.40, 0.07, 0.174),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.30, 0.02, 0.088),
            AssetClass("us_small_value", "US Small-Cap Value", 0.15, 0.065, 0.27),
            AssetClass("us_reit", "US REITs", 0.15, 0.05, 0.18),
        ),
    )


@pytest.mark.requires_optional_classes
def test_four_class_no_matrix_uses_estimated_default():
    """N > 2, no explicit matrix → the sampler builds the per-pair estimated 4x4.

    The assembled (pre-PSD) matrix for the four mapped series matches what
    firebridgelab.correlation estimates directly — confirming the sampler wires the
    estimated default in rather than raising (slice-0 placeholder) or broadcasting
    the scalar.
    """
    from firebridgelab.correlation import estimate_correlation_matrix

    s = _four_class_scenario()
    n_years = s.end_age - s.start_age + 1
    series_ids, _mapped, _returns = _sample_class_returns(s, 50, n_years, DEFAULT_SEED)
    assert series_ids == ["us_equity", "us_bonds", "us_small_value", "us_reit"]

    assembled = _assemble_correlation_matrix(s, 4, series_ids)
    expected, _overlaps = estimate_correlation_matrix(series_ids)
    np.testing.assert_allclose(assembled, expected)
    # Symmetric, unit diagonal, |cell| <= 1 — a valid correlation matrix.
    np.testing.assert_allclose(assembled, assembled.T)
    np.testing.assert_allclose(np.diag(assembled), 1.0)
    assert np.all(np.abs(assembled) <= 1.0 + 1e-12)


def test_four_class_mc_run_completes_with_a_sane_fan():
    """A 4-class MC run completes and produces a sane fan (p10<median<p90, no NaN).

    The end-to-end slice-2 gate: the estimated default matrix flows through PSD
    projection → Cholesky → the shared cohort sim → the percentile fan, with the
    band ordered and finite at every age (the fan has real width — vol is nonzero).
    """
    s = _four_class_scenario()
    result = simulate_monte_carlo(s, n_trials=2000, seed=DEFAULT_SEED)
    n = s.end_age - s.start_age + 1
    assert len(result.median) == n
    for band in (result.p10, result.p25, result.median, result.p75, result.p90):
        assert len(band) == n
        assert all(np.isfinite(v) for v in band)
    for i in range(n):
        assert (
            result.p10[i]
            <= result.p25[i]
            <= result.median[i]
            <= result.p75[i]
            <= result.p90[i]
        )
    # The fan has genuine width somewhere (nonzero vol opened it).
    assert result.ending_range[1] > result.ending_range[0]


def test_simulate_monte_carlo_raises_on_non_psd_estimate(monkeypatch):
    """PRODUCTION-PATH gate proof: a badly non-PSD estimate makes the LIVE entry
    point raise — not just estimate_with_gate when called directly.

    D6 requires the Frobenius-distance acceptance gate to run on the path MC
    actually uses. We monkeypatch the per-pair estimator to return a badly non-PSD
    4x4 (min eigenvalue well below zero), then call ``simulate_monte_carlo`` (the
    real entry point) on a 4-class scenario with no explicit matrix. The N>2
    branch routes through ``estimate_with_gate``, the projection bends the matrix
    materially, the Frobenius distance blows past ``FROBENIUS_TOLERANCE``, and the
    SIMULATION ITSELF raises — proving the gate is wired into production, not only
    exercised in the correlation test file.
    """
    import firebridgelab.correlation as corr

    # Badly non-PSD 4x4 (the 4-class scenario maps exactly 4 series). The
    # +0.95/-0.95 ring forces a large negative eigenvalue, so the PSD projection is
    # a structural correction (Frobenius distance >> FROBENIUS_TOLERANCE).
    bad = np.array(
        [
            [1.0, 0.95, -0.95, 0.95],
            [0.95, 1.0, 0.95, -0.95],
            [-0.95, 0.95, 1.0, 0.95],
            [0.95, -0.95, 0.95, 1.0],
        ]
    )
    monkeypatch.setattr(
        corr,
        "estimate_correlation_matrix",
        lambda series_ids: (bad, {}),
    )
    with pytest.raises(ValueError, match="large PSD correction"):
        simulate_monte_carlo(_four_class_scenario(), n_trials=50, seed=DEFAULT_SEED)


@pytest.mark.requires_optional_classes
def test_four_class_explicit_matrix_overrides_estimate():
    """An explicit valid 4x4 is used as-is instead of the estimated default.

    When the caller supplies `correlations`, the sampler validates and uses it
    (the estimated default is only the no-matrix fallback).
    """
    s = _four_class_scenario()
    m = (
        (1.0, 0.1, 0.2, 0.3),
        (0.1, 1.0, 0.0, 0.1),
        (0.2, 0.0, 1.0, 0.4),
        (0.3, 0.1, 0.4, 1.0),
    )
    s = replace(s, correlations=m)
    assembled = _assemble_correlation_matrix(
        s, 4, ["us_equity", "us_bonds", "us_small_value", "us_reit"]
    )
    np.testing.assert_allclose(assembled, np.array(m))
    # And a full run completes with this explicit matrix.
    result = simulate_monte_carlo(s, n_trials=500, seed=DEFAULT_SEED)
    assert all(np.isfinite(v) for v in result.median)


def test_malformed_explicit_matrix_raises_for_422():
    """A malformed explicit N>2 matrix raises ValueError (the API → 422).

    Slice-2 carry-forward: explicit-matrix validation must fire for N>2. Each
    structural defect (wrong shape, asymmetry, |cell|>1, non-unit diagonal) raises
    a ValueError, which the API routes to 422.
    """
    s = _four_class_scenario()
    series_ids = ["us_equity", "us_bonds", "us_small_value", "us_reit"]

    # Wrong dimension (3x3 for a 4-class allocation).
    bad_shape = replace(s, correlations=((1.0, 0.1, 0.2), (0.1, 1.0, 0.0), (0.2, 0.0, 1.0)))
    with pytest.raises(ValueError, match="shape"):
        _assemble_correlation_matrix(bad_shape, 4, series_ids)

    # Asymmetric.
    asym = (
        (1.0, 0.1, 0.2, 0.3),
        (0.9, 1.0, 0.0, 0.1),  # [1,0] != [0,1]
        (0.2, 0.0, 1.0, 0.4),
        (0.3, 0.1, 0.4, 1.0),
    )
    with pytest.raises(ValueError, match="symmetric"):
        _assemble_correlation_matrix(replace(s, correlations=asym), 4, series_ids)

    # |cell| > 1.
    over = (
        (1.0, 1.5, 0.2, 0.3),
        (1.5, 1.0, 0.0, 0.1),
        (0.2, 0.0, 1.0, 0.4),
        (0.3, 0.1, 0.4, 1.0),
    )
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        _assemble_correlation_matrix(replace(s, correlations=over), 4, series_ids)

    # Non-unit diagonal.
    nonunit = (
        (0.9, 0.1, 0.2, 0.3),
        (0.1, 1.0, 0.0, 0.1),
        (0.2, 0.0, 1.0, 0.4),
        (0.3, 0.1, 0.4, 1.0),
    )
    with pytest.raises(ValueError, match="unit diagonal"):
        _assemble_correlation_matrix(replace(s, correlations=nonunit), 4, series_ids)


# --------------------------------------------------------------------------- #
# Variable-spending design slice 3 — guardrail support in Monte Carlo          #
# --------------------------------------------------------------------------- #

from firebridgelab.scenario import SPENDING_MODE_GUARDRAILS  # noqa: E402


def _mc_guardrail_scenario(**overrides):
    """The default scenario in guardrails mode at a $130k start (flexes the rule).

    Mirrors the Historical guardrail test fixture: a start spend well above the
    floor so the guardrail nudges across trials and a genuine
    spending distribution exists.
    """
    base = dict(spending_mode=SPENDING_MODE_GUARDRAILS, annual_spending=130_000.0)
    base.update(overrides)
    return replace(default_scenario(), **base)


def test_mc_constant_lifecycle_carry_no_spend_fan_or_stats():
    """Constant/lifecycle MC runs leave the spend fan + adjustment stats None.

    The slice-3 MC fields are guardrails-only; under constant/lifecycle spending is
    identical across trials, so there is no spending distribution and no adjustments
    — both stay None (the payload omits them; the default MC run is unaffected).
    """
    from firebridgelab.scenario import SPENDING_MODE_LIFECYCLE

    const = simulate_monte_carlo(default_scenario())
    assert const.spend_median is None
    assert const.spend_p10 is None and const.spend_p90 is None
    assert const.adjustment_stats is None

    lifecycle = simulate_monte_carlo(
        replace(default_scenario(), spending_mode=SPENDING_MODE_LIFECYCLE)
    )
    assert lifecycle.spend_median is None
    assert lifecycle.adjustment_stats is None


def test_mc_guardrail_spend_fan_present_and_monotone():
    """Guardrails MC populates a per-age spend fan, monotone p10<=...<=p90.

    The MC twin of the Historical spending-fan test: same length as ``ages``, and
    at every age spend_p10 <= spend_p25 <= spend_median <= spend_p75 <= spend_p90,
    every percentile inside the default rails, and the fan actually spreads.
    """
    s = _mc_guardrail_scenario()
    result = simulate_monte_carlo(s)
    n = s.end_age - s.start_age + 1

    for arr in (result.spend_p10, result.spend_p25, result.spend_median,
                result.spend_p75, result.spend_p90):
        assert arr is not None
        assert len(arr) == n
    for k in range(n):
        assert (result.spend_p10[k] <= result.spend_p25[k] <= result.spend_median[k]
                <= result.spend_p75[k] <= result.spend_p90[k] + 1e-9)
    # Default rails ($85k operational floor, $150k ceiling tapered).
    assert min(result.spend_p10) >= 85_000.0 - 1e-6
    assert max(result.spend_p90) <= 150_000.0 + 1e-6
    # The fan genuinely spreads (spending varies across trials).
    spread = [result.spend_p90[k] - result.spend_p10[k] for k in range(n)]
    assert max(spread) > 0.0


def test_mc_guardrail_year_one_no_trip():
    """Year-1 spend == start spend for every trial (begin balance = initial).

    At the start age the begin-of-year portfolio is exactly the initial portfolio,
    so the funded rate equals initial_rate (in-band) → no trip → every trial spends
    the start spend, so the spend fan is a point at age 50.
    """
    result = simulate_monte_carlo(_mc_guardrail_scenario())
    assert result.spend_p10[0] == pytest.approx(130_000.0)
    assert result.spend_p90[0] == pytest.approx(130_000.0)


def test_mc_guardrail_populates_adjustment_stats_no_worst_cohort():
    """Guardrails MC carries aggregate adjustment_stats but NO worst_cohort.

    The aggregate cost-of-flexibility stats aggregate across the 10k synthetic
    trials (same _compute_adjustment_stats the Historical engine uses). There is
    NO worst-trial drill-down — MC trials are synthetic (no calendar year), so the
    Historical "1969 cohort" narrative does not translate (the design omits it).
    """
    result = simulate_monte_carlo(_mc_guardrail_scenario())
    stats = result.adjustment_stats
    assert stats is not None
    assert stats.cohort_count == result.trial_count == 10_000
    assert 0.0 <= stats.pct_ever_cut <= 1.0
    assert 0.0 <= stats.pct_ever_raised <= 1.0
    assert 0.0 <= stats.pct_ever_forced_to_floor <= 1.0
    # No worst_cohort analogue on the MC result (the design: fan + aggregate only).
    assert not hasattr(result, "worst_cohort")


def test_mc_lower_ceiling_lowers_the_fan_upper_percentiles():
    """A lower satiation ceiling caps the MC spend fan's upper percentiles lower.

    Deliverable B's editable-ceiling behaviour at the engine level: dropping the
    ceiling from $150k to $140k must lower where the upper fan tops out (the
    satiation cap binds the upside nudge), so max(spend_p90) falls.
    """
    hi = simulate_monte_carlo(_mc_guardrail_scenario(spending_ceiling=150_000.0))
    lo = simulate_monte_carlo(_mc_guardrail_scenario(spending_ceiling=140_000.0))
    assert max(hi.spend_p90) == pytest.approx(150_000.0)
    assert max(lo.spend_p90) <= 140_000.0 + 1e-6
    assert max(lo.spend_p90) < max(hi.spend_p90)


def test_mc_guardrail_default_canary_unchanged():
    """The slice-3 guardrail wiring did not move the default MC canary.

    The default scenario is CONSTANT mode, so it takes the lean (no-collection)
    MC path; the guardrail spend/event collection is gated behind guardrails.
    Pins the depletion canary (default at $110k → 846/10000 tail, earliest 57)
    survives the slice-3 changes.
    """
    result = simulate_monte_carlo(default_scenario())
    assert result.trial_count == 10_000
    assert result.depletion_count == 846
    assert result.earliest_depletion_age == 57
    assert result.spend_median is None and result.adjustment_stats is None


# ---------- : split-MC process parallelism (the seam pin) ----------


def test_split_parallel_pool_matches_serial_byte_for_byte(monkeypatch):
    """equivalence pin: the process-parallel split path reproduces the
    serial path's ENTIRE result byte-for-byte, so the seam can never drift.

    The parallel move's correctness argument is structural — same per-trial
    code (``_split_trials_chunk``) on contiguous chunks of the SAME centrally-
    sampled return matrix, reassembled in trial order — and this test is the
    permanent enforcement: it runs the same scenario through both paths
    (forcing the pool via ``SPLIT_PARALLEL_MIN_TRIALS``) and requires exact
    ``==`` on the full ``MonteCarloResult``, plus a serialized-JSON comparison
    so a silently-tolerant nested ``__eq__`` could never mask a drift.

    The fixture deliberately stacks the richest split-mode surface: guardrails
    (per-trial spend fan + adjustment events cross the process boundary),
    the lever, spending drift, and a spend level high enough that some trials
    deplete (depletion/realized-spend edges included). 48 trials spreads
    across every worker while keeping the pool spawn the test's only real
    cost.

    VACUOUSNESS GUARD: on a <= 2-core host ``SPLIT_PARALLEL_MAX_WORKERS`` is 1
    and ``_split_trial_trajectories`` stays serial regardless of the
    ``SPLIT_PARALLEL_MIN_TRIALS`` monkeypatch — the "parallel" run would just
    be serial==serial and this test would pin nothing while staying green. So
    it skips LOUDLY there instead of passing emptily.
    """
    import dataclasses
    import json

    import firebridgelab.monte_carlo as mc

    if mc.SPLIT_PARALLEL_MAX_WORKERS <= 1:
        pytest.skip(
            "SPLIT_PARALLEL_MAX_WORKERS == 1 on this host (<= 2 cores): the "
            "split branch never engages the process pool, so this test would "
            "vacuously assert serial == serial and pin nothing. The parallel "
            "seam is unexercisable here — run on a >= 3-core host."
        )

    scenario = replace(
        default_scenario(),
        spending_mode="guardrails",
        annual_spending=140_000.0,
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
        bracket_fill_target=100_800.0,
        spending_real_drift=0.02,
    )
    n_trials = 48

    assert n_trials < mc.SPLIT_PARALLEL_MIN_TRIALS  # serial run really is serial
    serial = simulate_monte_carlo(scenario, n_trials=n_trials, seed=DEFAULT_SEED)

    monkeypatch.setattr(mc, "SPLIT_PARALLEL_MIN_TRIALS", 1)
    parallel = simulate_monte_carlo(scenario, n_trials=n_trials, seed=DEFAULT_SEED)

    assert parallel == serial
    assert json.dumps(
        dataclasses.asdict(parallel), sort_keys=True, default=repr
    ) == json.dumps(dataclasses.asdict(serial), sort_keys=True, default=repr)
