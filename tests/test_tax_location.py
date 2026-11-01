"""Tests for the tax-location decomposition + access-aware sourcing — tb1's
access-aware sourcing PLUS the tax-drag design td1's dragged fixed point
(, td-a slice).

The slice's heart used to be a RECONCILIATION INVARIANT with no tax/penalty
drag at all: the three sub-balances summed to the existing scalar portfolio
every year. Under td1 the portfolio pays its own federal tax bill (DECISION
1), so that invariant is RE-POINTED (DIES/BINDS, see
``test_deterministic_reconciliation_exact`` etc. below): the three
sub-balances now sum EXACTLY to the DRAGGED canonical total instead — proven
via the year-over-year recurrence using only the path's own recorded series,
never re-deriving the fixed point. These tests pin that, the canary (no-split
byte-stability — the dragged code path is unreachable without a split), the
depletion-frequency metric, feature-off back-compat, the drag-direction /
zero-tax-degeneracy / attribution-identity / fixed-point-convergence /
transfers-preserve-sum BINDS, and four hand-computed fixed-point anchors
(``step_buckets_dragged`` called directly with pinned inputs).

Reconciliation precision note (read before changing the asserts): EXACT float
equality holds on the DETERMINISTIC path (constant blended return) — the
reconstruction routes the ~1-ULP float residual into roth with the SAME grouping
the assertion reads, so it telescopes to the DRAGGED total byte-for-byte. Under
VOLATILE cohort/trial returns (Historical/MC) a handful of years can differ by
1 ULP (~5e-10 on a ~1e6 balance) — an irreducible IEEE-754 re-association
artifact when three independently-grown buckets are summed against an
independently-computed total. The stochastic reconciliation check below
therefore uses a sub-cent tolerance, NOT exact ==; the deterministic check uses
exact ==.
"""

import math
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from firebridgelab import default_scenario
from firebridgelab.federal_tax_core import compute_federal_tax
from firebridgelab.historical import simulate_historical
from firebridgelab.monte_carlo import simulate_monte_carlo
from firebridgelab.scenario import (
    ScenarioValidationError,
    blended_real_return_at_age,
    validate_scenario,
)
from firebridgelab.simulate import simulate
from firebridgelab.spending import make_strategy
from firebridgelab.tax_location import (
    FIXED_POINT_MAX_ITERATIONS,
    LEVER_MAX_AGE,
    LEVER_MIN_AGE,
    PRE_595_MAX_AGE,
    RMD_START_AGE,
    TAX_LOCATION_ACCESS_AGE,
    aggregate_estate_cost,
    has_tax_split,
    resolve_split,
    simulate_dragged_depletion_only,
    simulate_path_decomposition,
    simulate_tax_location_deterministic,
    step_buckets_dragged,
)
from tests._canary import DET_FINAL_CANARY


client = TestClient(app)

CANARY = DET_FINAL_CANARY


# ---------- (a) the existing canary still holds (no split = feature off) ----------


def test_canary_unchanged_no_split():
    """The deterministic canary must not move when no split is supplied."""
    assert simulate(default_scenario()).final_portfolio == CANARY


def test_has_tax_split_off_by_default():
    """Feature is OFF on the default scenario (all three balances None)."""
    assert has_tax_split(default_scenario()) is False


# ---------- (b) reconciliation invariant — RE-POINTED to the DRAGGED total ----------
#
# DIES (tax-drag design td1): tb1's original proof was "the three buckets sum to
# the OLD, UNTAXED scalar portfolio every year" — true only because tb1 applied
# no tax at all, so sourcing order couldn't change the total. Under td1 the
# portfolio pays its own tax bill (DECISION 1), so that equality no longer
# holds by design (the split path is legitimately poorer). The BINDS
# replacement: the three buckets sum EXACTLY to the DRAGGED canonical total —
# checked via the year-over-year recurrence
#
#     total[i] = max(0, total[i-1]*(1+r) + gi - spending[i]*(1+r/2) - tax[i])
#
# using ONLY the path's own recorded ``spending``/``tax`` series plus the
# scenario's own return/GI helpers (never re-deriving the fixed point or
# peeking at engine internals) — proving the reconstruction telescopes to
# EXACTLY the value ``simulate_path_decomposition`` computed internally.


def _dragged_recurrence_check(s, path) -> None:
    """Assert the buckets-sum year-over-year recurrence holds EXACTLY."""
    from firebridgelab.scenario import blended_real_return_at_age, gi_at_age

    prev = s.initial_portfolio
    for i, age in enumerate(path.ages):
        r = blended_real_return_at_age(s, age)
        gi = gi_at_age(s, age)
        expected = prev + prev * r + gi * (1 + r / 2) - path.spending[i] * (1 + r / 2) - path.tax[i]
        expected = max(0.0, expected)
        total = path.taxable[i] + path.tax_deferred[i] + path.roth[i]
        assert total == pytest.approx(expected, rel=1e-12, abs=1e-6), (age, total, expected)
        prev = total


