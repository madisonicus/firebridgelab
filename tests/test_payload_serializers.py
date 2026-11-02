"""Direct unit tests for the three response serializers (the payload SEAM).

These exercise ``DeterministicPayload`` / ``HistoricalPayload`` /
``MonteCarloPayload`` .from_result IN ISOLATION — no FastAPI, no TestClient, no
HTTP round-trip. Before this slice every serializer was only reachable through
``client.get("/api/trajectory")`` + a full engine run; this file proves all three
are now unit-testable directly, which is the point of giving the deterministic
branch the same ``Payload.from_result`` seam the two stochastic modes had.

Scope: SHAPE (top-level key SET + ORDER + conditional presence) plus a thin
value spot-check. The math (band percentiles, depletion counts, final balances
under stress) is owned by the engine tests — these only assert the serializer
arranges what the engine handed it into the contracted dict.
"""

from firebridgelab import simulate_historical, simulate_monte_carlo
from firebridgelab.engine.scenario import default_scenario
from firebridgelab.engine.deterministic import simulate

from api.main import (
    DeterministicPayload,
    HistoricalPayload,
    MonteCarloPayload,
)
from tests._canary import DET_FINAL_CANARY

# The byte-stable deterministic canary at the default scenario (pinned across the
# API tests). It rides in the deterministic payload's trajectory block, so the
# serializer must surface it verbatim.
_DETERMINISTIC_FINAL_CANARY = DET_FINAL_CANARY

# Top-level key order each serializer must emit (mirrors the snapshot lists in
# tests/test_api.py; constant spending → no guardrails conditionals).
_DETERMINISTIC_KEYS = ["mode", "scenario", "trajectory"]

_HISTORICAL_BASE_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "cohort_count", "start_year", "flows", "testable_since",
]

_MC_BASE_KEYS = [
    "scenario", "bands", "ending_range", "depletion",
    "mode", "trial_count", "flows",
]


def test_deterministic_payload_serializer_shape_and_canary():
    """DeterministicPayload.from_result builds the contracted dict directly.

    Deterministic is a single fast pass, so we build a real trajectory and hand
    it to the serializer — no HTTP. Asserts the top-level key order and that the
    pinned final-portfolio canary survives serialization unchanged.
    """
    scenario = default_scenario()
    result = simulate(scenario)
    payload = DeterministicPayload.from_result(scenario, result)

    assert list(payload.keys()) == _DETERMINISTIC_KEYS
    assert payload["mode"] == "deterministic"
    # Single-sourcing default → buckets-only / tax-location conditionals omitted.
    assert "bucket_balances" not in payload
    assert "effective_equity" not in payload
    assert "tax_location" not in payload

    trajectory = payload["trajectory"]
    assert list(trajectory.keys()) == ["points", "depleted_age", "final_portfolio"]
    # Default 50→95 horizon, inclusive.
    assert len(trajectory["points"]) == 95 - 50 + 1
    # The byte-stable canary rides through the serializer untouched.
    assert trajectory["final_portfolio"] == _DETERMINISTIC_FINAL_CANARY


def test_historical_payload_serializer_shape():
    """HistoricalPayload.from_result builds the fan dict directly (no HTTP).

    Constant spending (the default) → the base fan shape with NO guardrails
    conditionals. Pins the key order and the per-age band length.
    """
    scenario = default_scenario()
    result = simulate_historical(scenario)
    payload = HistoricalPayload.from_result(scenario, result)

    assert list(payload.keys()) == _HISTORICAL_BASE_KEYS
    assert payload["mode"] == "historical"
    # Constant spending omits the guardrails block.
    assert "spending_bands" not in payload
    assert "adjustment_stats" not in payload
    assert "worst_cohort" not in payload
    assert len(payload["bands"]) == 95 - 50 + 1


def test_monte_carlo_payload_serializer_shape():
    """MonteCarloPayload.from_result builds the fan dict directly (no HTTP).

    A full 10k-trial run is overkill for a SHAPE test, so we drive the engine
    with a reduced ``n_trials`` (the serializer is agnostic to trial count). Pins
    the MC-base key order (``trial_count`` instead of ``cohort_count`` /
    ``start_year``) and that no guardrails fields leak into a constant run.
    """
    scenario = default_scenario()
    # Small, deterministic MC run — keeps the unit test fast; the serializer does
    # not care how many trials produced the aggregate result.
    result = simulate_monte_carlo(scenario, n_trials=200, seed=12345)
    payload = MonteCarloPayload.from_result(scenario, result)

    assert list(payload.keys()) == _MC_BASE_KEYS
    assert payload["mode"] == "monte_carlo"
    assert payload["trial_count"] == 200
    # Constant spending omits the guardrails block; MC never carries worst_cohort.
    assert "spending_bands" not in payload
    assert "adjustment_stats" not in payload
    assert "worst_cohort" not in payload
    assert len(payload["bands"]) == 95 - 50 + 1
