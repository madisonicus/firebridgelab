"""Plan files through the web API: import, export and the bundled examples.

The UI and the command line share one file format, so a plan loaded through
``/api/plan/import`` and written back through ``/api/plan/export`` must
describe the same scenario. All households are fictional."""

import pytest
from fastapi.testclient import TestClient

from api import plan_io
from api.main import app
from firebridgelab import default_scenario
from firebridgelab.plan import dump, loads

client = TestClient(app)

EXAMPLES = plan_io.example_names()


def test_examples_list_names_every_bundled_plan():
    r = client.get("/api/examples")
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == EXAMPLES
    assert len(rows) == 6
    assert all(row["name"] and row["description"] for row in rows)


def test_unknown_example_is_404():
    assert client.get("/api/examples/no-such-plan").status_code == 404


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_body_runs_and_round_trips(name):
    r = client.get(f"/api/examples/{name}")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["toml"] == plan_io.example_text(name)
    original = loads(payload["toml"]).scenario

    # The body the UI receives runs through the projection endpoint as-is.
    run = client.post("/api/trajectory", json=payload["body"])
    assert run.status_code == 200, run.text

    # Exporting the same body writes a plan file for the same scenario.
    out = client.post(
        "/api/plan/export",
        json={"scenario": payload["body"], "name": payload["name"],
              "description": payload["description"], "trials": payload["trials"],
              "seed": payload["seed"], "stress": payload["stress"]},
    )
    assert out.status_code == 200, out.text
    again = loads(out.json()["toml"])
    assert again.scenario == original
    assert again.name == payload["name"]
    assert again.trials == payload["trials"]
    assert list(again.stress) == payload["stress"]


def test_import_returns_the_body_and_settings():
    text = dump(default_scenario())
    r = client.post("/api/plan/import", json={"toml": text})
    assert r.status_code == 200, r.text
    body = r.json()["body"]
    assert [p["id"] for p in body["people"]] == ["a", "b"]
    assert [s["kind"] for s in body["income_streams"]] == [
        "pension", "pension", "social_security", "social_security"]
    # The legacy fields are not in the body: the household replaces them.
    assert not any(k.startswith("person_") for k in body)
    run = client.post("/api/trajectory", json=body)
    assert run.status_code == 200
    # Same final portfolio as the default scenario (SS round-trips via FRA).
    det = client.post("/api/trajectory", json={})
    assert run.json()["trajectory"]["final_portfolio"] == pytest.approx(
        det.json()["trajectory"]["final_portfolio"], rel=1e-9)


def test_import_error_names_the_key():
    r = client.post("/api/plan/import",
                    json={"toml": "[[people]]\nstart_age = 50\nend_age = 90\n"
                                  "[portfolio]\nbalance = 1\n[spending]\nanual = 1\n"})
    assert r.status_code == 422
    assert "anual" in r.json()["detail"] and "annual" in r.json()["detail"]


def test_import_rejects_bad_toml():
    r = client.post("/api/plan/import", json={"toml": "this is = = not toml"})
    assert r.status_code == 422
    assert "not valid TOML" in r.json()["detail"]


def test_export_of_an_invalid_scenario_is_422():
    r = client.post("/api/plan/export",
                    json={"scenario": {"people": [{"id": "a", "start_age": 60, "end_age": 50}]}})
    assert r.status_code == 422


def test_plan_extras_survive_export():
    text = dump(default_scenario()).replace(
        "slow_go_start_age = 75", "slow_go_start_age = 72")
    body = client.post("/api/plan/import", json={"toml": text}).json()["body"]
    assert body["slow_go_start_age"] == 72
    out = client.post("/api/plan/export", json={"scenario": body}).json()["toml"]
    assert loads(out).scenario.slow_go_start_age == 72