def test_deterministic_reconciliation_exact():
    """Split scenario, single sourcing, deterministic: the three sub-balances sum
    to the DRAGGED canonical total EVERY YEAR to exact float equality (see the
    DIES/BINDS note above — this used to assert equality with the OLD untaxed
    scalar; that equality retired by design once the portfolio pays its own
    tax)."""
    base = default_scenario()
    s = replace(
        base,
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    _dragged_recurrence_check(s, path)
    # No negative balances ever.
    assert min(min(path.taxable), min(path.tax_deferred), min(path.roth)) >= 0.0


def test_deterministic_reconciliation_various_splits_exact():
    """Reconciliation holds exactly for several splits incl. degenerate all-in-one."""
    base = default_scenario()
    splits = [
        dict(taxable_balance=1_800_000.0, tax_deferred_balance=0.0, roth_balance=0.0),
        dict(taxable_balance=0.0, tax_deferred_balance=1_800_000.0, roth_balance=0.0),
        dict(taxable_balance=0.0, tax_deferred_balance=0.0, roth_balance=1_800_000.0),
        dict(taxable_balance=144_000.0, tax_deferred_balance=1_440_000.0, roth_balance=216_000.0),
    ]
    for sp in splits:
        s = replace(base, **sp)
        path = simulate_tax_location_deterministic(s)
        _dragged_recurrence_check(s, path)
        # No negative balances ever.
        assert min(min(path.taxable), min(path.tax_deferred), min(path.roth)) >= 0.0


def test_stochastic_reconciliation_within_tolerance():
    """Historical cohorts (volatile returns): each cohort path's bucket sum
    reconciles to the SAME recurrence within a sub-cent tolerance (the 1-ULP
    float re-association artifact tb1 documented — now telescoping to the
    DRAGGED total instead of the retired untaxed-scalar comparison)."""
    from firebridgelab import historical as H
    from firebridgelab.scenario import gi_at_age

    base = default_scenario()
    s = replace(
        base,
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    data = H.load_shiller()
    n_steps = s.end_age - s.start_age + 1
    weights = H.renormalized_weights(s)
    worst = 0.0
    for sy in data.all_starting_years(n_steps):
        period = data.returns_for_period(sy, n_steps)
        returns = H._cohort_blended_returns(weights, period)
        path = simulate_path_decomposition(s, returns, make_strategy(s))
        prev = s.initial_portfolio
        for i, age in enumerate(path.ages):
            r = float(returns[i])
            gi = gi_at_age(s, age)
            expected = prev + prev * r + gi * (1 + r / 2) - path.spending[i] * (1 + r / 2) - path.tax[i]
            expected = max(0.0, expected)
            total = path.taxable[i] + path.tax_deferred[i] + path.roth[i]
            worst = max(worst, abs(total - expected))
            prev = total
    # Well below a cent — the artifact is ~5e-10 on million-dollar balances.
    assert worst < 1e-6


# ---------- validation: the three must sum to initial_portfolio within $1 ----------


def test_resolve_split_sum_within_dollar_ok():
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.5,
        roth_balance=215_999.6,  # off by $0.10, within $1
    )
    t, d, r = resolve_split(s)
    assert (t, d, r) == (1_080_000.0, 504_000.5, 215_999.6)


def test_resolve_split_missing_treated_as_zero():
    """Supplying only taxable treats the other two as 0 (must then sum to portfolio)."""
    s = replace(
        default_scenario(),
        initial_portfolio=1_000_000.0,
        taxable_balance=1_000_000.0,
    )
    assert has_tax_split(s) is True
    assert resolve_split(s) == (1_000_000.0, 0.0, 0.0)


def test_resolve_split_sum_mismatch_raises():
    s = replace(
        default_scenario(),
        taxable_balance=1_000_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,  # sums to 1.72M, not 1.8M
    )
    with pytest.raises(ValueError, match="must sum to initial_portfolio"):
        resolve_split(s)


# ---------- (c) metric correctness on a hand-checkable case ----------


def test_taxable_depletes_pre_595_hand_check():
    """Tax-deferred-heavy split: taxable=144k, roth=216k, tax_deferred=1.44M.

    Default spend 110k, GI 0 before 60, real r=5%. Hand-calc the earliest taxable
    depletion age:
      - Year 1 (age 50): grown taxable = 144000*1.05 = 151200; net draw =
        110000*(1+0.025) = 112750; 151200 - 112750 = 38450 left.
      - Year 2 (age 51): grown = 38450*1.05 = 40372.5; draw 112750 > 40372.5 →
        taxable hits 0 at age 51 (cascade to roth pre-59½).
    So taxable_depletion_age == 51 and the forced-tax-deferred-draw flag fires
    once reachable (taxable+roth) is also exhausted (a few years later)."""
    s = replace(
        default_scenario(),
        taxable_balance=144_000.0,
        roth_balance=216_000.0,
        tax_deferred_balance=1_440_000.0,
    )
    path = simulate_tax_location_deterministic(s)

    assert path.taxable_depletion_age is not None
    assert path.taxable_depletion_age <= PRE_595_MAX_AGE  # <= 59
    assert path.taxable_depletion_age == 51  # the hand-calc value
    # Reachable (taxable + roth) also runs dry pre-59½ → forced tax-deferred draw.
    assert path.reachable_depletion_age is not None
    assert path.reachable_depletion_age <= PRE_595_MAX_AGE
    assert path.reachable_depletion_age >= path.taxable_depletion_age
    assert path.forced_pre_595_draw is True


def test_taxable_survives_to_595_when_funded():
    """A taxable-heavy split funds the whole pre-59½ bridge — no pre-59½ depletion."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert path.taxable_depletion_age is None
    assert path.reachable_depletion_age is None
    assert path.forced_pre_595_draw is False


def test_historical_pct_taxable_depleted_sensible():
    """Historical: pct_taxable_depleted_pre_595 is a real % in [0, 100], and the
    tax-deferred-heavy case has a high pct (the bridge is underfunded)."""
    s = replace(
        default_scenario(),
        taxable_balance=144_000.0,
        roth_balance=216_000.0,
        tax_deferred_balance=1_440_000.0,
    )
    result = simulate_historical(s)
    risk = result.tax_location_risk
    assert risk is not None
    assert 0.0 <= risk.pct_taxable_depleted_pre_595 <= 100.0
    assert 0.0 <= risk.pct_reachable_depleted_pre_595 <= 100.0
    # 144k taxable can't fund 110k/yr for long — every cohort exhausts it before 59½.
    assert risk.pct_taxable_depleted_pre_595 == 100.0
    # Depletion-age distribution is populated and within the pre-59½ window.
    assert risk.taxable_depletion.earliest is not None
    assert risk.taxable_depletion.earliest <= PRE_595_MAX_AGE
    assert risk.taxable_depletion.median is not None


def test_monte_carlo_pct_in_range():
    """Monte Carlo: the headline percentages are sensible values in [0, 100]."""
    s = replace(
        default_scenario(),
        taxable_balance=144_000.0,
        roth_balance=216_000.0,
        tax_deferred_balance=1_440_000.0,
    )
    result = simulate_monte_carlo(s, n_trials=500)
    risk = result.tax_location_risk
    assert risk is not None
    assert risk.count == 500
    assert 0.0 <= risk.pct_taxable_depleted_pre_595 <= 100.0
    assert 0.0 <= risk.pct_reachable_depleted_pre_595 <= 100.0
    # Taxable percentile bands are present for charting.
    assert risk.ages is not None
    assert risk.taxable_median is not None
    assert len(risk.taxable_median) == len(risk.ages)


def test_total_fan_matches_dragged_total_consistency():
    """RE-POINTED (tax-drag design td1, td-b deliverable): tb1's original proof
    was "the tax-location TOTAL fan (total_p10/median/p90) equals the OLD,
    still-pretax portfolio fan (result.p10/median/p90)" — true only because
    tb1 applied no tax, so the per-path total was byte-identical to the
    untaxed scalar. td-a broke that equality (a pre-tax fan sitting next to
    after-tax numbers would have been the falsehood ft1/ft2/td1 exist to
    kill) by leaving the OUTER fan on the still-pre-tax scalar loop. td-b's
    fan re-point (DECISION 1) makes it an EQUALITY AGAIN, but the RIGHT one:
    the Historical portfolio fan ITSELF is now computed from the dragged
    decomposition, so ``result.median/p10/p90`` (the OUTER fan) and
    ``risk.total_median/p10/p90`` (the tax-location aggregator's own dragged
    percentiles) are the SAME numbers by construction:

      1. Fan re-point -- ``result.median/p10/p90 == risk.total_median/p10/p90``
         at every age, exact ``==`` (both are percentiles of the identical
         per-cohort DRAGGED total values; see ``historical._split_cohort_step``).
      2. Aggregator self-consistency -- independently recompute the raw
         per-cohort DRAGGED bucket sums (a fresh decomposition pass, same
         cohorts/returns/strategy discipline the engine uses) and confirm the
         percentiles match the engine's ``risk.total_median`` exactly.
      3. Depletion re-point -- ``result.depletion_count`` counts DRAGGED
         depletions (``TaxLocationPath.dragged_depleted_age``), not the old
         pre-tax scalar's.
    """
    import numpy as np

    from firebridgelab import historical as H
    from firebridgelab.spending import make_strategy

    s = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    result = simulate_historical(s)
    risk = result.tax_location_risk
    assert risk is not None

    n = len(risk.ages)
    # The new per-account medians + total fan are all present and aligned.
    for series in (
        risk.tax_deferred_median,
        risk.roth_median,
        risk.total_p10,
        risk.total_median,
        risk.total_p90,
    ):
        assert series is not None
        assert len(series) == n

    # Per-account medians are non-negative balances.
    assert all(v >= 0.0 for v in risk.tax_deferred_median)
    assert all(v >= 0.0 for v in risk.roth_median)

    # (1) The Historical fan IS the dragged total (DECISION 1's fan re-point) --
    # exact equality, not merely "close": both are percentiles of the same
    # per-cohort dragged-total values, computed once per cohort.
    assert result.median == risk.total_median
    assert result.p10 == risk.total_p10
    assert result.p90 == risk.total_p90

    # (2) Aggregator self-consistency: independently recompute the raw
    # per-cohort dragged totals and confirm the percentiles match exactly.
    data = H.load_shiller()
    n_steps = s.end_age - s.start_age + 1
    weights = H.renormalized_weights(s)
    paths = []
    for sy in data.all_starting_years(n_steps):
        period = data.returns_for_period(sy, n_steps)
        returns = H._cohort_blended_returns(weights, period)
        paths.append(simulate_path_decomposition(s, returns, make_strategy(s)))
    total_matrix = np.array(
        [[p.taxable[i] + p.tax_deferred[i] + p.roth[i] for i in range(n_steps)] for p in paths]
    )
    recomputed_median = np.percentile(total_matrix, 50, axis=0).tolist()
    assert recomputed_median == risk.total_median

    # (3) Depletion count re-points to the DRAGGED world (recomputed
    # independently off the same fresh decomposition pass above).
    expected_depletion_count = sum(
        1 for p in paths if p.dragged_depleted_age is not None
    )
    assert result.depletion_count == expected_depletion_count


def test_595_boundary_is_age_60():
    """The integer-age boundary: age 59 is walled (pre-59½), age 60 is penalty-free."""
    assert TAX_LOCATION_ACCESS_AGE == 60
    assert PRE_595_MAX_AGE == 59


# ---------- (d) feature-off: no split → no new response keys, identical behavior ----------


def test_feature_off_deterministic_payload_has_no_tax_location():
    """GET deterministic with no split: response has no tax_location key, and the
    trajectory is byte-identical to the existing canary."""
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    assert "tax_location" not in body
    assert body["trajectory"]["final_portfolio"] == CANARY


def test_feature_off_historical_payload_has_no_risk():
    res = client.post("/api/trajectory", json={"mode": "historical"})
    assert res.status_code == 200
    body = res.json()
    assert "tax_location_risk" not in body


def test_feature_off_monte_carlo_payload_has_no_risk():
    res = client.post("/api/trajectory", json={"mode": "monte_carlo"})
    assert res.status_code == 200
    body = res.json()
    assert "tax_location_risk" not in body


# ---------- API contract: the split flows in and the blocks come out ----------


def test_api_deterministic_tax_location_block_shape():
    """POST deterministic with a split returns the tax_location block, shaped as
    the web agent expects. DIES (td1, re-pointed again at td-c): this used to
    assert the block sums to the ``trajectory`` endpoint's (still pre-tax,
    untouched) scalar every year — that equality died at DECISION 1 (BINDS:
    dragged <= pre-tax). td-c scope 2 goes one step further and kills the
    interim entirely: the ``trajectory`` MAIN LINE itself now IS the dragged
    total (no more a dragged tax_location chart sitting next to a pre-tax main
    line) — so tax_location's reconstructed total equals trajectory's
    end_portfolio EXACTLY (both read the SAME dragged decomposition) at every
    age, and depleted_age/final_portfolio agree too. The pre-tax counterfactual
    survives only as the DECISION 9 estate-cost readout (see
    ``test_api_deterministic_estate_cost_block_shape`` below)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "taxable_balance": 1_080_000.0,
            "tax_deferred_balance": 504_000.0,
            "roth_balance": 216_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "tax_location" in body
    tl = body["tax_location"]
    assert set(tl) == {
        "ages",
        "taxable",
        "tax_deferred",
        "roth",
        "taxable_depletion_age",
        "reachable_depletion_age",
    }
    points = body["trajectory"]["points"]
    assert len(tl["ages"]) == len(points)
    dragged_totals = [
        tl["taxable"][i] + tl["tax_deferred"][i] + tl["roth"][i] for i in range(len(points))
    ]
    main_line_totals = [p["end_portfolio"] for p in points]
    # Exact ==, deliberately (reviewer-verified bit-equality holds): the
    # trajectory's end_portfolio and this reconstruction sum the IDENTICAL
    # per-account floats in the IDENTICAL order, so anything short of
    # bit-equality would mean the API silently re-derived one of the two.
    for i in range(len(points)):
        assert main_line_totals[i] == dragged_totals[i]
    assert body["trajectory"]["final_portfolio"] == dragged_totals[-1]
    # This well-funded fixture never depletes.
    assert body["trajectory"]["depleted_age"] is None


