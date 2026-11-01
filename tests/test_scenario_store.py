"""Tests for the Saved Scenarios store (sv1).

Two layers:
  1. scenario_store unit tests — slugify, path-traversal confinement, atomic
     write, upsert (created_at stable / updated_at changes), 404, oversize cap,
     count cap, conflict-file filter. All use a TMP SCENARIOS_DIR (monkeypatched)
     — these NEVER write into the real store dir.
  2. endpoint tests — the 4 routes via FastAPI TestClient (no live server),
     incl. error codes (200/400/404/413/422).

Run:
  python -m pytest tests/test_scenario_store.py -q
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import scenario_store as ss
import api.main as main


# ---------- fixtures ----------


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Point the store at a tmp dir for the duration of a test. Both the store
    module and main.py reference the SAME module-level SCENARIOS_DIR (main calls
    into scenario_store), so patching the one variable redirects all writes."""
    d = tmp_path / "scenarios"
    monkeypatch.setattr(ss, "SCENARIOS_DIR", d)
    return d


@pytest.fixture
def client(tmp_store):
    return TestClient(main.app)


def _valid_blob(name="My Retirement Plan"):
    return {
        "schema_version": 1,
        "name": name,
        "view": {
            "sim_mode": "deterministic",
            "spending_mode": "constant",
            "allocation_mode": "constant",
            "sourcing_mode": "single",
        },
        "inputs": {
            "initial_portfolio": 1_000_000,
            "annual_spending": 110_000,
            "allocation": [
                {"id": "us_total_market", "weight": 0.6, "real_return": 0.07, "volatility": 0.174},
                {"id": "us_agg_bonds", "weight": 0.4, "real_return": 0.02, "volatility": 0.088},
            ],
        },
    }


# ---------- slugify ----------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("My Retirement Plan", "my-retirement-plan"),
        ("delay-SS-to-70", "delay-ss-to-70"),
        ("  Aggressive!!  ", "aggressive"),
        ("a/b\\c", "a-b-c"),
        ("MiXeD CaSe 123", "mixed-case-123"),
        ("trailing---dashes---", "trailing-dashes"),
        ("__under_scores__", "under-scores"),
    ],
)
def test_slugify_cases(name, expected):
    assert ss.slugify(name) == expected


def test_slugify_caps_at_64_chars():
    slug = ss.slugify("x" * 200)
    assert len(slug) == 64
    assert ss._ID_RE.match(slug)


@pytest.mark.parametrize("name", ["", "   ", "!!!", "  ///  ", "...", "---"])
def test_slugify_empty_rejected(name):
    with pytest.raises(ss.InvalidScenarioId):
        ss.slugify(name)


# ---------- traversal rejection + confinement ----------


@pytest.mark.parametrize(
    "bad_id",
    [
        "..",
        "../x",
        "../../etc/passwd",
        "/etc/passwd",
        "a/b",
        "a\\b",
        "A",  # uppercase not allowed
        "foo.bar",  # dot not allowed
        "-leading",  # must start alnum
        "..%2f..%2fetc",  # literal — % is not allowed
        "x" * 65,  # over length
        "café",  # unicode
        "x\x00y",  # embedded null byte
    ],
)
def test_validate_id_rejects(bad_id):
    with pytest.raises(ss.InvalidScenarioId):
        ss._validate_id(bad_id)


def test_path_for_confined_to_store(tmp_store):
    """Even an id that slipped the pattern check (we call _path_for on a slug)
    must resolve inside the store dir; the confinement assert is the backstop."""
    p = ss._path_for("my-retirement-plan")
    store = tmp_store.resolve()
    assert ss._is_relative_to(p.resolve(), store)
    assert p.parent.resolve() == store


def test_traversal_name_cannot_escape_store(tmp_store):
    """A malicious NAME slugifies to a safe stem and is written INSIDE the store;
    nothing lands outside it."""
    blob = _valid_blob(name="../../etc/cron.d/x")
    stamped = ss.write_scenario(blob)
    assert stamped["id"] == "etc-cron-d-x"
    target = (tmp_store / "etc-cron-d-x.json").resolve()
    assert target.is_file()
    # The store dir holds exactly the one file (no escape, no extra litter).
    assert sorted(p.name for p in tmp_store.iterdir()) == ["etc-cron-d-x.json"]


# ---------- atomic write + upsert ----------


def test_write_then_read_roundtrip(tmp_store):
    stamped = ss.write_scenario(_valid_blob())
    got = ss.read_scenario("my-retirement-plan")
    assert got == stamped
    assert got["inputs"]["initial_portfolio"] == 1_000_000


