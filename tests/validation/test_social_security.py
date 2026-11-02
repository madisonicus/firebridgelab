"""Social Security: claim-age factors and the survivor benefit, against 20 CFR 404.

Sources (eCFR, current as of 2026-10-29, read 2026-11-02; www.ssa.gov blocks
fetches from the box this was written on, so the regulation is the source;
SSA's POMS on secure.ssa.gov was readable):

- 20 CFR 404.409: full retirement age is 67 for people born 1/2/1960 or later.
  https://www.ecfr.gov/current/title-20/part-404/section-404.409
- 20 CFR 404.410(a): reduction of 5/9 of 1% for each of the first 36 months
  before FRA and 5/12 of 1% for each further month.
  https://www.ecfr.gov/current/title-20/part-404/section-404.410
- 20 CFR 404.313(b)(2): a delayed retirement credit of 2/3 of 1% a month for
  people born after 1/1/1943, from FRA to 70.
  https://www.ecfr.gov/current/title-20/part-404/section-404.313
- 20 CFR 404.338: the widow(er)'s benefit equals the deceased's PIA (a), plus
  the deceased's delayed credits (b; 404.313(e)(1): credits up to the month
  of death), and if the deceased claimed early, the larger of the deceased's
  reduced benefit and 82.5% of the PIA (c).
  https://www.ecfr.gov/current/title-20/part-404/section-404.338
- 20 CFR 404.410(c)(1): a widow(er)'s benefit based on age that starts
  before FRA is reduced by PIA x 0.285 x (months before FRA) / (months from
  60 to the month before FRA); 404.335 / 404.410(c): it is payable from 60.
- SSA POMS RS 00615.320 (the "RIB-LIM"): the 404.338(c) limit applies to the
  widow(er)'s benefit after its reduction for age.
  https://secure.ssa.gov/poms.nsf/lnx/0300615320
- 20 CFR 404.407(a): a person entitled to an old-age benefit and another
  benefit has the other benefit reduced by the old-age benefit, so the total
  is the larger of the two.
  https://www.ecfr.gov/current/title-20/part-404/section-404.407

The model works in whole years, so claim ages and deaths are whole ages; the
regulation counts months. At whole ages the two agree exactly.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

import firebridgelab as rm
from firebridgelab import IncomeStream, Person
from firebridgelab.income.social_security import ss_factor


def _regulation_factor(claim_age: int, fra: int = 67) -> Fraction:
    """The claim factor computed from the regulation's text, in exact
    fractions: 404.410(a) before FRA, 404.313(b)(2) after it."""
    months = (claim_age - fra) * 12
    if months < 0:
        early = -months
        first, beyond = min(early, 36), max(0, early - 36)
        return 1 - first * Fraction(5, 9) / 100 - beyond * Fraction(5, 12) / 100
    return 1 + months * Fraction(2, 3) / 100


@pytest.mark.parametrize(
    "claim_age, published",
    [
        (62, Fraction(70, 100)),
        (63, Fraction(75, 100)),
        (64, Fraction(80, 100)),
        (65, Fraction(13, 15)),     # 1 - 24 x 5/9 % = 86.67%
        (66, Fraction(14, 15)),     # 1 - 12 x 5/9 % = 93.33%
        (67, Fraction(1)),
        (68, Fraction(108, 100)),
        (69, Fraction(116, 100)),
        (70, Fraction(124, 100)),
    ],
)
def test_claim_factor_matches_20_cfr(claim_age, published):
    """Claim-age factor for FRA 67 (404.409), from 404.410(a) (reduction) and
    404.313(b)(2) (credits). ``published`` is the regulation's arithmetic
    written out (62: 36 x 5/9 % + 24 x 5/12 % = 30%). Tolerance: 1e-12
    (floating point against exact fractions)."""
    assert _regulation_factor(claim_age) == published
    assert ss_factor(claim_age) == pytest.approx(float(published), abs=1e-12)


def _couple(a_pia, a_claim, b_pia, b_claim, death_age, start=66):
    """A same-age couple, both ``start`` at plan start; A dies at A's
    ``death_age``. Returns a function: age -> household Social Security."""
    plan = rm.with_household(
        replace(rm.default_scenario(), initial_portfolio=1_000_000.0, annual_spending=40_000.0),
        people=[Person("a", start, 95), Person("b", start, 95)],
        income_streams=[
            IncomeStream("a_ss", "social_security", a_pia, a_claim, owner="a"),
            IncomeStream("b_ss", "social_security", b_pia, b_claim, owner="b"),
        ],
    )
    plan = replace(plan, survivor_age=death_age, survivor_decedent="a")
    return lambda age: rm.household_year(plan, age).social_security


# (A's PIA, A's claim age, B's PIA, B's claim age, A's death age,
#  the survivor's benefit by the regulation, the rule that gives it)
SURVIVOR_CASES = [
    (30_000, 67, 10_000, 67, 75, 30_000, "404.338(a): the PIA, larger than B's own"),
    (30_000, 70, 10_000, 67, 75, 37_200, "404.338(b): PIA plus 24% delayed credits"),
    (30_000, 62, 10_000, 67, 75, 24_750, "404.338(c): 82.5% of PIA beats the 70% reduced benefit"),
    (30_000, 64, 10_000, 67, 75, 24_750, "404.338(c): 82.5% beats 80%"),
    (30_000, 65, 10_000, 67, 75, 26_000, "404.338(c): the reduced 86.67% beats 82.5%"),
    (10_000, 67, 30_000, 67, 75, 30_000, "404.407(a): B's own benefit is the larger"),
    (30_000, 70, 10_000, 67, 68, 32_400, "404.313(e)(1): died at 68 before claiming, credits to death"),
    (30_000, 70, 10_000, 67, 67, 30_000, "404.338(a): died at FRA before claiming, the PIA"),
]


@pytest.mark.parametrize("a_pia, a_claim, b_pia, b_claim, death, expected, rule", SURVIVOR_CASES)
def test_survivor_benefit_matches_20_cfr(a_pia, a_claim, b_pia, b_claim, death, expected, rule):
    """The survivor's Social Security in every year from the death, for a
    survivor at or past full retirement age (so the widow(er)'s benefit is not
    reduced for the survivor's age). Expected values are the regulation's
    arithmetic; ``rule`` names the paragraph. Tolerance: 1e-6 dollars."""
    ss = _couple(a_pia, a_claim, b_pia, b_claim, death)
    for age in (death, death + 1, 80, 90):
        assert ss(age) == pytest.approx(expected, abs=1e-6), (rule, age)


def _widow_fraction(start_age: int, fra: int = 67) -> Fraction:
    """404.410(c)(1) written out: the widow(er)'s benefit based on age that
    starts at the survivor's ``start_age`` is the PIA less
    PIA x 0.285 x (months before FRA) / (months from 60 to the month before
    FRA). FRA 67: the period is 84 months. 60 -> 71.5%, 62 -> 79.64%,
    64 -> 87.79%, 67 -> 100%."""
    months_early = 12 * max(0, fra - start_age)
    return 1 - Fraction(285, 1000) * months_early / (12 * (fra - 60))


@pytest.mark.parametrize(
    "survivor_age, published",
    [
        (60, Fraction(715, 1000)),                      # 84 of 84 months: 28.5% off
        (62, 1 - Fraction(285, 1000) * 60 / 84),        # 79.64%
        (64, 1 - Fraction(285, 1000) * 36 / 84),        # 87.79%
        (66, 1 - Fraction(285, 1000) * 12 / 84),        # 95.93%
        (67, Fraction(1)),                              # at FRA: unreduced
    ],
)
def test_widow_reduction_factor_matches_20_cfr(survivor_age, published):
    """404.410(c)(1)'s arithmetic, exact fractions against the model's
    ``widow_age_reduction``. Tolerance 1e-12."""
    from firebridgelab.income.social_security import widow_age_reduction

    assert _widow_fraction(survivor_age) == published
    assert 1 - widow_age_reduction(survivor_age) == pytest.approx(float(published), abs=1e-12)


@pytest.mark.parametrize("death_age", [58, 60, 62, 64, 67])
def test_survivor_below_full_retirement_age(death_age):
    """A same-age couple; A (PIA $30,000, claimed at 67) dies at
    ``death_age``, so B is the same age at the death. B's own PIA is $10,000,
    claimed at 67.

    - 404.335 / 404.410(c): a widow(er)'s benefit based on age starts at 60
      at the earliest, so a survivor widowed at 58 gets nothing at 58 or 59.
    - 404.410(c)(1): taken at the survivor's age x < 67 it is reduced to
      ``_widow_fraction(x)`` of the PIA, for good (the model takes it as soon
      as it is payable: at the death, or at 60).
    - 404.407(a): B gets the larger of that and B's own $10,000 from 67.

    Expected: 58 -> $0 until 60, then $21,450 (71.5%); 60 -> $21,450;
    62 -> $23,892.86; 64 -> $26,335.71; 67 -> $30,000. Tolerance 1e-6 dollars.
    """
    ss = _couple(30_000, 67, 10_000, 67, death_age, start=55)
    start = max(death_age, 60)
    expected = float(30_000 * _widow_fraction(start))
    for age in range(death_age, start):
        assert ss(age) == 0.0, age
    for age in range(start, 96, 1):
        assert ss(age) == pytest.approx(max(expected, 10_000.0 if age >= 67 else 0.0), abs=1e-6), age
    assert {58: 21_450.0, 60: 21_450.0, 62: 23_892.857142857, 64: 26_335.714285714,
            67: 30_000.0}[death_age] == pytest.approx(expected, abs=1e-6)


def test_survivor_own_benefit_beats_reduced_widow_benefit():
    """404.407(a): B's own benefit (PIA $36,000 claimed at 62: 70% = $25,200)
    beats the widow(er)'s benefit B can take at 62 on A's $30,000 PIA
    ($23,892.86), so B keeps the own benefit. Tolerance 1e-6 dollars."""
    ss = _couple(30_000, 67, 36_000, 62, 62, start=55)
    for age in (62, 70, 90):
        assert ss(age) == pytest.approx(25_200.0, abs=1e-6), age


@pytest.mark.parametrize(
    "b_age_at_death, expected, rule",
    [
        (60, 21_450.0, "71.5% of the PIA is below the 82.5% limit"),
        (62, 23_892.857142857, "79.64% of the PIA is below the limit"),
        (66, 24_750.0, "95.93% of the PIA is capped at 82.5% (404.338(c))"),
    ],
)
def test_survivor_reduction_and_early_claim_limit(b_age_at_death, expected, rule):
    """A claimed at 62 ($21,000 on a $30,000 PIA) and dies at 70; B is
    younger. The widow(er)'s benefit is the age-reduced PIA (404.410(c)(1)),
    limited to the larger of A's reduced benefit and 82.5% of the PIA,
    $24,750 (404.338(c); POMS RS 00615.320 applies the limit after the age
    reduction). B's own PIA is $0. Tolerance 1e-6 dollars."""
    b_start = 60 - (70 - b_age_at_death)  # B's age when A is 60
    plan = rm.with_household(
        replace(rm.default_scenario(), initial_portfolio=1_000_000.0, annual_spending=40_000.0),
        people=[Person("a", 60, 95), Person("b", b_start, 95)],
        income_streams=[
            IncomeStream("a_ss", "social_security", 30_000.0, 62, owner="a"),
            IncomeStream("b_ss", "social_security", 0.0, 67, owner="b"),
        ],
    )
    plan = replace(plan, survivor_age=70, survivor_decedent="a")
    for a_age in (70, 75, 85):
        got = rm.household_year(plan, a_age).social_security
        assert got == pytest.approx(expected, abs=1e-6), (rule, a_age)