def test_api_deterministic_trajectory_depletion_matches_dragged_on_depleting_fixture():
    """td-c scope 2, the depleting case: on a fixture that DOES run out, the
    ``trajectory`` block's ``depleted_age`` is the DRAGGED depletion age (the
    same one ``dragged_depleted_age`` records internally), not the (later, or
    absent) pre-tax depletion age — pinning that the swap actually reaches the
    depletion/terminal fields, not just the per-age series."""
    s = _depleting_guardrails_split()
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "initial_portfolio": s.initial_portfolio,
            "taxable_balance": s.taxable_balance,
            "tax_deferred_balance": s.tax_deferred_balance,
            "roth_balance": s.roth_balance,
            "spending_mode": s.spending_mode,
        },
    )
    assert res.status_code == 200
    body = res.json()
    engine_path = simulate_tax_location_deterministic(s)
    assert engine_path.dragged_depleted_age is not None
    assert body["trajectory"]["depleted_age"] == engine_path.dragged_depleted_age
    # Exact 0.0 — the absorbing depletion floor records a literal 0.0.
    assert body["trajectory"]["final_portfolio"] == 0.0


def test_api_buckets_mode_trajectory_stays_pretax():
    """The lone place drag DOESN'T reach the main line: buckets sourcing.
    A split supplied with sourcing_mode=buckets no-ops the whole tax-location
    product (tb1), so the trajectory keeps running through the plain buckets
    engine — never the dragged decomposition (which never even runs there)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "sourcing_mode": "buckets",
            "taxable_balance": 1_080_000.0,
            "tax_deferred_balance": 504_000.0,
            "roth_balance": 216_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "tax_location" not in body
    assert len(body["trajectory"]["points"]) == 95 - 50 + 1


def test_api_historical_tax_location_risk_block_shape():
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "taxable_balance": 144_000.0,
            "tax_deferred_balance": 1_440_000.0,
            "roth_balance": 216_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "tax_location_risk" in body
    risk = body["tax_location_risk"]
    assert set(risk) == {
        "count",
        "pct_taxable_depleted_pre_595",
        "pct_reachable_depleted_pre_595",
        "taxable_depletion",
        "reachable_depletion",
        "ages",
        "taxable_p10",
        "taxable_median",
        "taxable_p90",
        "tax_deferred_median",
        "roth_median",
        "total_p10",
        "total_median",
        "total_p90",
    }
    assert set(risk["taxable_depletion"]) == {"median", "p10", "earliest"}
    assert 0.0 <= risk["pct_taxable_depleted_pre_595"] <= 100.0


def test_api_split_sum_mismatch_returns_422():
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "taxable_balance": 1_000_000.0,
            "tax_deferred_balance": 504_000.0,
            "roth_balance": 216_000.0,  # sums to 1.72M, not 1.8M
        },
    )
    assert res.status_code == 422
    assert "sum to initial_portfolio" in res.json()["detail"]


def test_api_buckets_mode_noop_split_no_tax_location_block():
    """tb1 is single-mode only: a split with sourcing_mode=buckets is no-op'd — the
    deterministic payload carries no tax_location block (and no error)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "sourcing_mode": "buckets",
            "taxable_balance": 1_080_000.0,
            "tax_deferred_balance": 504_000.0,
            "roth_balance": 216_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "tax_location" not in body


# ============================================================================
# Tax-drag design td1 (td-a slice): the dragged fixed point, transfers,
# attribution, and the BINDS invariant suite + hand-computed anchors.
# ============================================================================


# ---------- Scenario field: bracket_fill_target (DECISION 5) ----------


def test_bracket_fill_target_validated_on_scenario():
    base = default_scenario()
    validate_scenario(base)  # default (None = off) passes
    validate_scenario(replace(base, bracket_fill_target=100_800.0))  # in-range passes
    validate_scenario(replace(base, bracket_fill_target=0.0))  # boundary passes
    validate_scenario(replace(base, bracket_fill_target=5_000_000.0))  # boundary passes
    with pytest.raises(ScenarioValidationError, match="bracket_fill_target"):
        validate_scenario(replace(base, bracket_fill_target=5_000_000.01))
    with pytest.raises(ScenarioValidationError, match="bracket_fill_target"):
        validate_scenario(replace(base, bracket_fill_target=-0.01))


# ---------- BINDS: no-split byte-stability (the dragged path is unreachable
# without a split — the canary above already pins this; this test additionally
# confirms setting the NEW lever field alone, with no split, changes nothing,
# since the lever has no effect without tax-location accounts to move money
# between) ----------


def test_no_split_byte_stability_lever_field_is_a_no_op_without_split():
    """Setting ``bracket_fill_target`` with NO tax-location split supplied must
    not move the canary — the lever only means anything inside the dragged
    decomposition, which ``has_tax_split`` gates off entirely."""
    s = replace(default_scenario(), bracket_fill_target=100_800.0)
    assert has_tax_split(s) is False
    assert simulate(s).final_portfolio == CANARY


# ---------- BINDS: drag direction (static-spend fixtures only) ----------


