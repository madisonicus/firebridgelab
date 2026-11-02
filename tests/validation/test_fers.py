"""FERS annuity COLA: 5 U.S.C. 8462 and OPM's statement of the rule.

Sources, read 2026-11-02:

- 5 U.S.C. 8462(b)(1): if the CPI change does not exceed 3%, the annuity is
  increased by the lesser of the CPI change or 2%; above 3%, by the CPI
  change minus 1 percentage point. 8462(c)(3)(A): an increase is not
  effective for an annuitant under 62. 8462(c)(3)(B): that applies only to
  annuitants under 8412, 8413 and 8414 (immediate, deferred and early
  retirement), and not to those under 8412(d)(1) or (e) (special-provision
  law enforcement officers, firefighters, air traffic controllers) or to
  disability retirees; survivor annuities are outside it. 8462(c)(1): the
  first increase is prorated by months on the rolls.
  https://www.law.cornell.edu/uscode/text/5/8462
- OPM, "How is the cost-of-living adjustment (COLA) determined?": the same
  three tiers, and FERS COLAs "are not provided until age 62".
  https://www.opm.gov/frequently-asked-questions/retire-faq/post-retirement/how-is-the-cost-of-living-adjustment-cola-determined/

A ``fers_diet`` stream holds its nominal check fixed until the owner's 62 and
gets the 8462(b) COLA each year from then on; ``fers_cola_before_62 = true``
gives the COLA from the start for the exempt annuitants. These tests check
both against a hand-built OPM-style schedule.
"""

from __future__ import annotations

from dataclasses import replace
from importlib.resources import files

import pytest

import firebridgelab as rm
from firebridgelab import IncomeStream, Person
from firebridgelab.income.pension import fers_diet_cola
from firebridgelab.plan import load


@pytest.mark.parametrize(
    "cpi, statute",
    [(0.015, 0.015), (0.020, 0.020), (0.025, 0.020), (0.030, 0.020), (0.040, 0.030)],
)
def test_cola_tiers_5_usc_8462(cpi, statute):
    """5 U.S.C. 8462(b)(1) at CPI 1.5/2.0/2.5/3.0/4.0%: COLA 1.5/2.0/2.0/2.0/
    3.0%. Tolerance: 1e-12. (The statute rounds the CPI change to 0.1%;
    these inputs are already round.)"""
    assert fers_diet_cola(cpi) == pytest.approx(statute, abs=1e-12)


def _opm_schedule(check: float, retire_age: int, cpi: float, age: int, cola_age: int = 62) -> float:
    """Hand-built OPM-style schedule in plan-start (retirement-date) dollars.
    The nominal check is fixed from retirement until ``cola_age`` (62: no
    COLA under 62), then grows by the 8462 COLA each year; real value =
    nominal / CPI level."""
    cola = min(cpi, 0.02) if cpi <= 0.03 else cpi - 0.01
    nominal = check * (1.0 + cola) ** max(0, age - cola_age)
    return nominal / (1.0 + cpi) ** (age - retire_age)


@pytest.mark.parametrize("cpi", [0.015, 0.020, 0.025, 0.030, 0.040])
def test_mra10_example_matches_opm_schedule(cpi):
    """The bundled ``mra10-single`` plan (one $22,500 ``fers_diet`` annuity
    from 57) against the hand-built schedule, every age 57-95.
    Tolerance: 1e-6 dollars. The first COLA's proration (8462(c)(1)) is a
    within-year detail the yearly model does not carry; at 62 a regular
    retiree has been on the rolls for years, so it is a full COLA anyway."""
    plan = load(files("firebridgelab") / "examples" / "mra10-single.toml")
    plan = replace(plan, assumed_inflation=cpi)
    for age in range(57, 96):
        got = rm.household_year(plan, age).pension
        assert got == pytest.approx(_opm_schedule(22_500.0, 57, cpi, age), abs=1e-6), age


