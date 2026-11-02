"""Households: one or two people, income streams, cash events, required spending.

Every expected value here is derived by hand in the test (the arithmetic is
written out), never read back from the engine. All households are fictional.
Most tests run at a 0% real return so a year is just ``end = start + income -
spending``; ``_zero_return_check`` asserts that premise.
"""

from dataclasses import replace

import numpy as np
import pytest

from firebridgelab import (
    AssetClass,
    CashEvent,
    IncomeStream,
    Person,
    Scenario,
    ScenarioValidationError,
    SpendingAdjustment,
    default_scenario,
    gi_at_age,
    household_year,
    legacy_income_streams,
    net_income_at_age,
    simulate,
    validate_scenario,
    with_household,
)
from firebridgelab.engine.allocation import blended_real_return_at_age, glidepath_boundaries
from firebridgelab.engine.sim_core import _simulate_cohort
from firebridgelab.income.survivor import is_survivor_year
from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS
from firebridgelab.spending.npv import compute_npv_gaps_by_age
from firebridgelab.spending.rules.guardrails import GuardrailStrategy
from firebridgelab.tax.location_path import simulate_tax_location_deterministic


def _base(**overrides) -> Scenario:
    """A fictional plan with no legacy income, a 0% real return and no spending."""
    fields = dict(
        start_age=50,
        end_age=95,
        initial_portfolio=1_000_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.0),),
        annual_spending=0.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=60,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=67,
    )
    fields.update(overrides)
    return Scenario(**fields)


def _zero_return_check(s: Scenario) -> None:
    assert blended_real_return_at_age(s, s.start_age) == 0.0


# --------------------------------------------------------------------------
# 1. A single-person plan
# --------------------------------------------------------------------------


def _single() -> Scenario:
    return with_household(
        _base(annual_spending=50_000.0),
        people=[Person("a", 60, 90)],
        income_streams=[
            IncomeStream("pension", "pension", 12_000.0, 62),
            IncomeStream("ss", "social_security", 24_000.0, 67),  # FRA amount, claim at FRA
        ],
    )


def test_single_person_horizon_income_and_terminal():
    s = _single()
    validate_scenario(s)
    _zero_return_check(s)
    assert (s.start_age, s.end_age) == (60, 90)

    assert gi_at_age(s, 61) == 0.0
    assert gi_at_age(s, 62) == 12_000.0
    assert gi_at_age(s, 67) == 12_000.0 + 24_000.0

    traj = simulate(s)
    assert len(traj.points) == 31  # ages 60..90
    # 31 years x $50k spent; income: 62-66 (5 years) x $12k, 67-90 (24 years) x $36k.
    expected = 1_000_000.0 - 31 * 50_000.0 + 5 * 12_000.0 + 24 * 36_000.0
    assert expected == 374_000.0
    assert traj.final_portfolio == pytest.approx(expected, abs=1e-6)


def test_single_person_files_single_with_one_senior_deduction():
    s = _single()
    for age in (60, 64, 80):
        assert household_year(s, age).single_filer is True
    assert household_year(s, 64).n_65_plus == 0
    assert household_year(s, 65).n_65_plus == 1
    assert is_survivor_year(s, 89) is False


def test_single_person_rejects_a_survivor_case():
    s = replace(_single(), survivor_age=80, survivor_decedent="a")
    with pytest.raises(ScenarioValidationError, match="two-person household"):
        validate_scenario(s)


def test_single_person_with_legacy_fields_uses_person_a_only():
    s = with_household(
        _base(person_a_pension_amount=10_000.0, person_b_pension_amount=7_000.0,
              person_b_pension_start_age=50),
        people=[Person("a", 50, 90)],
    )
    # Person B's legacy pension does not exist in a one-person household.
    assert gi_at_age(s, 59) == 0.0
    assert gi_at_age(s, 60) == 10_000.0


# --------------------------------------------------------------------------
# 2. A couple with a five-year age gap
# --------------------------------------------------------------------------


def _gap_couple(**overrides) -> Scenario:
    # A is 55, B is 50 (five years younger); both plan to 95, so the plan runs
    # to B's 95 = A's 100.
    return with_household(
        _base(**overrides),
        people=[Person("a", 55, 95), Person("b", 50, 95)],
        income_streams=[
            # B's pension starts at B's 60 = A's 65.
            IncomeStream("b_pension", "pension", 10_000.0, 60, owner="b",
                         survivor_fraction=0.5),
            # A claims SS at 70: 30,000 x (1 + 36 months x 2/3%) = 30,000 x 1.24.
            IncomeStream("a_ss", "social_security", 30_000.0, 70, owner="a"),
            # B claims at FRA 67 = A's 72.
            IncomeStream("b_ss", "social_security", 20_000.0, 67, owner="b"),
        ],
    )