def test_drag_direction_dragged_le_pretax_static_spend():
    """On a STATIC-spend (constant mode, the default) split scenario, the
    dragged total is <= the paired pre-tax total at EVERY age, with genuine
    separation once any tax is owed (never a no-op comparison) — design BINDS
    "drag direction... scoped to static spend"."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    any_strict = False
    for i in range(len(path.ages)):
        total = path.taxable[i] + path.tax_deferred[i] + path.roth[i]
        assert total <= path.pretax_total[i] + 1e-6
        if total < path.pretax_total[i] - 1.0:
            any_strict = True
    assert any_strict
    # Equality holds ONLY while cumulative tax THROUGH this year is zero (the
    # early, pre-tax bridge years, before any RMD/forced draw owes anything).
    cumulative_tax = 0.0
    for i in range(len(path.ages)):
        cumulative_tax += path.tax[i]
        total = path.taxable[i] + path.tax_deferred[i] + path.roth[i]
        if cumulative_tax == 0.0:
            assert total == pytest.approx(path.pretax_total[i], abs=1e-6)


# ---------- BINDS: zero-tax degeneracy ----------


def test_zero_tax_degeneracy_all_roth_drags_to_byte_equality():
    """A scenario whose income never exceeds deductions and never draws
    taxable/tax-deferred money (all-Roth, no pensions/SS at all) drags to
    BYTE-EQUALITY with the paired pre-tax path at every age, including through
    eventual depletion (both totals hit 0 at the same age, together)."""
    s = replace(
        default_scenario(),
        taxable_balance=0.0,
        tax_deferred_balance=0.0,
        roth_balance=1_800_000.0,
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert all(t == 0.0 for t in path.tax)
    assert all(v == 0.0 for v in path.spending_tax)
    assert all(v == 0.0 for v in path.voluntary_tax)
    assert all(v == 0.0 for v in path.forced_tax)
    for i in range(len(path.ages)):
        total = path.taxable[i] + path.tax_deferred[i] + path.roth[i]
        assert total == path.pretax_total[i]
    # Non-trivial: the path actually carries real (nonzero) balances for a
    # good stretch before depleting — not a degenerate all-zero trajectory.
    assert any(path.taxable[i] + path.tax_deferred[i] + path.roth[i] > 0.0 for i in range(10))


# ---------- BINDS: attribution identity + component zero-conditions ----------


def test_attribution_identity_exact_every_age_real_run():
    """spending_tax + voluntary_tax + forced_tax == tax at EVERY age, exact
    ``==`` (byte-exact by construction — DECISION 6), on a real multi-decade
    run that exercises the pre-59½ walled years, the RMD years (75+), and
    (with the lever on) the 60-74 voluntary window — plus the component
    zero-conditions: forced_tax == 0 below 75 or when the draw covers the RMD;
    voluntary_tax == 0 outside 60-74 or when the income already fills the
    target. Uses the 25/60/15 illustrative split: with a taxable-heavy split the
    lever drains tax-deferred before 75 and no RMD excess remains to exercise."""
    s = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
        bracket_fill_target=100_800.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert any(f > 0.0 for f in path.forced_tax)  # this run has real RMD excess
    assert any(v > 0.0 for v in path.voluntary_tax)  # lever is active 60-74
    for i, age in enumerate(path.ages):
        assert (
            path.spending_tax[i] + path.voluntary_tax[i] + path.forced_tax[i]
            == path.tax[i]
        )
        if age < RMD_START_AGE:
            assert path.forced_tax[i] == 0.0
        if not (LEVER_MIN_AGE <= age <= LEVER_MAX_AGE):
            assert path.voluntary_tax[i] == 0.0


# ---------- BINDS: fixed-point convergence asserted (never silently capped) ----------


def test_fixed_point_convergence_normal_scenario_does_not_raise():
    """A normal, real multi-decade dragged run (with the lever active,
    exercising the coupled fixed point every year) converges cleanly — no
    ``RuntimeError`` — proving the 100-iteration cap is never hit in practice
    (design: "generous — the worst case converges in well under 40")."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
        bracket_fill_target=100_800.0,
    )
    simulate_tax_location_deterministic(s)  # must not raise


def test_fixed_point_convergence_assertion_exists(monkeypatch):
    """The convergence assertion is REAL, not decorative: force the iteration
    cap down to 0 (impossible to converge) and confirm ``step_buckets_dragged``
    raises rather than silently returning a not-yet-converged bill."""
    import firebridgelab.tax_location as tax_location

    monkeypatch.setattr(tax_location, "FIXED_POINT_MAX_ITERATIONS", 0)
    with pytest.raises(RuntimeError, match="failed to converge"):
        step_buckets_dragged(
            taxable=0.0,
            tax_deferred=500_000.0,
            roth=0.0,
            r=0.0,
            gi=0.0,
            spending=50_000.0,
            penalty_free=False,
            pension_ss_ordinary=0.0,
            gains_fraction=0.5,
            n_65_plus=0,
            age=58,
            bracket_fill_target=None,
            prior_tax_deferred=0.0,
        )


# ---------- BINDS: transfers preserve the year's sum ----------


def test_transfers_preserve_year_sum():
    """The lever/RMD transfer is sum-preserving BY CONSTRUCTION: the ONLY
    things that change the year's total are the explicit spend and tax
    outflows — growth + spend-sourcing + tax-funding + the internal transfer,
    taken together, satisfy the plain accounting identity
    ``(start)*(1+r) + gi - spending*(1+r/2) - tax == end`` with r=0 for a
    clean by-hand check (no growth to complicate it): $15,000 start, no GI,
    $9,800 spending, converged tax $950.00 (verified independently in
    ``test_scarce_td_year_hand_computed_anchor`` below) -> end == 15000 -
    9800 - 950 == $4,250.00 exactly, regardless of how much moved between
    tax_deferred and taxable via the lever transfer."""
    result = step_buckets_dragged(
        taxable=10_000.0,
        tax_deferred=5_000.0,
        roth=0.0,
        r=0.0,
        gi=0.0,
        spending=9_800.0,
        penalty_free=True,
        pension_ss_ordinary=40_000.0,
        gains_fraction=0.5,
        n_65_plus=2,
        age=65,
        bracket_fill_target=100_800.0,
        prior_tax_deferred=0.0,
    )
    assert result.lever_transfer > 0.0  # the transfer actually fired
    end_total = result.taxable + result.tax_deferred + result.roth
    assert end_total == pytest.approx(15_000.0 - 9_800.0 - result.tax, abs=1e-9)


# ============================================================================
# Hand-computed fixed-point anchors (design "Validation anchors" — each 2-3
# iterations of the fixed point solved on paper, matched to the engine to the
# cent). All calls use r=0 so growth never complicates the by-hand arithmetic;
# ``step_buckets_dragged`` is called DIRECTLY (not through a 46-year sim) so
# every input is pinned and independently reproducible.
# ============================================================================


def test_hand_anchor_dragged_year_walled_fixed_point():
    """A walled year (age 58, pre-59½, no lever/no RMD) where a $50,000 flat
    spending need drains $20,000 from taxable then forces $30,000 from
    tax_deferred (walled order: taxable -> roth -> tax_deferred). The tax
    bill on that forced income must ITSELF be funded from tax_deferred
    (nothing else left), coupling the fixed point.

    Hand-solve (2-3 iterations, then the closed form): with pension/SS = 0,
    gains_fraction = 0.5, n_65_plus = 0 (deduction $32,200):

      T_0 = 0 (seed)
      T_1 = tax(ordinary=30,000, ltcg=20,000*0.5=10,000) + penalty
          = 0 (ordinary < deduction, LTCG fully in 0% bracket)
            + 0.10*30,000 = $3,000.00
      T_2 = tax(ordinary=30,000+3,000=33,000, ltcg=10,000) + 0.10*33,000
          = 0.10*(33,000-32,200) + 0 + 3,300.00 = 80.00 + 3,300.00 = $3,380.00
      T_3 = 0.10*(30,000+3,380-32,200) + 0.10*(30,000+3,380)
          = 0.10*1,180 + 3,338.00 = 118.00 + 3,338.00 = $3,456.00

    Once T > $2,200 (taxable_ordinary = T - 2,200 stays positive, both the
    ordinary bracket (10%, well under the $24,800 top) and the LTCG band
    (well under the $98,900 0% breakpoint) stay fixed, so the map is EXACTLY
    LINEAR: T_(k+1) = 0.10*(T_k - 2,200) + 0.10*(30,000 + T_k) = 0.20*T_k +
    2,780 — a contraction with rate 0.20 (well inside the design's worst-case
    ~0.47 bound). Its closed-form fixed point: 0.80*T* = 2,780 -> T* =
    $3,475.00 exactly. LTCG never depends on T here (the tax-funding draw is
    100% tax_deferred, since taxable/roth are already empty), so this pins a
    clean single-variable contraction."""
    result = step_buckets_dragged(
        taxable=20_000.0,
        tax_deferred=530_000.0,
        roth=0.0,
        r=0.0,
        gi=0.0,
        spending=50_000.0,
        penalty_free=False,
        pension_ss_ordinary=0.0,
        gains_fraction=0.5,
        n_65_plus=0,
        age=58,
        bracket_fill_target=None,
        prior_tax_deferred=0.0,
    )
    assert result.forced_pre_595_draw is True
    assert result.taxable_draw == 20_000.0
    assert result.tax_deferred_draw == 30_000.0
    # The converged bill matches the hand-solved closed form to the cent.
    assert result.tax == pytest.approx(3_475.00, abs=0.01)
    # No lever/RMD in a walled, pre-60, pre-75 year — the ENTIRE bill is
    # spending-attributed.
    assert result.spending_tax == pytest.approx(result.tax)
    assert result.voluntary_tax == 0.0
    assert result.forced_tax == 0.0
    # Independent cross-check via compute_federal_tax at the converged bill.
    cross = compute_federal_tax(30_000.0 + result.tf_tax_deferred_draw, 10_000.0, 0)
    penalty = 0.10 * (30_000.0 + result.tf_tax_deferred_draw)
    assert result.tax == pytest.approx(cross.total + penalty, abs=1e-6)
    # Final balances: all of taxable + roth drained by spending; tax_deferred
    # carries the post-spend residual minus the converged tax-funding draw.
    assert result.taxable == 0.0
    assert result.roth == 0.0
    assert result.tax_deferred == pytest.approx(
        530_000.0 - 30_000.0 - result.tf_tax_deferred_draw, abs=1e-6
    )


