"""Tests for the federal-tax overlay (federal-tax overlay design ft1).

Mirrors ``tests/test_tax_location.py``'s idioms: hand-checked pure-function
cases (bracket edges, the stacked-LTCG split, NIIT, the 65+ deduction), a
hand-checkable overlay case (composition, penalty gating, RMD excess), and the
API contract (feature-off byte-stability, feature-on shape in all three modes).
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from firebridgelab import default_scenario
from firebridgelab.tax.core import (
    EARLY_WITHDRAWAL_PENALTY_RATE,
    NIIT_RATE,
    NIIT_THRESHOLD_MFJ,
    STANDARD_DEDUCTION_MFJ,
    ADDITIONAL_DEDUCTION_65_PLUS,
    compute_federal_tax,
)
from firebridgelab.tax.overlay import aggregate_tax_overlay, compute_tax_overlay
from firebridgelab.engine.historical import simulate_historical
from firebridgelab.engine.monte_carlo import simulate_monte_carlo
from firebridgelab.engine.validation import ScenarioValidationError, validate_scenario
from firebridgelab.engine.deterministic import simulate
from firebridgelab.tax.location_path import TaxLocationPath, simulate_tax_location_deterministic

client = TestClient(app)


# ---------- (a) compute_federal_tax: pure-function hand-checked cases ----------


def test_zero_income_zero_tax():
    y = compute_federal_tax(0.0, 0.0)
    assert y.ordinary_tax == 0.0
    assert y.ltcg_tax == 0.0
    assert y.niit == 0.0
    assert y.total == 0.0


def test_deduction_only_band_no_tax():
    """Ordinary income entirely inside the standard deduction -> zero tax."""
    y = compute_federal_tax(STANDARD_DEDUCTION_MFJ - 1.0, 0.0)
    assert y.total == 0.0


@pytest.mark.parametrize(
    "bracket_top,rate_below,rate_at",
    [
        (24_800.0, 0.10, 0.12),
        (100_800.0, 0.12, 0.22),
        (211_400.0, 0.22, 0.24),
        (403_550.0, 0.24, 0.32),
        (512_450.0, 0.32, 0.35),
        (768_700.0, 0.35, 0.37),
    ],
)
def test_ordinary_bracket_edges(bracket_top, rate_below, rate_at):
    """A dollar under vs over each bracket edge taxes at the OLD vs NEW marginal
    rate for that last dollar (taxable income = ordinary_income - deduction)."""
    under = compute_federal_tax(STANDARD_DEDUCTION_MFJ + bracket_top - 1.0, 0.0)
    at = compute_federal_tax(STANDARD_DEDUCTION_MFJ + bracket_top, 0.0)
    over = compute_federal_tax(STANDARD_DEDUCTION_MFJ + bracket_top + 1.0, 0.0)
    # The marginal step from "at the edge" to "one dollar over" is taxed at the
    # NEW (higher) rate; the step from "one under" to "at the edge" at the OLD.
    assert at.ordinary_tax - under.ordinary_tax == pytest.approx(rate_below)
    assert over.ordinary_tax - at.ordinary_tax == pytest.approx(rate_at)


def test_ltcg_fully_inside_zero_bracket_bridge_year():
    """Bridge year: low ordinary income, big gains -> gains land entirely in the
    0% LTCG bracket (not flat-rated)."""
    ordinary_income = 20_000.0
    ltcg_income = 50_000.0
    y = compute_federal_tax(ordinary_income, ltcg_income)
    # deduction 32200 > ordinary_income(20000); leftover 12200 reduces taxable
    # LTCG to 37800, entirely below the $98,900 0% breakpoint.
    assert y.ordinary_tax == 0.0
    assert y.ltcg_tax == 0.0
    assert y.niit == 0.0
    assert y.total == 0.0


def test_ltcg_straddles_0_15_breakpoint_hand_computed():
    """Gains straddle the $98,900 0%/15% breakpoint; hand-compute the split."""
    ordinary_income = STANDARD_DEDUCTION_MFJ + 80_000.0  # taxable_ordinary = 80,000
    ltcg_income = 40_000.0  # leftover deduction is 0 (ordinary > deduction)
    y = compute_federal_tax(ordinary_income, ltcg_income)
    # LTCG band = [80,000, 120,000]: 18,900 @ 0% (up to 98,900) + 21,100 @ 15%.
    expected_ltcg_tax = 21_100.0 * 0.15
    assert y.ltcg_tax == pytest.approx(expected_ltcg_tax)
    # Ordinary tax on 80,000 taxable: 24,800@10% + 55,200@12%.
    expected_ordinary_tax = 24_800.0 * 0.10 + 55_200.0 * 0.12
    assert y.ordinary_tax == pytest.approx(expected_ordinary_tax)
    assert y.niit == 0.0
    assert y.total == pytest.approx(expected_ordinary_tax + expected_ltcg_tax)


def test_leftover_deduction_reduces_ltcg_and_crosses_20_pct():
    """Low ordinary income leaves most of the deduction unused; the leftover
    reduces taxable LTCG before the gains are stacked/sliced (and this case
    reaches into the 20% top LTCG bracket, plus triggers NIIT)."""
    ordinary_income = 5_000.0
    ltcg_income = 650_000.0
    y = compute_federal_tax(ordinary_income, ltcg_income)
    leftover = STANDARD_DEDUCTION_MFJ - ordinary_income  # 27,200
    taxable_ltcg = ltcg_income - leftover  # 622,800
    # LTCG band = [0, 622,800]: 98,900@0% + 514,800@15% + 9,100@20%.
    expected_ltcg_tax = 514_800.0 * 0.15 + 9_100.0 * 0.20
    assert y.ordinary_tax == 0.0
    assert y.ltcg_tax == pytest.approx(expected_ltcg_tax)
    magi = ordinary_income + ltcg_income
    expected_niit = NIIT_RATE * min(ltcg_income, magi - NIIT_THRESHOLD_MFJ)
    assert y.niit == pytest.approx(expected_niit)
    assert y.total == pytest.approx(expected_ltcg_tax + expected_niit)
    assert taxable_ltcg > 613_700.0  # sanity: this case does reach the top band


def test_niit_triggers_above_250k_magi():
    ordinary_income = 190_000.0
    ltcg_income = 100_000.0
    y = compute_federal_tax(ordinary_income, ltcg_income)
    magi = ordinary_income + ltcg_income  # 290,000
    expected_niit = NIIT_RATE * min(ltcg_income, magi - NIIT_THRESHOLD_MFJ)
    assert expected_niit == pytest.approx(1_520.0)
    assert y.niit == pytest.approx(expected_niit)


def test_65_plus_deduction_kicks_in():
    """The extra $1,650/person deduction shields income a same-dollar under-65
    household would owe tax on."""
    ordinary_income = STANDARD_DEDUCTION_MFJ + 2_800.0  # 2,800 taxable at n=0
    under_65 = compute_federal_tax(ordinary_income, 0.0, n_65_plus=0)
    over_65 = compute_federal_tax(ordinary_income, 0.0, n_65_plus=2)
    assert under_65.total == pytest.approx(2_800.0 * 0.10)
    # 2 x $1,650 = $3,300 additional deduction fully absorbs the 2,800 taxable.
    assert ADDITIONAL_DEDUCTION_65_PLUS * 2 > 2_800.0
    assert over_65.total == 0.0


# ---------- (b) compute_tax_overlay: now a PURE REPACKAGING pass (td1) ----------
#
# Under tax-drag design td1, ``compute_tax_overlay`` no longer derives income /
# tax / attribution from draws — that machinery moved to
# ``firebridgelab.tax_location.step_buckets_dragged`` (the fixed point needs the
# account balances, which live there). The hand-computed composition/penalty/
# RMD/attribution anchors that used to live HERE now live in
# ``tests/test_tax_location.py`` (``test_hand_anchor_*`` and
# ``test_attribution_identity_exact_every_age_real_run`` etc.) — testing
# ``step_buckets_dragged`` directly, where that logic actually resolves. These
# tests instead pin ``compute_tax_overlay``'s NEW, narrower contract: it reads
# a fully-populated DRAGGED ``TaxLocationPath``'s recorded series verbatim (a
# pure repackaging pass, td-d retired the one field it used to derive,
# ``net_spending``) and folds the scalar reductions (sums / medians /
# worst-year / terminal balances).


def _fully_populated_dragged_path() -> TaxLocationPath:
    """A hand-picked, FULLY POPULATED (all td1 fields set) TaxLocationPath —
    not derived from a fixed-point run; every value is chosen directly so
    ``compute_tax_overlay``'s pass-through + scalar-reduction arithmetic is
    independently hand-checkable. The attribution identity is satisfied by
    construction at each age (200=200+0+0; 900=300+100+500; 1500=400+200+900).

    ``realized_spend`` (td-b) deliberately DIVERGES from ``spending`` at the
    last age (9,000 intended vs 6,000 realized — a fundable-capped year), so
    the ``effective_rate`` test below can PIN that the overlay reads the
    REALIZED consumption series, never the intended one — a fixture where the
    two are equal would leave that contract untested."""
    return TaxLocationPath(
        ages=[58, 75, 90],
        taxable=[0.0, 50_000.0, 10_000.0],
        tax_deferred=[40_000.0, 30_000.0, 5_000.0],
        roth=[10_000.0, 5_000.0, 0.0],
        taxable_depletion_age=None,
        reachable_depletion_age=None,
        forced_pre_595_draw=True,
        taxable_draw=[0.0, 1_000.0, 2_000.0],
        tax_deferred_draw=[2_000.0, 3_000.0, 4_000.0],
        roth_draw=[0.0, 500.0, 0.0],
        spending=[5_000.0, 7_000.0, 9_000.0],
        realized_spend=[5_000.0, 7_000.0, 6_000.0],
        pretax_total=[100_000.0, 80_000.0, 20_000.0],
        rmd_excess=[0.0, 3_000.0, 4_000.0],
        lever_transfer=[0.0, 0.0, 0.0],
        ordinary_income=[2_000.0, 50_000.0, 80_000.0],
        ltcg_income=[0.0, 1_000.0, 2_000.0],
        tax=[200.0, 900.0, 1_500.0],
        spending_tax=[200.0, 300.0, 400.0],
        voluntary_tax=[0.0, 100.0, 200.0],
        forced_tax=[0.0, 500.0, 900.0],
        penalty=[150.0, 0.0, 0.0],
        # gross_outflow == realized_spend + tax (the td-b identity the engine
        # records; the last age is 6,000 + 1,500 — realized, never the 9,000
        # intended). Hand-set here (this fixture never runs the engine); the
        # real-run identity is pinned in test_tax_location.py's
        # test_gross_outflow_reads_realized_on_real_depleting_run.
        gross_outflow=[5_200.0, 7_900.0, 7_500.0],
    )


def test_compute_tax_overlay_copies_recorded_series_verbatim():
    """Every per-age series compute_tax_overlay exposes is copied VERBATIM from
    the dragged path, never re-derived (pure repackaging — td-d retired the one
    field that used to be derived, ``net_spending``)."""
    scenario = default_scenario()
    path = _fully_populated_dragged_path()
    overlay = compute_tax_overlay(scenario, path)

    assert overlay.ages == path.ages
    assert overlay.ordinary_income == path.ordinary_income
    assert overlay.ltcg_income == path.ltcg_income
    assert overlay.tax == path.tax
    assert overlay.spending_tax == path.spending_tax
    assert overlay.voluntary_tax == path.voluntary_tax
    assert overlay.forced_tax == path.forced_tax
    assert overlay.penalty == path.penalty
    assert overlay.gross_outflow == path.gross_outflow


def test_compute_tax_overlay_scalars_hand_checked():
    """Every path scalar (sums / averages / worst-year / terminal balances),
    hand-computed against the fully-populated fixture above. td-b:
    ``effective_rate`` reads the REALIZED consumption series — the fixture's
    last age has realized 6,000 vs intended 9,000, so this assert FAILS if
    the overlay ever regresses to the intended series."""
    scenario = default_scenario()
    path = _fully_populated_dragged_path()
    overlay = compute_tax_overlay(scenario, path)

    assert overlay.total_tax == pytest.approx(200.0 + 900.0 + 1_500.0)
    assert overlay.avg_annual_tax == pytest.approx((200.0 + 900.0 + 1_500.0) / 3)
    assert overlay.effective_rate == pytest.approx(
        (200.0 + 900.0 + 1_500.0) / (5_000.0 + 7_000.0 + 6_000.0)
    )
    assert overlay.worst_tax == pytest.approx(1_500.0)
    assert overlay.worst_tax_age == 90
    assert overlay.total_forced_tax == pytest.approx(0.0 + 500.0 + 900.0)
    assert overlay.total_voluntary_tax == pytest.approx(0.0 + 100.0 + 200.0)
    # "Estate cost of taxes" (DECISION 9, a later slice) inputs — the terminal
    # paired pre-tax total and the terminal dragged total.
    assert overlay.pretax_terminal == pytest.approx(20_000.0)
    assert overlay.dragged_terminal == pytest.approx(10_000.0 + 5_000.0 + 0.0)


def test_aggregate_tax_overlay_medians():
    """aggregate_tax_overlay reduces several paths to per-age + scalar medians."""
    scenario = default_scenario()
    path_a = _fully_populated_dragged_path()
    # Perturb path_b's intended AND realized spend so the median differs from
    # a single path's value.
    path_b = replace(
        path_a,
        spending=[s + 1_000.0 for s in path_a.spending],
        realized_spend=[s + 1_000.0 for s in path_a.realized_spend],
    )
    overlay_a = compute_tax_overlay(scenario, path_a)
    overlay_b = compute_tax_overlay(scenario, path_b)

    agg = aggregate_tax_overlay([overlay_a, overlay_b])
    assert agg.ages == overlay_a.ages
    assert len(agg.tax_median) == len(agg.ages)
    assert len(agg.forced_tax_median) == len(agg.ages)
    assert len(agg.voluntary_tax_median) == len(agg.ages)
    assert len(agg.gross_outflow_median) == len(agg.ages)
    # Median of two values is their mean.
    for i in range(len(agg.ages)):
        assert agg.tax_median[i] == pytest.approx(
            (overlay_a.tax[i] + overlay_b.tax[i]) / 2.0
        )
        assert agg.forced_tax_median[i] == pytest.approx(
            (overlay_a.forced_tax[i] + overlay_b.forced_tax[i]) / 2.0
        )
        assert agg.voluntary_tax_median[i] == pytest.approx(
            (overlay_a.voluntary_tax[i] + overlay_b.voluntary_tax[i]) / 2.0
        )
        assert agg.gross_outflow_median[i] == pytest.approx(
            (overlay_a.gross_outflow[i] + overlay_b.gross_outflow[i]) / 2.0
        )
    assert agg.median_avg_annual_tax == pytest.approx(
        (overlay_a.avg_annual_tax + overlay_b.avg_annual_tax) / 2.0
    )
    assert agg.median_effective_rate == pytest.approx(
        (overlay_a.effective_rate + overlay_b.effective_rate) / 2.0
    )
    assert agg.median_total_forced_tax == pytest.approx(
        (overlay_a.total_forced_tax + overlay_b.total_forced_tax) / 2.0
    )
    assert agg.median_total_voluntary_tax == pytest.approx(
        (overlay_a.total_voluntary_tax + overlay_b.total_voluntary_tax) / 2.0
    )


def test_aggregate_tax_overlay_empty():
    agg = aggregate_tax_overlay([])
    assert agg.ages == []
    assert agg.tax_median == []
    assert agg.forced_tax_median == []
    assert agg.median_avg_annual_tax == 0.0
    assert agg.median_total_forced_tax == 0.0


# ---------- (c) invariants: draws non-negative + reconcile with the net draw ----------


def test_engine_draws_non_negative_and_reconcile_with_net_draw():
    """Runs the REAL deterministic decomposition (not the hand-built path above)
    on a funded split and checks: every recorded draw is >= 0, and at each age
    the three draws sum to the SAME net-draw the tb1 module itself computes
    (``spending*(1+r/2) - gi``, when positive) within float tolerance."""
    from firebridgelab.engine.allocation import blended_real_return_at_age
    from firebridgelab.income.guaranteed import gi_at_age

    base = default_scenario()
    s = replace(
        base,
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)

    assert len(path.spending) == len(path.ages)
    assert len(path.taxable_draw) == len(path.ages)
    assert len(path.tax_deferred_draw) == len(path.ages)
    assert len(path.roth_draw) == len(path.ages)

    for i, age in enumerate(path.ages):
        assert path.taxable_draw[i] >= 0.0
        assert path.tax_deferred_draw[i] >= 0.0
        assert path.roth_draw[i] >= 0.0

        r = blended_real_return_at_age(s, age)
        gi = gi_at_age(s, age)
        net = (path.spending[i] - gi) * (1 + r / 2)
        total_draw = path.taxable_draw[i] + path.tax_deferred_draw[i] + path.roth_draw[i]
        if net <= 0:
            # Surplus year: no draws (the surplus is a taxable contribution).
            assert total_draw == pytest.approx(0.0, abs=1e-6)
        else:
            assert total_draw == pytest.approx(net, abs=1e-6)


def test_taxable_gains_fraction_validated_on_scenario():
    base = default_scenario()
    validate_scenario(base)  # default (0.5) passes
    bad = replace(base, taxable_gains_fraction=1.5)
    with pytest.raises(ScenarioValidationError, match="taxable_gains_fraction"):
        validate_scenario(bad)
    bad2 = replace(base, taxable_gains_fraction=-0.1)
    with pytest.raises(ScenarioValidationError, match="taxable_gains_fraction"):
        validate_scenario(bad2)


def test_taxable_gains_fraction_changes_ltcg_income():
    """A higher gains-fraction knob raises LTCG income (and therefore tax) for
    the SAME taxable draw — a smoke check that the knob actually reaches the
    overlay (Scenario -> compute_tax_overlay)."""
    base = default_scenario()
    s_low = replace(
        base,
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
        taxable_gains_fraction=0.1,
    )
    s_high = replace(s_low, taxable_gains_fraction=0.9)
    path_low = simulate_tax_location_deterministic(s_low)
    path_high = simulate_tax_location_deterministic(s_high)
    overlay_low = compute_tax_overlay(s_low, path_low)
    overlay_high = compute_tax_overlay(s_high, path_high)
    # Some age must have a nonzero taxable draw for this to be meaningful.
    assert any(d > 0 for d in path_low.taxable_draw)
    assert sum(overlay_high.ltcg_income) > sum(overlay_low.ltcg_income)


# ---------- (d) API: feature-off byte-stable, feature-on shape in all 3 modes ----------


def test_feature_off_deterministic_payload_has_no_federal_tax():
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    assert "federal_tax" not in res.json()


def test_feature_off_historical_payload_has_no_federal_tax():
    res = client.post("/api/trajectory", json={"mode": "historical"})
    assert res.status_code == 200
    assert "federal_tax" not in res.json()


def test_feature_off_monte_carlo_payload_has_no_federal_tax():
    res = client.post("/api/trajectory", json={"mode": "monte_carlo"})
    assert res.status_code == 200
    assert "federal_tax" not in res.json()


def _split_body(mode: str, **extra) -> dict:
    body = {
        "mode": mode,
        "taxable_balance": 1_080_000.0,
        "tax_deferred_balance": 504_000.0,
        "roth_balance": 216_000.0,
    }
    body.update(extra)
    return body


def test_api_deterministic_federal_tax_block_shape():
    res = client.post("/api/trajectory", json=_split_body("deterministic"))
    assert res.status_code == 200
    body = res.json()
    assert "federal_tax" in body
    ft = body["federal_tax"]
    assert set(ft) == {
        "ages",
        "tax",
        "spending_tax",
        "voluntary_tax",
        "forced_tax",
        "gross_outflow",
        "realized_spend",
        "ordinary_income",
        "ltcg_income",
        "penalty",
        "total_tax",
        "avg_annual_tax",
        "effective_rate",
        "worst_tax",
        "worst_tax_age",
        "total_forced_tax",
        "total_voluntary_tax",
    }
    n = len(ft["ages"])
    for key in (
        "tax",
        "spending_tax",
        "voluntary_tax",
        "forced_tax",
        "gross_outflow",
        "realized_spend",
        "ordinary_income",
        "ltcg_income",
        "penalty",
    ):
        assert len(ft[key]) == n
    # td-c DECISION 6: the identity is now the THREE-way split (this used to be
    # spending_tax + forced_tax == tax, false once the lever is active — this
    # fixture has no lever, so voluntary_tax is 0 at every age and the OLD
    # two-way check would still (misleadingly) pass; the three-way check below
    # is the one that actually pins the current contract).
    assert all(t >= 0.0 for t in ft["tax"])
    for i in range(n):
        assert (
            ft["spending_tax"][i] + ft["voluntary_tax"][i] + ft["forced_tax"][i]
            == ft["tax"][i]
        )
        assert ft["voluntary_tax"][i] == 0.0  # no lever on this fixture
        # DECISION 7: gross_outflow - realized_spend == tax, exact, every age.
        assert ft["gross_outflow"][i] - ft["realized_spend"][i] == pytest.approx(
            ft["tax"][i], abs=1e-9
        )
    # Key-swap guard: pin the payload's spending_tax/forced_tax series to the
    # engine's own overlay for the SAME resolved scenario. The identity above
    # is swap-symmetric (a + b == b + a), so it alone would still pass with the
    # two keys swapped in api/main.py's dict assembly — this equality would
    # not. The differ-somewhere sanity check keeps the guard non-vacuous.
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    overlay = compute_tax_overlay(s, path)
    assert any(st != f for st, f in zip(overlay.spending_tax, overlay.forced_tax))
    assert ft["spending_tax"] == overlay.spending_tax
    assert ft["forced_tax"] == overlay.forced_tax
    assert ft["gross_outflow"] == overlay.gross_outflow
    assert ft["realized_spend"] == path.realized_spend
    assert ft["total_voluntary_tax"] == pytest.approx(overlay.total_voluntary_tax)


def test_api_deterministic_federal_tax_block_lever_active_three_way_identity():
    """The identity's real test: with the lever ON, voluntary_tax is genuinely
    nonzero at some age and the three-way split still holds exactly through the
    API's JSON round-trip."""
    res = client.post(
        "/api/trajectory",
        json=_split_body("deterministic", bracket_fill_target=100_800.0),
    )
    assert res.status_code == 200
    ft = res.json()["federal_tax"]
    assert any(v > 0.0 for v in ft["voluntary_tax"])
    for i in range(len(ft["ages"])):
        assert (
            ft["spending_tax"][i] + ft["voluntary_tax"][i] + ft["forced_tax"][i]
            == ft["tax"][i]
        )