def test_write_stamps_provenance(tmp_store):
    stamped = ss.write_scenario(_valid_blob())
    assert stamped["id"] == "my-retirement-plan"
    assert stamped["created_at"]
    assert stamped["updated_at"]
    # app_revision is best-effort: a string (git SHA) or None, never raises.
    assert stamped["app_revision"] is None or isinstance(stamped["app_revision"], str)


def test_upsert_created_at_stable_updated_at_changes(tmp_store):
    first = ss.write_scenario(_valid_blob())
    # Force a distinct timestamp so updated_at provably advances.
    import api.scenario_store as mod

    orig = mod._now_iso
    mod._now_iso = lambda: "2030-01-01T00:00:00+00:00"
    try:
        second = ss.write_scenario(_valid_blob())
    finally:
        mod._now_iso = orig
    assert second["created_at"] == first["created_at"]  # stable
    assert second["updated_at"] == "2030-01-01T00:00:00+00:00"
    assert second["updated_at"] != first["updated_at"]  # advanced


def test_atomic_write_leaves_no_temp_files(tmp_store):
    ss.write_scenario(_valid_blob())
    leftovers = [p.name for p in tmp_store.iterdir() if p.name.startswith(".tmp")]
    assert leftovers == []


def test_atomic_write_no_partial_on_serialize_failure(tmp_store):
    """A blob that can't be JSON-serialized must not leave a partial file."""
    bad = _valid_blob()
    bad["inputs"]["bad"] = {1, 2, 3}  # a set is not JSON-serializable
    with pytest.raises(TypeError):
        ss.write_scenario(bad)
    assert list(tmp_store.glob("*.json")) == []
    assert [p for p in tmp_store.iterdir() if p.name.startswith(".tmp")] == []


# ---------- not found ----------


def test_read_missing_raises_not_found(tmp_store):
    with pytest.raises(ss.ScenarioNotFound):
        ss.read_scenario("does-not-exist")


def test_delete_missing_raises_not_found(tmp_store):
    with pytest.raises(ss.ScenarioNotFound):
        ss.delete_scenario("does-not-exist")


def test_delete_removes_file(tmp_store):
    ss.write_scenario(_valid_blob())
    ss.delete_scenario("my-retirement-plan")
    assert not (tmp_store / "my-retirement-plan.json").exists()


# ---------- oversize cap ----------


def test_oversize_blob_rejected(tmp_store):
    big = _valid_blob()
    big["inputs"]["junk"] = "z" * (ss.MAX_BLOB_BYTES + 1)
    with pytest.raises(ss.ScenarioTooLarge):
        ss.write_scenario(big)
    assert list(tmp_store.glob("*.json")) == []


# ---------- count cap ----------


def test_count_cap_blocks_new_creates_allows_overwrites(tmp_store, monkeypatch):
    monkeypatch.setattr(ss, "MAX_SCENARIOS", 3)
    for i in range(3):
        ss.write_scenario(_valid_blob(name=f"plan-{i}"))
    # A 4th distinct create is rejected.
    with pytest.raises(ss.ScenarioStoreFull):
        ss.write_scenario(_valid_blob(name="plan-3"))
    # But an OVERWRITE of an existing one is allowed (doesn't grow the store).
    overwritten = ss.write_scenario(_valid_blob(name="plan-0"))
    assert overwritten["id"] == "plan-0"


# ---------- sync-conflict filtering ----------


def test_list_excludes_sync_conflict_files(tmp_store):
    ss.write_scenario(_valid_blob(name="retirement-plan"))
    # Drop a Syncthing conflict copy + a non-json file alongside it.
    (tmp_store / "retirement-plan.sync-conflict-20260530-093000-ABCDEFG.json").write_text(
        json.dumps({"id": "phantom", "name": "Phantom"}), encoding="utf-8"
    )
    (tmp_store / "README.md").write_text("notes", encoding="utf-8")
    ids = [m["id"] for m in ss.list_scenarios()]
    assert ids == ["retirement-plan"]


def test_list_sorted_updated_desc(tmp_store):
    import api.scenario_store as mod

    orig = mod._now_iso
    try:
        mod._now_iso = lambda: "2030-01-01T00:00:00+00:00"
        ss.write_scenario(_valid_blob(name="older"))
        mod._now_iso = lambda: "2030-06-01T00:00:00+00:00"
        ss.write_scenario(_valid_blob(name="newer"))
    finally:
        mod._now_iso = orig
    ids = [m["id"] for m in ss.list_scenarios()]
    assert ids == ["newer", "older"]