@pytest.mark.parametrize("cpi", [0.020, 0.025, 0.040])
def test_deferred_annuity_matches_opm_schedule(cpi):
    """A deferred annuity: separated at 50 with a $20,000 annuity computed at
    separation, payable from 62 (5 U.S.C. 8413). The nominal amount is fixed
    until it starts, then gets the 8462 COLA. Modelled as one stream with
    ``nominal_until_start`` and ``fers_diet``; checked against the same
    hand-built schedule at every age 50-95. Tolerance: 1e-6 dollars."""
    plan = rm.with_household(
        replace(rm.default_scenario(), assumed_inflation=cpi),
        people=[Person("a", 50, 95)],
        income_streams=[
            IncomeStream("fers", "pension", 20_000.0, 62, owner="a",
                         nominal_until_start=True, cola="fers_diet"),
        ],
    )
    for age in range(50, 96):
        got = rm.household_year(plan, age).pension
        expected = 0.0 if age < 62 else _opm_schedule(20_000.0, 50, cpi, age)
        assert got == pytest.approx(expected, abs=1e-6), age


def _single(cpi: float, start: int = 57, people=None, owner="a", **stream_kw):
    return rm.with_household(
        replace(rm.default_scenario(), assumed_inflation=cpi),
        people=people or [Person("a", 57, 95)],
        income_streams=[IncomeStream("fers", "pension", 22_500.0, start, owner=owner,
                                     cola="fers_diet", **stream_kw)],
    )


@pytest.mark.parametrize("cpi", [0.015, 0.020, 0.025, 0.030, 0.040])
def test_single_stream_before_62_gets_no_cola_until_62(cpi):
    """8462(c)(3)(A): a regular (8412 or 8414) annuitant under 62 gets no
    COLA. One ``fers_diet`` stream of $22,500 from 57 against the hand-built
    schedule (fixed nominal 57-62, then the 8462(b) COLA each year), every
    age 57-95. At 2.5% CPI the value at 62 is 22,500 / 1.025^5 = $19,886.72
    (it was 1.02^5 = 10.4% higher before 0.2.0, when the stream got COLAs
    from 57). Tolerance: 1e-6 dollars."""
    plan = _single(cpi)
    for age in range(57, 96):
        got = rm.household_year(plan, age).pension
        assert got == pytest.approx(_opm_schedule(22_500.0, 57, cpi, age), abs=1e-6), age
    if cpi == 0.025:
        assert rm.household_year(plan, 62).pension == pytest.approx(22_500.0 / 1.025**5, abs=1e-6)


@pytest.mark.parametrize("cpi", [0.020, 0.025, 0.040])
def test_exempt_annuitant_gets_cola_from_the_start(cpi):
    """8462(c)(3)(B): special-provision (8412(d)(1), (e)) and disability
    retirees get COLAs under 62. ``fers_cola_before_62 = true`` gives the
    8462(b) COLA every year from the start: the schedule with ``cola_age``
    at the start age. Tolerance: 1e-6 dollars."""
    plan = _single(cpi, fers_cola_before_62=True)
    for age in range(57, 96):
        got = rm.household_year(plan, age).pension
        assert got == pytest.approx(_opm_schedule(22_500.0, 57, cpi, age, cola_age=57), abs=1e-6), age


def test_single_stream_equals_the_split_at_62():
    """The single stream is the same annuity as the pre-0.2.0 workaround: a
    ``cola = "none"`` stream from 57 to 62 and a ``nominal_until_start`` +
    ``fers_diet`` stream from 62. Equal at every age. Tolerance: 1e-9."""
    split = rm.with_household(
        replace(rm.default_scenario(), assumed_inflation=0.025),
        people=[Person("a", 57, 95)],
        income_streams=[
            IncomeStream("before", "pension", 22_500.0, 57, owner="a", end_age=62, cola="none"),
            IncomeStream("from62", "pension", 22_500.0, 62, owner="a",
                         nominal_until_start=True, cola="fers_diet"),
        ],
    )
    single = _single(0.025)
    for age in range(57, 96):
        assert rm.household_year(single, age).pension == pytest.approx(
            rm.household_year(split, age).pension, abs=1e-9), age


def test_cola_age_is_the_owners_62():
    """The rule reads the annuitant's own age. Person B is 3 years younger
    than A and retires at B's 57 (A's 60); B's first COLA year is B's 63,
    A's 66. Checked against the schedule on B's ages. Tolerance: 1e-6."""
    plan = _single(0.025, start=57, owner="b",
                   people=[Person("a", 60, 95), Person("b", 57, 92)])
    for a_age in range(60, 96):
        b_age = a_age - 3
        got = rm.household_year(plan, a_age).pension
        assert got == pytest.approx(_opm_schedule(22_500.0, 57, 0.025, b_age), abs=1e-6), a_age