def test_age_gap_horizon_and_stream_starts_on_person_a_axis():
    s = _gap_couple()
    validate_scenario(s)
    assert (s.start_age, s.end_age) == (55, 100)
    assert gi_at_age(s, 64) == 0.0
    assert gi_at_age(s, 65) == 10_000.0
    assert gi_at_age(s, 69) == 10_000.0
    assert gi_at_age(s, 70) == pytest.approx(10_000.0 + 37_200.0)
    assert gi_at_age(s, 71) == pytest.approx(47_200.0)
    assert gi_at_age(s, 72) == pytest.approx(10_000.0 + 37_200.0 + 20_000.0)
    # Glidepath inflections are the A-axis start ages (plus the 401k age 60).
    assert glidepath_boundaries(s) == [55, 60, 65, 70, 72]


def test_age_gap_senior_deduction_counts_each_person_on_their_own_age():
    s = _gap_couple()
    assert household_year(s, 64).n_65_plus == 0   # A 64, B 59
    assert household_year(s, 65).n_65_plus == 1   # A 65, B 60
    assert household_year(s, 69).n_65_plus == 1   # A 69, B 64
    assert household_year(s, 70).n_65_plus == 2   # A 70, B 65
    assert household_year(s, 70).single_filer is False


def test_age_gap_survivor_timing_when_the_younger_person_dies():
    # B dies at B's own age 70 = A's 75.
    s = replace(_gap_couple(), survivor_age=70, survivor_decedent="b")
    validate_scenario(s)
    assert is_survivor_year(s, 74) is False
    assert is_survivor_year(s, 75) is True
    # From 75: B's pension at its 0.5 survivor fraction; SS = the larger of
    # A's 37,200 and B's 20,000.
    assert gi_at_age(s, 74) == pytest.approx(10_000.0 + 37_200.0 + 20_000.0)
    assert gi_at_age(s, 75) == pytest.approx(5_000.0 + 37_200.0)
    hy = household_year(s, 75)
    assert hy.single_filer is True
    assert hy.n_65_plus == 1  # the survivor, A, is 75


def test_age_gap_survivor_timing_when_the_older_person_dies():
    # A dies at A's own age 80 (the A axis, so A-age 80). B (75) survives.
    s = replace(_gap_couple(), survivor_age=80, survivor_decedent="a")
    assert is_survivor_year(s, 79) is False
    assert is_survivor_year(s, 80) is True
    # B's own pension continues in full; SS = max(37,200, 20,000).
    assert gi_at_age(s, 80) == pytest.approx(10_000.0 + 37_200.0)
    assert household_year(s, 80).n_65_plus == 1  # B is 75


def test_age_gap_survivor_age_outside_the_plan_is_rejected():
    s = replace(_gap_couple(), survivor_age=50, survivor_decedent="b")  # B's 50 = plan start
    with pytest.raises(ScenarioValidationError, match="survivor_age"):
        validate_scenario(s)


def test_people_and_horizon_must_agree():
    s = replace(_gap_couple(), end_age=95)
    with pytest.raises(ScenarioValidationError, match="must\\s+match the people"):
        validate_scenario(s)


# --------------------------------------------------------------------------
# 3-6. Stream kinds and COLA rules
# --------------------------------------------------------------------------


def test_part_time_work_income_ends_at_55():
    s = with_household(
        _base(annual_spending=40_000.0),
        people=[Person("a", 50, 60)],
        income_streams=[IncomeStream("work", "work", 30_000.0, 50, end_age=55)],
    )
    assert gi_at_age(s, 54) == 30_000.0
    assert gi_at_age(s, 55) == 0.0
    assert household_year(s, 52).ordinary == 30_000.0  # wages are ordinary income
    # Ages 50..60 = 11 years at $40k; 5 years (50-54) of $30k work income.
    expected = 1_000_000.0 - 11 * 40_000.0 + 5 * 30_000.0
    assert simulate(s).final_portfolio == pytest.approx(expected, abs=1e-6)


