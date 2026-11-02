"""FERS annuity COLA: 5 U.S.C. 8462 and OPM's statement of the rule.

Sources, read 2026-11-02:

- 5 U.S.C. 8462(b)(1): if the CPI change does not exceed 3%, the annuity is
  increased by the lesser of the CPI change or 2%; above 3%, by the CPI
  change minus 1 percentage point. 8462(b)(3)(A): no adjustment for an
  annuitant under 62 (exceptions in (b)(3)(B): disability, survivors,
  special-provision retirees). 8462(c)(1): the first increase is prorated by
  months on the rolls. https://www.law.cornell.edu/uscode/text/5/8462
- OPM, "How is the cost-of-living adjustment (COLA) determined?": the same
  three tiers, and FERS COLAs "are not provided until age 62".
  https://www.opm.gov/frequently-asked-questions/retire-faq/post-retirement/how-is-the-cost-of-living-adjustment-cola-determined/

The model applies ``fers_diet`` from a stream's start age. The age-62 rule is
expressed by splitting the annuity into a fixed-nominal stream before 62 and a
``fers_diet`` stream from 62, as the bundled ``mra10-single`` example does;
these tests check that construction against a hand-built OPM-style schedule.
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


def _opm_schedule(check: float, retire_age: int, cpi: float, age: int) -> float:
    """Hand-built OPM-style schedule in plan-start (retirement-date) dollars.
    The nominal check is fixed from retirement until 62 (no COLA under 62),
    then grows by the 8462 COLA each year; real value = nominal / CPI level."""
    cola = min(cpi, 0.02) if cpi <= 0.03 else cpi - 0.01
    nominal = check * (1.0 + cola) ** max(0, age - 62)
    return nominal / (1.0 + cpi) ** (age - retire_age)


@pytest.mark.parametrize("cpi", [0.015, 0.020, 0.025, 0.030, 0.040])
def test_mra10_example_matches_opm_schedule(cpi):
    """The bundled ``mra10-single`` plan (a $22,500 annuity from 57: a
    ``cola = "none"`` stream to 62, then ``nominal_until_start`` +
    ``fers_diet`` from 62) against the hand-built schedule, every age 57-95.
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


def test_known_gap_single_fers_stream_before_62():
    """A limitation, pinned so it stays visible (docs/validation.md). A single
    ``fers_diet`` stream starting before 62 gets the COLA from its start age;
    5 U.S.C. 8462(b)(3)(A) pays none until 62 for a regular retiree. At CPI
    2.5% a $22,500 annuity from 57 is overstated from 58 on, by 1.02^5 - 1 =
    10.4% from 62. Split the stream at 62 (as ``mra10-single`` does) to model
    the rule; special-provision and disability retirees, who do get COLAs
    before 62, are modelled correctly by the single stream."""
    plan = rm.with_household(
        replace(rm.default_scenario(), assumed_inflation=0.025),
        people=[Person("a", 57, 95)],
        income_streams=[IncomeStream("fers", "pension", 22_500.0, 57, owner="a", cola="fers_diet")],
    )
    model = rm.household_year(plan, 62).pension
    rule = _opm_schedule(22_500.0, 57, 0.025, 62)
    assert model / rule == pytest.approx(1.02 ** 5, rel=1e-12)