def test_api_historical_federal_tax_block_shape():
    res = client.post("/api/trajectory", json=_split_body("historical"))
    assert res.status_code == 200
    body = res.json()
    assert "federal_tax" in body
    ft = body["federal_tax"]
    assert set(ft) == {
        "ages",
        "tax_median",
        "forced_tax_median",
        "voluntary_tax_median",
        "gross_outflow_median",
        "median_avg_annual_tax",
        "median_effective_rate",
        "median_worst_tax",
        "median_worst_tax_age",
        "median_total_forced_tax",
        "median_total_voluntary_tax",
    }
    assert len(ft["tax_median"]) == len(ft["ages"])
    assert len(ft["forced_tax_median"]) == len(ft["ages"])
    assert len(ft["voluntary_tax_median"]) == len(ft["ages"])
    assert len(ft["gross_outflow_median"]) == len(ft["ages"])


def test_api_monte_carlo_federal_tax_block_shape():
    res = client.post("/api/trajectory", json=_split_body("monte_carlo"))
    assert res.status_code == 200
    body = res.json()
    assert "federal_tax" in body
    ft = body["federal_tax"]
    assert set(ft) == {
        "ages",
        "tax_median",
        "forced_tax_median",
        "voluntary_tax_median",
        "gross_outflow_median",
        "median_avg_annual_tax",
        "median_effective_rate",
        "median_worst_tax",
        "median_worst_tax_age",
        "median_total_forced_tax",
        "median_total_voluntary_tax",
    }