def test_rental_income_continues_after_its_owner_dies():
    s = replace(
        with_household(
            _base(),
            people=[Person("a", 60, 90), Person("b", 60, 90)],
            income_streams=[
                IncomeStream("rental", "rental", 18_000.0, 60, owner="a"),
                IncomeStream("a_pension", "pension", 20_000.0, 60, owner="a"),
            ],
        ),
        survivor_age=70,
        survivor_decedent="a",
    )
    assert gi_at_age(s, 69) == 38_000.0
    # Rental's default survivor fraction is 1.0, a pension's is 0.
    assert gi_at_age(s, 70) == 18_000.0


def test_annuity_with_no_cola_erodes_at_assumed_inflation():
    s = with_household(
        _base(assumed_inflation=0.025),
        people=[Person("a", 60, 90)],
        income_streams=[IncomeStream("spia", "annuity", 10_000.0, 60, cola="none")],
    )
    assert gi_at_age(s, 60) == 10_000.0
    assert gi_at_age(s, 61) == pytest.approx(10_000.0 / 1.025)          # 9,756.10
    assert gi_at_age(s, 70) == pytest.approx(10_000.0 / 1.025 ** 10)    # 7,811.98
    assert gi_at_age(s, 70) == pytest.approx(7_811.98, abs=0.01)


def test_pension_with_a_fixed_one_percent_cola():
    stream = IncomeStream("pension", "pension", 20_000.0, 62, cola="fixed", cola_rate=0.01)
    s = with_household(
        _base(assumed_inflation=0.025),
        people=[Person("a", 60, 90)],
        income_streams=[stream],
    )
    assert gi_at_age(s, 62) == 20_000.0
    # Each year the check rises 1% nominal while prices rise 2.5%.
    assert gi_at_age(s, 72) == pytest.approx(20_000.0 * (1.01 / 1.025) ** 10)
    # (1.01 / 1.025) = 0.985366; ln = -0.014742; x10 -> e^-0.14742 = 0.862929.
    assert gi_at_age(s, 72) == pytest.approx(17_258.6, abs=0.1)

    # The same check stated in nominal dollars at commencement (two years out).
    nominal = with_household(
        _base(assumed_inflation=0.025),
        people=[Person("a", 60, 90)],
        income_streams=[replace(stream, nominal_until_start=True)],
    )
    assert gi_at_age(nominal, 62) == pytest.approx(20_000.0 / 1.025 ** 2)