def test_list_skips_unparseable_file(tmp_store):
    ss.write_scenario(_valid_blob(name="good"))
    (tmp_store / "broken.json").write_text("{not json", encoding="utf-8")
    ids = [m["id"] for m in ss.list_scenarios()]
    assert ids == ["good"]


# ---------- endpoint tests (FastAPI TestClient, no live server) ----------


def test_get_scenarios_empty(client):
    res = client.get("/api/scenarios")
    assert res.status_code == 200
    assert res.json() == []


def test_put_then_get_then_list(client):
    res = client.put("/api/scenarios/my-retirement-plan", json=_valid_blob())
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "my-retirement-plan"
    assert body["created_at"] and body["updated_at"]

    got = client.get("/api/scenarios/my-retirement-plan")
    assert got.status_code == 200
    assert got.json()["name"] == "My Retirement Plan"

    lst = client.get("/api/scenarios").json()
    assert [m["id"] for m in lst] == ["my-retirement-plan"]
    # List is metadata only.
    assert set(lst[0].keys()) == {"id", "name", "created_at", "updated_at"}


def test_put_upsert_keeps_created_at(client):
    first = client.put("/api/scenarios/my-retirement-plan", json=_valid_blob()).json()
    second = client.put("/api/scenarios/my-retirement-plan", json=_valid_blob()).json()
    assert second["created_at"] == first["created_at"]


def test_get_missing_404(client):
    assert client.get("/api/scenarios/nope").status_code == 404


def test_delete_then_404(client):
    client.put("/api/scenarios/my-retirement-plan", json=_valid_blob())
    assert client.delete("/api/scenarios/my-retirement-plan").status_code == 200
    assert client.delete("/api/scenarios/my-retirement-plan").status_code == 404


def test_put_malformed_blob_422(client):
    # Missing required `view`.
    bad = _valid_blob()
    del bad["view"]
    assert client.put("/api/scenarios/x", json=bad).status_code == 422


def test_put_bad_schema_version_422(client):
    bad = _valid_blob()
    bad["schema_version"] = 2
    assert client.put("/api/scenarios/my-retirement-plan", json=bad).status_code == 422


def test_put_bad_view_mode_422(client):
    bad = _valid_blob()
    bad["view"]["sim_mode"] = "bogus"
    assert client.put("/api/scenarios/my-retirement-plan", json=bad).status_code == 422


def test_put_get_fr_guardrail_spending_mode_round_trips(client):
    """fr_guardrail is an accepted view spending_mode → saves (200) and reloads
    with the same mode (the _SAVE_VIEW_SPENDING_MODES roundtrip, FR slice 3)."""
    blob = _valid_blob(name="FR bands plan")
    blob["view"]["spending_mode"] = "fr_guardrail"
    put = client.put("/api/scenarios/fr-bands-plan", json=blob)
    assert put.status_code == 200
    got = client.get("/api/scenarios/fr-bands-plan")
    assert got.status_code == 200
    assert got.json()["view"]["spending_mode"] == "fr_guardrail"


def test_put_empty_name_422(client):
    bad = _valid_blob(name="   ")
    assert client.put("/api/scenarios/x", json=bad).status_code == 422


def test_get_malformed_id_400(client):
    # Uppercase is a valid URL segment but an invalid slug -> 400.
    assert client.get("/api/scenarios/MyPlan").status_code == 400
    assert client.delete("/api/scenarios/MyPlan").status_code == 400


def test_put_oversize_413(client):
    big = _valid_blob()
    big["inputs"]["junk"] = "z" * (ss.MAX_BLOB_BYTES + 1)
    assert client.put("/api/scenarios/big", json=big).status_code == 413


def test_put_count_cap_400(client, monkeypatch):
    monkeypatch.setattr(ss, "MAX_SCENARIOS", 2)
    assert client.put("/api/scenarios/a", json=_valid_blob(name="a")).status_code == 200
    assert client.put("/api/scenarios/b", json=_valid_blob(name="b")).status_code == 200
    assert client.put("/api/scenarios/c", json=_valid_blob(name="c")).status_code == 400


def test_put_id_in_path_does_not_escape(client):
    """The slug is derived from name; the path id is just informational. A name
    that slugifies safely is written to the safe stem regardless of the path."""
    res = client.put("/api/scenarios/my-retirement-plan", json=_valid_blob(name="My Retirement Plan"))
    assert res.status_code == 200
    assert res.json()["id"] == "my-retirement-plan"