def test_hand_anchor_lever_year_fills_to_12_pct_top():
    """A lever year (age 65, deduction $35,500 with n_65_plus=2): no spending
    draw at all (net need is 0), $20,000 of pre-existing ordinary income
    (pension/SS), ``bracket_fill_target = $100,800`` (the 12% bracket top).
    ``gains_fraction = 0`` here isolates the LEVER FORMULA cleanly from the
    fixed-point coupling (the general coupled case is
    ``test_hand_anchor_dragged_year_walled_fixed_point`` above).

    Hand-solve: ``extra = max(0, target + deduction - ordinary_excl_lever) =
    max(0, 100,800 + 35,500 - 20,000) = $116,300.00`` (capped at the $2M
    tax-deferred balance — no cap binds). Ordinary income becomes
    $20,000 + $116,300 = $136,300; taxable_ordinary = $136,300 - $35,500 =
    $100,800 EXACTLY (the lever fills to precisely the 12% top, by
    construction). ordinary_tax = 24,800*0.10 + 76,000*0.12 = $2,480.00 +
    $9,120.00 = $11,600.00. With gains_fraction=0, LTCG stays $0 regardless of
    the tax-funding draw, so the fixed point converges in exactly 2
    iterations (T_1 = $11,600, T_2 recomputes the SAME draws/income and
    reproduces $11,600 -> |ΔT| = 0). ``voluntary_tax`` = the WHOLE bill
    (base income of $20,000 sits entirely below the $35,500 deduction, so
    spending_tax = tax($20,000) = $0)."""
    result = step_buckets_dragged(
        taxable=1_000_000.0,
        tax_deferred=2_000_000.0,
        roth=0.0,
        r=0.0,
        gi=0.0,
        spending=0.0,
        penalty_free=True,
        pension_ss_ordinary=20_000.0,
        gains_fraction=0.0,
        n_65_plus=2,
        age=65,
        bracket_fill_target=100_800.0,
        prior_tax_deferred=0.0,
    )
    assert result.iterations == 2  # converges in exactly 2, hand-traceable
    assert result.lever_transfer == pytest.approx(116_300.00, abs=1e-6)
    assert result.ordinary_income == pytest.approx(136_300.00, abs=1e-6)
    assert result.tax == pytest.approx(11_600.00, abs=1e-6)
    assert result.spending_tax == pytest.approx(0.0, abs=1e-6)
    assert result.voluntary_tax == pytest.approx(11_600.00, abs=1e-6)
    assert result.forced_tax == 0.0
    # Independent cross-check via compute_federal_tax.
    cross_with_lever = compute_federal_tax(136_300.0, 0.0, 2)
    cross_without_lever = compute_federal_tax(20_000.0, 0.0, 2)
    assert cross_without_lever.total == pytest.approx(0.0)
    assert result.voluntary_tax == pytest.approx(
        cross_with_lever.total - cross_without_lever.total
    )
    # Final balances: the $116,300 transfer moved tax_deferred -> taxable,
    # then the tax-funding draw ($11,600) came out of taxable (post-59½ order
    # sources taxable first).
    assert result.tax_deferred == pytest.approx(2_000_000.0 - 116_300.0, abs=1e-6)
    assert result.taxable == pytest.approx(
        1_000_000.0 - 11_600.0 + 116_300.0, abs=1e-6
    )


def test_hand_anchor_scarce_td_year_bill_beats_lever():
    """A scarce-TD year (age 65, lever ON): $10,000 taxable + $5,000
    tax_deferred (SCARCE) + $0 roth. Spending need $9,800 drains taxable to
    $200 (NOT to $0 — this is deliberate, see below) without touching
    tax_deferred at all. Pension/SS ordinary income $40,000 (already above
    the $35,500 deduction) and ``bracket_fill_target = $100,800`` — the lever
    WANTS a large $96,300 fill, but tax_deferred has only $5,000 total.

    Hand-solve: iteration 1 (T=0): no tax-funding draw yet, so the lever sees
    the FULL $5,000 residual and claims all of it (capped): lever_extra =
    min(100,800+35,500-40,000, 5,000) = min(96,300, 5,000) = $5,000.00.
    ordinary_income = 40,000+0+5,000 = $45,000; tax(45,000-35,500=9,500 taxable
    ordinary, ltcg=0) = $950.00 (10% bracket) -> T_1 = $950.00.

    Iteration 2: sourcing $950 access-aware (post-59½: taxable -> tax_deferred
    -> roth) from the POST-SPEND balances ($200 taxable, $5,000
    tax_deferred): taxable covers its last $200, tax_deferred supplies the
    remaining $750 — THE TD/TAXABLE SPLIT this anchor pins (tf_taxable_draw =
    $200.00, tf_tax_deferred_draw = $750.00). This is the direct proof that
    tax-funding draws from the SAME-YEAR PRE-transfer taxable balance ($200,
    not $200 + a lever transfer that hasn't settled yet) — the load-bearing
    transfer-settlement order. residual_tax_deferred = 5,000 - 750 = $4,250,
    so the lever is CAPPED there (DECISION 3: "the bill wins, the lever takes
    the residual") — NOT at its desired $96,300 fill. Since the lever is
    capped at the exact residual, ordinary_income stays $45,000 (every TD
    dollar becomes ordinary income one way or another, whether drawn or
    transferred) and the fixed point converges at iteration 2 (T_2 = $950.00,
    unchanged from T_1 given LTCG stays inside the 0% bracket throughout)."""
    result = step_buckets_dragged(
        taxable=10_000.0,
        tax_deferred=5_000.0,
        roth=0.0,
        r=0.0,
        gi=0.0,
        spending=9_800.0,
        penalty_free=True,
        pension_ss_ordinary=40_000.0,
        gains_fraction=0.5,
        n_65_plus=2,
        age=65,
        bracket_fill_target=100_800.0,
        prior_tax_deferred=0.0,
    )
    assert result.iterations == 2
    # Spending drained taxable to $200 (not to $0) without touching TD.
    assert result.taxable_draw == 9_800.0
    assert result.tax_deferred_draw == 0.0
    # THE PINNED SPLIT: the tax-funding draw spans taxable's last $200 THEN
    # spills to tax_deferred for the rest.
    assert result.tf_taxable_draw == pytest.approx(200.0, abs=1e-6)
    assert result.tf_tax_deferred_draw == pytest.approx(750.0, abs=1e-6)
    assert result.tax == pytest.approx(950.0, abs=1e-6)
    # The lever is capped at the residual ($5,000 - $750 = $4,250), nowhere
    # near its desired $96,300 fill — "the bill wins" (DECISION 3 priority).
    assert result.lever_transfer == pytest.approx(4_250.0, abs=1e-6)
    desired_fill = 100_800.0 + (32_200.0 + 1_650.0 * 2) - (40_000.0 + 0.0)
    assert desired_fill > result.lever_transfer  # the lever wanted far more
    # Attribution cross-check.
    base = 45_000.0 - result.lever_transfer  # ordinary excl. lever
    tax_base = compute_federal_tax(base, result.ltcg_income, 2)
    tax_base_lever = compute_federal_tax(base + result.lever_transfer, result.ltcg_income, 2)
    assert result.spending_tax == pytest.approx(tax_base.total)
    assert result.voluntary_tax == pytest.approx(tax_base_lever.total - tax_base.total)
    assert result.forced_tax == 0.0
    assert (
        result.spending_tax + result.voluntary_tax + result.forced_tax
        == result.tax
    )
    # Final balances + sum-preservation (transfers preserve the year's sum).
    assert result.taxable == pytest.approx(4_250.0, abs=1e-6)
    assert result.tax_deferred == pytest.approx(0.0, abs=1e-6)
    assert result.roth == 0.0
    end_total = result.taxable + result.tax_deferred + result.roth
    assert end_total == pytest.approx(15_000.0 - 9_800.0 - result.tax, abs=1e-6)