def test_tax_free_stream_counts_as_income_but_not_ordinary():
    s = with_household(
        _base(),
        people=[Person("a", 60, 90)],
        income_streams=[IncomeStream("va", "other", 15_000.0, 60, tax_treatment="tax_free")],
    )
    hy = household_year(s, 60)
    assert (hy.gi, hy.ordinary, hy.social_security) == (15_000.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# 7. A home sale with capital-gain tax
# --------------------------------------------------------------------------


def _home_sale(**overrides) -> Scenario:
    return with_household(
        _base(**overrides),
        people=[Person("a", 65, 90)],
        income_streams=[],
        cash_events=[CashEvent(70, 400_000.0, tax_treatment="capital_gain", basis_fraction=0.5)],
    )


def test_home_sale_inflow_enters_the_pre_tax_projection():
    with_sale = simulate(_home_sale(annual_spending=30_000.0))
    without = simulate(replace(_home_sale(annual_spending=30_000.0), cash_events=()))
    gap = [a.end_portfolio - b.end_portfolio for a, b in zip(with_sale.points, without.points)]
    assert gap[4] == 0.0                     # age 69
    assert gap[5] == pytest.approx(400_000)  # age 70, at a 0% return
    assert gap[-1] == pytest.approx(400_000)
    assert household_year(_home_sale(), 70).event_ltcg == 200_000.0


def test_home_sale_capital_gain_is_taxed_in_the_sale_year():
    # All $1M taxable, no spending, no other income, and a 0 gains fraction so
    # paying the bill from taxable adds no further gain: the year-70 bill is the
    # tax on the sale's gain alone.
    s = _home_sale(
        taxable_balance=1_000_000.0, tax_deferred_balance=0.0, roth_balance=0.0,
        taxable_gains_fraction=0.0,
    )
    path = simulate_tax_location_deterministic(s)
    tax = dict(zip(path.ages, path.tax))
    # Gain = 400,000 x (1 - 0.5) = 200,000. Single filer aged 70: deduction
    # 16,100 + 2,050 = 18,150, all of it against the gain (no ordinary income).
    # Taxable gain 181,850; 0% to 49,450 then 15%: 0.15 x 132,400 = 19,860.
    # NIIT: the $200,000 single threshold is fixed in nominal dollars (IRC
    # 1411(b)), so five years in at 2.5% inflation it is worth
    # 200,000 / 1.025**5 = 200,000 / 1.13141 = 176,770.86 in plan-start dollars.
    # MAGI 200,000 exceeds it by 23,229.14: NIIT = 0.038 x 23,229.14 = 882.71.
    niit = 0.038 * (200_000.0 - 200_000.0 / 1.025**5)
    assert niit == pytest.approx(882.71, abs=0.01)
    assert tax[70] == pytest.approx(19_860.0 + 882.71, abs=0.01)
    assert tax[69] == 0.0 and tax[71] == 0.0
    # The inflow lands in taxable: 1,000,000 + 400,000 - 20,742.71 by year end.
    assert path.taxable[path.ages.index(70)] == pytest.approx(1_379_257.29, abs=0.01)


def test_outflow_event_and_ordinary_event():
    s = with_household(
        _base(),
        people=[Person("a", 60, 90)],
        income_streams=[],
        cash_events=[
            CashEvent(62, -45_000.0, id="car"),
            CashEvent(63, 50_000.0, tax_treatment="ordinary", id="bonus"),
        ],
    )
    traj = simulate(s)
    assert traj.points[2].end_portfolio == pytest.approx(955_000.0)
    assert traj.points[3].end_portfolio == pytest.approx(1_005_000.0)
    assert household_year(s, 63).event_ordinary == 50_000.0
    bad = replace(s, cash_events=(CashEvent(62, -1.0, tax_treatment="ordinary"),))
    with pytest.raises(ScenarioValidationError, match="outflow"):
        validate_scenario(bad)


# --------------------------------------------------------------------------
# 8-9. Required spending adjustments
# --------------------------------------------------------------------------


_PRE_MEDICARE = SpendingAdjustment(12_000.0, 55, 65, id="aca")


def test_pre_medicare_healthcare_adjustment_adds_required_spending():
    plain = with_household(
        _base(annual_spending=20_000.0), people=[Person("a", 55, 90)], income_streams=[]
    )
    s = replace(plain, spending_adjustments=(_PRE_MEDICARE,))
    traj = simulate(s)
    spend = {p.age: p.spending for p in traj.points}
    assert spend[55] == 32_000.0 and spend[64] == 32_000.0
    assert spend[65] == 20_000.0
    # 36 years x 20,000 + 10 x 12,000 = 840,000 of the 1,000,000: no depletion.
    assert traj.final_portfolio == pytest.approx(160_000.0)
    # Ten years (55-64) of $12k extra.
    assert simulate(plain).final_portfolio - traj.final_portfolio == pytest.approx(120_000.0)
    assert net_income_at_age(s, 60) == -12_000.0
    assert net_income_at_age(s, 65) == 0.0


def _guardrail_household() -> Scenario:
    return with_household(
        _base(
            spending_mode=SPENDING_MODE_GUARDRAILS,
            annual_spending=50_000.0,
            spending_floor=40_000.0,
            spending_ceiling=150_000.0,
        ),
        people=[Person("a", 50, 90)],
        income_streams=[],
        spending_adjustments=[SpendingAdjustment(10_000.0, 50, 70, id="mortgage")],
    )


def test_guardrails_anchor_counts_the_adjustment_as_required():
    s = _guardrail_household()
    strat = GuardrailStrategy(s)
    # (50,000 discretionary + 10,000 required - 0 GI) / 1,000,000.
    assert strat.initial_rate == pytest.approx(0.06)
    no_adj = GuardrailStrategy(replace(s, spending_adjustments=()))
    assert no_adj.initial_rate == pytest.approx(0.05)


def test_guardrails_cut_only_discretionary_spending():
    s = _guardrail_household()
    n = s.end_age - s.start_age + 1
    returns = np.zeros(n)
    returns[0] = -0.30
    _ends, _dep, spends = _simulate_cohort(s, returns)
    # Year 0: rate 60,000 / 1,000,000 = 0.06 = the anchor -> hold. Total spend
    # 50,000 + 10,000 required. End = 1,000,000 x 0.7 - 60,000 x (1 - 0.15)
    # = 700,000 - 51,000 = 649,000.
    assert spends[0] == pytest.approx(60_000.0)
    assert _ends[0] == pytest.approx(649_000.0)
    # Year 1: rate 60,000 / 649,000 = 0.0924 > 0.06 x 1.2 = 0.072 -> cut the
    # DISCRETIONARY base 10%: 45,000, plus the 10,000 the rule cannot cut.
    assert spends[1] == pytest.approx(55_000.0)
    # Had the 10,000 been invisible to the rule, the rate would have been
    # 50,000 / 649,000 = 0.077 against an anchor of 0.05 x 1.2 = 0.06: also a
    # cut, but of the whole 60,000. The required part never moves while the
    # portfolio lasts:
    assert all(sp >= 10_000.0 + 40_000.0 for sp in spends[:10])


def test_funded_ratio_gap_includes_required_spending():
    s = replace(_guardrail_household(), end_age=69, people=(Person("a", 50, 69),))
    npv = compute_npv_gaps_by_age(s, target_real=50_000.0, discount_rate=0.0)
    # Last year: gap = 50,000 - (0 GI - 10,000 required) = 60,000.
    assert npv[69] == pytest.approx(60_000.0)
    assert npv[68] == pytest.approx(120_000.0)


# --------------------------------------------------------------------------
# 10. Legacy fields == the equivalent explicit streams
# --------------------------------------------------------------------------


def _explicit_default(scenario: Scenario) -> Scenario:
    # The default household written out by hand: pensions $24k at 60 and $9k
    # at 65 (survivor fraction = the scenario's 0.5), SS FRA $33k claimed at 67
    # and $26k claimed at 70.
    return replace(
        scenario,
        income_streams=(
            IncomeStream("a_pension", "pension", 24_000.0, 60, owner="a",
                         survivor_fraction=0.5),
            IncomeStream("b_pension", "pension", 9_000.0, 65, owner="b",
                         survivor_fraction=0.5),
            IncomeStream("a_ss", "social_security", 33_000.0, 67, owner="a"),
            IncomeStream("b_ss", "social_security", 26_000.0, 70, owner="b"),
        ),
    )


@pytest.mark.parametrize(
    "variant",
    [
        {},
        {"survivor_age": 80, "survivor_decedent": "b"},
        {"survivor_age": 72, "survivor_decedent": "a", "spending_mode": SPENDING_MODE_GUARDRAILS},
        {"taxable_balance": 600_000.0, "tax_deferred_balance": 900_000.0, "roth_balance": 300_000.0},
    ],
)
def test_legacy_fields_match_explicit_streams(variant):
    legacy = replace(default_scenario(), **variant)
    explicit = _explicit_default(legacy)
    for age in range(legacy.start_age, legacy.end_age + 1):
        assert gi_at_age(explicit, age) == gi_at_age(legacy, age)
        assert household_year(explicit, age) == household_year(legacy, age)
    assert simulate(explicit) == simulate(legacy)
    if "taxable_balance" in variant:
        assert simulate_tax_location_deterministic(explicit) == simulate_tax_location_deterministic(legacy)


def test_legacy_fields_match_explicit_people():
    legacy = default_scenario()
    explicit = with_household(
        legacy, people=[Person("a", 50, 95), Person("b", 50, 95)],
        income_streams=legacy_income_streams(legacy),
    )
    assert explicit.start_age == 50 and explicit.end_age == 95
    validate_scenario(explicit)
    assert simulate(explicit).final_portfolio == simulate(legacy).final_portfolio


def test_stream_validation_messages():
    s = _single()
    bad_kind = replace(s, income_streams=(IncomeStream("x", "lottery", 1.0, 60),))
    with pytest.raises(ScenarioValidationError, match="kind must be one of"):
        validate_scenario(bad_kind)
    bad_ss = replace(s, income_streams=(IncomeStream("x", "social_security", 1.0, 60),))
    with pytest.raises(ScenarioValidationError, match="claim age"):
        validate_scenario(bad_ss)
    dup = replace(s, income_streams=(IncomeStream("x", "work", 1.0, 60),) * 2)
    with pytest.raises(ScenarioValidationError, match="duplicate"):
        validate_scenario(dup)
    bad_adj = replace(s, spending_adjustments=(SpendingAdjustment(1.0, 70, 65),))
    with pytest.raises(ScenarioValidationError, match="end_age"):
        validate_scenario(bad_adj)


def test_social_security_haircut_stress_scales_explicit_streams():
    from firebridgelab.analysis.stress import _ss_haircut_23

    s = _single()
    cut = _ss_haircut_23(s)
    # 24,000 x 0.77 = 18,480 of SS; the 12,000 pension is untouched.
    assert gi_at_age(cut, 67) == pytest.approx(12_000.0 + 18_480.0)
    assert gi_at_age(cut, 62) == 12_000.0
