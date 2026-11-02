"""Social Security: claim-age factors and the survivor benefit, against 20 CFR 404.

Sources (eCFR, current as of 2026-10-29, read 2026-11-02; ssa.gov blocks
fetches from the box this was written on, so the regulation is the source):

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


def test_known_gap_survivor_before_full_retirement_age():
    """A simplification, pinned so it stays visible (docs/validation.md).

    20 CFR 404.410(c)(1) reduces a widow(er)'s benefit taken before FRA by
    0.285 x (months before FRA) / (months from 60 to FRA). A survivor widowed
    and claiming at 62 with FRA 67 is 60 months early of 84:
    1 - 0.285 x 60/84 = 79.64% of the PIA, and before 60 there is no benefit
    at all (404.410(c): entitlement based on age begins at 60). The model pays the unreduced benefit at any survivor
    age, so it overstates the survivor's benefit in those years.
    """
    reduced = 1 - 0.285 * 60 / 84
    ss = _couple(30_000, 67, 10_000, 67, 62, start=60)
    model = ss(62)
    regulation = 30_000 * reduced
    assert regulation == pytest.approx(23_892.86, abs=0.01)
    assert model == pytest.approx(30_000.0)
    assert model > regulation
