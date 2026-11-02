"""Milestone M4: tax accuracy. Every expected value below is derived by hand in
the comment above it from the rule's primary source (docs/methodology.md)."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from importlib import resources

import pytest
from fastapi.testclient import TestClient

from api.main import app
from firebridgelab import plan as P
from firebridgelab.engine.scenario import default_scenario
from firebridgelab.engine.validation import ScenarioValidationError, validate_scenario
from firebridgelab.income.household import IncomeStream, Person, with_household
from firebridgelab.tax import irmaa as I
from firebridgelab.tax import params
from firebridgelab.tax.location import step_buckets_dragged
from firebridgelab.tax.location_path import simulate_tax_location_deterministic
from firebridgelab.tax.payroll import payroll_tax
from firebridgelab.tax.roth import compare_roth_conversion, target_taxable_income
from firebridgelab.tax.ss_taxation import fill_amount, taxable_social_security
from firebridgelab.tax.state import STATE_CODES, StateYear, state_tax, state_year


# --------------------------------------------------------------------------
# Every data table names its source
# --------------------------------------------------------------------------


def _tables(doc: dict, where: str):
    for key, value in doc.items():
        if isinstance(value, dict):
            yield f"{where}.{key}", value


def test_every_data_table_has_a_source():
    names = params.data_files()
    assert {"federal_2026", "medicare_2026", "states_2026"} <= set(names)
    missing = []
    for name in names:
        text = resources.files("firebridgelab.tax").joinpath(f"data/{name}.toml").read_text()
        doc = tomllib.loads(text)
        if not doc.get("source"):
            missing.append(name)
        for where, table in _tables(doc, name):
            if where == f"{name}.states":
                for code, st in table.items():
                    if not st.get("source"):
                        missing.append(f"{where}.{code}")
                continue
            if not table.get("source"):
                missing.append(where)
    assert not missing, f"data tables without a source: {missing}"


def test_state_list():
    assert STATE_CODES == (
        "AK", "FL", "GA", "IA", "IL", "IN", "LA", "MS", "NC", "NH", "NV", "PA",
        "SD", "TN", "TX", "WY",
    )


# --------------------------------------------------------------------------
# 1. Social Security: the provisional-income formula (IRC 86, Pub. 915)
# --------------------------------------------------------------------------


def test_pub915_worked_example():
    # Pub. 915 (2025), Jamie and Jessie: $10,000 of benefits, $40,500 of other
    # income, joint. Provisional 45,500; line 10 = 13,500; line 11 = 12,000;
    # line 12 = 1,500; line 15 = min(5,000, 6,000) = 5,000; line 16 = 1,275;
    # taxable 6,275 (the IRS figure), below 85% x 10,000 = 8,500.
    assert taxable_social_security(10_000.0, 40_500.0, single=False) == pytest.approx(6_275.0)


def test_below_the_base_nothing_is_taxable():
    # Single: 20,000 / 2 + 14,000 = 24,000 < 25,000.
    assert taxable_social_security(20_000.0, 14_000.0, single=True) == 0.0


def test_fifty_percent_tier():
    # Single, 20,000 benefits, 20,000 other: provisional 30,000; line 10 =
    # 5,000; line 11 = 9,000; taxable = min(10,000, 2,500) = 2,500.
    assert taxable_social_security(20_000.0, 20_000.0, single=True) == pytest.approx(2_500.0)


def test_eighty_five_percent_cap():
    assert taxable_social_security(30_000.0, 500_000.0, single=False) == pytest.approx(25_500.0)


def test_base_amounts_shrink_in_real_terms():
    # scale 0.5: base 12,500, adjusted 17,000. Provisional 30,000; line 10 =
    # 17,500; line 11 = 4,500; line 12 = 13,000; line 15 = 2,250; taxable =
    # 2,250 + 0.85 x 13,000 = 13,300 (< 17,000 cap).
    assert taxable_social_security(20_000.0, 20_000.0, True, scale=0.5) == pytest.approx(13_300.0)


def test_fill_amount_counts_the_benefits_it_makes_taxable():
    # Joint, 30,000 benefits, 10,000 other income. Ordinary income target
    # 50,000. Provisional = 25,000 + c. From c = 19,000 (provisional 44,000)
    # taxable SS = 6,000 + 0.85 (c - 19,000) until the 25,500 cap, so total
    # ordinary = 10,000 + c + 6,000 + 0.85 (c - 19,000) = 1.85 c - 150.
    # 1.85 c - 150 = 50,000 -> c = 27,108.11.
    c = fill_amount(10_000.0, 0.0, 30_000.0, 50_000.0, single=False)
    assert c == pytest.approx(50_150.0 / 1.85, abs=0.01)


@pytest.mark.parametrize("benefits", [0.0, 8_000.0, 30_000.0, 60_000.0])
@pytest.mark.parametrize("other", [0.0, 15_000.0, 40_000.0, 120_000.0])
@pytest.mark.parametrize("target", [20_000.0, 60_000.0, 150_000.0])
@pytest.mark.parametrize("single", [True, False])
def test_fill_amount_hits_the_target(benefits, other, target, single):
    c = fill_amount(other, 5_000.0, benefits, target, single, scale=0.9)
    total = other + c + taxable_social_security(benefits, other + c + 5_000.0, single, 0.9)
    if c == 0.0:
        assert total >= target - 1e-6
    else:
        assert total == pytest.approx(target, abs=1e-6)


# --------------------------------------------------------------------------
# 2. Payroll tax (IRC 3101, 1401, 1402; SSA 2026 wage base $184,500)
# --------------------------------------------------------------------------


def test_fica_on_wages():
    # 6.2% x 100,000 + 1.45% x 100,000 = 7,650.
    assert payroll_tax({"a": 100_000.0}, {}, single=True).tax == pytest.approx(7_650.0)


def test_fica_wage_base_and_additional_medicare():
    # 6.2% x 184,500 = 11,439; 1.45% x 250,000 = 3,625; 0.9% x (250,000 -
    # 200,000) = 450. Total 15,514.
    p = payroll_tax({"a": 250_000.0}, {}, single=True)
    assert p.fica == pytest.approx(15_064.0)
    assert p.additional_medicare == pytest.approx(450.0)
    assert p.tax == pytest.approx(15_514.0)


def test_self_employment_tax_and_half_deduction():
    # Net earnings 92.35% x 100,000 = 92,350. 12.4% = 11,451.40; 2.9% =
    # 2,678.15; SE tax 14,129.55; deduction half = 7,064.775.
    p = payroll_tax({}, {"a": 100_000.0}, single=True)
    assert p.se_tax == pytest.approx(14_129.55)
    assert p.se_deduction == pytest.approx(7_064.775)


def test_self_employment_wage_base_shared_with_wages():
    # Wages 150,000: FICA 9,300 + 2,175 = 11,475. SE net 92,350: Social
    # Security only on the 34,500 left under the base: 4,278; Medicare
    # 2,678.15. Joint threshold 250,000 less wages 150,000 = 100,000 > 92,350:
    # no additional tax. Total 18,431.15.
    p = payroll_tax({"a": 150_000.0}, {"a": 100_000.0}, single=False)
    assert p.tax == pytest.approx(18_431.15)


def test_self_employment_below_400_owes_nothing():
    assert payroll_tax({}, {"a": 400.0}, single=True).tax == 0.0


# --------------------------------------------------------------------------
# 3. IRMAA (CMS 2026; 20 CFR 418.1135, 418.1115)
# --------------------------------------------------------------------------


def test_irmaa_tier_boundaries():
    assert I.monthly_adjustment(109_000.0, True) == (0.0, 0.0)
    assert I.monthly_adjustment(109_000.01, True) == (81.20, 14.50)
    assert I.monthly_adjustment(499_999.0, True) == (446.30, 83.30)
    # "Greater than or equal to $500,000" is the top tier.
    assert I.monthly_adjustment(500_000.0, True) == (487.00, 91.00)
    assert I.monthly_adjustment(218_000.0, False) == (0.0, 0.0)
    # The $500,000 bound is frozen in nominal dollars into 2027: one year in
    # at 2.5% it is 500,000 / 1.025 = 487,804.88 in real terms.
    assert I.monthly_adjustment(490_000.0, True, 1, 0.025) == (487.00, 91.00)
    assert I.monthly_adjustment(487_000.0, True, 1, 0.025) == (446.30, 83.30)


def test_irmaa_annual_couple():
    # Joint MAGI 300,000: tier 3, (202.90 + 37.50) x 12 x 2 people = 5,769.60.
    assert I.annual_surcharge(300_000.0, False, 2) == pytest.approx(5_769.60)


def _single(age=65, **kw):
    base = replace(
        default_scenario(),
        initial_portfolio=1_000_000.0,
        taxable_balance=1_000_000.0,
        tax_deferred_balance=0.0,
        roth_balance=0.0,
        annual_spending=40_000.0,
        **kw,
    )
    return with_household(base, people=[Person("a", age, age + 10)], income_streams=[])


def test_irmaa_is_required_spending_from_prior_magi():
    # Age 65, IRMAA on, MAGI for the first two years given as 150,000 single:
    # tier 3, (202.90 + 37.50) x 12 = 2,884.80 a year, added to spending.
    s = _single(irmaa=True, irmaa_prior_magi=150_000.0)
    path = simulate_tax_location_deterministic(s)
    assert path.irmaa[0] == pytest.approx(2_884.80)
    assert path.irmaa[1] == pytest.approx(2_884.80)
    assert path.spending[0] == pytest.approx(40_000.0 + 2_884.80)
    # From the third year the plan's own MAGI two years back sets the tier.
    for i in range(2, len(path.ages)):
        expected = I.annual_surcharge(path.magi[i - 2], True, 1, i, s.assumed_inflation)
        assert path.irmaa[i] == pytest.approx(expected)
    # Without a prior MAGI the first two years carry no surcharge.
    none = simulate_tax_location_deterministic(replace(s, irmaa_prior_magi=None))
    assert none.irmaa[:2] == [0.0, 0.0]


def test_irmaa_starts_at_medicare_age():
    # Plan starts at 64: no surcharge at 64; at 65 the lookback year (63) is
    # before the plan, so the prior MAGI (top tier) sets it: (487 + 91) x 12.
    s = _single(age=64, irmaa=True, irmaa_prior_magi=1_000_000.0)
    path = simulate_tax_location_deterministic(s)
    assert path.irmaa[0] == 0.0
    assert path.irmaa[1] == pytest.approx(12 * (487.00 + 91.00))


# --------------------------------------------------------------------------
# 4. The fixed point with the extensions (step_buckets_dragged directly)
# --------------------------------------------------------------------------


def _step(**kw):
    # Taxable-only funding with a 0 gains fraction: paying the bill adds no
    # income, so the bill is closed-form.
    args = dict(
        taxable=1_000_000.0, tax_deferred=500_000.0, roth=0.0, r=0.0, gi=0.0,
        spending=0.0, penalty_free=True, gains_fraction=0.0, n_65_plus=0, age=62,
        single=True, bracket_fill_target=None, prior_tax_deferred=500_000.0,
        pension_ss_ordinary=60_000.0,
    )
    args.update(kw)
    pos = [args.pop(k) for k in ("taxable", "tax_deferred", "roth", "r", "gi", "spending",
                                  "penalty_free")]
    return step_buckets_dragged(*pos, **args)


def _custom_state(rate, **kw):
    fields = dict(code="custom", rate=rate, zero_bracket=0.0, from_federal_taxable=False,
                  deduction=0.0, low_agi_extra=0.0, low_agi_limit=0.0,
                  agi_limit=float("inf"), exempt_ss=False, pension_exempt_frac=0.0,
                  ira_exempt_frac=0.0, exclusion=0.0, covers=frozenset(), pension=0.0,
                  other=0.0)
    fields.update(kw)
    return StateYear(**fields)


def test_state_tax_joins_the_bill():
    # Single, 60,000 ordinary: taxable 43,900; federal 1,240 + 12% x 31,500
    # = 5,020. Custom 5% state on AGI 60,000 = 3,000. Bill 8,020.
    res = _step(state=_custom_state(0.05))
    assert res.state_tax == pytest.approx(3_000.0)
    assert res.tax == pytest.approx(8_020.0)
    assert res.spending_tax + res.voluntary_tax + res.forced_tax == res.tax


def test_payroll_tax_is_spending_attributed():
    res = _step(fixed_tax=7_650.0)
    assert res.tax == pytest.approx(5_020.0 + 7_650.0)
    assert res.spending_tax == pytest.approx(5_020.0 + 7_650.0)
    assert res.payroll_tax == 7_650.0


def test_roth_conversion_fills_to_the_bracket_top():
    # Single, 20,000 ordinary, target the top of 12% (50,400 taxable):
    # ordinary 50,400 + 16,100 = 66,500, so 46,500 converts. Tax on 66,500:
    # 1,240 + 12% x 38,000 = 5,800; on 20,000: 10% x 3,900 = 390; the
    # conversion's 5,410 is voluntary.
    res = _step(pension_ss_ordinary=20_000.0,
                conversion_target=target_taxable_income("12%", True))
    assert res.roth_conversion == pytest.approx(46_500.0)
    assert res.tax == pytest.approx(5_800.0)
    assert res.voluntary_tax == pytest.approx(5_410.0)
    assert res.tax_deferred == pytest.approx(500_000.0 - 46_500.0)
    assert res.roth == pytest.approx(46_500.0)
    assert res.lever_transfer == 0.0


def test_roth_conversion_with_provisional_social_security():
    # Joint, 30,000 benefits, 10,000 other ordinary. A 17,800 taxable target
    # is 17,800 + 32,200 = 50,000 ordinary: the fill test above, c = 27,108.11.
    res = _step(single=False, pension_ss_ordinary=10_000.0, ss_benefits=30_000.0,
                conversion_target=17_800.0)
    assert res.roth_conversion == pytest.approx(50_150.0 / 1.85, abs=0.01)
    assert res.ordinary_income == pytest.approx(50_000.0, abs=0.01)


def test_provisional_rule_taxes_low_income_benefits_less():
    # Joint, 40,000 benefits and 10,000 other income: provisional 30,000 <
    # 32,000, nothing taxable and no tax; the flat rule would include 34,000.
    res = _step(single=False, pension_ss_ordinary=10_000.0, ss_benefits=40_000.0)
    assert res.taxable_ss == 0.0
    assert res.tax == 0.0
    flat = _step(single=False, pension_ss_ordinary=10_000.0 + 34_000.0,
                 ss_flat_taxable=34_000.0)
    assert flat.tax > 0.0


# --------------------------------------------------------------------------
# 5. State income tax
# --------------------------------------------------------------------------


def _couple(ages, streams, state, **kw):
    base = replace(
        default_scenario(),
        initial_portfolio=1_000_000.0,
        taxable_balance=500_000.0,
        tax_deferred_balance=500_000.0,
        roth_balance=0.0,
        state=state,
        **kw,
    )
    people = [Person("a", ages[0], 95)] + ([Person("b", ages[1], 95)] if len(ages) > 1 else [])
    return with_household(base, people=people, income_streams=streams)


def test_illinois_exempts_retirement_income():
    # Couple 66/66, pension 30,000, IRA 50,000, taxable SS 20,000, gains
    # 10,000: AGI 110,000. Less SS, pension, IRA: 10,000. Exemptions 2 x
    # 2,925 + 2 x 1,000 (65+) = 7,850. Taxable 2,150 x 4.95% = 106.425.
    s = _couple((66, 66), [IncomeStream("p", "pension", 30_000.0, 60)], "IL")
    sy = state_year(s, 66)
    assert sy.deduction == pytest.approx(7_850.0)
    assert sy.pension == pytest.approx(30_000.0)
    assert state_tax(sy, 110_000.0, 0.0, 20_000.0, 50_000.0, 10_000.0) == pytest.approx(106.425)
    # Above the $500,000 joint AGI limit the exemptions are lost.
    # 600,000 less the 30,000 pension = 570,000 x 4.95% = 28,215.
    assert state_tax(sy, 600_000.0, 0.0, 0.0, 0.0, 0.0) == pytest.approx(28_215.0)


def test_georgia_retirement_exclusion_per_person_by_age():
    # A 66 ($65,000), B 63 ($35,000): exclusion 100,000. Pension 40,000, IRA
    # 60,000, gains 70,000, taxable SS 25,000: AGI 195,000; less SS 170,000;
    # covered 170,000, exclusion 100,000 -> 70,000; less 30,000 deduction =
    # 40,000 x 4.99% = 1,996.
    s = _couple((66, 63), [IncomeStream("p", "pension", 40_000.0, 60)], "GA")
    sy = state_year(s, 66)
    assert sy.exclusion == pytest.approx(100_000.0)
    assert state_tax(sy, 195_000.0, 0.0, 25_000.0, 60_000.0, 70_000.0) == pytest.approx(1_996.0)


def test_mississippi_zero_band_and_early_ira():
    # Single 70: pension 20,000 (exempt), IRA 30,000 (exempt at 60+), gains
    # 40,000, taxable SS 10,000 (exempt): base 40,000; deduction 8,300 +
    # 1,500 (65+) = 9,800 -> 30,200; first 10,000 at 0%: 20,200 x 4% = 808.
    s = _couple((70,), [IncomeStream("p", "pension", 20_000.0, 60)], "MS")
    sy = state_year(s, 70)
    assert state_tax(sy, 100_000.0, 0.0, 10_000.0, 30_000.0, 40_000.0) == pytest.approx(808.0)
    # At 58 the IRA withdrawal is taxable: 70,000 - 8,300 - 10,000 = 51,700 x 4%.
    young = _couple((58,), [IncomeStream("p", "pension", 20_000.0, 50)], "MS")
    sy58 = state_year(young, 58)
    assert state_tax(sy58, 100_000.0, 0.0, 10_000.0, 30_000.0, 40_000.0) == pytest.approx(
        0.04 * 51_700.0)


def test_pennsylvania_ira_exempt_from_60():
    s = _couple((58,), [], "PA")
    # No deductions: 3.07% of the 30,000 withdrawal at 58, nothing at 60.
    assert state_tax(state_year(s, 58), 30_000.0, 0.0, 0.0, 30_000.0, 0.0) == pytest.approx(921.0)
    assert state_tax(state_year(s, 60), 30_000.0, 0.0, 0.0, 30_000.0, 0.0) == 0.0


def test_iowa_starts_from_federal_taxable_income():
    # Single 50 with 40,000 wages: federal taxable 23,900 x 3.8% = 908.20.
    s = _couple((50,), [IncomeStream("w", "work", 40_000.0, 50, end_age=60)], "IA")
    assert state_tax(state_year(s, 50), 40_000.0, 23_900.0, 0.0, 0.0, 0.0) == pytest.approx(908.2)


def test_no_tax_state_and_move():
    s = _couple((60,), [], "PA", state_move_age=65, state_after_move="FL")
    assert state_year(s, 64).code == "PA"
    assert state_year(s, 65) is None


def test_custom_state_in_a_path_charges_more_than_none():
    s = _couple((62,), [IncomeStream("p", "pension", 50_000.0, 60)], None,
                annual_spending=60_000.0)
    base = simulate_tax_location_deterministic(s)
    custom = simulate_tax_location_deterministic(replace(s, state="custom", state_custom_rate=0.05))
    assert all(v == 0.0 for v in base.state_tax)
    assert custom.state_tax[0] > 0.0
    assert sum(custom.tax) > sum(base.tax)
    nostate = simulate_tax_location_deterministic(replace(s, state="TX"))
    assert nostate.tax == base.tax


# --------------------------------------------------------------------------
# Payroll in a path, Roth comparison, validation, plan files
# --------------------------------------------------------------------------


def test_payroll_in_a_path():
    s = _couple((55,), [IncomeStream("w", "work", 100_000.0, 55, end_age=57)], None,
                payroll_tax=True, annual_spending=50_000.0)
    path = simulate_tax_location_deterministic(s)
    assert path.payroll_tax[:3] == pytest.approx([7_650.0, 7_650.0, 0.0])


def test_roth_comparison_reports_with_and_without():
    s = _couple((60,), [IncomeStream("p", "pension", 20_000.0, 60)], None,
                annual_spending=50_000.0, roth_conversion_target="22%",
                roth_conversion_start_age=60, roth_conversion_end_age=70)
    c = compare_roth_conversion(s)
    without = simulate_tax_location_deterministic(replace(s, roth_conversion_target=None))
    assert c.lifetime_tax_without == pytest.approx(sum(without.tax))
    assert c.total_converted > 0.0
    path = simulate_tax_location_deterministic(s)
    assert all(v == 0.0 for v in path.roth_conversion[10:])
    assert c.terminal_after_tax_with == pytest.approx(
        path.taxable[-1] + path.roth[-1] + 0.78 * path.tax_deferred[-1])


def test_options_need_a_tax_split():
    s = replace(default_scenario(), payroll_tax=True)
    with pytest.raises(ScenarioValidationError, match="needs a tax-location split"):
        validate_scenario(s)
    with pytest.raises(ScenarioValidationError, match="state must be one of"):
        validate_scenario(replace(default_scenario(), state="CA"))
    with pytest.raises(ScenarioValidationError, match="roth_conversion_target"):
        validate_scenario(replace(default_scenario(), roth_conversion_target="23%"))


PLAN = """
[[people]]
start_age = 60
end_age = 90