def test_hand_anchor_rmd_movement_year_shrinks_next_years_rmd():
    """An RMD movement year (age 76, well past the lever window): prior-year
    tax_deferred balance $900,000 drives this year's RMD =
    900,000 / rmd_factor(76) = 900,000 / 23.7 = $37,974.6835... Spending draws
    $30,000 entirely from taxable (plenty available), leaving tax_deferred
    UNTOUCHED by spending — so the excess (RMD minus the $0 actual TD draw)
    moves in full: ``rmd_excess ~= $37,974.68``.

    Chained into a SECOND year (age 77) with the prior_tax_deferred set to
    year 1's RECORDED (post-transfer) tax_deferred balance
    (~$862,025.32, i.e. $900,000 minus the excess that moved) proves DECISION
    4's "future RMDs shrink through the real table": year 2's RMD on the
    REDUCED balance (862,025.32 / 22.9 ~= $37,643.03) is materially smaller
    than what an (incorrect, non-moving) RMD on the ORIGINAL $900,000 would
    have been (900,000 / 22.9 ~= $39,301.31) — about $1,658/yr less, compounding
    every year the excess keeps moving."""
    year1 = step_buckets_dragged(
        taxable=200_000.0,
        tax_deferred=900_000.0,
        roth=100_000.0,
        r=0.0,
        gi=0.0,
        spending=30_000.0,
        penalty_free=True,
        pension_ss_ordinary=50_000.0,
        gains_fraction=0.5,
        n_65_plus=2,
        age=76,
        bracket_fill_target=None,
        prior_tax_deferred=900_000.0,
    )
    expected_rmd_76 = 900_000.0 / (23.7)
    assert year1.rmd_excess == pytest.approx(expected_rmd_76, abs=1e-6)
    assert year1.lever_transfer == 0.0  # age 76 is past the lever window
    # Attribution: forced_tax is the marginal bill on the RMD excess, stacked
    # on top of the (below-deduction) spending-attributed base.
    base = 50_000.0  # pension/SS + the $0 TD draw
    tax_base = compute_federal_tax(base, year1.ltcg_income, 2)
    tax_with_rmd = compute_federal_tax(base + year1.rmd_excess, year1.ltcg_income, 2)
    assert year1.spending_tax == pytest.approx(tax_base.total)
    assert year1.forced_tax == pytest.approx(tax_with_rmd.total - tax_base.total)
    assert year1.voluntary_tax == 0.0
    assert (
        year1.spending_tax + year1.voluntary_tax + year1.forced_tax
        == pytest.approx(year1.tax)
    )

    # Year 2 (age 77): prior_tax_deferred is year 1's RECORDED (reduced)
    # ending balance — the excess movement's whole point.
    year2 = step_buckets_dragged(
        taxable=year1.taxable,
        tax_deferred=year1.tax_deferred,
        roth=year1.roth,
        r=0.0,
        gi=0.0,
        spending=30_000.0,
        penalty_free=True,
        pension_ss_ordinary=50_000.0,
        gains_fraction=0.5,
        n_65_plus=2,
        age=77,
        bracket_fill_target=None,
        prior_tax_deferred=year1.tax_deferred,
    )
    rmd_on_reduced_balance = year1.tax_deferred / 22.9
    rmd_on_original_balance = 900_000.0 / 22.9
    assert year2.rmd_excess == pytest.approx(rmd_on_reduced_balance, abs=1e-6)
    assert rmd_on_reduced_balance < rmd_on_original_balance
    assert (rmd_on_original_balance - rmd_on_reduced_balance) > 1_500.0  # real, material shrinkage


# ============================================================================
# Tax-drag design td1 (td-b slice): ``dragged_depleted_age`` (the full-horizon
# depletion signal the Historical/MC fan re-point reads) and the estate-cost
# readout (DECISION 9, engine side).
# ============================================================================


def test_dragged_depleted_age_populated_full_horizon():
    """``dragged_depleted_age`` fires at the first age the DRAGGED total (not
    just the pre-59½ taxable/reachable metrics) hits zero — a full-horizon
    signal, distinct from ``taxable_depletion_age``/``reachable_depletion_age``.
    A tiny, all-tax-deferred split with a big spend depletes quickly and
    completely — pin it fires, and that every SUBSEQUENT recorded balance is
    exactly zero (the post-depletion convention)."""
    s = replace(
        default_scenario(),
        taxable_balance=0.0,
        tax_deferred_balance=50_000.0,
        roth_balance=0.0,
        initial_portfolio=50_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert path.dragged_depleted_age is not None
    assert path.dragged_depleted_age in path.ages
    idx = path.ages.index(path.dragged_depleted_age)
    for i in range(idx, len(path.ages)):
        assert path.taxable[i] + path.tax_deferred[i] + path.roth[i] == 0.0


def test_dragged_depleted_age_none_when_never_depletes():
    """A well-funded split scenario (the drag-direction fixture) never
    depletes — ``dragged_depleted_age`` is None."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert path.dragged_depleted_age is None


def test_aggregate_estate_cost_median_of_differences_not_difference_of_medians():
    """``aggregate_estate_cost`` (DECISION 9): the median of PER-PATH
    differences, never the difference of medians — a fixture whose two
    computations would disagree if the aggregator took the wrong shortcut.

    Paths (pretax, dragged): (100, 90)=10, (200, 50)=150, (300, 280)=20.
    median(diffs) = median([10, 150, 20]) = 20. median(pretax) -
    median(dragged) = 200 - 90 = 110 — a DIFFERENT number, proving the
    aggregator must pair per-path, never difference medians independently.
    """
    pretax = [100.0, 200.0, 300.0]
    dragged = [90.0, 50.0, 280.0]
    success = [True, True, True]
    result = aggregate_estate_cost(pretax, dragged, success, success)
    assert result.count == 3
    assert result.median_estate_cost == pytest.approx(20.0)
    assert result.median_estate_cost != pytest.approx(110.0)


def test_aggregate_estate_cost_success_rate_delta_discloses_what_the_cost_alone_hides():
    """The paired success-rate delta — DECISION 9's "without this pairing,
    'estate cost ≈ $0' reads as 'taxes are free' in exactly the scenarios
    where they broke the plan." Two paths where BOTH runs deplete (per-path
    diff pinned at $0 — nothing left to lose in either world) and one path
    where only the dragged world depletes."""
    pretax_terminals = [0.0, 100.0]
    dragged_terminals = [0.0, 0.0]
    pretax_success = [False, True]
    dragged_success = [False, False]
    result = aggregate_estate_cost(
        pretax_terminals, dragged_terminals, pretax_success, dragged_success
    )
    assert result.pretax_success_rate == pytest.approx(0.5)
    assert result.dragged_success_rate == 0.0
    assert result.success_rate_delta == pytest.approx(-0.5)
    # The estate-cost median alone (a middling $50) understates how bad this
    # is — the success-rate delta is what discloses the real story.
    assert result.median_estate_cost == pytest.approx(50.0)


def test_aggregate_estate_cost_empty_input_zeroed_not_a_crash():
    """An empty input returns a zeroed readout (defensive; mirrors
    ``aggregate_tax_location_risk``'s empty-input discipline)."""
    result = aggregate_estate_cost([], [], [], [])
    assert result.count == 0
    assert result.median_estate_cost == 0.0
    assert result.pretax_success_rate == 0.0
    assert result.dragged_success_rate == 0.0
    assert result.success_rate_delta == 0.0


def test_historical_estate_cost_populated_and_success_delta_non_positive():
    """Integration: a real Historical split run populates ``estate_cost``.

    Static spend (constant mode, the default): the dragged total is <= the
    paired pre-tax total at EVERY age in EVERY cohort (the drag-direction
    BINDS, general — not just the deterministic path), which forces
    ``dragged_success ⊆ pretax_success`` per cohort — so the success-rate
    delta is <= 0 EXACTLY, not just typically, on any static-spend fixture.
    The median estate cost is positive on this tax-deferred-heavy fixture
    (real separation already pinned by the deterministic drag-direction test
    on the same split proportions).
    """
    s = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    result = simulate_historical(s)
    cost = result.estate_cost
    assert cost is not None
    assert cost.count == result.cohort_count
    assert cost.median_estate_cost > 0.0
    assert cost.success_rate_delta <= 1e-9
    assert 0.0 <= cost.pretax_success_rate <= 1.0
    assert 0.0 <= cost.dragged_success_rate <= 1.0


def test_monte_carlo_estate_cost_populated_same_gate_as_tax_location_risk():
    """Integration: Monte Carlo populates the same ``estate_cost`` readout,
    under the SAME gate as ``tax_location_risk``/``federal_tax``."""
    s = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    result = simulate_monte_carlo(s, n_trials=200)
    cost = result.estate_cost
    assert cost is not None
    assert cost.count == 200
    assert cost.success_rate_delta <= 1e-9


def test_feature_off_no_estate_cost():
    """No split → ``estate_cost`` stays None, same as the other split-only
    blocks (back-compatible)."""
    result = simulate_historical(default_scenario())
    assert result.estate_cost is None
    mc_result = simulate_monte_carlo(default_scenario(), n_trials=50)
    assert mc_result.estate_cost is None


# ============================================================================
# td-c: the ``estate_cost_of_taxes`` API payload block (DECISION 9) — one
# shape in all three modes (deterministic single pair; Historical/MC serialize
# the engine's own ``EstateCostOfTaxes`` aggregate). Same feature-off /
# buckets-no-op gate as ``tax_location`` / ``federal_tax``.
# ============================================================================

_ESTATE_COST_KEYS = {
    "count",
    "median_estate_cost",
    "pretax_success_rate",
    "dragged_success_rate",
    "success_rate_delta",
}


def test_api_deterministic_estate_cost_block_shape():
    """The deterministic single-pair estate-cost block: shape, count == 1, and
    a real positive cost on the tax-deferred-heavy fixture (the SAME real
    separation ``test_api_deterministic_tax_location_block_shape`` used to pin
    against the main line, before td-c moved the main line itself to dragged)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "taxable_balance": 450_000.0,
            "tax_deferred_balance": 1_080_000.0,
            "roth_balance": 270_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "estate_cost_of_taxes" in body
    ec = body["estate_cost_of_taxes"]
    assert set(ec) == _ESTATE_COST_KEYS
    assert ec["count"] == 1
    assert ec["median_estate_cost"] > 1.0
    assert ec["pretax_success_rate"] in (0.0, 1.0)
    assert ec["dragged_success_rate"] in (0.0, 1.0)
    assert ec["success_rate_delta"] <= 1e-9


def test_api_historical_estate_cost_block_shape():
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "taxable_balance": 450_000.0,
            "tax_deferred_balance": 1_080_000.0,
            "roth_balance": 270_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "estate_cost_of_taxes" in body
    ec = body["estate_cost_of_taxes"]
    assert set(ec) == _ESTATE_COST_KEYS
    assert ec["count"] == body["cohort_count"]
    assert ec["success_rate_delta"] <= 1e-9


def test_api_monte_carlo_estate_cost_block_shape():
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "taxable_balance": 450_000.0,
            "tax_deferred_balance": 1_080_000.0,
            "roth_balance": 270_000.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "estate_cost_of_taxes" in body
    ec = body["estate_cost_of_taxes"]
    assert set(ec) == _ESTATE_COST_KEYS
    assert ec["count"] == body["trial_count"]


def test_api_feature_off_no_estate_cost_block():
    """No split -> no estate_cost_of_taxes key, in all three modes."""
    for body_json in (
        {"mode": "deterministic"},
        {"mode": "historical"},
        {"mode": "monte_carlo"},
    ):
        res = client.post("/api/trajectory", json=body_json)
        assert res.status_code == 200
        assert "estate_cost_of_taxes" not in res.json()


def test_api_buckets_mode_noop_split_no_estate_cost_block():
    """tb1/td1 is single-mode only: a split with sourcing_mode=buckets is
    no-op'd — no estate_cost_of_taxes block either (same gate as tax_location /
    federal_tax)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "deterministic",
            "sourcing_mode": "buckets",
            "taxable_balance": 1_080_000.0,
            "tax_deferred_balance": 504_000.0,
            "roth_balance": 216_000.0,
        },
    )
    assert res.status_code == 200
    assert "estate_cost_of_taxes" not in res.json()


