"""Tests for the slice-1 asset-class data layer (asset-class design, slice 1).

These pin the data-layer pipeline for the NEW asset classes — the staged annual
REAL return series (firebridgelab.returns.class_series), the class-menu seeding with the
geometric-mean real return + stdev (firebridgelab.engine.allocation.CLASS_MENU), and the series
registration (firebridgelab.returns.series.SERIES_FOR_CLASS / SERIES_REGISTRY_NEW).

The real correctness gate (design slice-1 "Gate") is the **closed-form
self-check**: each series' geometric mean must reproduce its realized cumulative
growth, i.e. (1+g)^N == prod(1+r_t). That identity is asserted for every series.

The slice's other invariant — the default 2-class scenario's NUMERIC value
canary stays byte-stable (D4 does not reseed equity/bonds) — is pinned in
test_simulate.py / test_api.py and re-confirmed here at the allocation level.
"""

import numpy as np
import pytest

from firebridgelab.returns.class_series import (
    NEW_CLASS_SERIES,
    ClassSeries,
    load_class_series,
)
from firebridgelab.returns.series import (
    SERIES_FOR_CLASS,
    SERIES_REGISTRY,
    SERIES_REGISTRY_NEW,
)
from firebridgelab.engine.allocation import CLASS_MENU, blended_real_return
from firebridgelab.engine.scenario import default_scenario


# Expected approximate start year per series (asset-class design D5). These pin
# the data window so a re-fetch that silently truncates a series surfaces.
EXPECTED_START_YEAR = {
    # slice 1
    "us_small_value": 1927,  # Ken French annual VW begins 1927 (1926-07 monthly)
    "lt_treasury": 1928,     # Damodaran histretSP begins 1928
    "it_treasury": 1954,     # FRED GS5 begins 1953-04 → first full Jan-Jan 1954
    "st_treasury": 1928,     # Damodaran 3-mo T.Bill begins 1928
    "us_reit": 1972,         # FTSE Nareit base Dec 1971 → first return year 1972
    # slice 4a
    "gold": 1972,            # Damodaran gold col; pre-1971 $35-peg artifact excluded (FYI #2)
    "intl_developed": 1970,  # EFASIM (MSCI EAFE proxy) live 1969-12-31 → 1970 full
    "emerging_markets": 1995,  # VWOSIM (FTSE EM proxy) inception 1994-05 → first full 1995
    "commodities": 1980,     # GSGSIM (S&P GSCI proxy) inception 1979-12-27 → first full 1980
    "tips": 2001,            # TIPSIM (Bloomberg US-TIPS proxy) inception 2000-06 → first full 2001
}

# Sane real geometric-mean ranges (fractions) per class — wide enough not to be
# brittle, tight enough to catch a deflation/units bug (e.g. a nominal leak).
SANE_GEO_RANGE = {
    # slice 1
    "us_small_value": (0.07, 0.14),   # highest-returning factor, ~11% real
    "lt_treasury": (0.005, 0.030),    # long Treasury ~1.5% real
    "it_treasury": (0.005, 0.030),    # intermediate Treasury ~1.2% real
    "st_treasury": (-0.010, 0.020),   # T-bills barely beat inflation, ~0.3% real
    "us_reit": (0.040, 0.090),        # REITs ~6.5% real since 1972
    # slice 4a. Gold's band is wider/higher than the design's "~0-3%" prior:
    # 1972-2025 gold ran $35→~$3,500/oz (8.9% nominal, 3.9% CPI → 4.8% real),
    # verified not a nominal leak (decomposed in the build review). The prior was
    # a loose hint, not ground truth.
    "gold": (0.00, 0.07),             # ~4.8% real over 1972-2025
    "intl_developed": (0.02, 0.08),   # EAFE ~4.6% real
    "emerging_markets": (0.00, 0.10),  # EM ~3.8% real, very high vol
    "commodities": (-0.04, 0.04),     # near-zero real, ~-0.2%
    "tips": (-0.005, 0.040),          # low real, ~1.8%
}


def test_all_new_series_load():
    """Every slice-1 series is present in the staged data with a positive length."""
    series = load_class_series()
    for sid in NEW_CLASS_SERIES:
        assert sid in series, f"missing series {sid}"
        cs = series[sid]
        assert isinstance(cs, ClassSeries)
        assert cs.n > 0
        assert len(cs.years) == len(cs.real_return) == cs.n


@pytest.mark.parametrize("sid", NEW_CLASS_SERIES)
def test_closed_form_geometric_mean_identity(sid):
    """THE GATE: (1+geo_mean)^N reproduces realized cumulative growth prod(1+r_t).

    This is the slice-1 correctness check — the geometric mean is *defined* as
    the constant rate reproducing realized growth, so if this fails the seeded
    Deterministic default would drift off the true compounded path.
    """
    cs = load_class_series()[sid]
    lhs = (1.0 + cs.geometric_mean) ** cs.n
    rhs = cs.cumulative_growth
    assert lhs == pytest.approx(rhs, rel=1e-9)
    # And it equals the direct prod(1+r) — independent of the cached property.
    assert rhs == pytest.approx(float(np.prod(1.0 + cs.real_return)), rel=1e-12)


