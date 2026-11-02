"""Closed forms: the engine on a constant return against annuity arithmetic.

References are textbook time-value-of-money identities, written out in each
test. The engine's timing convention (``engine/step.py``): the year's balance
grows by ``r`` and the year's withdrawal is taken at mid-year, so it costs
``s * (1 + r/2)`` at year end:

    B_k = B_{k-1} (1 + r) - s (1 + r/2)

Summing the geometric series gives the closed forms below.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import firebridgelab as rm
from firebridgelab.engine.deterministic import simulate
from firebridgelab.engine.allocation import AssetClass
from firebridgelab.engine.scenario import Scenario

P = 1_000_000.0


def _flat(r: float, spend: float, years: int = 30, **kw) -> Scenario:
    """One asset class at a constant real return ``r``, no income, no fee."""
    return Scenario(
        start_age=65, end_age=64 + years, initial_portfolio=P,
        allocation=(AssetClass("all", "All", 1.0, r, 0.0),),
        annual_spending=spend,
        person_a_pension_amount=0.0, person_b_pension_amount=0.0,
        person_a_ss_amount=0.0, person_b_ss_amount=0.0,
        person_a_pension_start_age=60, person_b_pension_start_age=60,
        person_a_ss_start_age=67, person_b_ss_start_age=67,
        **kw,
    )


@pytest.mark.parametrize("r", [0.0, 0.01, 0.03, 0.05])
@pytest.mark.parametrize("spend", [30_000.0, 45_000.0])
def test_future_value_of_constant_withdrawals(r, spend):
    """Final balance = P(1+r)^n - s(1+r/2) * [(1+r)^n - 1]/r (s * n at r = 0):
    the future value of the portfolio less the future value of an annuity of
    mid-year withdrawals. Tolerance: relative 1e-9."""
    n = 30
    g = (1.0 + r) ** n
    annuity = n if r == 0 else (g - 1.0) / r
    expected = P * g - spend * (1.0 + r / 2.0) * annuity
    got = simulate(_flat(r, spend)).final_portfolio
    assert got == pytest.approx(max(expected, 0.0), rel=1e-9, abs=1e-6)


@pytest.mark.parametrize("r", [0.01, 0.03, 0.05])
def test_present_value_of_constant_real_withdrawal(r):
    """The constant real withdrawal whose present value is exactly the
    portfolio: s* = P r (1+r)^n / ((1 + r/2)((1+r)^n - 1)). Run at s*, the
    engine ends at zero; the solver, which rounds down to $1,000, reports
    floor(s*/1000) * 1000. Tolerance: final balance within 1e-6 of zero."""
    n = 30
    g = (1.0 + r) ** n
    s_star = P * r * g / ((1.0 + r / 2.0) * (g - 1.0))
    assert abs(simulate(_flat(r, s_star)).final_portfolio) < 1e-6
    solved = rm.solve_max_spend(_flat(r, 40_000.0), threshold=1.0, mode="deterministic")
    assert solved.solved_spend == (s_star // 1000) * 1000


@pytest.mark.parametrize("r", [0.0, 0.01, 0.03])
def test_amortization_frontier_is_level_mid_year_withdrawal(r):
    """The ``amortization`` rule sets each year's spend to the constant
    withdrawal that runs the current balance to zero over the remaining years
    at the planning rate (the Bogleheads VPW principle), withdrawn at mid-year
    as the engine does. Remaining balance B over m years:
    B = L(1+r/2) * sum_{j=1..m} (1+r)^-j, so L_k = B_k r / ((1+r/2)(1-(1+r)^-m))
    (B/m at r = 0). On a flat return equal to the planning rate the recursion
    B_{k+1} = B_k(1+r) - L_k(1+r/2) leaves L_{k+1} = L_k, so every year's spend
    is the level s* of ``test_present_value_of_constant_real_withdrawal`` and
    the portfolio ends at zero. Tolerance: relative 1e-9 on every year's spend,
    1e-6 on the final balance.

    Fixed in 0.2.0: the rule used to plan with start-of-year (annuity-due)
    payments, so its first-year spend was (1+r/2)/(1+r) of s* (1.5% low at 3%)
    and it drifted up 6.7% over 30 years, leaving money at the end.
    """
    n = 30
    plan = replace(_flat(r, 50_000.0), spending_mode="amortization", amortization_real_return=r)
    traj = simulate(plan)
    balance = P
    for k, point in enumerate(traj.points):
        m = n - k
        expected = balance / m if r == 0 else balance * r / ((1.0 + r / 2.0) * (1.0 - (1.0 + r) ** (-m)))
        assert point.spending == pytest.approx(expected, rel=1e-9), k
        balance = balance * (1.0 + r) - expected * (1.0 + r / 2.0)

    g = (1.0 + r) ** n
    level = P / n if r == 0 else P * r * g / ((1.0 + r / 2.0) * (g - 1.0))
    for point in traj.points:
        assert point.spending == pytest.approx(level, rel=1e-9)
    assert abs(traj.final_portfolio) < 1e-6