def test_api_buckets_mode_noop_split_no_federal_tax_block():
    """tb1 (and therefore ft1) is single-mode only: buckets sourcing no-ops the
    split, so no federal_tax block either."""
    res = client.post(
        "/api/trajectory",
        json=_split_body("deterministic", sourcing_mode="buckets"),
    )
    assert res.status_code == 200
    assert "federal_tax" not in res.json()


def test_api_taxable_gains_fraction_out_of_range_422():
    res = client.post(
        "/api/trajectory", json=_split_body("deterministic", taxable_gains_fraction=1.5)
    )
    assert res.status_code == 422
    assert "taxable_gains_fraction" in res.json()["detail"]


def test_api_taxable_gains_fraction_get_query_param_422():
    res = client.get("/api/trajectory", params={"taxable_gains_fraction": -0.2})
    assert res.status_code == 422
    assert "taxable_gains_fraction" in res.json()["detail"]


# ---------- (d.1) bracket_fill_target lever (tax-drag design td1 DECISION 5, td-c) ----------


def test_api_bracket_fill_target_out_of_range_high_422():
    """> 5,000,000 is a 422 — the API bound matches the engine's
    ``validate_scenario`` exactly (no separate API-side bound check to drift)."""
    res = client.post(
        "/api/trajectory",
        json=_split_body("deterministic", bracket_fill_target=5_000_001.0),
    )
    assert res.status_code == 422
    assert "bracket_fill_target" in res.json()["detail"]


