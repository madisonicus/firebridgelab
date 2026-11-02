"""POST bodies with ``people`` / ``income_streams`` / ``cash_events`` /
``spending_adjustments``: accepted, echoed, and rejected with clear 422s.
All households are fictional."""

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)

_SINGLE = {
    "people": [{"id": "a", "start_age": 58, "end_age": 92}],
    "income_streams": [
        {"id": "pension", "kind": "pension", "amount": 18000, "start_age": 60},
        {"id": "ss", "kind": "social_security", "amount": 28000, "start_age": 67},
        {"id": "consulting", "kind": "work", "amount": 25000, "start_age": 58, "end_age": 62},
    ],
    "cash_events": [{"age": 70, "amount": 150000, "tax_treatment": "capital_gain",
                     "basis_fraction": 0.4, "id": "condo"}],
    "spending_adjustments": [{"amount": 9000, "start_age": 58, "end_age": 65, "id": "aca"}],
    "initial_portfolio": 900000,
    "annual_spending": 55000,
}


def test_post_household_echoes_and_runs():
    r = client.post("/api/trajectory", json=_SINGLE)
    assert r.status_code == 200, r.text
    body = r.json()
    sc = body["scenario"]
    assert sc["start_age"] == 58 and sc["end_age"] == 92
    assert sc["people"] == [{"id": "a", "start_age": 58, "end_age": 92, "name": ""}]
    assert [s["id"] for s in sc["income_streams"]] == ["pension", "ss", "consulting"]
    assert sc["cash_events"][0]["id"] == "condo"
    assert sc["spending_adjustments"][0]["amount"] == 9000
    points = body["trajectory"]["points"]
    assert points[0]["age"] == 58 and points[-1]["age"] == 92
    # Required spending is part of the recorded spend; the work income is GI.
    assert points[0]["spending"] == pytest.approx(55000 + 9000)
    assert points[0]["guaranteed_income"] == pytest.approx(25000)
    assert points[7]["spending"] == pytest.approx(55000)  # age 65


def test_post_household_in_historical_and_with_a_tax_split():
    hist = client.post("/api/trajectory", json={**_SINGLE, "mode": "historical"})
    assert hist.status_code == 200, hist.text
    split = client.post(
        "/api/trajectory",
        json={**_SINGLE, "taxable_balance": 400000, "tax_deferred_balance": 400000,
              "roth_balance": 100000},
    )
    assert split.status_code == 200, split.text
    assert "federal_tax" in split.json()


def test_legacy_body_echoes_no_explicit_household():
    r = client.post("/api/trajectory", json={})
    sc = r.json()["scenario"]
    assert sc["people"] is None and sc["income_streams"] is None
    assert sc["cash_events"] == [] and sc["spending_adjustments"] == []


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"income_streams": [{"id": "x", "kind": "lottery", "amount": 1, "start_age": 60}]},
         "kind must be one of"),
        ({"income_streams": [{"id": "x", "kind": "social_security", "amount": 1,
                              "start_age": 75}]}, "claim age"),
        ({"income_streams": [{"id": "x", "kind": "pension", "amount": 1, "start_age": 60,
                              "owner": "b"}]}, "owner must be one of"),
        ({"cash_events": [{"age": 99, "amount": 1}]}, "outside the plan"),
        ({"spending_adjustments": [{"amount": 1, "start_age": 65, "end_age": 60}]},
         "end_age"),
        ({"survivor_age": 80}, "two-person household"),
        ({"start_age": 50}, "conflicts with people"),
        ({"people": [{"id": "b", "start_age": 58, "end_age": 92}]}, "people\\[0\\].id"),
    ],
)
def test_post_household_validation_is_a_clear_422(patch, message):
    r = client.post("/api/trajectory", json={**_SINGLE, **patch})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert __import__("re").search(message, detail), detail


def test_post_household_type_error_is_a_422():
    r = client.post(
        "/api/trajectory",
        json={**_SINGLE, "income_streams": [{"id": "x", "kind": "work", "amount": "lots",
                                             "start_age": 60}]},
    )
    assert r.status_code == 422


def test_couple_with_age_gap_via_api():
    r = client.post(
        "/api/trajectory",
        json={
            "people": [{"id": "a", "start_age": 55, "end_age": 95},
                       {"id": "b", "start_age": 50, "end_age": 95}],
            "income_streams": [
                {"id": "b_pension", "kind": "pension", "amount": 10000, "start_age": 60,
                 "owner": "b"},
            ],
            "survivor_age": 70,
            "survivor_decedent": "b",
        },
    )
    assert r.status_code == 200, r.text
    sc = r.json()["scenario"]
    assert (sc["start_age"], sc["end_age"]) == (55, 100)
    gi = {p["age"]: p["guaranteed_income"] for p in r.json()["trajectory"]["points"]}
    assert gi[64] == 0 and gi[65] == 10000
    assert gi[75] == 0  # B (the owner) dies at B's 70 = A's 75; pension default fraction 0


def test_solve_with_household():
    r = client.post("/api/solve", json={**_SINGLE, "mode": "deterministic"})
    assert r.status_code == 200, r.text
    assert r.json()["scenario"]["spending_adjustments"][0]["id"] == "aca"
