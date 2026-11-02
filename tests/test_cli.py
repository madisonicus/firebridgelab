"""The ``firebridgelab`` command. Fictional plans only; small Monte Carlo counts."""

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from firebridgelab import cli
from firebridgelab.cli import outcomes as oc

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "firebridgelab" / "examples").glob("*.toml"))
LEAN = str(ROOT / "firebridgelab" / "examples" / "leanfire-single.toml")
TAXED = str(ROOT / "firebridgelab" / "examples" / "part-time-bridge.toml")
MRA = str(ROOT / "firebridgelab" / "examples" / "mra10-single.toml")


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_console_script_runs_as_a_subprocess():
    r = subprocess.run(
        [sys.executable, "-m", "firebridgelab", "run", MRA, "--mode", "deterministic",
         "--format", "json"],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    assert obj["outcomes"][0]["mode"] == "deterministic"
    assert obj["years"][0]["age_a"] == 57


def test_help_everywhere(capsys):
    for sub in ([], ["init"], ["run"], ["compare"], ["solve"], ["sweep"], ["report"], ["ui"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(sub + ["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "usage: firebridgelab" in out


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_runs(capsys, path):
    code, out, err = run(capsys, "run", path, "--trials", 200)
    assert code == 0, err
    assert "success (never depleted)" in out
    assert "Deterministic, year by year" in out
    assert "not financial advice" in out


def test_init_starter_example_list_and_overwrite(capsys, tmp_path):
    target = tmp_path / "plan.toml"
    code, _out, err = run(capsys, "init", target)
    assert code == 0 and target.read_text().startswith("# A firebridgelab plan file")
    code, _out, err = run(capsys, "init", target)
    assert code == 2 and "already exists" in err
    code, _out, _err = run(capsys, "init", "--example", "leanfire-single", target, "--force")
    assert code == 0 and "Leanfire single" in target.read_text()
    code, out, _err = run(capsys, "init", "--list")
    assert code == 0 and "mra10-single" in out.split()
    code, _out, err = run(capsys, "init", "--example", "nope", "-")
    assert code == 2 and "examples:" in err
    # The starter runs.
    assert run(capsys, "run", target, "--mode", "deterministic")[0] == 0


def test_run_json_has_headline_and_tax(capsys):
    code, out, _ = run(capsys, "run", TAXED, "--format", "json", "--trials", 200)
    assert code == 0
    obj = json.loads(out)
    modes = [o["mode"] for o in obj["outcomes"]]
    assert modes == ["deterministic", "historical", "monte_carlo"]
    for o in obj["outcomes"]:
        assert 0 <= o["success_rate"] <= 1
        assert o["lifetime_tax"] > 0
        assert set(o["spending"]) == {"48", "50", "60", "70", "80", "90"}
    assert "federal_tax" in obj["years"][0]


def test_run_csv_and_years(capsys):
    code, out, _ = run(capsys, "run", LEAN, "--mode", "historical", "--format", "csv")
    rows = list(csv.reader(io.StringIO(out)))
    assert code == 0 and rows[0] == ["metric", "historical"]
    keys = [r[0] for r in rows]
    assert "ever_at_floor_share" in keys and "terminal.p50" in keys
    code, out, _ = run(capsys, "run", LEAN, "--years", "--format", "csv")
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0][0] == "age_a" and len(rows) == 1 + (95 - 40 + 1)


def test_run_stress_mode(capsys):
    code, out, _ = run(capsys, "run", ROOT / "firebridgelab" / "examples" / "rental-and-pension.toml",
                       "--mode", "stress")
    assert code == 0 and "stress gfc_2008" in out


def test_run_set_override(capsys):
    _, base, _ = run(capsys, "run", LEAN, "--mode", "deterministic", "--format", "json")
    _, more, _ = run(capsys, "run", LEAN, "--mode", "deterministic", "--format", "json",
                     "--set", "spending.rule=constant", "--set", "spending.annual=60000")
    spend = lambda o: json.loads(o)["outcomes"][0]["spending"]["40"]["p50"]  # noqa: E731
    assert spend(base) == 50_000 and spend(more) == 65_000   # + $5k ACA


def test_compare(capsys):
    code, out, _ = run(capsys, "compare", MRA, LEAN, "--mode", "historical")
    assert code == 0
    assert "MRA+10 single retiree" in out and "vs MRA+10 single retiree" in out
    code, out, _ = run(capsys, "compare", MRA, LEAN, "--mode", "deterministic", "--format", "json")
    obj = json.loads(out)
    assert obj["plans"] == ["MRA+10 single retiree", "Leanfire single"]
    assert len(obj["outcomes"]["deterministic"]) == 2
    code, _out, err = run(capsys, "compare", MRA)
    assert code == 2 and "at least two" in err


def test_solve(capsys):
    code, out, _ = run(capsys, "solve", MRA, "--confidence", 0.9, "--format", "json")
    obj = json.loads(out)
    assert code == 0 and obj["mode"] == "historical"
    assert obj["achieved_fraction"] >= 0.9 and obj["solved_spend"] % 1000 == 0
    code, _out, err = run(capsys, "solve", LEAN)   # guardrails cannot be solved
    assert code == 1 and "spending.rule=constant" in err
    code, out, _ = run(capsys, "solve", LEAN, "--set", "spending.rule=constant")
    assert code == 0 and "max sustainable" in out


def test_sweep_grid(capsys):
    code, out, _ = run(capsys, "sweep", LEAN, "--mode", "deterministic", "--format", "csv",
                       "--set", "spending.annual=40000:50000:5000",
                       "--set", "income_streams.ss.start_age=62,70")
    assert code == 0
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0][:2] == ["spending.annual", "income_streams.ss.start_age"]
    grid = [(int(r[0]), int(r[1])) for r in rows[1:]]
    assert grid == [(a, c) for a in (40000, 45000, 50000) for c in (62, 70)]
    assert all(r[-1] == "" for r in rows[1:])   # no invalid cells


def test_sweep_values_and_grid_helpers():
    assert cli.sweep_values("1:3:1") == [1, 2, 3]
    assert cli.sweep_values("0.03:0.05:0.01") == [0.03, 0.04, 0.05]
    assert cli.sweep_values('constant,"guardrails"') == ["constant", "guardrails"]
    assert cli.sweep_values("{a=1,b=2},{a=3,b=4}") == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    keys, grid = cli.sweep_grid(["x=1,2", "y=a,b,c"])
    assert keys == ["x", "y"] and len(grid) == 6 and grid[0] == (1, "a")


def test_sweep_invalid_cell_is_reported(capsys):
    code, out, err = run(capsys, "sweep", LEAN, "--mode", "deterministic",
                         "--set", "income_streams.ss.start_age=70,75")
    assert code == 1 and "invalid" in out and "1 of 2 cells" in err


def test_unknown_key_and_bad_value_exit_2(capsys, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text((ROOT / "firebridgelab" / "examples" / "mra10-single.toml").read_text()
                   .replace("annual = 44_000", "anual = 44_000\nannual = 44_000"))
    code, out, err = run(capsys, "run", bad)
    assert code == 2 and out == ""
    assert "unknown key 'spending.anual'; did you mean 'annual'?" in err
    code, _out, err = run(capsys, "run", MRA, "--set", "spending.annual=lots")
    assert code == 2 and "'spending.annual' must be a number" in err
    code, _out, err = run(capsys, "run", tmp_path / "missing.toml")
    assert code == 2 and "cannot read plan file" in err


def test_report_is_self_contained(capsys, tmp_path):
    pytest.importorskip("matplotlib")
    target = tmp_path / "r.html"
    code, _out, err = run(capsys, "report", TAXED, "-o", target, "--trials", 200)
    assert code == 0, err
    html = target.read_text()
    assert html.startswith("<!doctype html>")
    assert html.count("<svg") == 4   # portfolio, spending, guaranteed income, tax
    for section in ("Headline", "Portfolio", "Spending", "Guaranteed income",
                    "Federal tax", "Assumptions", "Methodology and limitations"):
        assert f"<h2>{section}</h2>" in html
    assert "not financial advice" in html
    # Nothing to fetch: no scripts, no external stylesheets, images or links.
    assert "<script" not in html and "<link" not in html
    assert 'src="http' not in html and "url(http" not in html and "@import" not in html


def test_report_without_matplotlib(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    code, _out, err = run(capsys, "report", MRA, "-o", "-")
    assert code == 1 and "firebridgelab[report]" in err


def test_ui(capsys, monkeypatch):
    calls = []
    fake = type(sys)("uvicorn")
    fake.run = lambda app, **kw: calls.append((app, kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setitem(sys.modules, "fastapi", type(sys)("fastapi"))
    code, _out, err = run(capsys, "ui", "--port", 8123)
    assert code == 0 and calls == [("api.main:app", {"host": "127.0.0.1", "port": 8123,
                                                     "log_level": "warning"})]
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    code, _out, err = run(capsys, "ui")
    assert code == 1 and "firebridgelab[web]" in err


def test_key_ages():
    from firebridgelab import plan as P

    s = P.load(LEAN)
    assert oc.key_ages(s) == [40, 50, 60, 70, 80, 90]