def test_api_bracket_fill_target_negative_422():
    res = client.post(
        "/api/trajectory",
        json=_split_body("deterministic", bracket_fill_target=-1.0),
    )
    assert res.status_code == 422
    assert "bracket_fill_target" in res.json()["detail"]


def test_api_bracket_fill_target_boundary_values_accepted():
    """0 and 5,000,000 are IN-bounds (inclusive per the engine's 0 <= x <= 5e6)."""
    for value in (0.0, 5_000_000.0):
        res = client.post(
            "/api/trajectory",
            json=_split_body("deterministic", bracket_fill_target=value),
        )
        assert res.status_code == 200, value
        assert res.json()["scenario"]["bracket_fill_target"] == pytest.approx(value)


def test_api_bracket_fill_target_is_post_only_get_never_activates_it():
    """The lever rides the SAME POST-only transport as the split balances — GET
    has no query param for it at all, so a GET request carrying
    ``bracket_fill_target`` in the query string is simply ignored by FastAPI
    (unmapped param) and the lever never activates: the resolved scenario's
    ``bracket_fill_target`` is the scenario default (off), identical to a
    bare GET with no such param."""
    baseline = client.get("/api/trajectory")
    with_param = client.get(
        "/api/trajectory", params={"bracket_fill_target": 100_800.0}
    )
    assert baseline.status_code == with_param.status_code == 200
    assert (
        baseline.json()["scenario"]["bracket_fill_target"]
        == with_param.json()["scenario"]["bracket_fill_target"]
        == default_scenario().bracket_fill_target
    )
    assert with_param.json()["trajectory"] == baseline.json()["trajectory"]


