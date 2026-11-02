"""Plan files: loading, writing, round trips, overrides and error messages.

Every plan here is a fictional household.
"""

import re
from pathlib import Path

import pytest

import firebridgelab as rm
from firebridgelab import plan as P
from firebridgelab.engine.deterministic import simulate
from tests._canary import DET_FINAL_CANARY

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.toml"))
STARTER = ROOT / "firebridgelab" / "plan_starter.toml"

# The default fictional couple, written as a plan file.
DEFAULT_COUPLE = """
[[people]]
start_age = 50
end_age = 95
[[people]]
start_age = 50
end_age = 95

[[income_streams]]
id = "a_pension"
kind = "pension"
amount = 24_000
start_age = 60
survivor_fraction = 0.5
[[income_streams]]
id = "b_pension"
kind = "pension"
owner = "b"
amount = 9_000
start_age = 65
survivor_fraction = 0.5
[[income_streams]]
id = "a_ss"
kind = "social_security"
amount = 33_000
start_age = 67
[[income_streams]]
id = "b_ss"
kind = "social_security"
owner = "b"
amount = 26_000
start_age = 70

[portfolio]
balance = 1_800_000

[spending]
annual = 110_000
"""

MINIMAL = """
[[people]]
start_age = 45
end_age = 90
[portfolio]
balance = 1_000_000
[spending]
annual = 40_000
"""

# Every section and optional key in use at once (keys that exclude each other,
# glidepath vs buckets, are covered by their own tests).
MAXIMAL = """
name = "Everything"
description = "Every key"

[[people]]
id = "a"
name = "Quinn"
start_age = 52
end_age = 95
[[people]]
id = "b"
name = "Rowan"
start_age = 49
end_age = 97

[[income_streams]]
id = "q_pension"
kind = "pension"
owner = "a"
amount = 20_000
start_age = 60
end_age = 90
cola = "fixed"
cola_rate = 0.01
nominal_until_start = true
survivor_fraction = 0.5
tax_treatment = "ordinary"
label = "Quinn's pension"
[[income_streams]]
id = "r_ss"
kind = "social_security"
owner = "b"
amount = 22_000
start_age = 68

[[cash_events]]
id = "house"
owner = "household"
age = 70
amount = 300_000
tax_treatment = "capital_gain"
basis_fraction = 0.5
label = "Sell the house"

[[spending_adjustments]]
id = "aca"
owner = "b"
amount = 8_000
start_age = 49
end_age = 65
survivor_fraction = 0.5
label = "Health insurance"

[portfolio]
balance = 1_500_000
taxable = 500_000
tax_deferred = 800_000
roth = 200_000
taxable_gains_fraction = 0.4
bracket_fill_target = 60_000
annual_fee = 0.002

[allocation]
mode = "constant"
sourcing = "single"
weights = { us_total_market = 0.5, us_small_value = 0.1, it_treasury = 0.4 }
real_return = { us_total_market = 0.05 }
volatility = { it_treasury = 0.06 }
correlation = 0.1
correlations = [[1.0, 0.8, 0.1], [0.8, 1.0, 0.05], [0.1, 0.05, 1.0]]

[spending]
annual = 70_000
rule = "guardrails"
floor = 60_000
ceiling = 100_000
guard_band = 0.25
guard_adjust = 0.05
guard_clamp_base = true
slow_go_start_age = 76
no_go_start_age = 86
slow_go_multiplier = 0.9
no_go_multiplier = 0.8
amortization_real_return = 0.015
real_drift = 0.001

[assumptions]
inflation = 0.03
pension_real_drift = 0.002

[survivor]
decedent = "a"
age = 85
spending_fraction = 0.7

[simulation]
trials = 777
seed = 9
distribution = "student_t"
t_df = 6
stress = ["gfc_2008"]
"""

GLIDE = MINIMAL.replace("[spending]", """[allocation]
mode = "glidepath"
phases = [
  { us_total_market = 0.4, us_agg_bonds = 0.6 },
  { us_total_market = 0.7, us_agg_bonds = 0.3 },
]
real_return = { us_agg_bonds = 0.015 }

[spending]""")

BUCKETS = MINIMAL.replace("[spending]", """[allocation]
sourcing = "buckets"
[buckets]
n1 = 3
n2 = 5
cash = { st_treasury = 1.0 }
growth = { us_total_market = 0.8, us_small_value = 0.2 }

[spending]""")


def _error(text: str) -> str:
    with pytest.raises(P.PlanError) as exc:
        P.loads(text)
    return str(exc.value)


# ---------------------------------------------------------------------------
# Loading and the round trip
# ---------------------------------------------------------------------------


def test_default_couple_plan_reproduces_the_canary():
    s = P.loads(DEFAULT_COUPLE).scenario
    assert simulate(s).final_portfolio == DET_FINAL_CANARY