@pytest.mark.parametrize("sid", NEW_CLASS_SERIES)
def test_series_start_year_as_expected(sid):
    """Each series begins at its sourced start year (a silent-truncation guard)."""
    cs = load_class_series()[sid]
    assert cs.first_year == EXPECTED_START_YEAR[sid]
    # Last year should be recent (data refreshed in 2026) — at least 2023.
    assert cs.last_year >= 2023


@pytest.mark.parametrize("sid", NEW_CLASS_SERIES)
def test_geometric_mean_in_sane_range(sid):
    """Real geo mean sits in the documented sane band (catches a nominal leak)."""
    cs = load_class_series()[sid]
    lo, hi = SANE_GEO_RANGE[sid]
    assert lo <= cs.geometric_mean <= hi, (
        f"{sid} geo {cs.geometric_mean:.4%} outside [{lo:.2%}, {hi:.2%}]"
    )


@pytest.mark.parametrize("sid", NEW_CLASS_SERIES)
def test_stdev_in_sane_range(sid):
    """Annual real-return stdev is positive and below an implausible ceiling.

    Small-value is the most volatile (~30%); even that is well under 50%. A zero
    or absurd stdev would mean a parsing bug, not a real series.
    """
    cs = load_class_series()[sid]
    assert 0.01 < cs.stdev < 0.50, f"{sid} stdev {cs.stdev:.4%} implausible"
    # arithmetic >= geometric always (Jensen); the gap is ~vol^2/2.
    assert cs.arithmetic_mean >= cs.geometric_mean - 1e-12


def test_geometric_below_arithmetic_by_vol_squared_over_two():
    """geo ≈ arith − vol²/2 (the variance drag), to first order.

    This pins the D2 relationship the MC sampler relies on (it derives each
    class's arithmetic drift as geometric + own vol²/2). The approximation is
    first-order, so allow a generous tolerance — it just confirms the sign and
    rough magnitude, not an exact identity.
    """
    for sid in NEW_CLASS_SERIES:
        cs = load_class_series()[sid]
        approx_geo = cs.arithmetic_mean - 0.5 * cs.stdev**2
        assert cs.geometric_mean == pytest.approx(approx_geo, abs=0.02)


def test_class_menu_seeded_with_geometric_mean():
    """CLASS_MENU carries each NEW class at weight 0, real_return = geo mean,
    volatility = series stdev (D2/D5). The menu values must equal the loader's."""
    series = load_class_series()
    for sid in NEW_CLASS_SERIES:
        assert sid in CLASS_MENU, f"{sid} missing from CLASS_MENU"
        ac = CLASS_MENU[sid]
        assert ac.id == sid
        assert ac.label  # non-empty display name
        assert ac.weight == 0.0  # inert menu entry, not in default allocation
        assert ac.real_return == pytest.approx(series[sid].geometric_mean)
        assert ac.volatility == pytest.approx(series[sid].stdev)


def test_new_classes_registered_class_id_equals_series_id():
    """SERIES_FOR_CLASS maps each NEW class id to a series id == the class id
    (D5), and SERIES_REGISTRY_NEW carries every staged series."""
    for sid in NEW_CLASS_SERIES:
        assert SERIES_FOR_CLASS[sid] == sid
        assert sid in SERIES_REGISTRY_NEW
    # The legacy pair still binds to the existing Shiller series (untouched).
    assert SERIES_FOR_CLASS["us_total_market"] == "us_equity"
    assert SERIES_FOR_CLASS["us_agg_bonds"] == "us_bonds"


def test_new_series_not_in_shiller_registry():
    """The NEW series are NOT in the Shiller-backed SERIES_REGISTRY — they come
    from a separate staged source and aren't cohort-replayable until slice 3, so
    a weighted-but-new class is treated as UNMAPPED (renormalized out) in a slice-
    1 Historical run rather than crashing the Shiller replay path."""
    for sid in NEW_CLASS_SERIES:
        assert sid not in SERIES_REGISTRY


def test_default_scenario_allocation_unchanged():
    """D4 confirmation: the default allocation stays length-2 at 7%/2% blending
    to 5.0% real — seeding the menu must NOT perturb the default scenario (its
    numeric value canary lives in test_simulate.py / test_api.py)."""
    ds = default_scenario()
    assert len(ds.allocation) == 2
    assert ds.allocation[0].id == "us_total_market"
    assert ds.allocation[0].weight == 0.60
    assert ds.allocation[0].real_return == 0.07
    assert ds.allocation[1].id == "us_agg_bonds"
    assert ds.allocation[1].weight == 0.40
    assert ds.allocation[1].real_return == 0.02
    assert blended_real_return(ds) == pytest.approx(0.05)