def test_api_bracket_fill_target_buckets_mode_logged_noop():
    """In buckets sourcing mode the lever is a logged no-op (same idiom as the
    whole split's buckets-mode no-op log in firebridgelab.historical/monte_carlo) —
    never a 422, never silently applied. Functionally: the request succeeds,
    the resolved scenario still echoes the supplied value (threaded through
    verbatim, same convention as the split balances), but no tax_location /
    federal_tax block appears and the trajectory is the plain buckets line
    (the lever has no reachable effect there)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "sourcing_mode": "buckets",
            "bracket_fill_target": 100_800.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["bracket_fill_target"] == pytest.approx(100_800.0)
    assert "federal_tax" not in body
    assert "tax_location" not in body


def test_api_bracket_fill_target_none_is_off_default():
    """Omitted -> lever off (None), same as a fresh default_scenario()."""
    res = client.post("/api/trajectory", json=_split_body("deterministic"))
    assert res.status_code == 200
    assert res.json()["scenario"]["bracket_fill_target"] is None


# ---------- (e) attribution split (spending_tax / voluntary_tax / forced_tax) ----------
#
# The hand-computed composition/penalty/RMD/bracket-spillover/NIIT-crossing
# anchors that used to live here (against hand-built, non-fixed-point
# TaxLocationPath fixtures) now live in ``tests/test_tax_location.py`` —
# ``test_hand_anchor_dragged_year_walled_fixed_point``,
# ``test_hand_anchor_lever_year_fills_to_12_pct_top``,
# ``test_hand_anchor_scarce_td_year_bill_beats_lever``,
# ``test_hand_anchor_rmd_movement_year_shrinks_next_years_rmd``, and
# ``test_attribution_identity_exact_every_age_real_run`` — testing
# ``step_buckets_dragged`` directly, since that is where the attribution split
# now resolves (td1 DECISION 6). The test below still belongs HERE: it
# exercises ``compute_tax_overlay`` end-to-end against a REAL fixed-point run
# (not a hand-built path), so it legitimately tests this module's repackaging
# pass.


def test_ft2_identity_exact_on_real_run():
    """spending_tax + voluntary_tax + forced_tax == tax at every age — EXACT
    float equality (byte-exact by construction: tax is assembled as the sum of
    the three attribution components), on a REAL fixed-point run with the
    lever ON (so voluntary_tax is genuinely exercised, not trivially zero)."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
        bracket_fill_target=100_800.0,
    )
    path = simulate_tax_location_deterministic(s)
    overlay = compute_tax_overlay(s, path)
    assert any(v > 0.0 for v in overlay.voluntary_tax)  # the lever actually fires
    for i in range(len(overlay.ages)):
        assert (
            overlay.spending_tax[i] + overlay.voluntary_tax[i] + overlay.forced_tax[i]
            == overlay.tax[i]
        )
    # And the path scalars match the same folds the test reproduces.
    assert overlay.total_forced_tax == sum(overlay.forced_tax)
    assert overlay.total_voluntary_tax == sum(overlay.voluntary_tax)