@pytest.mark.parametrize("text", [DEFAULT_COUPLE, MINIMAL, MAXIMAL, GLIDE, BUCKETS],
                         ids=["default", "minimal", "maximal", "glidepath", "buckets"])
def test_round_trip(text):
    plan = P.loads(text)
    again = P.loads(P.dump(plan))
    assert again == plan
    # dump is stable: writing the reloaded plan gives the same text.
    assert P.dump(again) == P.dump(plan)


@pytest.mark.parametrize("path", EXAMPLES + [STARTER], ids=lambda p: p.stem)
def test_examples_load_and_round_trip(path):
    plan = P.read(path)
    assert P.loads(P.dump(plan)) == plan
    assert P.load(path) == plan.scenario


def test_six_examples_ship():
    assert {p.stem for p in EXAMPLES} == {
        "deferred-federal-couple", "mra10-single", "leanfire-single",
        "ss-only-couple-age-gap", "part-time-bridge", "rental-and-pension",
    }


def test_maximal_plan_values_land_on_the_scenario():
    plan = P.loads(MAXIMAL)
    s = plan.scenario
    assert (plan.name, plan.trials, plan.seed, plan.stress) == ("Everything", 777, 9, ("gfc_2008",))
    assert (s.start_age, s.end_age) == (52, 100)   # Rowan's 97 is Quinn's 100
    assert s.people[1] == rm.Person("b", 49, 97, "Rowan")
    assert s.income_streams[0].cola_rate == 0.01 and s.income_streams[0].end_age == 90
    assert s.cash_events[0].owner == "household" and s.cash_events[0].basis_fraction == 0.5
    assert s.spending_adjustments[0].survivor_fraction == 0.5
    assert (s.taxable_balance, s.tax_deferred_balance, s.roth_balance) == (500_000, 800_000, 200_000)
    assert (s.taxable_gains_fraction, s.bracket_fill_target, s.annual_fee) == (0.4, 60_000, 0.002)
    total = {ac.id: ac for ac in s.allocation}
    assert total["us_total_market"].real_return == 0.05
    assert total["it_treasury"].volatility == 0.06
    assert s.correlations[0][1] == 0.8 and s.correlation == 0.1
    assert (s.spending_mode, s.spending_floor, s.spending_ceiling) == ("guardrails", 60_000, 100_000)
    assert (s.guard_band, s.guard_adjust, s.guard_clamp_base) == (0.25, 0.05, True)
    assert (s.slow_go_start_age, s.no_go_start_age) == (76, 86)
    assert (s.slow_go_multiplier, s.no_go_multiplier) == (0.9, 0.8)
    assert (s.amortization_real_return, s.spending_real_drift) == (0.015, 0.001)
    assert (s.assumed_inflation, s.pension_real_drift) == (0.03, 0.002)
    assert (s.survivor_decedent, s.survivor_age, s.survivor_spending_fraction) == ("a", 85, 0.7)
    assert (s.mc_distribution, s.mc_t_df) == ("student_t", 6.0)


def test_defaults():
    s = P.loads(MINIMAL).scenario
    assert [(ac.id, ac.weight, ac.real_return) for ac in s.allocation] == [
        ("us_total_market", 0.6, 0.07), ("us_agg_bonds", 0.4, 0.02)]
    assert s.allocation == rm.default_scenario().allocation
    assert (s.spending_floor, s.spending_ceiling) == (0.8 * 40_000, 1.5 * 40_000)
    assert s.income_streams == () and s.survivor_age is None
    assert P.loads(MINIMAL).trials == 10_000


def test_split_without_balance_sums_the_accounts():
    text = MINIMAL.replace("balance = 1_000_000", "taxable = 300_000\nroth = 100_000")
    assert P.loads(text).scenario.initial_portfolio == 400_000


def test_glidepath_and_buckets_build():
    g = P.loads(GLIDE).scenario
    assert g.allocation_mode == "glidepath" and len(g.phase_allocations) == 2
    assert g.allocation == g.phase_allocations[0]
    assert all(ac.real_return == 0.015 for ph in g.phase_allocations for ac in ph
               if ac.id == "us_agg_bonds")
    b = P.loads(BUCKETS).scenario
    assert (b.buckets.n1, b.buckets.n2) == (3, 5)
    assert b.buckets.income == rm.default_bucket_config().income
    assert [ac.id for ac in b.buckets.growth.allocation] == ["us_total_market", "us_small_value"]


def test_dump_of_a_legacy_scenario_runs_the_same_plan():
    text = P.dump(rm.default_scenario())
    s = P.loads(text).scenario
    assert s.people is not None and len(s.income_streams) == 4
    assert simulate(s).final_portfolio == pytest.approx(
        simulate(rm.default_scenario()).final_portfolio, rel=1e-6)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_unknown_key_names_it_and_suggests():
    msg = _error(MINIMAL.replace("annual = 40_000", "annual = 40_000\nflor = 30_000"))
    assert "unknown key 'spending.flor'" in msg and "did you mean 'floor'" in msg