[[income_streams]]
id = "consult"
kind = "work"
amount = 30000
start_age = 60
end_age = 63
self_employed = true

[portfolio]
taxable = 400000
tax_deferred = 600000
roth = 0

[spending]
annual = 50000

[tax]
ss_taxation = "flat85"
payroll = true
irmaa = true
irmaa_prior_magi = 120000

[tax.roth_conversion]
target = "12%"
start_age = 60
end_age = 70

[tax.state]
code = "PA"
move_age = 70
move_to = "FL"
"""


def test_plan_file_tax_table_round_trips():
    s = P.loads(PLAN).scenario
    assert s.ss_taxation == "flat85" and s.payroll_tax and s.irmaa
    assert s.irmaa_prior_magi == 120_000.0
    assert s.roth_conversion_target == "12%" and s.roth_conversion_end_age == 70
    assert (s.state, s.state_move_age, s.state_after_move) == ("PA", 70, "FL")
    assert s.income_streams[0].self_employed
    assert P.loads(P.dump(s)).scenario == s
    dollars = P.loads(PLAN.replace('target = "12%"', "target = 90000")).scenario
    assert dollars.roth_conversion_target == 90_000.0
    assert P.loads(P.dump(dollars)).scenario == dollars


def test_plan_file_rejects_unknown_state():
    with pytest.raises(P.PlanError, match="tax.state.code"):
        P.loads(PLAN.replace('code = "PA"', 'code = "CA"'))


def test_example_plans_default_to_provisional():
    s = P.loads(PLAN.replace('ss_taxation = "flat85"\n', "")).scenario
    assert s.ss_taxation == "provisional"


# --------------------------------------------------------------------------
# API body fields
# --------------------------------------------------------------------------


def _api_client():
    return TestClient(app)


def test_api_accepts_and_echoes_tax_options():
    body = {
        "mode": "deterministic",
        "taxable_balance": 1_080_000.0,
        "tax_deferred_balance": 504_000.0,
        "roth_balance": 216_000.0,
        "state": "NC",
        "irmaa": True,
        "roth_conversion_target": "12%",
        "roth_conversion_start_age": 60,
        "roth_conversion_end_age": 70,
    }
    res = _api_client().post("/api/trajectory", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["scenario"]["state"] == "NC"
    assert out["scenario"]["ss_taxation"] == "provisional"
    ft = out["federal_tax"]
    assert any(v > 0 for v in ft["state_tax"])
    assert any(v > 0 for v in ft["roth_conversion"])
    cmp_ = ft["roth_conversion_comparison"]
    assert cmp_["total_converted"] == pytest.approx(sum(ft["roth_conversion"]))


def test_api_rejects_options_without_a_split():
    res = _api_client().post("/api/trajectory", json={"mode": "deterministic", "state": "PA"})
    assert res.status_code == 422
    assert "tax-location split" in res.text