# ---------- : federal_tax_total (the allocation-free hot-path total) ----------


def test_federal_tax_total_equals_compute_federal_tax_total_everywhere():
    """pin: ``federal_tax_total`` == ``compute_federal_tax(...).total``,
    exact ``==``, across a grid that crosses every bracket edge, both LTCG
    breakpoints, the NIIT threshold, and the deduction boundary (0 and 2
    seniors).

    The hot-path variant exists only to skip the ``FederalTaxYear``
    construction (~3.5M calls per split-MC 10k run read ONLY the total); both
    entry points are thin wrappers over the same ``_tax_components``
    arithmetic, and this test is the permanent enforcement that they can
    never drift.
    """
    from firebridgelab.tax.core import federal_tax_total

    ordinary_grid = [
        0.0, 1.0, 31_234.56, 32_200.0, 35_500.0, 24_800.0, 57_000.0,
        100_800.0, 133_000.0, 211_400.0, 243_600.0, 403_550.0, 435_750.0,
        512_450.0, 544_650.0, 768_700.0, 800_900.0, 2_000_000.0,
    ]
    ltcg_grid = [0.0, 250.75, 45_000.0, 98_900.0, 155_000.0, 613_700.0, 700_000.0]
    for ordinary in ordinary_grid:
        for ltcg in ltcg_grid:
            for n_65 in (0, 2):
                assert federal_tax_total(ordinary, ltcg, n_65) == compute_federal_tax(
                    ordinary, ltcg, n_65
                ).total