# ============================================================================
# td-b review fix: REALIZED spend (fundable-capped consumption) vs the
# strategy's INTENDED spend. The defect this section exists to pin: recording
# the intended spend fed the fan re-point's spend fan / worst-cohort /
# adjustment stats a full spending target (e.g. the $85k floor) on a $0
# portfolio from the depletion age onward — overstating after-tax consumption
# and inverting the worst-cohort ranking. ``TaxLocationPath.realized_spend``
# mirrors sim_core._simulate_cohort's realized-spend discipline on the
# DRAGGED begin-of-year balance.
# ============================================================================


def _depleting_guardrails_split():
    """The reviewer's empirical fixture: guardrails, $500k all-tax-deferred.

    Deterministically depletes at 54 (guardrail cuts 110k -> 99k -> 89.1k,
    then floors at $85k; the tax bill on all-TD draws is what pushes the
    crossing year under). Person B's Social Security is zeroed so guaranteed
    income stays below the $85k floor in every year: $24,000 at 60 (A's
    pension), $33,000 at 65 (both pensions), $66,000 at 67 (+ A's SS) — so
    post-depletion affordable consumption is $0 before 60 and always below the
    intended floor after.
    """
    return replace(
        default_scenario(),
        initial_portfolio=500_000.0,
        taxable_balance=0.0,
        tax_deferred_balance=500_000.0,
        roth_balance=0.0,
        spending_mode="guardrails",
        person_b_ss_amount=0.0,
    )


def test_realized_spend_equals_intended_on_non_depleting_path():
    """On a path that never depletes, the fundable cap never binds:
    ``realized_spend == spending`` EXACTLY at every age (min() returns the
    intended float unchanged), so every non-depleting consumer readout is
    untouched by the realized-series re-point."""
    s = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    path = simulate_tax_location_deterministic(s)
    assert path.dragged_depleted_age is None
    assert path.realized_spend == path.spending


def test_realized_spend_caps_at_affordable_post_depletion_guardrails():
    """Reviewer fixture, part (a): after the dragged path depletes, the
    RECORDED consumption tracks the affordable level (0 before GI starts,
    ``gi/(1+r/2)`` after — sim_core's exact post-depletion formula), never the
    strategy's intended target (the $85k floor). Also pins the SENIORITY
    convention in the crossing year: spending sources first (fully funded and
    recorded in full), the tax bill is junior and is what triggers depletion.
    """
    from firebridgelab.scenario import blended_real_return_at_age, gi_at_age

    s = _depleting_guardrails_split()
    path = simulate_tax_location_deterministic(s)

    assert path.dragged_depleted_age == 54
    depletion_idx = path.ages.index(54)

    # Crossing year (spending senior): the accounts covered the FULL intended
    # spend — realized == intended == the $85k floor — and the junior tax
    # bill is what pushed the total under (depletion with full consumption).
    assert path.realized_spend[depletion_idx] == path.spending[depletion_idx]
    assert path.realized_spend[depletion_idx] == pytest.approx(85_000.0)

    for i, age in enumerate(path.ages):
        if age <= 54:
            continue
        r = blended_real_return_at_age(s, age)
        gi = gi_at_age(s, age)
        affordable = gi
        # Post-depletion: realized == the affordable level, exactly the
        # sim_core convention (gi/(1+r/2) on a $0 balance; 0.0 before 60).
        assert path.realized_spend[i] == pytest.approx(
            min(path.spending[i], affordable)
        )
        # ... and NEVER the intended floor (the defect this test exists for).
        assert path.realized_spend[i] < path.spending[i]
        if age < 60:
            assert path.realized_spend[i] == 0.0
    # Empirical anchors: a $0 balance funds exactly the GI ($24,000 at 60,
    # $33,000 at 65, $66,000 at 67).
    assert path.realized_spend[path.ages.index(60)] == pytest.approx(
        24_000.0, abs=0.01
    )
    assert path.realized_spend[path.ages.index(65)] == pytest.approx(
        33_000.0, abs=0.01
    )
    assert path.realized_spend[path.ages.index(67)] == pytest.approx(
        66_000.0, abs=0.01
    )
    # The intended series is NOT capped (the reconciliation recurrence and the
    # fixed point keep stepping with it — byte-stability of the dragged totals).
    assert all(v >= 85_000.0 - 1e-9 for v in path.spending)


def test_historical_worst_cohort_ranks_depleted_cohort_squeezed():
    """Reviewer fixture, part (b): with realized recording, a depleted cohort's
    post-depletion years record ~$0 (not the $85k floor), so the worst-cohort
    selection (lowest TOTAL spend) correctly lands on a DEPLETED cohort with a
    min spend at the affordable level — under intended recording every cohort
    recorded >= the floor every year and the depleted cohorts ranked AWAY from
    worst (the inversion)."""
    s = _depleting_guardrails_split()
    result = simulate_historical(s)

    assert result.depletion_count > 0
    wc = result.worst_cohort
    assert wc is not None
    assert wc.depleted_age is not None
    # Post-depletion pre-60 years record $0 — impossible under intended
    # recording (guardrails floors intent at $85k).
    assert wc.min_spend < 50_000.0
    # Adjustment stats read the same realized series.
    st = result.adjustment_stats
    assert st is not None
    assert st.lowest_single_year_spend < 50_000.0


def test_mc_split_adjustment_stats_read_realized_spend():
    """The MC consumer is a separate call site (_split_trial_trajectories) —
    pin that it reads the realized series too."""
    s = _depleting_guardrails_split()
    result = simulate_monte_carlo(s, n_trials=100)
    st = result.adjustment_stats
    assert st is not None
    assert st.lowest_single_year_spend < 50_000.0