def test_unknown_top_level_library_name_points_at_plan_key():
    msg = _error("annual_spending = 5\n" + MINIMAL)
    assert "unknown key 'annual_spending'" in msg and "spending.annual" in msg


def test_unknown_key_in_a_stream_names_the_stream():
    text = DEFAULT_COUPLE.replace('id = "a_ss"', 'id = "a_ss"\nclaim_age = 67')
    assert "unknown key 'income_streams['a_ss'].claim_age'" in _error(text)


def test_misspelt_required_key():
    msg = _error(MINIMAL.replace("annual = 40_000", "anual = 40_000"))
    assert "missing required key 'spending.annual'" in msg and "anual" in msg


def test_bad_value_type():
    msg = _error(MINIMAL.replace("balance = 1_000_000", 'balance = "a lot"'))
    assert "'portfolio.balance' must be a number" in msg and "'a lot'" in msg


def test_bad_choice_suggests():
    msg = _error(MINIMAL.replace("annual = 40_000", 'annual = 40_000\nrule = "guardrail"'))
    assert "'spending.rule' must be one of" in msg and "did you mean 'guardrails'" in msg


def test_percent_weights_get_a_hint():
    msg = _error(MINIMAL.replace("[spending]",
                                 "[allocation]\nweights = { us_total_market = 60, us_agg_bonds = 40 }\n[spending]"))
    assert "fractions" in msg


def test_unknown_class_suggests():
    msg = _error(MINIMAL.replace("[spending]",
                                 "[allocation]\nweights = { us_total_markt = 1.0 }\n[spending]"))
    assert "unknown asset class 'us_total_markt'" in msg and "us_total_market" in msg


def test_library_validation_errors_come_through():
    msg = _error(MINIMAL + '\n[survivor]\ndecedent = "a"\nage = 80\n')
    assert "invalid plan" in msg and "two-person" in msg


def test_buckets_table_without_bucket_sourcing_is_an_error():
    assert "sourcing" in _error(BUCKETS.replace('sourcing = "buckets"', ""))


def test_unknown_stress_id():
    msg = _error(MINIMAL + '\n[simulation]\nstress = ["gfc_2009"]\n')
    assert "gfc_2009" in msg and "gfc_2008" in msg


def test_toml_syntax_error_names_the_source():
    with pytest.raises(P.PlanError, match="my.toml: not valid TOML"):
        P.loads("[[people]\n", source="my.toml")


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_apply_override_by_id_person_index_and_alias():
    plan = P.loads(DEFAULT_COUPLE, overrides=[
        ("income_streams.a_ss.start_age", 70),
        ("people.b.end_age", 97),
        ("income_streams.1.amount", 10_000),
        ("annual_spending", 90_000),
        ("survivor.decedent", "b"),
        ("survivor.age", 80),
    ])
    s = plan.scenario
    assert s.income_streams[2].start_age == 70
    assert s.end_age == 97 and s.income_streams[1].amount == 10_000
    assert s.annual_spending == 90_000 and s.survivor_age == 80


def test_apply_override_missing_item_lists_ids():
    with pytest.raises(P.PlanError, match="no item 'a_sss'.*did you mean 'a_ss'"):
        P.loads(DEFAULT_COUPLE, overrides=[("income_streams.a_sss.start_age", 70)])


def test_parse_value():
    assert P.parse_value("60000") == 60000
    assert P.parse_value("0.5") == 0.5
    assert P.parse_value("true") is True
    assert P.parse_value("guardrails") == "guardrails"
    assert P.parse_value("{a = 1}") == {"a": 1}


# ---------------------------------------------------------------------------
# The schema doc covers every key
# ---------------------------------------------------------------------------


def test_docs_mention_every_key():
    keys: set = set()
    for text in (MAXIMAL, GLIDE, BUCKETS):
        P.from_document(P._parse_toml(text, "x"), key_log=keys)
    doc = (ROOT / "docs" / "plan-files.md").read_text()
    sections = {m.group(1): m.end() for m in re.finditer(r"^## `\[{1,2}([\w.]+)\]{1,2}`", doc, re.M)}
    missing = []
    for key in sorted(keys):
        section, _, leaf = key.rpartition(".")
        if not section and leaf in sections:
            continue   # a table, documented by its own heading
        start = sections.get(section, 0) if section else 0
        end = min([v for v in sections.values() if v > start] + [len(doc)])
        if f"| `{leaf}` |" not in doc[start:end]:
            missing.append(key)
    assert not missing, f"docs/plan-files.md does not document: {missing}"
