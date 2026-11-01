"""Tests pinning down the API surface.

  1. Defaults: bare GET returns the hardcoded default scenario.
  2. Overrides: query params flow into the Scenario the engine sees.
  3. Validation: bad inputs return HTTP 422 with a useful detail.
  4. Per-person GI override: delaying Person B's SS to 70 flows through.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from firebridgelab import default_bucket_config, default_scenario, simulate
from tests._canary import DET_YEAR0_CANARY, DET_FINAL_PIN, DET_YEAR0_PIN


client = TestClient(app)


def test_defaults_unchanged_with_no_params():
    """GET /api/trajectory with no params must reproduce the default scenario."""
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    expected = default_scenario()
    assert body["scenario"]["initial_portfolio"] == expected.initial_portfolio
    assert body["scenario"]["annual_spending"] == expected.annual_spending
    # allocation echoes back as a list of dicts (the default 60/40 blend). Slice-0
    # re-baseline (asset-class design): each class now carries a stable `id` (the
    # class id / allocation key) and a display `label`, REPLACING the old `name`
    # key — class `us_total_market` ("US Total Stock Market") binds the existing
    # `us_equity` series, `us_agg_bonds` ("US Bonds (10-yr Treasury proxy)") binds `us_bonds`.
    # `volatility` is still the per-class Shiller default (a defaulted field the
    # Deterministic/Historical engines never read, so the trajectory numbers below
    # are unchanged). This is the expected, reviewed echo-shape change, NOT the
    # numeric canary (which still holds — see the age-50 value just below).
    # The default scenario's per-class volatility is single-sourced with
    # /api/classes, so assert against that live figure rather than
    # a hardcoded rounded literal (drift-proof, the pattern).
    classes = {c["id"]: c for c in client.get("/api/classes").json()["classes"]}
    assert body["scenario"]["allocation"] == [
        {"id": "us_total_market", "label": "US Total Stock Market", "weight": 0.60, "real_return": 0.07, "volatility": classes["us_total_market"]["volatility"]},
        {"id": "us_agg_bonds", "label": "US Bonds (10-yr Treasury proxy)", "weight": 0.40, "real_return": 0.02, "volatility": classes["us_agg_bonds"]["volatility"]},
    ]
    # Year-0 (age 50) is the deterministic canary at the default scenario.
    year_zero = body["trajectory"]["points"][0]
    assert year_zero["age"] == 50
    assert year_zero["end_portfolio"] == DET_YEAR0_CANARY


def test_query_params_override_defaults():
    """Sending overrides changes the scenario the engine simulates."""
    res = client.get(
        "/api/trajectory",
        params={"initial_portfolio": 5_000_000, "annual_spending": 210_000},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["initial_portfolio"] == 5_000_000
    assert body["scenario"]["annual_spending"] == 210_000
    year_zero = body["trajectory"]["points"][0]
    assert year_zero["start_portfolio"] == 5_000_000.0
    # Sanity: end = P*(1+r) - S*(1+r/2) with r the rebalanced 60/40 return.
    from firebridgelab import blended_real_return_at_age, default_scenario

    r = blended_real_return_at_age(default_scenario(), 50)
    assert year_zero["end_portfolio"] == pytest.approx(
        5_000_000 * (1 + r) - 210_000 * (1 + r / 2), abs=1e-6
    )


def test_invalid_portfolio_returns_422():
    """Negative portfolio is rejected with a 422 and a useful message."""
    res = client.get("/api/trajectory", params={"initial_portfolio": -100})
    assert res.status_code == 422
    assert "initial_portfolio" in res.json()["detail"]


def test_allocation_weights_not_summing_to_one_returns_422():
    """Allocation whose weights don't sum to 100% is rejected with a 422."""
    res = client.get(
        "/api/trajectory",
        params={"allocation": "Equity:0.60:0.07;Bonds:0.32:0.02"},
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "sum to 100%" in detail
    assert "92%" in detail


def test_higher_equity_allocation_beats_default_final():
    """An 80/20 (6.0% real) allocation ends richer than the 60/40 (5.0%) default."""
    default_res = client.get(
        "/api/trajectory",
        params={"allocation": "Equity:0.60:0.07;Bonds:0.40:0.02"},
    )
    high_eq_res = client.get(
        "/api/trajectory",
        params={"allocation": "Equity:0.80:0.07;Bonds:0.20:0.02"},
    )
    assert default_res.status_code == 200
    assert high_eq_res.status_code == 200
    default_final = default_res.json()["trajectory"]["final_portfolio"]
    high_eq_final = high_eq_res.json()["trajectory"]["final_portfolio"]
    assert high_eq_final > default_final


def test_end_age_before_start_age_returns_422():
    """end_age <= start_age is rejected."""
    res = client.get(
        "/api/trajectory",
        params={"start_age": 70, "end_age": 65},
    )
    assert res.status_code == 422
    assert "end_age" in res.json()["detail"]


def test_deterministic_default_mode_unchanged():
    """No `mode` param → deterministic shape, unchanged (plus an echoed mode key).

    The default deterministic response keeps its `scenario` + `trajectory` shape;
    Build 1 additively echoes `mode: 'deterministic'`. This pins that deterministic
    mode is the default and its payload didn't regress.
    """
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert "trajectory" in body
    assert body["trajectory"]["points"][0]["end_portfolio"] == DET_YEAR0_CANARY


def test_historical_mode_returns_bands_and_depletion():
    """mode=historical returns the Build-2 percentile-fan contract for the default."""
    res = client.get("/api/trajectory", params={"mode": "historical"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "historical"
    # Cohorts run from full history (1871) per the brief.
    assert body["start_year"] == 1871
    assert body["cohort_count"] > 100
    # Build 2: `bands` is one {age, p10, p25, median, p75, p90} per age over the
    # default horizon 50..95.
    bands = body["bands"]
    assert len(bands) == 95 - 50 + 1
    assert bands[0]["age"] == 50
    assert bands[-1]["age"] == 95
    for b in bands:
        assert set(b) == {"age", "p10", "p25", "median", "p75", "p90"}
        # Percentiles are ordered and non-negative at every age.
        assert 0 <= b["p10"] <= b["p25"] <= b["median"] <= b["p75"] <= b["p90"]
    # Ending range is the (p10, p90) of the final-age column, matching the bands.
    erange = body["ending_range"]
    assert erange["p10"] == bands[-1]["p10"]
    assert erange["p90"] == bands[-1]["p90"]
    assert erange["p10"] <= erange["p90"]
    # Default constant mode shows a historical tail (17 of 110 cohorts);
    # the structural depletion fields are present and aligned.
    assert body["depletion"]["count"] == 17
    assert body["depletion"]["earliest_age"] == 63
    # Echoes the resolved scenario like the deterministic path.
    assert body["scenario"]["initial_portfolio"] == default_scenario().initial_portfolio


def test_historical_mode_respects_scenario_overrides():
    """Historical mode honours query-param overrides (high spend, no GI depletes)."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "annual_spending": 300_000,
            "person_a_pension_amount": 0,
            "person_b_pension_amount": 0,
            "person_a_ss_fra_amount": 0,
            "person_b_ss_fra_amount": 0,
            "allocation": "Equity:1.0:0.07",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["depletion"]["count"] > 0
    assert body["depletion"]["earliest_age"] is not None


def test_unknown_mode_returns_422():
    """An unrecognised mode is rejected with a 422 and a useful message."""
    res = client.get("/api/trajectory", params={"mode": "bootstrap"})
    assert res.status_code == 422
    assert "mode must be one of" in res.json()["detail"]


def test_historical_mode_validates_scenario():
    """Historical mode still 422s on a bad scenario (validation runs before sim)."""
    res = client.get(
        "/api/trajectory",
        params={"mode": "historical", "initial_portfolio": -100},
    )
    assert res.status_code == 422
    assert "initial_portfolio" in res.json()["detail"]


# ---------- Build 3: Historical comparison (two-scenario) ----------
#
# The historical comparison is TWO independent ?mode=historical calls (Scenario A
# and Scenario B), exactly as the deterministic comparison fires two
# /api/trajectory calls — there is NO new comparison endpoint (Decision 7 / API
# contract). These tests pin that the two-call path the front-end uses returns
# independent, correctly-differing fans.


def test_historical_comparison_two_calls_differ_by_allocation():
    """A 60/40 (A) vs 80/20 (B) historical comparison: B's fan ends higher.

    The front-end Build-3 comparison is two ?mode=historical calls. Both run the
    full 1871-history cohorts; the higher-equity B has a higher ending median and
    a wider 10–90 ending range (more upside AND more downside) than the 60/40 A —
    the volatility/sequence-of-returns tradeoff the feature exists to surface.
    """
    res_a = client.get(
        "/api/trajectory",
        params={"mode": "historical", "allocation": "Equity:0.60:0.07;Bonds:0.40:0.02"},
    )
    res_b = client.get(
        "/api/trajectory",
        params={"mode": "historical", "allocation": "Equity:0.80:0.07;Bonds:0.20:0.02"},
    )
    assert res_a.status_code == 200
    assert res_b.status_code == 200
    a = res_a.json()
    b = res_b.json()

    # Both run the same cohort set (same horizon over the same history).
    assert a["start_year"] == b["start_year"] == 1871
    assert a["cohort_count"] == b["cohort_count"]
    assert a["cohort_count"] > 100

    a_end_median = a["bands"][-1]["median"]
    b_end_median = b["bands"][-1]["median"]
    # Higher equity → higher ending median across history.
    assert b_end_median > a_end_median
    # And a wider ending 10–90 spread (more dispersion from more equity).
    a_spread = a["ending_range"]["p90"] - a["ending_range"]["p10"]
    b_spread = b["ending_range"]["p90"] - b["ending_range"]["p10"]
    assert b_spread > a_spread


def test_post_historical_window_start_year_clamps_cohorts():
    """POST mode=historical with window_start_year restricts cohorts to that floor.

    Comparison-window alignment (option 3): the default 60/40 runs 1871+ natively;
    POSTing window_start_year=1954 clamps its earliest cohort to 1954 and reports
    testable_since=1954. The clamp NARROWS (cohort count drops), it never expands.
    """
    native = client.post("/api/trajectory", json={"mode": "historical"}).json()
    assert native["testable_since"] == 1871

    clamped = client.post(
        "/api/trajectory", json={"mode": "historical", "window_start_year": 1954}
    )
    assert clamped.status_code == 200
    body = clamped.json()
    assert body["testable_since"] == 1954
    assert body["start_year"] == 1954
    assert body["cohort_count"] < native["cohort_count"]


def test_post_historical_window_start_year_aligns_a_b_comparison():
    """A 1871 scenario vs a 1954 scenario, BOTH clamped to 1954, score from 1954.

    The two-call comparison the front-end makes: the common floor =
    max(testable_since_A, testable_since_B). Sending it as window_start_year to BOTH
    makes their earliest cohort the same year — the head-to-head is measured over the
    same starting cohort instead of one side getting all of history.
    """
    # A: default 60/40 (native 1871). B: a 60/40 using intermediate treasury
    # (it_treasury, data starts 1954) → native 1954.
    a_alloc = [
        {"id": "us_total_market", "weight": 0.6, "real_return": 0.07, "volatility": 0.17},
        {"id": "us_agg_bonds", "weight": 0.4, "real_return": 0.02, "volatility": 0.05},
    ]
    b_alloc = [
        {"id": "us_total_market", "weight": 0.6, "real_return": 0.07, "volatility": 0.17},
        {"id": "it_treasury", "weight": 0.4, "real_return": 0.02, "volatility": 0.05},
    ]
    a_native = client.post(
        "/api/trajectory", json={"mode": "historical", "allocation": a_alloc}
    ).json()
    b_native = client.post(
        "/api/trajectory", json={"mode": "historical", "allocation": b_alloc}
    ).json()
    assert a_native["testable_since"] == 1871
    assert b_native["testable_since"] == 1954

    floor = max(a_native["testable_since"], b_native["testable_since"])  # 1954
    a_clamped = client.post(
        "/api/trajectory",
        json={"mode": "historical", "allocation": a_alloc, "window_start_year": floor},
    ).json()
    b_clamped = client.post(
        "/api/trajectory",
        json={"mode": "historical", "allocation": b_alloc, "window_start_year": floor},
    ).json()
    # BOTH now report the common testable window.
    assert a_clamped["testable_since"] == 1954
    assert b_clamped["testable_since"] == 1954
    # A was genuinely narrowed; B (already native 1954) is unchanged.
    assert a_clamped["cohort_count"] < a_native["cohort_count"]
    assert b_clamped["cohort_count"] == b_native["cohort_count"]


def test_post_historical_window_start_year_omitted_unchanged():
    """Omitting window_start_year leaves the historical run on its native window.

    Single (non-comparison) runs are unchanged — the additive field defaults to
    None and the canary 1871/110 holds.
    """
    omitted = client.post("/api/trajectory", json={"mode": "historical"}).json()
    explicit_none = client.post(
        "/api/trajectory", json={"mode": "historical", "window_start_year": None}
    ).json()
    assert omitted["testable_since"] == explicit_none["testable_since"] == 1871
    assert omitted["cohort_count"] == explicit_none["cohort_count"]
    # Same fan — the omitted/None floor changes nothing.
    assert omitted["bands"][-1]["median"] == explicit_none["bands"][-1]["median"]


def test_historical_comparison_asymmetric_depletion():
    """Per-scenario depletion is independent across the two comparison calls.

    Scenario A (default $110k 60/40 with full GI) shows a small historical
    tail; Scenario B (high spend, no GI, all-equity) depletes in many more
    cohorts. Pins that each call reports its OWN depletion stats — the data
    the per-scenario readout ("A: … N of M depleted · B: … M of M depleted")
    is built from — and that B's stress dominates A's tail.
    """
    res_a = client.get("/api/trajectory", params={"mode": "historical"})
    res_b = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "annual_spending": 300_000,
            "person_a_pension_amount": 0,
            "person_b_pension_amount": 0,
            "person_a_ss_fra_amount": 0,
            "person_b_ss_fra_amount": 0,
            "allocation": "Equity:1.0:0.07",
        },
    )
    assert res_a.status_code == 200
    assert res_b.status_code == 200
    a = res_a.json()
    b = res_b.json()

    assert a["depletion"]["count"] == 17
    assert a["depletion"]["earliest_age"] == 63
    assert b["depletion"]["count"] > a["depletion"]["count"]
    assert b["depletion"]["earliest_age"] is not None
    # Independent fans: B's stressed ending median sits below A's.
    assert b["bands"][-1]["median"] < a["bands"][-1]["median"]


def test_historical_comparison_shared_ymax_source_is_max_p90():
    """The front-end's shared Y max = max p90 across BOTH fans; pin that source.

    Both comparison fans share one Y axis computed from the largest p90 across
    the two payloads (historicalSharedYMax). This checks the data backing that:
    B (80/20) reaches a higher p90 somewhere than A (60/40), so the shared max is
    driven by B — i.e. A's fan is drawn on B's taller scale, not its own.
    """
    res_a = client.get(
        "/api/trajectory",
        params={"mode": "historical", "allocation": "Equity:0.60:0.07;Bonds:0.40:0.02"},
    )
    res_b = client.get(
        "/api/trajectory",
        params={"mode": "historical", "allocation": "Equity:0.80:0.07;Bonds:0.20:0.02"},
    )
    a = res_a.json()
    b = res_b.json()
    max_p90_a = max(band["p90"] for band in a["bands"])
    max_p90_b = max(band["p90"] for band in b["bands"])
    assert max_p90_b > max_p90_a  # B drives the shared Y max


# ---------- Monte Carlo mode (mode=monte_carlo) ----------
#
# Monte Carlo returns the SAME fan payload shape as historical (median + per-age
# bands + ending range + depletion), so the front-end fan renderer is reused. It
# carries `trial_count` (the MC analogue of cohort_count) instead of
# cohort_count/start_year. New params: per-class `volatility` inside the
# allocation string, and a `correlation` scalar. 422 on vol < 0 and
# |correlation| > 0.99.


def test_monte_carlo_mode_returns_fan_payload():
    """mode=monte_carlo returns the Historical-shaped fan payload for the default."""
    res = client.get("/api/trajectory", params={"mode": "monte_carlo"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "monte_carlo"
    # MC carries trial_count (its cohort analogue); NOT cohort_count/start_year.
    assert body["trial_count"] == 10_000
    assert "cohort_count" not in body
    assert "start_year" not in body
    # Same `bands` contract as historical: one {age, p10, p25, median, p75, p90}
    # per age over the default horizon 50..95.
    bands = body["bands"]
    assert len(bands) == 95 - 50 + 1
    assert bands[0]["age"] == 50
    assert bands[-1]["age"] == 95
    for b in bands:
        assert set(b) == {"age", "p10", "p25", "median", "p75", "p90"}
        assert 0 <= b["p10"] <= b["p25"] <= b["median"] <= b["p75"] <= b["p90"]
    erange = body["ending_range"]
    assert erange["p10"] == bands[-1]["p10"]
    assert erange["p90"] == bands[-1]["p90"]
    assert erange["p10"] <= erange["p90"]
    # Descriptive depletion stats (no success/failure language in the payload).
    assert "count" in body["depletion"]
    assert "earliest_age" in body["depletion"]
    # Echoes the resolved scenario, including the MC volatility + correlation.
    # Volatility is single-sourced with /api/classes — assert
    # against that live figure, not a hardcoded rounded literal.
    assert body["scenario"]["correlation"] == 0.20
    equity_vol = next(
        c["volatility"] for c in client.get("/api/classes").json()["classes"]
        if c["id"] == "us_total_market"
    )
    assert body["scenario"]["allocation"][0]["volatility"] == equity_vol


def test_monte_carlo_mode_is_seed_reproducible():
    """Two identical mode=monte_carlo calls return byte-identical bands (fixed seed)."""
    a = client.get("/api/trajectory", params={"mode": "monte_carlo"}).json()
    b = client.get("/api/trajectory", params={"mode": "monte_carlo"}).json()
    assert a["bands"] == b["bands"]
    assert a["ending_range"] == b["ending_range"]
    assert a["depletion"] == b["depletion"]


def test_monte_carlo_negative_volatility_returns_422():
    """A negative per-class volatility (in the allocation string) is rejected 422."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo",
            "allocation": "Equity:0.60:0.07:-0.10;Bonds:0.40:0.02:0.088",
        },
    )
    assert res.status_code == 422
    assert "volatility" in res.json()["detail"]


def test_monte_carlo_correlation_out_of_range_returns_422():
    """|correlation| > 0.99 is rejected with a 422 (cholesky guard)."""
    too_high = client.get(
        "/api/trajectory", params={"mode": "monte_carlo", "correlation": 1.0}
    )
    too_low = client.get(
        "/api/trajectory", params={"mode": "monte_carlo", "correlation": -1.5}
    )
    assert too_high.status_code == 422
    assert "correlation must be between" in too_high.json()["detail"]
    assert too_low.status_code == 422


def test_monte_carlo_correlation_at_limit_is_accepted():
    """correlation = 0.99 (the inclusive limit) is accepted (200), not a 422."""
    res = client.get(
        "/api/trajectory", params={"mode": "monte_carlo", "correlation": 0.99}
    )
    assert res.status_code == 200
    assert res.json()["scenario"]["correlation"] == 0.99


def test_monte_carlo_allocation_not_summing_returns_422():
    """A non-summing allocation is rejected 422 (shared parser, same as other modes)."""
    res = client.get(
        "/api/trajectory",
        params={"mode": "monte_carlo", "allocation": "Equity:0.60:0.07;Bonds:0.32:0.02"},
    )
    assert res.status_code == 422
    assert "sum to 100%" in res.json()["detail"]


def test_monte_carlo_no_mapped_class_returns_422():
    """A monte_carlo run whose allocation maps no series 422s (engine ValueError)."""
    res = client.get(
        "/api/trajectory",
        params={"mode": "monte_carlo", "allocation": "Crypto:1.0:0.07"},
    )
    assert res.status_code == 422


def test_monte_carlo_volatility_in_allocation_flows_through():
    """An explicit per-class volatility in the allocation string echoes back."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo",
            "allocation": "Equity:0.60:0.07:0.20;Bonds:0.40:0.02:0.05",
        },
    )
    assert res.status_code == 200
    alloc = res.json()["scenario"]["allocation"]
    assert alloc[0]["volatility"] == 0.20
    assert alloc[1]["volatility"] == 0.05


def test_monte_carlo_omitted_volatility_resolves_to_canonical_default():
    """An allocation string that OMITS per-class volatility resolves each class to
    its OWN canonical default, not the equity dataclass default.

    Before the fix, ``us_agg_bonds`` silently inherited the equity vol (0.174)
    whenever a vol-less allocation string was parsed, inflating MC depletion
    ~2.5-3x. The bond figure is fetched from ``/api/classes`` — the same
    registry the fix resolves against — so this stays drift-proof if the
    canonical bond volatility is ever recalibrated.
    """
    classes = client.get("/api/classes").json()["classes"]
    bonds_vol = next(c["volatility"] for c in classes if c["id"] == "us_agg_bonds")
    equity_vol = next(c["volatility"] for c in classes if c["id"] == "us_total_market")
    assert bonds_vol != 0.174
    assert bonds_vol < 0.174

    res = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo",
            "allocation": "Equity:0.60:0.07;Bonds:0.40:0.02",
        },
    )
    assert res.status_code == 200
    alloc = res.json()["scenario"]["allocation"]
    # Every class resolves to its OWN canonical /api/classes vol — equity too
    # (the true series stdev, not the rounded dataclass default). Drift-proof.
    assert alloc[0]["volatility"] == equity_vol
    assert alloc[1]["volatility"] == bonds_vol


def test_deterministic_correlation_param_lowers_rebalanced_return():
    """Correlation now feeds the Deterministic return (review 2026-10-11): the
    rebalanced compound return uses the same covariance Monte Carlo samples, so
    a HIGHER equity-bond correlation means less diversification return."""
    res = client.get("/api/trajectory", params={"correlation": 0.5})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert body["trajectory"]["points"][0]["end_portfolio"] < DET_YEAR0_PIN


# ---------- Flow-overlay: deterministic flows on the fan payloads ----------
#
# Historical AND Monte-Carlo payloads carry a `flows` list — one
# {age, spending, guaranteed_income} per age over the horizon — sourced from the
# SAME simulate(scenario) the Deterministic mode runs (single source of truth).
# The front-end overlays a GI-floor line (always) + a spending line (constant/
# lifecycle only) on the fan's secondary axis from this. The flows are
# deterministic (GI streams turn on at fixed ages; constant spend is flat), so they
# equal simulate(default_scenario()) per age regardless of the stochastic fan.


def _expected_flows():
    """The deterministic flows for the default scenario — the single source."""
    return [
        {"age": p.age, "spending": p.spending, "guaranteed_income": p.guaranteed_income}
        for p in simulate(default_scenario()).points
    ]


def test_historical_payload_carries_deterministic_flows():
    """mode=historical returns `flows` equal to simulate(scenario) per age ."""
    res = client.get("/api/trajectory", params={"mode": "historical"})
    assert res.status_code == 200
    flows = res.json()["flows"]
    expected = _expected_flows()
    # One entry per age over the default horizon 50..95, both fields always present.
    assert len(flows) == 95 - 50 + 1
    assert flows[0]["age"] == 50
    assert flows[-1]["age"] == 95
    for f in flows:
        assert set(f) == {"age", "spending", "guaranteed_income"}
    # Equals the deterministic trajectory's flows per age (single source of truth).
    assert flows == expected
    # Early spending == the constant input ($110k) before any depletion.
    assert flows[0]["spending"] == default_scenario().annual_spending == 110_000.0
    # GI floor JUMPS when Person A's pension starts at age 60 (24,000): zero
    # before, the stream from the start age on.
    before = next(f for f in flows if f["age"] == 59)
    at_start = next(f for f in flows if f["age"] == 60)
    assert before["guaranteed_income"] == 0.0
    assert at_start["guaranteed_income"] == pytest.approx(24_000.0)


def test_monte_carlo_payload_carries_deterministic_flows():
    """mode=monte_carlo returns the SAME deterministic `flows` as Historical .

    The flows are deterministic functions of the scenario, so they're identical to
    the Historical flows and to simulate(scenario) — the fan's randomness doesn't
    touch them. One MC call keeps this fast.
    """
    res = client.get("/api/trajectory", params={"mode": "monte_carlo"})
    assert res.status_code == 200
    flows = res.json()["flows"]
    expected = _expected_flows()
    assert len(flows) == 95 - 50 + 1
    for f in flows:
        assert set(f) == {"age", "spending", "guaranteed_income"}
    # Same single source as Historical: equals simulate(scenario) per age.
    assert flows == expected
    # GI floor jump at the first pension start age (60) is present in the MC payload too.
    at_start = next(f for f in flows if f["age"] == 60)
    assert at_start["guaranteed_income"] == pytest.approx(24_000.0)


# ---------- Lifecycle spending (spending_mode param) ----------
#
# The `spending_mode` query param is orthogonal to `mode`: default "constant"
# reproduces today's behavior byte-for-byte; "lifecycle" activates the
# age-anchored phase curve. These pin the API path: default is unchanged, an
# unknown mode 422s, and lifecycle (less late-life spending) ends HIGHER than
# constant for the same input — the sanity sign-check from the brief.


def test_spending_mode_defaults_to_constant_unchanged():
    """No spending_mode param → constant, byte-for-byte unchanged canary."""
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["spending_mode"] == "constant"
    # The locked deterministic canary (default scenario) must not move.
    assert body["trajectory"]["points"][0]["end_portfolio"] == DET_YEAR0_CANARY
    assert round(body["trajectory"]["final_portfolio"] / 1_000_000, 2) == 6.07


def test_lifecycle_mode_ends_higher_than_constant():
    """Lifecycle (deterministic) ends higher than constant for the same input.

    Same go-go peak; lifecycle tapers spending later (×0.85 then ×0.75), so the
    portfolio ends with more — the brief's sign-check.
    """
    constant_res = client.get("/api/trajectory")
    lifecycle_res = client.get("/api/trajectory", params={"spending_mode": "lifecycle"})
    assert constant_res.status_code == 200
    assert lifecycle_res.status_code == 200
    assert lifecycle_res.json()["scenario"]["spending_mode"] == "lifecycle"
    constant_final = constant_res.json()["trajectory"]["final_portfolio"]
    lifecycle_final = lifecycle_res.json()["trajectory"]["final_portfolio"]
    assert lifecycle_final > constant_final


def test_lifecycle_mode_tapers_spending_by_phase():
    """The deterministic spending column shows $110k / $93.5k / $82.5k by phase.

    Pins that the API path actually shapes the per-year spending: go-go (age 74)
    at the $110k default peak, slow-go (age 75) at $93.5k (×0.85),
    no-go (age 85) at $82.5k (×0.75).
    """
    res = client.get("/api/trajectory", params={"spending_mode": "lifecycle"})
    assert res.status_code == 200
    by_age = {p["age"]: p for p in res.json()["trajectory"]["points"]}
    assert by_age[74]["spending"] == 110_000.0
    assert by_age[75]["spending"] == pytest.approx(93_500.0)
    assert by_age[85]["spending"] == pytest.approx(82_500.0)


def test_lifecycle_mode_composes_with_historical():
    """spending_mode=lifecycle also applies in historical mode (tapered fan)."""
    constant_res = client.get("/api/trajectory", params={"mode": "historical"})
    lifecycle_res = client.get(
        "/api/trajectory", params={"mode": "historical", "spending_mode": "lifecycle"}
    )
    assert constant_res.status_code == 200
    assert lifecycle_res.status_code == 200
    assert lifecycle_res.json()["scenario"]["spending_mode"] == "lifecycle"
    # Less late-life spending → higher ending median across history.
    constant_end = constant_res.json()["bands"][-1]["median"]
    lifecycle_end = lifecycle_res.json()["bands"][-1]["median"]
    assert lifecycle_end > constant_end


def test_guardrails_mode_accepted_and_runs_deterministic():
    """spending_mode=guardrails is accepted and runs the deterministic engine."""
    res = client.get("/api/trajectory", params={"spending_mode": "guardrails"})
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["spending_mode"] == "guardrails"
    # The guardrail knobs default to the research values on the echoed scenario.
    assert body["scenario"]["guard_band"] == 0.20
    assert body["scenario"]["guard_adjust"] == 0.10
    # The default operational floor is $85k; $45k is the absolute/survivable
    # context line, not the clamp.
    assert body["scenario"]["spending_floor"] == 85_000.0
    assert body["scenario"]["spending_ceiling"] == 150_000.0


def test_guardrails_historical_returns_spending_bands():
    """Guardrails in historical mode adds a `spending_bands` fan to the payload.

    Variable-spending design slice 1: spending varies across cohorts, so the
    payload carries a per-age spending percentile fan mirroring `bands`. With a
    start spend above the conservative SAFEMAX, the fan spreads.
    """
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "historical"
    assert "spending_bands" in body
    sb = body["spending_bands"]
    # Same shape + length as the portfolio `bands` (one per age, 50..95).
    assert len(sb) == len(body["bands"]) == 95 - 50 + 1
    assert sb[0]["age"] == 50
    assert sb[-1]["age"] == 95
    for b in sb:
        assert set(b) == {"age", "p10", "p25", "median", "p75", "p90"}
        assert b["p10"] <= b["p25"] <= b["median"] <= b["p75"] <= b["p90"] + 1e-9
        # Every percentile respects the default rails (operational floor $85k).
        assert 85_000.0 - 1e-6 <= b["p10"]
        assert b["p90"] <= 150_000.0 + 1e-6
    # Year-1 spend == start spend for every cohort (no band trip at the start age).
    assert sb[0]["p10"] == pytest.approx(130_000.0)
    assert sb[0]["p90"] == pytest.approx(130_000.0)
    # The fan spreads somewhere in the bridge years (spending genuinely varies).
    assert any(b["p90"] - b["p10"] > 0 for b in sb)


def test_constant_and_lifecycle_historical_omit_spending_bands():
    """Constant/lifecycle historical payloads have NO spending_bands (back-compat).

    Spending is identical across cohorts in those modes, so the spending fan is
    absent and the key is omitted — the front-end only renders it under guardrails.
    """
    for mode in ("constant", "lifecycle"):
        res = client.get(
            "/api/trajectory",
            params={"mode": "historical", "spending_mode": mode},
        )
        assert res.status_code == 200
        assert "spending_bands" not in res.json()


def test_guardrails_historical_returns_adjustment_stats_and_worst_cohort():
    """Guardrails + Historical adds `adjustment_stats` + `worst_cohort` (slice 2).

    The cost-of-flexibility readout (aggregate cut/raise/floor stats) and the
    worst-cohort drill-down (year-by-year annotated path with $ triggers + the
    GI-driven raise labels). Pins the payload shape the front-end renders.
    """
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
        },
    )
    assert res.status_code == 200
    body = res.json()

    stats = body["adjustment_stats"]
    assert set(stats) == {
        "cohort_count", "pct_ever_cut", "pct_ever_raised", "median_first_cut_age",
        "median_years_at_floor", "deepest_cut",
        "lowest_single_year_spend", "pct_ever_forced_to_floor",
    }
    assert stats["cohort_count"] == body["cohort_count"] == 110
    assert 0.0 <= stats["pct_ever_cut"] <= 1.0
    assert 0.0 <= stats["pct_ever_forced_to_floor"] <= 1.0

    wc = body["worst_cohort"]
    assert set(wc) == {"start_year", "total_spend", "min_spend", "depleted_age", "path"}
    assert 1871 <= wc["start_year"] <= 2025
    # Full-horizon annotated path; each year carries the dollars + display label.
    assert len(wc["path"]) == 95 - 50 + 1
    for y in wc["path"]:
        assert set(y) == {
            "age", "action", "gi_driven", "spend", "begin_portfolio", "gi", "label",
        }
        assert y["action"] in ("cut", "raise", "hold")
    # A GI-driven raise (GI landing at 60/65/67/70) is labelled distinctly from a market
    # raise — the resolved design decision the drill-down must not mislead on.
    gi_raises = [y for y in wc["path"] if y["action"] == "raise" and y["gi_driven"]]
    for y in gi_raises:
        assert y["label"] == "raise — guaranteed income started"


def test_constant_and_lifecycle_historical_omit_adjustment_fields():
    """Constant/lifecycle historical payloads omit the slice-2 fields (back-compat)."""
    for mode in ("constant", "lifecycle"):
        res = client.get(
            "/api/trajectory",
            params={"mode": "historical", "spending_mode": mode},
        )
        assert res.status_code == 200
        body = res.json()
        assert "adjustment_stats" not in body
        assert "worst_cohort" not in body


def test_unknown_spending_mode_returns_422():
    """An unrecognised spending_mode is rejected with a 422 and a useful message."""
    res = client.get("/api/trajectory", params={"spending_mode": "smile"})
    assert res.status_code == 422
    assert "spending_mode must be one of" in res.json()["detail"]


def test_per_person_override_delays_person_b_ss():
    """Person B's SS claim age is a per-person override: the default claims at 70
    (delayed), and an override to FRA 67 claims earlier at the unadjusted amount.

    Default: Person B's FRA amount is $26,000, claimed at 70 → factor 1.24 →
    $32,240 from 70. GI at 67-69 is $66,000 (A pension 24,000 + B pension 9,000 +
    A SS 33,000), and at 70 it is $66,000 + $32,240 = $98,240.
    Override claim 67: factor 1.0 → $26,000 from 67, so GI is $92,000 at 67 and
    stays $92,000 at 70 — BELOW the delayed $98,240, because delaying actuarially
    increases the benefit. Confirms a single per-person claim-age param derives
    the effective amount + start age without disturbing the other three streams.
    """
    default = client.get("/api/trajectory")
    assert default.status_code == 200
    body = default.json()
    assert body["scenario"]["person_b_ss_start_age"] == 70
    assert body["scenario"]["person_b_ss_amount"] == pytest.approx(32_240.0)
    by_age = {p["age"]: p for p in body["trajectory"]["points"]}
    assert by_age[67]["guaranteed_income"] == pytest.approx(66_000.0)
    assert by_age[69]["guaranteed_income"] == pytest.approx(66_000.0)
    assert by_age[70]["guaranteed_income"] == pytest.approx(98_240.0)

    res = client.get(
        "/api/trajectory",
        params={"person_b_ss_claim_age": 67},
    )
    assert res.status_code == 200
    body = res.json()
    # The derived effective amount + start age reach the engine (echoed scenario).
    assert body["scenario"]["person_b_ss_start_age"] == 67
    assert body["scenario"]["person_b_ss_amount"] == pytest.approx(26_000.0)
    by_age = {p["age"]: p for p in body["trajectory"]["points"]}
    assert by_age[67]["guaranteed_income"] == pytest.approx(92_000.0)
    assert by_age[70]["guaranteed_income"] == pytest.approx(92_000.0)


# ---------- SS claim-age slider (derived effective amount) ----------
#
# The request specifies SS per person as an FRA amount + claim age; the API
# derives the engine-facing effective amount = fra × ss_factor(claim_age) and the
# start age = claim_age, then builds the (still amount-driven) Scenario.


def test_ss_fra_amount_and_claim_age_derive_effective_amount():
    """FRA amount $33,000 @ claim 64 → factor 0.80 → effective $26,400 @ age 64.

    Pins the slice contract: the engine receives the actuarially-reduced effective
    amount and the claim age as the start age (the echoed scenario reflects both).
    """
    res = client.get(
        "/api/trajectory",
        params={"person_a_ss_fra_amount": 33_000, "person_a_ss_claim_age": 64},
    )
    assert res.status_code == 200
    sc = res.json()["scenario"]
    assert sc["person_a_ss_amount"] == pytest.approx(26_400.0)
    assert sc["person_a_ss_start_age"] == 64


def test_ss_default_claim_age_reproduces_today_effective_values():
    """Omitting SS params (or the FRA amount @ the default claim age) yields the
    default effective SS: Person A $33,000 @ 67 (factor 1.0), Person B
    $26,000 × 1.24 = $32,240 @ 70."""
    expected = default_scenario()
    # Bare request: defaults flow through unchanged.
    bare = client.get("/api/trajectory").json()["scenario"]
    assert bare["person_a_ss_amount"] == pytest.approx(expected.person_a_ss_amount)
    assert bare["person_a_ss_start_age"] == expected.person_a_ss_start_age
    assert bare["person_b_ss_amount"] == pytest.approx(expected.person_b_ss_amount)
    assert bare["person_b_ss_start_age"] == expected.person_b_ss_start_age
    # Explicit FRA amount @ FRA claim 67 → identical effective amount (factor 1.0).
    explicit = client.get(
        "/api/trajectory",
        params={"person_a_ss_fra_amount": 33_000, "person_a_ss_claim_age": 67},
    ).json()["scenario"]
    assert explicit["person_a_ss_amount"] == pytest.approx(33_000.0)
    assert explicit["person_a_ss_start_age"] == 67
    # Person B: FRA $26,000 @ claim 70 → the default effective $32,240.
    explicit_b = client.get(
        "/api/trajectory",
        params={"person_b_ss_fra_amount": 26_000, "person_b_ss_claim_age": 70},
    ).json()["scenario"]
    assert explicit_b["person_b_ss_amount"] == pytest.approx(expected.person_b_ss_amount)
    assert explicit_b["person_b_ss_start_age"] == 70


def test_ss_claim_age_only_uses_default_fra_amount_as_pia():
    """Supplying ONLY a claim age (no FRA amount) falls back to the default
    scenario's PIA, then applies ss_factor.

    The default PIA is recovered from the default effective amount at the default
    claim age, so a default claim age other than FRA is not discounted twice.
    Person A (default claim 67, factor 1.0, PIA $33,000):
      - claim 70 → 33,000 × 1.24 = 40,920.00 (delayed credit)
      - claim 62 → 33,000 × 0.70 = 23,100.00 (early reduction)
    Person B (default claim 70, effective $32,240 = PIA $26,000 × 1.24):
      - claim 67 → 26,000.00 (factor 1.0), not 32,240 re-used as the PIA
    """
    default_fra = default_scenario().person_a_ss_amount  # 33,000 @ FRA 67 (factor 1.0)
    # Delayed: claim 70 → factor 1.24.
    delayed = client.get(
        "/api/trajectory",
        params={"person_a_ss_claim_age": 70},
    )
    assert delayed.status_code == 200
    sc = delayed.json()["scenario"]
    assert sc["person_a_ss_amount"] == pytest.approx(default_fra * 1.24)
    assert sc["person_a_ss_amount"] == pytest.approx(40_920.0)
    assert sc["person_a_ss_start_age"] == 70
    # Early: claim 62 → factor 0.70.
    early = client.get(
        "/api/trajectory",
        params={"person_a_ss_claim_age": 62},
    )
    assert early.status_code == 200
    sc = early.json()["scenario"]
    assert sc["person_a_ss_amount"] == pytest.approx(default_fra * 0.70)
    assert sc["person_a_ss_amount"] == pytest.approx(23_100.0)
    assert sc["person_a_ss_start_age"] == 62
    # Person B: claim-age-only at FRA 67 recovers the $26,000 PIA.
    fra_b = client.get("/api/trajectory", params={"person_b_ss_claim_age": 67})
    assert fra_b.status_code == 200
    sc = fra_b.json()["scenario"]
    assert sc["person_b_ss_amount"] == pytest.approx(26_000.0)
    assert sc["person_b_ss_start_age"] == 67


def test_ss_claim_age_out_of_range_returns_422():
    """A claim age outside [62, 70] is a 422 with a clear message (the ss_factor
    ValueError, translated like the other engine-error → 422 cases)."""
    res = client.get(
        "/api/trajectory",
        params={"person_a_ss_fra_amount": 33_000, "person_a_ss_claim_age": 71},
    )
    assert res.status_code == 422
    assert "claim_age" in res.json()["detail"]


def test_ss_claim_age_out_of_range_returns_422_post():
    """The POST path translates the same out-of-range claim age to a 422."""
    res = client.post(
        "/api/trajectory",
        json={"person_b_ss_fra_amount": 26_000, "person_b_ss_claim_age": 61},
    )
    assert res.status_code == 422
    assert "claim_age" in res.json()["detail"]


def test_ss_claim_age_derives_effective_amount_post():
    """POST FRA amount + claim age derives the same effective amount as GET."""
    res = client.post(
        "/api/trajectory",
        json={"person_b_ss_fra_amount": 30_000, "person_b_ss_claim_age": 70},
    )
    assert res.status_code == 200
    sc = res.json()["scenario"]
    assert sc["person_b_ss_amount"] == pytest.approx(30_000.0 * 1.24)
    assert sc["person_b_ss_start_age"] == 70


# ---------- Spending solver (/api/solve) ----------
#
# /api/solve mirrors /api/trajectory's scenario params + mode, plus a confidence
# dial (historical-only). It binary-searches annual_spending for the max level
# clearing the confidence threshold and writes it back into the echoed scenario.
# These pin the response contract, the write-back, and the 422 discipline.


def test_solve_historical_happy_path():
    """GET /api/solve (historical) returns the locked response contract."""
    res = client.get("/api/solve", params={"mode": "historical", "confidence": 0.90})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "historical"
    assert body["spending_mode"] == "constant"
    assert body["confidence_target"] == 0.90
    assert body["hit_ceiling"] is False
    assert body["cohort_count"] > 100
    # The solved spend is a $1k-rounded number that clears the requested bar.
    assert body["solved_spend"] % 1_000 == 0
    assert body["achieved_fraction"] >= 0.90
    # Write-back: the echoed scenario's annual_spending == the solved number.
    assert body["scenario"]["annual_spending"] == body["solved_spend"]
    # Full response key set is exact (front-end depends on it).
    assert set(body) == {
        "mode",
        "spending_mode",
        "confidence_target",
        "solved_spend",
        "achieved_fraction",
        "hit_ceiling",
        "cohort_count",
        "scenario",
    }


def test_solve_deterministic_happy_path():
    """GET /api/solve (deterministic) → no dial: confidence_target/cohort_count null."""
    res = client.get("/api/solve", params={"mode": "deterministic"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert body["confidence_target"] is None
    assert body["cohort_count"] is None
    assert body["solved_spend"] % 1_000 == 0
    # The single path survives at the displayed number.
    assert body["achieved_fraction"] == 1.0
    assert body["scenario"]["annual_spending"] == body["solved_spend"]


def test_solve_defaults_to_deterministic_with_no_mode():
    """No mode param → deterministic solve (matches /api/trajectory's default)."""
    res = client.get("/api/solve")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert body["confidence_target"] is None


def test_solve_lifecycle_solves_higher_peak_than_constant():
    """Lifecycle solves a higher go-go peak than constant (shape flows through)."""
    constant = client.get("/api/solve", params={"mode": "historical", "confidence": 0.90})
    lifecycle = client.get(
        "/api/solve",
        params={"mode": "historical", "confidence": 0.90, "spending_mode": "lifecycle"},
    )
    assert constant.status_code == 200
    assert lifecycle.status_code == 200
    assert lifecycle.json()["spending_mode"] == "lifecycle"
    assert lifecycle.json()["solved_spend"] > constant.json()["solved_spend"]


def test_solve_unknown_mode_returns_422():
    """An unrecognised mode is rejected with a 422."""
    res = client.get("/api/solve", params={"mode": "bootstrap"})
    assert res.status_code == 422
    assert "mode must be one of" in res.json()["detail"]


def test_solve_bad_confidence_returns_422():
    """Confidence outside (0, 1] is rejected with a 422."""
    too_high = client.get("/api/solve", params={"mode": "historical", "confidence": 1.5})
    too_low = client.get("/api/solve", params={"mode": "historical", "confidence": 0})
    assert too_high.status_code == 422
    assert "confidence must be in (0, 1]" in too_high.json()["detail"]
    assert too_low.status_code == 422


def test_solve_bad_allocation_returns_422():
    """A malformed/non-summing allocation is rejected with a 422 (shared parser)."""
    res = client.get(
        "/api/solve",
        params={"allocation": "Equity:0.60:0.07;Bonds:0.32:0.02"},
    )
    assert res.status_code == 422
    assert "sum to 100%" in res.json()["detail"]


def test_solve_no_mapped_class_historical_returns_422():
    """A historical solve whose allocation maps no series 422s (engine ValueError).

    The engine raises ValueError; the endpoint must route it to a 422 rather than
    letting it surface as a 500.
    """
    res = client.get(
        "/api/solve",
        params={"mode": "historical", "allocation": "Crypto:1.0:0.0"},
    )
    assert res.status_code == 422


# Guardrails spending is VIEW-ONLY: its spend is portfolio-dependent, which
# breaks the max-spend solver's monotonicity assumption (it would return a
# confidently-wrong number). The engine guard raises ValueError; the API routes
# it to a 422 with a plain-language, view-only detail. Fenced on GET + POST and
# (mode-agnostic) across every engine mode.
_GUARDRAILS_SOLVE_DETAIL = (
    "Max-spend solve is not available in dynamic-spending (guardrails / "
    "funded-ratio) modes — they're view-only; set your start spend and "
    "read the spending fan."
)


@pytest.mark.parametrize("mode", ["deterministic", "historical", "monte_carlo"])
def test_solve_guardrails_returns_422(mode):
    """GET /api/solve with spending_mode=guardrails is a 422 with the view-only detail."""
    res = client.get(
        "/api/solve",
        params={"mode": mode, "spending_mode": "guardrails", "confidence": 0.80},
    )
    assert res.status_code == 422
    assert res.json()["detail"] == _GUARDRAILS_SOLVE_DETAIL


def test_post_solve_guardrails_returns_422():
    """POST /api/solve with spending_mode=guardrails is a 422 with the view-only detail."""
    res = client.post(
        "/api/solve",
        json={
            "mode": "historical",
            "spending_mode": "guardrails",
            "confidence": 0.80,
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"] == _GUARDRAILS_SOLVE_DETAIL


@pytest.mark.parametrize("mode", ["deterministic", "historical", "monte_carlo"])
def test_solve_fr_guardrail_returns_422(mode):
    """GET /api/solve with spending_mode=fr_guardrail is a 422 with the view-only
    detail (the solver fence fires via the typed-not-solvable predicate, same as
    guardrails / fr_smooth — no 500)."""
    res = client.get(
        "/api/solve",
        params={"mode": mode, "spending_mode": "fr_guardrail", "confidence": 0.80},
    )
    assert res.status_code == 422
    assert res.json()["detail"] == _GUARDRAILS_SOLVE_DETAIL


def test_fr_guardrail_mode_accepted_and_runs_deterministic():
    """spending_mode=fr_guardrail is accepted and runs the deterministic engine."""
    res = client.get("/api/trajectory", params={"spending_mode": "fr_guardrail"})
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["spending_mode"] == "fr_guardrail"


# ---------- Amortization Frontier (spending_mode=amortization) ----------

def test_amortization_mode_accepted_and_runs_deterministic():
    """spending_mode=amortization is accepted and runs the deterministic engine, and
    the resolved scenario echoes the (default) amortization_real_return knob."""
    res = client.get("/api/trajectory", params={"spending_mode": "amortization"})
    assert res.status_code == 200
    body = res.json()
    assert body["scenario"]["spending_mode"] == "amortization"
    assert body["scenario"]["amortization_real_return"] == pytest.approx(0.01)


def test_amortization_real_return_knob_round_trips_get():
    """The amortization_real_return query param overrides the default and echoes in
    the resolved scenario (GET path)."""
    res = client.get(
        "/api/trajectory",
        params={"spending_mode": "amortization", "amortization_real_return": 0.03},
    )
    assert res.status_code == 200
    assert res.json()["scenario"]["amortization_real_return"] == pytest.approx(0.03)


def test_amortization_real_return_knob_round_trips_post():
    """The amortization_real_return field overrides the default on the POST body."""
    res = client.post(
        "/api/trajectory",
        json={
            "spending_mode": "amortization",
            "amortization_real_return": 0.025,
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 200
    assert res.json()["scenario"]["amortization_real_return"] == pytest.approx(0.025)


def test_amortization_historical_returns_spending_fan():
    """Amortization is collects_spends=True, so Historical returns a spending fan
    (like the FR family). The fan is UNCLAMPED — percentiles may cross the $85k/$150k
    rails (the rails are reference lines only)."""
    res = client.get(
        "/api/trajectory",
        params={"mode": "historical", "spending_mode": "amortization"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "spending_bands" in body
    assert len(body["spending_bands"]) == 95 - 50 + 1


def test_amortization_bad_real_return_returns_422():
    """An out-of-range amortization_real_return (> 0.5) is a clean 422, not a 500."""
    res = client.get(
        "/api/trajectory",
        params={"spending_mode": "amortization", "amortization_real_return": 5.0},
    )
    assert res.status_code == 422
    assert "amortization_real_return" in res.json()["detail"]


@pytest.mark.parametrize("mode", ["deterministic", "historical", "monte_carlo"])
def test_solve_amortization_returns_422(mode):
    """GET /api/solve with spending_mode=amortization is a 422 (the frontier IS the
    spend — no external target for the solver to bisect). The API maps every
    SpendingStrategyNotSolvable to ONE canonical dynamic-spending detail
    (api/error_translation.py), so the frontier reuses it rather than surfacing its
    own raw reason_not_solvable string."""
    res = client.get(
        "/api/solve",
        params={"mode": mode, "spending_mode": "amortization", "confidence": 0.80},
    )
    assert res.status_code == 422
    assert res.json()["detail"] == _GUARDRAILS_SOLVE_DETAIL


def test_amortization_buckets_not_composable_returns_422():
    """amortization is composable_with_buckets=False → buckets + amortization 422s
    (same fence as the other dynamic modes)."""
    res = client.get(
        "/api/trajectory",
        params={"sourcing_mode": "buckets", "spending_mode": "amortization"},
    )
    assert res.status_code == 422


# ---------- Spending solver under Monte Carlo (/api/solve?mode=monte_carlo) ----------
#
# Build 2 wires the solver into Monte Carlo: the confidence dial is active (like
# historical), volatility rides in the allocation string + a correlation scalar
# is its own param, and 422 on vol < 0 / |correlation| > 0.99. The response is
# the same contract as historical (cohort_count carries the trial count).


def test_solve_monte_carlo_happy_path():
    """GET /api/solve (monte_carlo) returns 200 with the dial-driven contract."""
    res = client.get("/api/solve", params={"mode": "monte_carlo", "confidence": 0.80})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "monte_carlo"
    assert body["spending_mode"] == "constant"
    # The dial is active in MC (unlike deterministic), so confidence_target echoes.
    assert body["confidence_target"] == 0.80
    assert body["hit_ceiling"] is False
    # cohort_count carries the 10k trial count (the MC analogue).
    assert body["cohort_count"] == 10_000
    assert body["solved_spend"] % 1_000 == 0
    assert body["achieved_fraction"] >= 0.80
    # Write-back: the echoed scenario's annual_spending == the solved number.
    assert body["scenario"]["annual_spending"] == body["solved_spend"]
    # Same exact key set as the other solve modes (front-end depends on it).
    assert set(body) == {
        "mode",
        "spending_mode",
        "confidence_target",
        "solved_spend",
        "achieved_fraction",
        "hit_ceiling",
        "cohort_count",
        "scenario",
    }


def test_solve_monte_carlo_is_reproducible():
    """The same MC solve twice writes back the exact same number (fixed seed)."""
    a = client.get("/api/solve", params={"mode": "monte_carlo", "confidence": 0.80})
    b = client.get("/api/solve", params={"mode": "monte_carlo", "confidence": 0.80})
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["solved_spend"] == b.json()["solved_spend"]
    assert a.json()["achieved_fraction"] == b.json()["achieved_fraction"]


def test_solve_monte_carlo_negative_volatility_returns_422():
    """A negative per-class vol (in the allocation string) is rejected 422 on solve."""
    res = client.get(
        "/api/solve",
        params={
            "mode": "monte_carlo",
            "allocation": "Equity:0.60:0.07:-0.10;Bonds:0.40:0.02:0.088",
        },
    )
    assert res.status_code == 422
    assert "volatility" in res.json()["detail"]


def test_solve_monte_carlo_correlation_out_of_range_returns_422():
    """|correlation| > 0.99 is rejected 422 on the solve path too (cholesky guard)."""
    res = client.get(
        "/api/solve", params={"mode": "monte_carlo", "correlation": 1.0}
    )
    assert res.status_code == 422
    assert "correlation must be between" in res.json()["detail"]


def test_solve_monte_carlo_correlation_at_limit_is_accepted():
    """correlation = 0.99 (the inclusive limit) solves (200), not a 422."""
    res = client.get(
        "/api/solve",
        params={"mode": "monte_carlo", "confidence": 0.80, "correlation": 0.99},
    )
    assert res.status_code == 200
    assert res.json()["scenario"]["correlation"] == 0.99


def test_solve_monte_carlo_bad_confidence_returns_422():
    """Confidence outside (0, 1] is rejected 422 in MC mode (dial is active)."""
    res = client.get("/api/solve", params={"mode": "monte_carlo", "confidence": 1.5})
    assert res.status_code == 422
    assert "confidence must be in (0, 1]" in res.json()["detail"]


# ---------- Slice 0: POST JSON transport (the multi-class path) ----------
#
# POST /api/trajectory and /api/solve carry `allocation` as [{id, weight,
# real_return, volatility}] + optional `correlations`, and echo the SAME
# {id, label, ...} allocation shape. GET stays restricted to the two legacy
# classes via the name→id alias; multi-class/preset requests use POST.


def test_post_trajectory_round_trips_id_allocation():
    """POST allocation round-trips: the id-native body echoes the same shape.

    Sends the 2-class default by explicit class ids and asserts the echoed
    allocation carries id/label/weight/real_return/volatility — the POST contract.
    """
    res = client.post(
        "/api/trajectory",
        json={
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02, "volatility": 0.088},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert body["scenario"]["allocation"] == [
        {"id": "us_total_market", "label": "US Total Stock Market", "weight": 0.60, "real_return": 0.07, "volatility": 0.174},
        {"id": "us_agg_bonds", "label": "US Bonds (10-yr Treasury proxy)", "weight": 0.40, "real_return": 0.02, "volatility": 0.088},
    ]
    # Same 60/40 knobs → the canary holds on the POST path to the rounding of the
    # supplied vols (0.174/0.088 vs the exact series stdevs the default carries —
    # vol now enters the Deterministic rebalanced return).
    assert body["trajectory"]["points"][0]["end_portfolio"] == pytest.approx(
        DET_YEAR0_PIN, rel=1e-4
    )
    assert round(body["trajectory"]["final_portfolio"] / 1_000_000, 2) == 6.07


def test_post_trajectory_omitted_volatility_resolves_to_canonical_default():
    """A POST allocation entry that OMITS ``volatility`` resolves to that class's
    own canonical default, not the equity dataclass default.

    Same fix as the GET path, exercised through the id-native POST contract —
    ``_parse_allocation_entries`` is not menu-restricted, so this also proves
    the resolver's fallback path is only hit for unrecognized ids.
    """
    classes = client.get("/api/classes").json()["classes"]
    bonds_vol = next(c["volatility"] for c in classes if c["id"] == "us_agg_bonds")
    equity_vol = next(c["volatility"] for c in classes if c["id"] == "us_total_market")
    assert bonds_vol != 0.174
    assert bonds_vol < 0.174
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 200
    alloc = res.json()["scenario"]["allocation"]
    assert alloc[0]["volatility"] == equity_vol
    assert alloc[1]["volatility"] == bonds_vol


def test_post_trajectory_with_no_body_uses_defaults():
    """POST with an empty body resolves the default scenario (same as bare GET)."""
    res = client.post("/api/trajectory", json={})
    assert res.status_code == 200
    body = res.json()
    expected = default_scenario()
    assert body["scenario"]["initial_portfolio"] == expected.initial_portfolio
    assert body["trajectory"]["points"][0]["end_portfolio"] == DET_YEAR0_CANARY


def test_post_trajectory_custom_label_is_honored():
    """An explicit label in a POST entry overrides the canonical default label."""
    res = client.post(
        "/api/trajectory",
        json={
            "allocation": [
                {"id": "us_total_market", "label": "Stocks!", "weight": 1.0, "real_return": 0.07},
            ],
        },
    )
    assert res.status_code == 200
    assert res.json()["scenario"]["allocation"][0]["label"] == "Stocks!"


def test_post_trajectory_bad_weights_returns_422():
    """A non-summing POST allocation is rejected 422 (same rule as GET)."""
    res = client.post(
        "/api/trajectory",
        json={
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.32, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 422
    assert "100%" in res.json()["detail"]


def test_post_trajectory_no_mapped_class_returns_422():
    """A POST allocation mapping no known series 422s (engine ValueError routed).

    POST can express an unmapped id (GET can't), so this is where the engine's
    no-mapped-class → 422 routing is exercised on the transport boundary.
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "allocation": [{"id": "crypto", "weight": 1.0, "real_return": 0.07, "volatility": 0.174}],
        },
    )
    assert res.status_code == 422


def test_post_trajectory_carries_correlations_matrix():
    """An explicit `correlations` matrix flows through the POST body onto the run.

    A 2-class MC run with an explicit 2x2 matrix runs (200); the matrix is the
    forward-looking multi-class representation (slice 0 wires the transport).
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02, "volatility": 0.088},
            ],
            "correlations": [[1.0, 0.2], [0.2, 1.0]],
        },
    )
    assert res.status_code == 200
    assert res.json()["mode"] == "monte_carlo"


@pytest.mark.requires_optional_classes
def test_post_historical_multiclass_surfaces_testable_since():
    """Slice 3: a multi-class Historical POST surfaces `testable_since` + the
    restricted cohort count (the D3 "testable since YYYY" window-policy label).

    50/30/20 equity/bonds/us_reit: the REIT's 1972 start binds the window, so
    `testable_since` == `start_year` == 1972 and `cohort_count` is the restricted
    common-overlap count (9 for the default 46-year horizon: 1972..1980).
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "allocation": [
                {"id": "us_total_market", "weight": 0.50, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.30, "real_return": 0.02, "volatility": 0.088},
                {"id": "us_reit", "weight": 0.20, "real_return": 0.0666, "volatility": 0.18},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "historical"
    assert body["testable_since"] == 1972
    assert body["start_year"] == 1972
    assert body["cohort_count"] == 9
    # The fan still spans the full default horizon (the window restricts COHORTS,
    # not the per-cohort age span).
    assert len(body["bands"]) == 95 - 50 + 1


@pytest.mark.requires_optional_classes
def test_post_historical_no_common_window_returns_422():
    """Slice 3: a multi-class Historical POST whose selected classes share no
    replayable common window 422s (engine ValueError routed, not a 500).

    A short-history class (us_reit, 1972-2025 = 54 years) over a horizon longer
    than its data — ages 18..110 = 93 year-steps — has no cohort start year with
    a full common window, so the engine raises ValueError. The historical
    trajectory branch must route that to a 422 (it returned a 500 before the fix).
    test_historical.test_no_common_window_raises pins the engine side; this pins
    the API transport boundary so the 500 can't silently regress.
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "start_age": 18,
            "end_age": 110,
            "allocation": [
                {"id": "us_total_market", "weight": 0.50, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.30, "real_return": 0.02, "volatility": 0.088},
                {"id": "us_reit", "weight": 0.20, "real_return": 0.0666, "volatility": 0.18},
            ],
        },
    )
    assert res.status_code == 422


def test_get_historical_still_carries_testable_since_for_default():
    """The default GET Historical payload also carries `testable_since` (== 1871).

    Confirms the new field rides the GET path too and equals the 2-class default's
    1871 (the legacy Shiller path, byte-stable)."""
    res = client.get("/api/trajectory", params={"mode": "historical"})
    assert res.status_code == 200
    body = res.json()
    assert body["testable_since"] == 1871
    assert body["start_year"] == 1871


def test_post_multi_class_mc_no_matrix_uses_estimated_default():
    """A 4-class MC POST with NO matrix runs (200) via the estimated default (slice 2).

    The N>2 no-explicit-matrix path: the engine estimates the per-pair/max-overlap
    default matrix for the four mapped series and returns the fan payload (no 422,
    no scalar broadcast).
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "allocation": [
                {"id": "us_total_market", "weight": 0.40, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.30, "real_return": 0.02, "volatility": 0.088},
                {"id": "us_small_value", "weight": 0.15, "real_return": 0.065, "volatility": 0.27},
                {"id": "us_reit", "weight": 0.15, "real_return": 0.05, "volatility": 0.18},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "monte_carlo"
    assert body["trial_count"] > 0


@pytest.mark.requires_optional_classes
def test_post_multi_class_mc_malformed_matrix_returns_422():
    """A malformed explicit N>2 matrix POST is rejected 422 (slice-2 carry-forward).

    A 4-class MC run with a 3x3 (wrong-dimension) `correlations` matrix: the engine
    raises ValueError on the shape mismatch and the API routes it to a 422 (today a
    bad matrix on a 2-class POST is silently ignored — for N>2 it must surface).
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "allocation": [
                {"id": "us_total_market", "weight": 0.40, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.30, "real_return": 0.02, "volatility": 0.088},
                {"id": "us_small_value", "weight": 0.15, "real_return": 0.065, "volatility": 0.27},
                {"id": "us_reit", "weight": 0.15, "real_return": 0.05, "volatility": 0.18},
            ],
            "correlations": [[1.0, 0.2, 0.1], [0.2, 1.0, 0.3], [0.1, 0.3, 1.0]],
        },
    )
    assert res.status_code == 422
    assert "shape" in res.json()["detail"]


def test_post_solve_round_trips():
    """POST /api/solve resolves and solves from the id-native body (deterministic)."""
    res = client.post(
        "/api/solve",
        json={
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert "solved_spend" in body
    assert body["scenario"]["allocation"][0]["id"] == "us_total_market"


def test_post_trajectory_bad_mode_returns_422():
    """An unknown mode in the POST body is rejected 422 (shared mode validation)."""
    res = client.post("/api/trajectory", json={"mode": "bogus"})
    assert res.status_code == 422
    assert "mode must be one of" in res.json()["detail"]


# ---------- Slice 4b: class menu + presets + friendly 422 messages ----------
#
# GET /api/classes feeds the 12-class allocation matrix; GET /api/presets feeds
# the preset picker; the broad-MC non-PSD case and the no-replayable-window
# Historical case return a user-friendly 422 detail.

# The 12 canonical class ids, in design "The 12 classes" table order.
_EXPECTED_CLASS_IDS = [
    "us_total_market",
    "us_small_value",
    "intl_developed",
    "emerging_markets",
    "us_reit",
    "us_agg_bonds",
    "lt_treasury",
    "it_treasury",
    "st_treasury",
    "tips",
    "gold",
    "commodities",
]


@pytest.mark.requires_optional_classes
def test_classes_returns_full_12_class_menu():
    """GET /api/classes returns all 12 classes in design order with the menu fields."""
    res = client.get("/api/classes")
    assert res.status_code == 200
    classes = res.json()["classes"]
    assert [c["id"] for c in classes] == _EXPECTED_CLASS_IDS
    for c in classes:
        assert set(c) == {
            "id",
            "label",
            "real_return",
            "volatility",
            "data_start_year",
            "is_equity",
        }
        assert isinstance(c["label"], str) and c["label"]
        assert isinstance(c["is_equity"], bool)
        # Real returns are data-derived geometric means; a couple of classes
        # (broad commodities) are slightly negative, so the floor is the API's
        # _RETURN_MIN (-0.05), not 0.0.
        assert -0.05 <= c["real_return"] <= 0.15
        assert c["volatility"] >= 0.0
    by_id = {c["id"]: c for c in classes}
    # Data start years are the "testable since" hints — pin a few load-bearing ones.
    assert by_id["us_total_market"]["data_start_year"] == 1871
    assert by_id["us_reit"]["data_start_year"] == 1972
    assert by_id["tips"]["data_start_year"] == 2001
    # The legacy classes carry their canonical labels (derived menu, not CLASS_MENU).
    assert by_id["us_total_market"]["label"] == "US Total Stock Market"
    assert by_id["us_agg_bonds"]["label"] == "US Bonds (10-yr Treasury proxy)"


@pytest.mark.requires_optional_classes
def test_classes_is_equity_is_single_source_of_truth():
    """`is_equity` on /api/classes is the SINGLE published copy of the engine's
    equity classification.

    This is the sole consistency check (replacing the deleted JS drift test): every
    class carries the flag, and the set of ``is_equity is True`` ids equals
    ``firebridgelab.buckets.EQUITY_CLASS_IDS`` EXACTLY — so the browser can drop its
    hand-kept replica and read the classification from the API instead.
    """
    from firebridgelab.buckets import EQUITY_CLASS_IDS

    classes = client.get("/api/classes").json()["classes"]
    # Every class carries the flag (additive, never omitted).
    assert all("is_equity" in c for c in classes)
    flagged_equity = {c["id"] for c in classes if c["is_equity"] is True}
    # The published equity set is byte-equal to the engine source of record.
    assert flagged_equity == set(EQUITY_CLASS_IDS)
    # Sanity: the menu actually covers every engine-equity id (no equity id silently
    # dropped from the menu), so the equality above isn't vacuously true.
    menu_ids = {c["id"] for c in classes}
    assert set(EQUITY_CLASS_IDS) <= menu_ids


def test_default_scenario_volatility_matches_published_classes():
    """default_scenario()'s legacy-class volatility EQUALS what /api/classes
    publishes for that class (regression).

    Before the fix, `_DEFAULT_EQUITY_VOLATILITY` / `_DEFAULT_BOND_VOLATILITY`
    (firebridgelab/scenario.py) were hand-ROUNDED copies of the same series stdev that
    `_class_menu_entries()` computed independently for `/api/classes` — so a
    vol-less parsed allocation (which resolves to the exact published figure)
    silently diverged from the default scenario's own Monte Carlo run by
    ~2e-4 per class. Both paths — and `default_bucket_config()` — now go
    through the single `firebridgelab.scenario._legacy_default_volatility()` helper,
    so this equality holds by construction, not by coincidence; a future
    re-rounding of the fallback constants can't reintroduce the drift.
    """
    classes = {c["id"]: c for c in client.get("/api/classes").json()["classes"]}

    scenario_by_id = {ac.id: ac for ac in default_scenario().allocation}
    assert (
        scenario_by_id["us_total_market"].volatility
        == classes["us_total_market"]["volatility"]
    )
    assert (
        scenario_by_id["us_agg_bonds"].volatility
        == classes["us_agg_bonds"]["volatility"]
    )

    bucket_cfg = default_bucket_config()
    assert (
        bucket_cfg.growth.allocation[0].volatility
        == classes["us_total_market"]["volatility"]
    )
    assert (
        bucket_cfg.income.allocation[0].volatility
        == classes["us_agg_bonds"]["volatility"]
    )


@pytest.mark.requires_optional_classes
def test_presets_sum_to_100_over_known_classes():
    """GET /api/presets returns the named portfolios; weights sum to 100 over menu ids."""
    res = client.get("/api/presets")
    assert res.status_code == 200
    presets = res.json()["presets"]
    names = [p["name"] for p in presets]
    # The shipped set (design deliverable 2).
    assert "Classic 60/40" in names
    assert "Golden Butterfly" in names
    assert "All-Weather" in names
    valid_ids = set(_EXPECTED_CLASS_IDS)
    for p in presets:
        assert set(p) >= {"name", "weights", "faithful"}
        assert sum(p["weights"].values()) == 100
        assert set(p["weights"]) <= valid_ids  # only known class ids
    by_name = {p["name"]: p for p in presets}
    # Faithful flag: 60/40 + Golden Butterfly faithful; 3-Fund + Coffeehouse approx.
    assert by_name["Classic 60/40"]["faithful"] is True
    assert by_name["Golden Butterfly"]["faithful"] is True
    assert by_name["Bogleheads 3-Fund"]["faithful"] is False
    assert by_name["Coffeehouse"]["faithful"] is False
    # Classic 60/40 reconstructs exactly to the engine default's 60/40.
    assert by_name["Classic 60/40"]["weights"] == {"us_total_market": 60, "us_agg_bonds": 40}


def test_preset_classic_60_40_round_trips_through_post():
    """A preset's weights, sent through POST, reproduce the 60/40 default canary.

    Confirms the preset → matrix → POST pipeline end-to-end: the 60/40 preset's
    integer weights, sent as fractional class weights, resolve to the byte-stable
    default deterministic ending.
    """
    res = client.post(
        "/api/trajectory",
        json={
            "allocation": [
                {"id": "us_total_market", "weight": 0.60, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.40, "real_return": 0.02},
            ],
        },
    )
    assert res.status_code == 200
    assert round(res.json()["trajectory"]["final_portfolio"] / 1_000_000, 2) == 6.07


@pytest.mark.requires_optional_classes
def test_broad_monte_carlo_non_psd_returns_friendly_422():
    """A broad (all-12) Monte Carlo mix 422s with a user-facing non-PSD message.

    The D6 Frobenius gate raises a technical ValueError (Frobenius distance …);
    the API must surface a plain-language detail telling the user to narrow the
    mix or switch modes — not the raw eigenvalue arithmetic.
    """
    classes = client.get("/api/classes").json()["classes"]
    n = len(classes)
    # Equal-ish weights summing to 1.0 over all 12 classes; returns clamped into
    # the API's [0, 0.15) bound; the per-class menu volatility.
    alloc = []
    for c in classes:
        alloc.append(
            {
                "id": c["id"],
                "weight": 1.0 / n,
                "real_return": max(0.0, min(0.149, c["real_return"])),
                "volatility": c["volatility"],
            }
        )
    # Nudge the last weight so the fractions sum to exactly 1.0 within tolerance.
    alloc[-1]["weight"] += 1.0 - sum(a["weight"] for a in alloc)
    res = client.post("/api/trajectory", json={"mode": "monte_carlo", "allocation": alloc})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "overlapping history" in detail
    assert "Frobenius" not in detail  # no raw engine arithmetic leaks
    assert "eigen" not in detail.lower()


@pytest.mark.requires_optional_classes
def test_historical_no_window_returns_friendly_422():
    """A Historical run whose classes share no replayable window 422s with a
    user-facing message (not the raw cohort-span arithmetic)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "start_age": 18,
            "end_age": 110,
            "allocation": [
                {"id": "us_total_market", "weight": 0.50, "real_return": 0.07},
                {"id": "us_agg_bonds", "weight": 0.30, "real_return": 0.02},
                {"id": "us_reit", "weight": 0.20, "real_return": 0.0666},
            ],
        },
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "overlapping history" in detail
    assert "no cohort has a full" not in detail  # raw engine text doesn't leak


@pytest.mark.requires_optional_classes
def test_multi_class_post_round_trip_with_testable_since():
    """A multi-class Historical POST round-trips: id-native body in, fan + the
    testable_since window label out (the full slice-4b transport contract)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "allocation": [
                {"id": "us_total_market", "weight": 0.40, "real_return": 0.07},
                {"id": "us_small_value", "weight": 0.20, "real_return": 0.065},
                {"id": "us_agg_bonds", "weight": 0.20, "real_return": 0.02},
                {"id": "us_reit", "weight": 0.20, "real_return": 0.05},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "historical"
    # us_reit (1972) binds the window, so the run is testable since 1972.
    assert body["testable_since"] == 1972
    assert body["cohort_count"] > 0
    # The echoed allocation is id-native (the four classes the body sent).
    echoed_ids = [a["id"] for a in body["scenario"]["allocation"]]
    assert echoed_ids == ["us_total_market", "us_small_value", "us_agg_bonds", "us_reit"]


@pytest.mark.requires_optional_classes
def test_broad_monte_carlo_non_psd_solve_returns_friendly_422():
    """The friendly non-PSD message also surfaces on the /api/solve path (shared
    _friendly_engine_error routing), not just /api/trajectory."""
    classes = client.get("/api/classes").json()["classes"]
    n = len(classes)
    alloc = [
        {
            "id": c["id"],
            "weight": 1.0 / n,
            "real_return": max(0.0, min(0.149, c["real_return"])),
            "volatility": c["volatility"],
        }
        for c in classes
    ]
    alloc[-1]["weight"] += 1.0 - sum(a["weight"] for a in alloc)
    res = client.post(
        "/api/solve",
        json={"mode": "monte_carlo", "confidence": 0.8, "allocation": alloc},
    )
    assert res.status_code == 422
    assert "overlapping history" in res.json()["detail"]


# ---------- Variable-spending design slice 3: MC guardrail fan + editable knobs ----


def test_guardrails_monte_carlo_returns_spending_bands_and_adjustment_stats():
    """Guardrails in Monte Carlo mode adds a spend fan + adjustment stats (slice 3).

    Mirrors the Historical guardrail payload: spending varies across the synthetic
    trials, so the payload carries a per-age spending percentile fan + the aggregate
    cost-of-flexibility stats. NO worst_cohort — MC trials have no calendar year, so
    the Historical "1969 cohort" drill-down is omitted for MC.
    """
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "monte_carlo"
    assert "spending_bands" in body
    assert "adjustment_stats" in body
    # No worst-cohort drill-down for MC (synthetic trials, no calendar year).
    assert "worst_cohort" not in body

    sb = body["spending_bands"]
    assert len(sb) == len(body["bands"]) == 95 - 50 + 1
    assert sb[0]["age"] == 50 and sb[-1]["age"] == 95
    for b in sb:
        assert set(b) == {"age", "p10", "p25", "median", "p75", "p90"}
        assert b["p10"] <= b["p25"] <= b["median"] <= b["p75"] <= b["p90"] + 1e-9
        # Operational floor is $85k (the default).
        assert 85_000.0 - 1e-6 <= b["p10"]
        assert b["p90"] <= 150_000.0 + 1e-6
    # Year-1 spend == start spend for every trial (no band trip at the start age).
    assert sb[0]["p10"] == pytest.approx(130_000.0)
    assert sb[0]["p90"] == pytest.approx(130_000.0)
    assert any(b["p90"] - b["p10"] > 0 for b in sb)

    stats = body["adjustment_stats"]
    assert set(stats) == {
        "cohort_count", "pct_ever_cut", "pct_ever_raised", "median_first_cut_age",
        "median_years_at_floor", "deepest_cut",
        "lowest_single_year_spend", "pct_ever_forced_to_floor",
    }
    assert stats["cohort_count"] == body["trial_count"] == 10_000


def test_constant_and_lifecycle_monte_carlo_omit_guardrail_fields():
    """Constant/lifecycle MC payloads omit the spend fan + adjustment stats.

    Spending is identical across trials in those modes, so the slice-3 MC fields
    are absent — the default MC run's payload is unchanged (back-compatible).
    """
    for mode in ("constant", "lifecycle"):
        res = client.get(
            "/api/trajectory",
            params={"mode": "monte_carlo", "spending_mode": mode},
        )
        assert res.status_code == 200
        body = res.json()
        assert "spending_bands" not in body
        assert "adjustment_stats" not in body


def test_guardrail_knobs_accepted_and_echoed_on_get():
    """All four guardrail knobs are editable on GET and echoed on the scenario."""
    res = client.get(
        "/api/trajectory",
        params={
            "spending_mode": "guardrails",
            "guard_band": 0.25,
            "guard_adjust": 0.15,
            "spending_floor": 70_000,
            "spending_ceiling": 190_000,
        },
    )
    assert res.status_code == 200
    sc = res.json()["scenario"]
    assert sc["guard_band"] == 0.25
    assert sc["guard_adjust"] == 0.15
    assert sc["spending_floor"] == 70_000.0
    assert sc["spending_ceiling"] == 190_000.0


def test_guardrail_knobs_accepted_and_echoed_on_post():
    """All four guardrail knobs are editable on the POST body and echoed back."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "monte_carlo",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
            "guard_band": 0.30,
            "guard_adjust": 0.20,
            "spending_floor": 80_000,
            "spending_ceiling": 160_000,
        },
    )
    assert res.status_code == 200
    sc = res.json()["scenario"]
    assert sc["guard_band"] == 0.30
    assert sc["guard_adjust"] == 0.20
    assert sc["spending_floor"] == 80_000.0
    assert sc["spending_ceiling"] == 160_000.0


@pytest.mark.parametrize(
    "params",
    [
        {"guard_band": 1.0},        # band must be < 1
        {"guard_band": -0.1},       # band must be >= 0
        {"guard_adjust": 1.5},      # nudge must be <= 1
        {"guard_adjust": -0.01},    # nudge must be >= 0
        {"spending_floor": -1.0},   # floor must be >= 0
        {"spending_ceiling": 50_000},                    # ceiling < default floor 85k
        {"spending_floor": 100_000, "spending_ceiling": 80_000},  # ceiling <= floor
    ],
)
def test_guardrail_knob_bad_values_return_422_on_get(params):
    """Out-of-range / inconsistent guardrail knobs are rejected 422 on GET."""
    res = client.get(
        "/api/trajectory",
        params={"spending_mode": "guardrails", **params},
    )
    assert res.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"guard_band": 1.0},
        {"guard_adjust": 2.0},
        {"spending_floor": -10.0},
        {"spending_floor": 100_000, "spending_ceiling": 80_000},
    ],
)
def test_guardrail_knob_bad_values_return_422_on_post(body):
    """Out-of-range / inconsistent guardrail knobs are rejected 422 on POST."""
    res = client.post(
        "/api/trajectory",
        json={"spending_mode": "guardrails", **body},
    )
    assert res.status_code == 422


def test_guardrail_knob_boundary_values_accepted():
    """The inclusive boundaries (band=0, nudge=0, nudge=1, floor=0) are accepted."""
    res = client.get(
        "/api/trajectory",
        params={
            "spending_mode": "guardrails",
            "guard_band": 0.0,
            "guard_adjust": 1.0,
            "spending_floor": 0.0,
            "spending_ceiling": 160_000,
        },
    )
    assert res.status_code == 200


def test_lower_ceiling_lowers_mc_spending_fan_upper_percentiles():
    """A lower satiation ceiling caps the MC spend fan's upper percentiles lower.

    Deliverable B + C: editing the ceiling must visibly re-shape the fan. Dropping
    the ceiling from $170k to $140k lowers where the upper fan tops out.
    """
    hi = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo", "spending_mode": "guardrails",
            "annual_spending": 130_000, "spending_ceiling": 170_000,
        },
    ).json()
    lo = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo", "spending_mode": "guardrails",
            "annual_spending": 130_000, "spending_ceiling": 140_000,
        },
    ).json()
    hi_max = max(b["p90"] for b in hi["spending_bands"])
    lo_max = max(b["p90"] for b in lo["spending_bands"])
    assert lo_max <= 140_000.0 + 1e-6
    assert lo_max < hi_max


def test_guardrail_knobs_dont_perturb_default_constant_run():
    """Supplying no knobs leaves the default constant run byte-identical (canary).

    The knobs are defaulted Scenario fields; a request that doesn't touch them must
    echo the research defaults and reproduce the default deterministic final value.
    """
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    sc = body["scenario"]
    assert sc["guard_band"] == 0.20
    assert sc["guard_adjust"] == 0.10
    assert sc["spending_floor"] == 85_000.0
    assert sc["spending_ceiling"] == 150_000.0
    # Deterministic canary holds (byte-stability) at the default scenario.
    assert body["trajectory"]["final_portfolio"] == pytest.approx(
        DET_FINAL_PIN, rel=1e-4
    )


# ---------- Slice 4: payload-shape snapshots ----------
#
# Pin the top-level key SET (and order, via list comparison) for each of the 5
# payload variants the API can return. The JSON shape is the contract `web/app.js`
# parses, so any silent drift here (a renamed key, a forgotten conditional, a
# reordered insertion path) moves the UI. These act as a tripwire for the
# HistoricalPayload / MonteCarloPayload classes the slice introduced.
#
# Variant matrix:
#   1. Deterministic                              — its own simple shape, NOT
#                                                   through HistoricalPayload.
#   2. Historical + constant/lifecycle spending   — fan, no guardrails fields.
#   3. Historical + guardrails spending           — fan + spending_bands +
#                                                   adjustment_stats + worst_cohort.
#   4. Monte Carlo + constant/lifecycle spending  — fan, no guardrails fields.
#   5. Monte Carlo + guardrails spending          — fan + spending_bands +
#                                                   adjustment_stats (NO worst_cohort
#                                                   — MC trials have no calendar year).

_DETERMINISTIC_KEYS = ["mode", "scenario", "trajectory"]

_HISTORICAL_BASE_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "cohort_count", "start_year", "flows", "testable_since",
]

_HISTORICAL_GUARDRAILS_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "cohort_count", "start_year", "flows",
    "spending_bands", "adjustment_stats", "worst_cohort",
    "testable_since",
]

_MC_BASE_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "trial_count", "flows",
]

_MC_GUARDRAILS_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "trial_count", "flows",
    "spending_bands", "adjustment_stats",
]


def test_payload_snapshot_deterministic():
    """Deterministic payload: top-level keys + horizon length pin (variant 1)."""
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == _DETERMINISTIC_KEYS
    # Spot-check the trajectory structure under the deterministic branch.
    assert len(body["trajectory"]["points"]) == 95 - 50 + 1


def test_payload_snapshot_historical_constant():
    """Historical + constant spending: no guardrails fields (variant 2).

    Same assertion shape as the guardrails snapshot below; the contrast is the
    expected key list — constant/lifecycle omit ``spending_bands`` /
    ``adjustment_stats`` / ``worst_cohort``.
    """
    res = client.get(
        "/api/trajectory",
        params={"mode": "historical", "spending_mode": "constant"},
    )
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == _HISTORICAL_BASE_KEYS
    assert len(body["bands"]) == 95 - 50 + 1


def test_payload_snapshot_historical_guardrails():
    """Historical + guardrails: ALL conditional fields present (variant 3).

    Pins that guardrails activates every conditional payload field on the
    Historical path — the spending fan AND the cost-of-flexibility readout
    AND the worst-cohort drill-down.
    """
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == _HISTORICAL_GUARDRAILS_KEYS
    assert len(body["bands"]) == 95 - 50 + 1
    assert len(body["spending_bands"]) == 95 - 50 + 1


def test_payload_snapshot_mc_constant():
    """MC + constant spending: no guardrails fields, no start_year/cohort_count (variant 4).

    Pins the MC-base shape (``trial_count`` instead of ``cohort_count`` +
    ``start_year``) and that no guardrails fields leak into a constant run.
    """
    res = client.get(
        "/api/trajectory",
        params={"mode": "monte_carlo", "spending_mode": "constant"},
    )
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == _MC_BASE_KEYS
    assert len(body["bands"]) == 95 - 50 + 1


def test_payload_snapshot_mc_guardrails():
    """MC + guardrails: spending_bands + adjustment_stats, NO worst_cohort (variant 5).

    Synthetic MC trials have no calendar year, so the Historical "1969 cohort"
    drill-down does not translate — the design pins MC to fan + aggregate stats
    and omits the worst-trial drill-down. This pins the MC payload omits it.
    """
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "monte_carlo",
            "spending_mode": "guardrails",
            "annual_spending": 130_000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == _MC_GUARDRAILS_KEYS
    assert "worst_cohort" not in body
    assert len(body["bands"]) == 95 - 50 + 1
    assert len(body["spending_bands"]) == 95 - 50 + 1


# ---------- Glidepath per-class-assumption consistency (v1 contract) ----------


def _glide_phase(eq_w, bd_w, bond_return=0.02):
    """A two-class glidepath phase as a POST allocation list ({id,weight,real_return,volatility})."""
    return [
        {"id": "us_total_market", "weight": eq_w, "real_return": 0.07, "volatility": 0.174},
        {"id": "us_agg_bonds", "weight": bd_w, "real_return": bond_return, "volatility": 0.088},
    ]


def test_post_glidepath_conflicting_class_params_is_422():
    """POST glidepath with the SAME id at DIFFERENT real_return across phases → 422.

    The v1 contract: a glidepath varies WEIGHTS across phases; per-class return
    assumptions (real_return / volatility) are global to a class. Sending the bond
    id at 2% in one phase and 5% in another would make the deterministic engine and
    Monte Carlo silently describe different portfolios — the trust boundary rejects it.
    """
    body = {
        "allocation_mode": "glidepath",
        # 5 default phases (50-59 / 60-64 / 65-66 / 67-69 / 70-95); bonds 2% in
        # the early phases, 5% later — same id, conflicting return assumption.
        "phase_allocations": [
            _glide_phase(0.6, 0.4, bond_return=0.02),
            _glide_phase(0.6, 0.4, bond_return=0.02),
            _glide_phase(0.6, 0.4, bond_return=0.05),
            _glide_phase(0.6, 0.4, bond_return=0.05),
            _glide_phase(0.6, 0.4, bond_return=0.05),
        ],
    }
    res = client.post("/api/trajectory", json=body)
    assert res.status_code == 422
    assert "conflicting real_return/volatility" in res.json()["detail"]
    assert "us_agg_bonds" in res.json()["detail"]


def test_post_glidepath_same_params_different_weights_ok():
    """The LEGAL case (what the UI produces): same ids + params, different WEIGHTS.

    A real bond tent — conservative bridge weights, aggressive later weights, all at
    the SAME per-class returns — must run (200), not 422.
    """
    body = {
        "allocation_mode": "glidepath",
        "phase_allocations": [
            _glide_phase(0.3, 0.7),  # conservative bridge
            _glide_phase(0.3, 0.7),
            _glide_phase(0.8, 0.2),  # re-risk once income arrives
            _glide_phase(0.8, 0.2),
        ],
    }
    res = client.post("/api/trajectory", json=body)
    assert res.status_code == 200


# ---------- Buckets sourcing (bucket-strategy design, slice 1a API) ----------


def test_buckets_default_get_unchanged():
    """A bare GET still defaults to single sourcing (the canary echo unchanged)."""
    body = client.get("/api/trajectory").json()
    assert body["scenario"]["sourcing_mode"] == "single"
    assert body["scenario"]["buckets"] is None
    # The deterministic canary is untouched by the additive sourcing field.
    assert body["trajectory"]["points"][0]["end_portfolio"] == DET_YEAR0_CANARY


def test_buckets_get_historical_carries_buffer_survival():
    """GET buckets + Historical returns the buffer-survival readout block."""
    res = client.get(
        "/api/trajectory", params={"mode": "historical", "sourcing_mode": "buckets"}
    )
    assert res.status_code == 200
    bs = res.json()["buffer_survival"]
    assert "pct_cohorts_forced_to_sell_growth" in bs
    assert "median_years_buffer_lasted" in bs
    assert "worst_forced_sale_age" in bs
    assert bs["count"] > 0


def test_buckets_monte_carlo_carries_buffer_survival():
    """POST buckets + Monte Carlo carries the same buffer-survival readout (MC twin)."""
    res = client.post(
        "/api/trajectory", json={"mode": "monte_carlo", "sourcing_mode": "buckets"}
    )
    assert res.status_code == 200
    bs = res.json()["buffer_survival"]
    assert bs["count"] == 10_000


def test_single_sourcing_omits_buffer_survival():
    """Single sourcing omits the buffer_survival key (back-compatible)."""
    res = client.get("/api/trajectory", params={"mode": "historical"})
    assert res.status_code == 200
    assert "buffer_survival" not in res.json()


# ---------- Buckets sourcing: bucket-balance series (bucket-strategy design, slice 1b) ----------


def test_buckets_deterministic_carries_bucket_balances():
    """Deterministic buckets carries the 3-line bucket-balance series (the chart's data).

    The per-age cash/income/growth lists are parallel to ``ages`` and sum, age by
    age, to the deterministic trajectory's ``end_portfolio`` — the same sim
    ``simulate`` projects onto the single portfolio line, so the chart can't drift
    from the projection.
    """
    res = client.get("/api/trajectory", params={"sourcing_mode": "buckets"})
    assert res.status_code == 200
    body = res.json()
    bb = body["bucket_balances"]
    points = body["trajectory"]["points"]
    assert bb["ages"] == [p["age"] for p in points]
    assert len(bb["cash"]) == len(bb["income"]) == len(bb["growth"]) == len(points)
    for i, p in enumerate(points):
        summed = bb["cash"][i] + bb["income"][i] + bb["growth"][i]
        assert summed == pytest.approx(p["end_portfolio"], abs=1e-6)


def test_single_sourcing_deterministic_omits_bucket_balances():
    """Single (default) deterministic omits bucket_balances (back-compatible)."""
    res = client.get("/api/trajectory")
    assert res.status_code == 200
    assert "bucket_balances" not in res.json()


# ---------- Effective-equity glidepath (the imposed-allocation readout) ----------


def _eff_share_at(ee, age):
    """The effective-equity share at ``age`` in an effective_equity payload block."""
    return ee["shares"][ee["ages"].index(age)]


@pytest.mark.parametrize("mode", ["deterministic", "historical", "monte_carlo"])
def test_buckets_carries_effective_equity_all_modes(mode):
    """effective_equity rides on ALL three modes' buckets payloads (a setup property).

    It is computed off the deterministic reference, so the per-age series + anchor
    ages are identical regardless of sim mode. Default ($110k) → ~45% at age 50.
    """
    res = client.get(
        "/api/trajectory", params={"mode": mode, "sourcing_mode": "buckets"}
    )
    assert res.status_code == 200
    ee = res.json()["effective_equity"]
    # Shape: parallel ages/shares series + the three anchor ages.
    assert len(ee["ages"]) == len(ee["shares"]) > 0
    assert ee["start_age"] == 50
    assert ee["end_age"] == 95
    assert ee["full_gi_age"] == 70  # latest GI start (Person B's SS at 70)
    assert _eff_share_at(ee, 50) == pytest.approx(0.45, abs=0.005)
    # Rises toward ~all-equity by the end (GI-driven glide).
    assert _eff_share_at(ee, 95) > 0.95


def test_buckets_effective_equity_high_spend_lower_start_share():
    """A higher spend lowers the age-50 effective-equity share (~30% at $140k)."""
    res = client.post(
        "/api/trajectory",
        json={"sourcing_mode": "buckets", "annual_spending": 140_000.0},
    )
    assert res.status_code == 200
    ee = res.json()["effective_equity"]
    assert _eff_share_at(ee, 50) == pytest.approx(0.30, abs=0.005)


@pytest.mark.parametrize("mode", ["deterministic", "historical", "monte_carlo"])
def test_single_sourcing_omits_effective_equity(mode):
    """Single sourcing omits the effective_equity key in every mode (back-compatible)."""
    res = client.get("/api/trajectory", params={"mode": mode})
    assert res.status_code == 200
    assert "effective_equity" not in res.json()


def test_buckets_historical_carries_worst_cohort_series():
    """Historical buckets carries the worst-cohort 3-bucket balance series + label.

    The worst cohort's three per-age balance lines (cash/income/growth) ride
    alongside the buffer-survival readout, labelled by start year + annotated with
    the first-forced-sale / depletion ages, so the UI can title the chart "the worst
    cohort (1928)" and draw cash draining while growth rides.
    """
    res = client.get(
        "/api/trajectory", params={"mode": "historical", "sourcing_mode": "buckets"}
    )
    assert res.status_code == 200
    wc = res.json()["worst_bucket_cohort"]
    assert isinstance(wc["start_year"], int)
    assert len(wc["ages"]) == len(wc["cash"]) == len(wc["income"]) == len(wc["growth"])
    assert len(wc["ages"]) > 0
    # The label fields the chart title/annotations read (forced-sale + depletion may
    # be null, but the keys are always present).
    assert "first_forced_sale_age" in wc
    assert "depleted_age" in wc


def test_buckets_monte_carlo_omits_worst_cohort_series():
    """MC buckets omits worst_bucket_cohort (synthetic trials have no calendar cohort).

    Consistent with the guardrails worst-cohort being Historical-only: MC keeps the
    buffer-survival aggregate stats but no per-cohort calendar drill-down.
    """
    res = client.post(
        "/api/trajectory", json={"mode": "monte_carlo", "sourcing_mode": "buckets"}
    )
    assert res.status_code == 200
    body = res.json()
    assert "worst_bucket_cohort" not in body
    assert "buffer_survival" in body  # the aggregate stat still rides


def test_single_sourcing_historical_omits_worst_cohort_series():
    """Single Historical omits worst_bucket_cohort (back-compatible)."""
    res = client.get("/api/trajectory", params={"mode": "historical"})
    assert res.status_code == 200
    assert "worst_bucket_cohort" not in res.json()


def test_buckets_invalid_sourcing_mode_422():
    """An unknown sourcing_mode is a 422 with a useful detail."""
    res = client.get("/api/trajectory", params={"sourcing_mode": "nope"})
    assert res.status_code == 422
    assert "sourcing_mode" in res.json()["detail"]


def test_buckets_negative_n1_422():
    """A negative N1 is a 422 (N1/N2 must be non-negative integers)."""
    res = client.get(
        "/api/trajectory", params={"sourcing_mode": "buckets", "bucket_n1": -1}
    )
    assert res.status_code == 422
    assert "N1" in res.json()["detail"]


def test_buckets_get_n1_n2_override_flows_through():
    """GET bucket_n1 / bucket_n2 flow into the echoed bucket config."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "sourcing_mode": "buckets",
            "bucket_n1": 4,
            "bucket_n2": 6,
        },
    )
    assert res.status_code == 200
    buckets = res.json()["scenario"]["buckets"]
    assert buckets["n1"] == 4
    assert buckets["n2"] == 6


def test_buckets_guardrails_fence_422():
    """Buckets + guardrails is fenced with a 422 (composing them is a later slice)."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "sourcing_mode": "buckets",
            "spending_mode": "guardrails",
        },
    )
    assert res.status_code == 422
    assert "guardrails" in res.json()["detail"].lower()


def test_buckets_post_guardrails_fence_422():
    """The POST path also fences buckets + guardrails with a 422."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "sourcing_mode": "buckets",
            "spending_mode": "guardrails",
        },
    )
    assert res.status_code == 422


def test_buckets_fr_guardrail_fence_422():
    """Buckets + fr_guardrail is fenced with a 422 (same generalized fence as the
    other dynamic spending modes — no 500)."""
    res = client.get(
        "/api/trajectory",
        params={
            "mode": "historical",
            "sourcing_mode": "buckets",
            "spending_mode": "fr_guardrail",
        },
    )
    assert res.status_code == 422
    assert "funded-ratio" in res.json()["detail"].lower()


def test_buckets_post_fr_guardrail_fence_422():
    """The POST path also fences buckets + fr_guardrail with a 422."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "historical",
            "sourcing_mode": "buckets",
            "spending_mode": "fr_guardrail",
        },
    )
    assert res.status_code == 422


def test_buckets_post_bucket_allocation_not_summing_to_100_422():
    """A per-bucket allocation whose weights don't sum to 100% is a 422."""
    body = {
        "mode": "historical",
        "sourcing_mode": "buckets",
        "buckets": {
            "cash": {
                "id": "cash",
                "allocation": [
                    {"id": "st_treasury", "weight": 0.5, "real_return": 0.01}
                ],
            }
        },
    }
    res = client.post("/api/trajectory", json=body)
    assert res.status_code == 422
    assert "sum to 100%" in res.json()["detail"]


def test_buckets_post_custom_allocation_round_trip():
    """A valid POST bucket config round-trips: 200 + the echoed bucket allocations."""
    body = {
        "mode": "monte_carlo",
        "sourcing_mode": "buckets",
        "buckets": {
            "cash": {
                "id": "cash",
                "allocation": [
                    {"id": "st_treasury", "weight": 1.0, "real_return": 0.005}
                ],
            },
            "n1": 3,
            "n2": 8,
        },
    }
    res = client.post("/api/trajectory", json=body)
    assert res.status_code == 200
    scen = res.json()["scenario"]
    assert scen["sourcing_mode"] == "buckets"
    assert scen["buckets"]["n1"] == 3
    assert scen["buckets"]["n2"] == 8
    assert scen["buckets"]["cash"]["allocation"][0]["id"] == "st_treasury"


def test_buckets_solver_fenced_422():
    """The max-spend solver is fenced in buckets mode (POST /api/solve → 422)."""
    res = client.post(
        "/api/solve", json={"mode": "historical", "sourcing_mode": "buckets"}
    )
    assert res.status_code == 422
    assert "buckets" in res.json()["detail"].lower()


# ---------- Tax-drag solver (tax-drag design td1 DECISION 8, td-c) ----------
#
# A split-active scenario (sums to the default 1.8M initial_portfolio, same
# proportions the engine-level td-b solver tests use) reused across these.

_TD_SPLIT_BODY = {
    "taxable_balance": 450_000.0,
    "tax_deferred_balance": 1_080_000.0,
    "roth_balance": 270_000.0,
}


def test_solve_monte_carlo_split_returns_tax_drag_fence_422():
    """POST /api/solve mode=monte_carlo + a split is a 422 carrying the
    engine's OWN fence message verbatim (TaxDragMonteCarloNotSolvable ->
    api.error_translation, td-c) — the dragged MC solve is a later card."""
    from firebridgelab.solver import TAX_DRAG_MC_FENCE_MESSAGE

    res = client.post(
        "/api/solve",
        json={"mode": "monte_carlo", "confidence": 0.80, **_TD_SPLIT_BODY},
    )
    assert res.status_code == 422
    assert res.json()["detail"] == TAX_DRAG_MC_FENCE_MESSAGE


def test_solve_deterministic_split_rides_drag_and_lands_at_or_below_no_split():
    """POST /api/solve mode=deterministic with a split rides the DRAGGED
    engine (DECISION 8) — solves fine (no fence) and lands at or below the
    no-split solve on the same scenario, since the portfolio now also funds a
    real tax bill."""
    no_split = client.post("/api/solve", json={"mode": "deterministic"})
    split = client.post(
        "/api/solve", json={"mode": "deterministic", **_TD_SPLIT_BODY}
    )
    assert no_split.status_code == split.status_code == 200
    assert split.json()["solved_spend"] <= no_split.json()["solved_spend"]
    assert split.json()["solved_spend"] > 0


def test_solve_historical_split_rides_drag_and_lands_at_or_below_no_split():
    """Same property, Historical mode (POST /api/solve)."""
    no_split = client.post(
        "/api/solve", json={"mode": "historical", "confidence": 0.90}
    )
    split = client.post(
        "/api/solve",
        json={"mode": "historical", "confidence": 0.90, **_TD_SPLIT_BODY},
    )
    assert no_split.status_code == split.status_code == 200
    assert split.json()["solved_spend"] <= no_split.json()["solved_spend"]
    assert split.json()["solved_spend"] > 0
    assert split.json()["cohort_count"] == no_split.json()["cohort_count"]


def test_solve_no_split_byte_stable_after_td_c():
    """No-split solver responses are BYTE-STABLE across every mode after the
    td-c API rework — the dragged branch is unreachable without a split."""
    for mode, extra in (
        ("deterministic", {}),
        ("historical", {"confidence": 0.90}),
        ("monte_carlo", {"confidence": 0.80}),
    ):
        res = client.post("/api/solve", json={"mode": mode, **extra})
        assert res.status_code == 200, mode
        body = res.json()
        assert body["solved_spend"] > 0
        assert set(body) == {
            "mode",
            "spending_mode",
            "confidence_target",
            "solved_spend",
            "achieved_fraction",
            "hit_ceiling",
            "cohort_count",
            "scenario",
        }


# ---------- Stress Test (stress-test design slice 1) ----------


def test_stress_post_returns_single_trajectory():
    """POST mode=stress + stress_id returns the single-trajectory deterministic payload.

    A stress is an OVERLAY rendered as ONE line, so it reuses DeterministicPayload
    (the response carries `mode: "deterministic"` and a `trajectory` with points).
    """
    res = client.post(
        "/api/trajectory",
        json={"mode": "stress", "stress_id": "stagflation_1970s"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"
    assert "trajectory" in body and body["trajectory"]["points"]
    # The default plan is preserved (overlay): spend + GI untouched.
    assert body["scenario"]["annual_spending"] == default_scenario().annual_spending


def test_stress_get_matches_post():
    """GET mode=stress&stress_id=… returns the same terminal as the POST path."""
    g = client.get("/api/trajectory?mode=stress&stress_id=stagflation_1970s")
    p = client.post(
        "/api/trajectory",
        json={"mode": "stress", "stress_id": "stagflation_1970s"},
    )
    assert g.status_code == 200 and p.status_code == 200
    assert (
        g.json()["trajectory"]["final_portfolio"]
        == p.json()["trajectory"]["final_portfolio"]
    )


def test_stress_family_b_split_matches_engine_via_api():
    """End-to-end (firebridgelab.stress td-c fix, exercised through the API): a
    Family-B stress (a scenario perturbation, e.g. ss_haircut_23) with a split
    supplied now pays real tax — the same td1 HARD CONSTRAINT Family A already
    honored (test_stress.py pins the engine-level property directly). Here we
    only pin that the API's trajectory is BYTE-IDENTICAL to
    ``firebridgelab.simulate_stress`` for the same resolved scenario (a key-swap-guard
    style check — the API must not silently re-derive or diverge)."""
    from dataclasses import replace

    from firebridgelab import default_scenario, simulate_stress

    body_json = {
        "mode": "stress",
        "stress_id": "ss_haircut_23",
        "taxable_balance": 450_000.0,
        "tax_deferred_balance": 1_080_000.0,
        "roth_balance": 270_000.0,
    }
    res = client.post("/api/trajectory", json=body_json)
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "deterministic"

    s = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    engine_traj = simulate_stress(s, "ss_haircut_23")
    assert body["trajectory"]["final_portfolio"] == pytest.approx(
        engine_traj.final_portfolio
    )
    assert body["trajectory"]["depleted_age"] == engine_traj.depleted_age
    api_ends = [p["end_portfolio"] for p in body["trajectory"]["points"]]
    engine_ends = [p.end_portfolio for p in engine_traj.points]
    assert api_ends == pytest.approx(engine_ends)


def test_stress_overlay_lower_than_deterministic():
    """The stress terminal is meaningfully below the plain deterministic terminal."""
    det = client.post("/api/trajectory", json={}).json()
    stress = client.post(
        "/api/trajectory",
        json={"mode": "stress", "stress_id": "stagflation_1970s"},
    ).json()
    assert (
        stress["trajectory"]["final_portfolio"]
        < det["trajectory"]["final_portfolio"] * 0.5
    )


def test_stress_cost_of_living_exposes_effective_rails_scaled_120pct():
    """cost_of_living_shock payload carries effective_spending_floor/ceiling at ×1.20.

    The spending-chart guide line reads these (not the live UI knob) so it labels
    the floor for the perturbed world; under cost_of_living_shock the run clamps to ×1.20.
    """
    sc = default_scenario()
    body = client.post(
        "/api/trajectory",
        json={"mode": "stress", "stress_id": "cost_of_living_shock"},
    ).json()
    assert body["effective_spending_floor"] == pytest.approx(
        sc.spending_floor * 1.20, abs=1e-6
    )
    assert body["effective_spending_ceiling"] == pytest.approx(
        sc.spending_ceiling * 1.20, abs=1e-6
    )
    # The OVERLAY INVARIANT: the echoed scenario stays the UN-perturbed plan, so the
    # frontend never repaints the input knobs from it (selecting a stress must not
    # change the visible inputs). The effective rails ride as SEPARATE fields.
    assert body["scenario"]["spending_floor"] == sc.spending_floor
    assert body["scenario"]["spending_ceiling"] == sc.spending_ceiling


def test_stress_family_a_and_ss_haircut_effective_rails_equal_input():
    """Family-A + ss_haircut payloads expose effective rails == the input rails.

    They don't perturb the rails, so the guide line stays at the knob (already
    correct); the fix must not move it for them.
    """
    sc = default_scenario()
    for stress_id in ("stagflation_1970s", "ss_haircut_23"):
        body = client.post(
            "/api/trajectory",
            json={"mode": "stress", "stress_id": stress_id},
        ).json()
        assert body["effective_spending_floor"] == pytest.approx(
            sc.spending_floor, abs=1e-6
        ), stress_id
        assert body["effective_spending_ceiling"] == pytest.approx(
            sc.spending_ceiling, abs=1e-6
        ), stress_id


def test_deterministic_payload_omits_effective_rails():
    """The ordinary (non-stress) deterministic payload carries NO effective-rail keys.

    The extras are stress-only and appended last; a plain deterministic run is
    byte-unchanged (its canary still holds).
    """
    body = client.post("/api/trajectory", json={}).json()
    assert "effective_spending_floor" not in body
    assert "effective_spending_ceiling" not in body


def test_stress_unknown_id_returns_422():
    """An unknown stress id is a clean 422 (typed StressNotAvailable → friendly detail)."""
    res = client.post(
        "/api/trajectory", json={"mode": "stress", "stress_id": "nope"}
    )
    assert res.status_code == 422
    assert "stress" in res.json()["detail"].lower()


def test_stress_missing_id_returns_422():
    """mode=stress with no stress_id is a 422."""
    res = client.post("/api/trajectory", json={"mode": "stress"})
    assert res.status_code == 422


def test_stress_scope_guard_buckets_returns_422():
    """A buckets scenario under stress is a clean 422 (scope guard, later slice)."""
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "stress",
            "stress_id": "stagflation_1970s",
            "sourcing_mode": "buckets",
            "buckets": {"n1": 2, "n2": 7},
        },
    )
    assert res.status_code == 422


def test_stress_unmapped_class_id_returns_422_not_500():
    """An allocation with an unmapped class id is a clean 422, not an uncaught 500.

    The deterministic path accepts an arbitrary class id and returns 200 (it reads
    the supplied real_return directly). Stress mode can't assign such a class a
    framework column, so the per-class return builder must raise the typed
    StressNotAvailable (→ friendly 422 via api.error_translation) rather than a bare
    KeyError that would escape the API's except-ValueError handler as an HTTP 500
    with a leaked stack trace.
    """
    res = client.post(
        "/api/trajectory",
        json={
            "mode": "stress",
            "stress_id": "stagflation_1970s",
            "allocation": [
                {"id": "bogus_class", "weight": 1.0, "real_return": 0.05}
            ],
        },
    )
    assert res.status_code == 422, res.text
    # The friendly StressNotAvailable detail, not a raw KeyError / stack trace.
    detail = res.json()["detail"]
    assert detail == (
        "That stress test isn't available for this plan — stress tests "
        "currently run on a single-portfolio, constant allocation. Switch off "
        "buckets / glidepath, or pick a listed stress scenario."
    )