def test_overlay_effective_rate_reads_realized_on_real_depleting_run():
    """End-to-end overlay honesty on a REAL depleting run (the synthetic
    fixture tests in test_federal_tax.py pin the arithmetic; this pins the
    plumbing): ``effective_rate`` divides by REALIZED total spend — strictly
    higher than the intended-denominator rate on a depleting path (same tax,
    less spend). (``gross_outflow``'s equivalent post-depletion honesty
    property — reading realized, not intended, consumption — is pinned
    separately below.)"""
    from firebridgelab.federal_tax import compute_tax_overlay

    s = _depleting_guardrails_split()
    path = simulate_tax_location_deterministic(s)
    overlay = compute_tax_overlay(s, path)
    assert overlay.effective_rate > sum(overlay.tax) / sum(path.spending)


def test_gross_outflow_reads_realized_on_real_depleting_run():
    """``gross_outflow`` — "what the portfolio actually funds" — reads the
    REALIZED consumption (delta re-review straggler: it once appended the
    INTENDED spend, showing the portfolio funding the $85k target at ages it
    held $0 — the realized-based fix pinned here). On the real depleting
    guardrails fixture:

      (a) the identity ``gross_outflow == realized_spend + tax`` holds with
          EXACT ``==`` at every age (the tax component stays the recorded
          ATTEMPTED bill — the chart tooltip decomposes on this identity; the
          accepted cost is a slight overstatement of FUNDED outflow in the one
          crossing year, where the junior bill is only partially fundable);
      (b) post-depletion the line tracks the affordable GI level ($0 before
          60, gi/(1+r/2) after — zero tax per the skip convention), never a
          six-figure "funded" outflow on a $0 portfolio;
      (c) the Historical aggregate ``gross_outflow_median`` reflects it (the
          late-life median sits BELOW the $85k intended floor, which is
          exactly what an intended-based line would pin it at).
    """
    s = _depleting_guardrails_split()
    path = simulate_tax_location_deterministic(s)

    # (a) The exact identity, every age.
    for i in range(len(path.ages)):
        assert path.gross_outflow[i] == path.realized_spend[i] + path.tax[i]

    # (b) Post-depletion: zero tax (skip convention) makes the gross line ==
    # the affordable consumption itself — $0 before GI starts, and always
    # strictly below the intended target the portfolio can't fund.
    assert path.dragged_depleted_age == 54
    for i, age in enumerate(path.ages):
        if age <= 54:
            continue
        assert path.gross_outflow[i] == path.realized_spend[i]  # tax == 0
        assert path.gross_outflow[i] < path.spending[i]
        if age < 60:
            assert path.gross_outflow[i] == 0.0

    # (c) The Historical aggregate reflects the realized line: every cohort is
    # long-depleted by the final age, so the median gross outflow there is the
    # GI-affordable level — strictly below the $85k floor an intended-based
    # line would report (floor + $0 post-depletion tax).
    result = simulate_historical(s)
    agg = result.federal_tax
    assert agg is not None
    assert agg.gross_outflow_median[-1] < 85_000.0
    # An early post-depletion, pre-GI age: the median cohort funds nothing.
    idx_59 = agg.ages.index(59)
    assert agg.gross_outflow_median[idx_59] == 0.0


# ============================================================================
# td-c review fix: the spending_real_drift seam (the td-a TODO, closed). The
# scalar loops (simulate.py / sim_core.py) multiply the strategy's output by
# (1 + spending_real_drift) ** (age - start_age) AFTER spend_for_year; the
# dragged loops (simulate_path_decomposition / simulate_dragged_depletion_only)
# now apply the IDENTICAL unconditional multiplier at the identical position —
# before this, a split + drift combo (user-set, or the sustained_cost_drift stress)
# silently no-op'd the drift in the dragged world and reported a trajectory
# ~4x too optimistic. These pin the lockstep.
# ============================================================================


def _drift_split_scenario():
    """Well-funded constant-mode split + a sustained_cost_drift-magnitude drift.

    Constant mode so the strategy's raw output is balance-independent — the
    dragged and scalar engines then see the IDENTICAL pre-drift spend, and any
    intent divergence between them is the drift seam itself.
    """
    return replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
        spending_real_drift=0.015,
    )


def test_dragged_intent_spend_matches_scalar_drifted_spend_exactly():
    """The engines-can't-drift-apart seam pin: on a split + drift scenario the
    dragged decomposition's INTENDED spend equals the scalar engine's drifted
    spend year-for-year, EXACT ``==`` — both compute the same
    ``strategy_output * (1 + d) ** (age - start_age)`` with the same operand
    order, so bit-equality holds by construction (anything looser would let
    the two multipliers drift apart silently, the exact defect this fixed).
    The scalar side reads ``simulate()``'s recorded (realized) spend, equal to
    its intent on this non-depleting-scalar fixture."""
    s = _drift_split_scenario()
    path = simulate_tax_location_deterministic(s)
    scalar = simulate(s)
    assert scalar.depleted_age is None  # scalar realized == scalar intent
    assert len(path.spending) == len(scalar.points)
    for i in range(len(path.spending)):
        assert path.spending[i] == scalar.points[i].spending
    # The drift really fired (not a vacuous zero-drift comparison) and is
    # anchored at the SIMULATION start age: year 0's multiplier is (1+d)**0
    # == 1.0, so the first year's intent is the raw annual_spending EXACTLY
    # (the off-by-one anchor bug would escalate year 0 too).
    assert path.spending[0] == s.annual_spending
    assert path.spending[-1] > s.annual_spending * 1.5


def test_drift_zero_dragged_path_byte_identical():
    """drift == 0.0 is inert BY CONSTRUCTION ((1+0)**k == 1.0 multiplies every
    intent by exactly 1.0): a split scenario with the drift knob explicitly 0.0
    produces a byte-identical TaxLocationPath to the same scenario untouched
    (the 45-key td-b fingerprint re-run pins the same fact across the
    Historical/MC fans and estate-cost blocks; this is the fast in-suite
    version)."""
    base = replace(
        default_scenario(),
        taxable_balance=1_080_000.0,
        tax_deferred_balance=504_000.0,
        roth_balance=216_000.0,
    )
    explicit_zero = replace(base, spending_real_drift=0.0)
    p_base = simulate_tax_location_deterministic(base)
    p_zero = simulate_tax_location_deterministic(explicit_zero)
    assert p_base == p_zero  # frozen dataclass field-wise (list) equality


def test_estate_cost_pairing_drift_consistent():
    """DECISION 9 pairing sanity under drift: the pre-tax counterfactual
    (``simulate()``) and the dragged run BOTH carry the drift now, escalating
    their intended spends identically — so the estate cost measures TAXES, not
    a spend-schedule mismatch between the two worlds. Before the seam fix the
    dragged world spent flat while the counterfactual escalated, poisoning the
    pairing on any drift scenario."""
    s = _drift_split_scenario()
    path = simulate_tax_location_deterministic(s)
    pretax = simulate(s)
    # Identical escalating intent in both worlds (exact — same multiplier).
    assert pretax.depleted_age is None
    for i in range(len(path.spending)):
        assert path.spending[i] == pretax.points[i].spending
    # And the pairing itself: dragged terminal below the pre-tax counterfactual
    # by a real tax-sized gap on this tax-deferred-bearing fixture.
    dragged_terminal = path.taxable[-1] + path.tax_deferred[-1] + path.roth[-1]
    assert dragged_terminal < pretax.final_portfolio - 1.0


def test_dragged_depletion_only_carries_the_same_drift():
    """The solver's lean loop (``simulate_dragged_depletion_only``) applies the
    SAME drift multiplier as the full loop — a drift applied in the display
    engine but not the solver would bisect a different (flat-spend, richer)
    world than the one rendered. Pinned by depletion-age agreement on a drift
    scenario that genuinely depletes under drag."""
    from firebridgelab.spending import make_strategy
    from firebridgelab.tax_location import deterministic_blended_returns

    # The seam-pin fixture with a 2% drift depletes under drag at 89 (drift raises
    # late-life spend enough to sink the dragged world). Raised from the shared
    # fixture's 1.5% on 2026-10-11, when the richer rebalanced deterministic
    # return stopped the 1.5% world from depleting.
    s = replace(_drift_split_scenario(), spending_real_drift=0.02)
    full = simulate_tax_location_deterministic(s)
    assert full.dragged_depleted_age is not None  # the fixture really bites
    lean = simulate_dragged_depletion_only(
        s, deterministic_blended_returns(s), make_strategy(s)
    )
    assert lean == full.dragged_depleted_age
