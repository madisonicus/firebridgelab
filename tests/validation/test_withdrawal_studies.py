"""Withdrawal-rate studies: Trinity (1998, 2011) and Bengen (1994).

The model replays Shiller's data: S&P Composite real total return and a
10-year Treasury built from the yield with duration 7, January to January,
withdrawals at mid-year (``returns/shiller.py``, ``engine/step.py``). The
studies used Ibbotson calendar-year data with different bonds: long-term
high-grade corporates (Trinity) and intermediate-term Treasuries (Bengen). So
these tests compare on the studies' own start years and check the *direction*
and *size* of the gap, not equality. docs/validation.md has the cell-by-cell
tables and the controls that explain the gap.

The per-cohort arithmetic below is the engine's own step (balance grows by
``r``, the withdrawal is taken at mid-year, ``* (1 + r/2)``), and
``test_closed_form_cohorts_match_the_engine`` ties it to
``simulate_historical`` over every cohort, as notebook 01 does.

Tolerances are counted in cohorts, because a success rate over 41 or 55
overlapping periods moves in steps of one cohort (2.4 or 1.8 points).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

import firebridgelab as rm
from firebridgelab.engine.cohort_windows import cohort_return_sequences

RATES = (0.03, 0.04, 0.05, 0.06)
MIXES = {"100/0": 1.0, "75/25": 0.75, "50/50": 0.5, "25/75": 0.25, "0/100": 0.0}

# Cooley, Hubbard & Walz (1998), "Retirement Savings: Choosing a Withdrawal
# Rate That Is Sustainable", AAII Journal 20(2), February 1998, pp. 16-21,
# Table 3 "Inflation-Adjusted Portfolio Success Rates: 1926 to 1995", 30-year
# rows, 3% / 4% / 5% / 6% columns. 41 overlapping 30-year periods (start
# years 1926-1966). S&P 500 and long-term high-grade corporates, Ibbotson
# SBBI 1996 Yearbook.
TRINITY_1998 = {
    "100/0": (100, 95, 85, 68),
    "75/25": (100, 98, 83, 68),
    "50/50": (100, 95, 76, 51),
    "25/75": (100, 71, 27, 20),
    "0/100": (80, 20, 17, 12),
}
TRINITY_1998_STARTS = (1926, 1966)

# Cooley, Hubbard & Walz (2011), "Portfolio Success Rates: Where to Draw the
# Line", Journal of Financial Planning 24(4), April 2011, pp. 48-60, Table 2
# (withdrawals adjusted for inflation), 30-year rows, 3-6% columns. 55
# overlapping 30-year periods, January 1926 to December 2009 (start years
# 1926-1980). Monthly S&P 500 and high-grade corporate returns, monthly
# rebalancing and month-end withdrawals. The table is an image in the
# journal; these values were read from it.
TRINITY_2011 = {
    "100/0": (100, 98, 80, 62),
    "75/25": (100, 100, 82, 60),
    "50/50": (100, 96, 67, 51),
    "25/75": (100, 80, 31, 22),
    "0/100": (84, 35, 22, 11),
}
TRINITY_2011_STARTS = (1926, 1980)

# Bengen (1994), "Determining Withdrawal Rates Using Historical Data",
# Journal of Financial Planning 7(4), October 1994, pp. 171-180. Ibbotson
# SBBI 1992 Yearbook: large-company stocks and intermediate-term Treasuries,
# start years 1926-1976 (51 "scenario years"), withdrawals at year end.
BENGEN_STARTS = (1926, 1976)


def _plan(equity: float, years: int = 30, rate: float = 0.04):
    base = rm.default_scenario()
    stock, bond = base.allocation
    return replace(
        base,
        start_age=65,
        end_age=64 + years,
        initial_portfolio=1_000_000.0,
        annual_spending=rate * 1_000_000.0,
        allocation=(replace(stock, weight=equity), replace(bond, weight=1.0 - equity)),
        person_a_pension_amount=0.0,
        person_b_pension_amount=0.0,
        person_a_ss_amount=0.0,
        person_b_ss_amount=0.0,
        annual_fee=0.0,
        spending_mode="constant",
    )


def _cohort_swr(returns) -> float:
    """The largest constant real withdrawal (fraction of the starting balance)
    that keeps every year-end balance at or above zero, with the engine's
    mid-year withdrawal: B_k = B_{k-1}(1 + r_k) - s(1 + r_k/2)."""
    growth, annuity, best = 1.0, 0.0, np.inf
    for r in returns:
        growth *= 1.0 + r
        annuity = annuity * (1.0 + r) + (1.0 + r / 2.0)
        best = min(best, growth / annuity)
    return best


def _swr_by_year(equity: float) -> dict[int, float]:
    years, returns = cohort_return_sequences(_plan(equity))
    return {y: _cohort_swr(r) for y, r in zip(years, returns)}


def _yearly_returns(equity: float) -> dict[int, float]:
    years, returns = cohort_return_sequences(_plan(equity, years=1))
    return {y: float(r[0]) for y, r in zip(years, returns)}


def _longevity(series: dict[int, float], start: int, rate: float, cap: int = 50) -> int | None:
    """Years until a constant real withdrawal of ``rate`` exhausts the
    portfolio started in ``start`` (engine step), or None if it lasts ``cap``
    years or to the end of the data."""
    balance = 1.0
    for k in range(cap):
        r = series.get(start + k)
        if r is None:
            return None
        balance = balance * (1.0 + r) - rate * (1.0 + r / 2.0)
        if balance < 0.0:
            return k + 1
    return None


def _successes(swr: dict[int, float], starts: tuple[int, int], rate: float) -> tuple[int, int]:
    sel = [v for y, v in swr.items() if starts[0] <= y <= starts[1]]
    return sum(v >= rate for v in sel), len(sel)


@pytest.fixture(scope="module")
def swr_tables() -> dict[str, dict[int, float]]:
    return {name: _swr_by_year(eq) for name, eq in MIXES.items()}


@pytest.mark.parametrize("name", list(MIXES))
def test_closed_form_cohorts_match_the_engine(name):
    """The per-cohort arithmetic in this module is the engine's: at 4% the
    cohorts it says fail are exactly the ones ``simulate_historical`` depletes
    (all 126 Shiller cohorts, 1871-1996). Tolerance: exact."""
    plan = _plan(MIXES[name])
    swr = _swr_by_year(MIXES[name])
    hist = rm.simulate_historical(plan)
    assert hist.cohort_count == len(swr)
    assert hist.depletion_count == sum(v < 0.04 for v in swr.values())


def _compare(swr_tables, table, starts, n_expected, max_gap):
    rows = []
    for name, published in table.items():
        for rate, pct in zip(RATES, published):
            ok, n = _successes(swr_tables[name], starts, rate)
            assert n == n_expected
            theirs = round(pct * n / 100)
            rows.append((name, rate, ok, theirs, max_gap[MIXES[name] >= 0.5]))
    return rows


def test_trinity_1998_table3_30_years(swr_tables):
    """Cooley, Hubbard & Walz (1998), AAII Journal, Table 3, 30-year rows.
    AAII Journal 20(2), February 1998, Table 3, p. 19. Compared on the same 41
    start years, 1926-1966.

    Tolerance, in cohorts out of 41:
    - direction: we never succeed in more than one cohort more than they do
      (our 10-year Treasury proxy yields less than their long corporates);
    - size: at most 5 cohorts fewer for mixes with 50% stocks or more, at most
      7 for the bond-heavy mixes, where the bond series dominates;
    - the 4% column for 50% stocks or more: within 2 cohorts.
    """
    for name, rate, ours, theirs, gap in _compare(
        swr_tables, TRINITY_1998, TRINITY_1998_STARTS, 41, {True: 5, False: 7}
    ):
        assert ours <= theirs + 1, (name, rate, ours, theirs)
        assert theirs - ours <= gap, (name, rate, ours, theirs)
        if rate == 0.04 and MIXES[name] >= 0.5:
            assert abs(theirs - ours) <= 2, (name, ours, theirs)


def test_trinity_2011_table2_30_years(swr_tables):
    """Cooley, Hubbard & Walz (2011), Journal of Financial Planning, April
    2011, Table 2, 30-year rows. Source:
    https://www.financialplanningassociation.org/article/journal/APR11-portfolio-success-rates-where-draw-line
    Compared on the same 55 start years, 1926-1980.

    Tolerance, in cohorts out of 55: direction as for 1998 (at most one cohort
    more than theirs); size at most 8 fewer for 50% stocks or more and 11 for
    the bond-heavy mixes; the 4% column for 50% stocks or more within 4. The
    2011 study's monthly rebalancing and month-end withdrawals add to the
    data differences of the 1998 comparison, so the bands are wider.
    """
    for name, rate, ours, theirs, gap in _compare(
        swr_tables, TRINITY_2011, TRINITY_2011_STARTS, 55, {True: 8, False: 11}
    ):
        assert ours <= theirs + 1, (name, rate, ours, theirs)
        assert theirs - ours <= gap, (name, rate, ours, theirs)
        if rate == 0.04 and MIXES[name] >= 0.5:
            assert abs(theirs - ours) <= 4, (name, ours, theirs)


def test_bengen_three_percent_lasts_fifty_years():
    """Bengen (1994), Figure 1(a) discussion: a 3% first-year
    withdrawal, then inflation-adjusted, at 50/50 "ensures that portfolio
    longevity is never less than 50 years" for every start year 1926-1976.
    Source: Journal of Financial Planning 7(4), October 1994.
    Tolerance: exact (no start year may run out within 50 years)."""
    series = _yearly_returns(0.5)
    for start in range(BENGEN_STARTS[0], BENGEN_STARTS[1] + 1):
        assert _longevity(series, start, 0.03) is None, start


def test_bengen_worst_case_four_percent():
    """Bengen (1994), Figure 1(b) discussion: at 4% and 50/50, "In no past
    case has it caused a portfolio to be exhausted before 33 years", the
    shortest being the 1966 start; "a 4.25-percent first-year withdrawal could
    exhaust a portfolio in as little as 28 years". So his worst 30-year safe
    rate lies in [4.00%, 4.25%) at 50/50, and at 75/25 he reports the same
    worst case (1966, one year shorter).

    Ours, start years 1926-1976: the worst start is also 1966, and the worst
    30-year safe rate is lower. Tolerance: at most 0.5 percentage point below
    4.00% and never above 4.25%. Most of the gap is data: with FRED
    intermediate Treasuries in place of the 10-year proxy the 1966 rate rises
    by about 0.2 point, and end-of-year withdrawals add about 0.04
    (docs/validation.md). The minimum longevity at 4% is shorter than his 33
    years for the same reason; it must still be at least 20.
    """
    for equity in (0.5, 0.75):
        swr = _swr_by_year(equity)
        window = {y: v for y, v in swr.items() if BENGEN_STARTS[0] <= y <= BENGEN_STARTS[1]}
        worst_year = min(window, key=window.get)
        assert worst_year == 1966
        assert 0.035 <= window[worst_year] < 0.0425, (equity, window[worst_year])

    series = _yearly_returns(0.5)
    lives = {
        y: _longevity(series, y, 0.04)
        for y in range(BENGEN_STARTS[0], BENGEN_STARTS[1] + 1)
    }
    shortest = min((v, y) for y, v in lives.items() if v is not None)
    assert shortest[1] == 1966
    assert 20 <= shortest[0] < 33


def test_bengen_six_percent_odds():
    """Bengen (1994), Figure 1(d) discussion: at 6% and 50/50, for a client
    needing 30 years, "31 scenario years when he would outlive his assets, and
    only 20 which would have been adequate" (of 51 start years, 1926-1976).

    Tolerance: within 4 cohorts. Bengen's Ibbotson data ended in 1992, so his
    start years after 1962 could not be run on 30 actual years; ours run on
    actual data to 2025, and the bond series differ as above.
    """
    series = _yearly_returns(0.5)
    failures = sum(
        1
        for y in range(BENGEN_STARTS[0], BENGEN_STARTS[1] + 1)
        if (life := _longevity(series, y, 0.06)) is not None and life <= 30
    )
    assert abs(failures - 31) <= 4, failures
