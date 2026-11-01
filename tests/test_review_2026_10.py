"""Independent checks for the 2026-10-11 review fixes and new inputs.

Every expected value here is derived by hand (or from a published rule) in the
test itself, not captured from the engine's output.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from firebridgelab import default_scenario, simulate
from firebridgelab._step import max_fundable_spend, net_return, step_year
from firebridgelab.buckets import _step_buckets
from firebridgelab.federal_tax_core import compute_federal_tax
from firebridgelab.monte_carlo import _sample_class_returns, rebalanced_compound_return
from firebridgelab.scenario import (
    ScenarioValidationError,
    fers_diet_cola,
    gi_at_age,
    pension_income_at_age,
    spending_multiplier,
    ss_income_at_age,
    validate_scenario,
)


# ---------- Mid-year timing: GI and spending are symmetric ----------


def test_step_year_credits_gi_at_mid_year_like_spending():
    # P=100, r=10%, GI=10, spend=20: 100 + 10 + 10*1.05 - 20*1.05 = 99.5
    end, depleted = step_year(100.0, 0.10, 10.0, 20.0)
    assert end == pytest.approx(99.5)
    assert not depleted


def test_gi_equal_to_spending_is_a_wash():
    # Income that exactly matches spending leaves the portfolio to compound alone.
    end, _ = step_year(1_000.0, 0.07, 50.0, 50.0)
    assert end == pytest.approx(1_070.0)


def test_max_fundable_spend_lands_on_zero_and_equals_gi_when_broke():
    s = max_fundable_spend(1_000.0, 0.06, 40.0)
    end, _ = step_year(1_000.0, 0.06, 40.0, s)
    assert end == pytest.approx(0.0, abs=1e-9)
    assert max_fundable_spend(0.0, 0.06, 40.0) == pytest.approx(40.0)


# ---------- Fees ----------


def test_net_return_formula():
    assert net_return(0.05, 0.01) == pytest.approx(1.05 * 0.99 - 1)
    assert net_return(0.05, 0.0) == 0.05


def test_fee_lowers_deterministic_first_year_by_the_netted_return():
    s = default_scenario()
    base = simulate(s).points[0]
    fee = simulate(replace(s, annual_fee=0.005)).points[0]
    r = base.growth / base.start_portfolio
    r_net = (1 + r) * (1 - 0.005) - 1
    expected = (
        s.initial_portfolio * (1 + r_net)
        + base.guaranteed_income * (1 + r_net / 2)
        - base.spending * (1 + r_net / 2)
    )
    assert fee.end_portfolio == pytest.approx(expected)


# ---------- Deterministic rebalanced return ----------


def test_rebalanced_return_two_class_hand_formula():
    s = default_scenario()
    (eq, bd) = s.allocation
    w = np.array([eq.weight, bd.weight])
    vol = np.array([eq.volatility, bd.volatility])
    arith = np.array([eq.real_return, bd.real_return]) + vol**2 / 2
    rho = s.correlation
    var = (w[0] * vol[0]) ** 2 + (w[1] * vol[1]) ** 2 + 2 * w[0] * w[1] * vol[0] * vol[1] * rho
    assert rebalanced_compound_return(s) == pytest.approx(w @ arith - var / 2)


def test_rebalanced_return_zero_vol_is_weighted_average():
    s = default_scenario()
    flat = tuple(replace(ac, volatility=0.0) for ac in s.allocation)
    s0 = replace(s, allocation=flat)
    assert rebalanced_compound_return(s0) == pytest.approx(0.6 * 0.07 + 0.4 * 0.02)


# ---------- FERS-style pension options: nominal freeze, diet COLA ----------


@pytest.mark.parametrize(
    "cpi, cola",
    [(0.015, 0.015), (0.02, 0.02), (0.025, 0.02), (0.03, 0.02), (0.04, 0.03)],
)
def test_fers_diet_cola_rule(cpi, cola):
    assert fers_diet_cola(cpi) == pytest.approx(cola)


def test_pension_nominal_until_start_and_diet_cola():
    s = replace(
        default_scenario(),
        pension_nominal_until_start=True,
        pension_cola="fers_diet",
        assumed_inflation=0.025,
    )
    # Each pension is frozen in nominal terms from plan start to its own start
    # age, then indexed by the diet COLA (2% against 2.5% inflation).
    a_frozen = s.person_a_pension_start_age - s.start_age
    b_frozen = s.person_b_pension_start_age - s.start_age
    a_at_start = s.person_a_pension_amount / 1.025**a_frozen
    b_at_start = s.person_b_pension_amount / 1.025**b_frozen
    # Only A's pension is in payment at A's start age.
    assert pension_income_at_age(s, s.person_a_pension_start_age) == pytest.approx(a_at_start)
    # Ten years later: ten COLA years for A, and B has been paid for the years
    # since its own start.
    age = s.person_a_pension_start_age + 10
    b_years = age - s.person_b_pension_start_age
    expected = a_at_start * (1.02 / 1.025) ** 10 + b_at_start * (1.02 / 1.025) ** b_years
    assert pension_income_at_age(s, age) == pytest.approx(expected)
    # Social Security is untouched.
    assert ss_income_at_age(s, 72) == ss_income_at_age(default_scenario(), 72)


def test_default_pension_is_constant_real():
    s = default_scenario()
    # Both pensions are in payment from 65; constant real thereafter.
    assert pension_income_at_age(s, 65) == pension_income_at_age(s, 90)


# ---------- Survivor case ----------


def test_survivor_keeps_larger_ss_and_pension_fraction():
    s0 = default_scenario()
    s = replace(s0, survivor_age=75, survivor_decedent="b", survivor_pension_fraction=0.5)
    assert gi_at_age(s, 74) == gi_at_age(s0, 74)
    expected = (
        s0.person_a_pension_amount
        + 0.5 * s0.person_b_pension_amount
        + max(s0.person_a_ss_amount, s0.person_b_ss_amount)
    )
    assert gi_at_age(s, 75) == pytest.approx(expected)


def test_survivor_spending_multiplier():
    s = replace(default_scenario(), survivor_age=80, survivor_spending_fraction=0.75)
    assert spending_multiplier(s, 79) == 1.0
    assert spending_multiplier(s, 80) == 0.75


def test_survivor_age_must_fall_inside_the_plan():
    with pytest.raises(ScenarioValidationError):
        validate_scenario(replace(default_scenario(), survivor_age=40))


# ---------- Single-filer federal tax (2026, Rev. Proc. 2025-32) ----------


def test_single_filer_tax_hand_computed():
    # $140k ordinary + $60k LTCG, one filer 65+.
    # Deduction 16,100 + 2,050 = 18,150 -> taxable ordinary 121,850.
    ordinary = (
        12_400 * 0.10
        + (50_400 - 12_400) * 0.12
        + (105_700 - 50_400) * 0.22
        + (121_850 - 105_700) * 0.24
    )
    # Gains stack on 121,850 -> all in the 15% band (0% band ends at 49,450).
    ltcg = 60_000 * 0.15
    # NIIT: MAGI 200,000 vs the $200k single threshold -> 0.
    t = compute_federal_tax(140_000, 60_000, n_65_plus=1, single=True)
    assert t.ordinary_tax == pytest.approx(ordinary)
    assert t.ltcg_tax == pytest.approx(ltcg)
    assert t.niit == pytest.approx(0.0)


def test_single_filer_niit_threshold():
    # MAGI 230,000 is 30,000 over the $200k single threshold.
    t = compute_federal_tax(170_000, 60_000, n_65_plus=0, single=True)
    assert t.niit == pytest.approx(0.038 * 30_000)


# ---------- Monte Carlo sampler ----------


def _moments(scenario, n=20_000):
    _, _, returns = _sample_class_returns(scenario, n, 10, seed=7)
    return returns.reshape(-1, returns.shape[-1])


def test_normal_sampler_matches_requested_moments():
    s = default_scenario()
    x = _moments(s)
    for i, ac in enumerate(s.allocation):
        assert x[:, i].mean() == pytest.approx(ac.real_return + ac.volatility**2 / 2, abs=0.003)
        assert x[:, i].std() == pytest.approx(ac.volatility, rel=0.02)


def test_student_t_keeps_moments_and_fattens_tails():
    s = default_scenario()
    t = replace(s, mc_distribution="student_t", mc_t_df=5.0)
    xn, xt = _moments(s), _moments(t)
    eq = 0
    assert xt[:, eq].std() == pytest.approx(s.allocation[eq].volatility, rel=0.04)
    assert np.corrcoef(xt[:, 0], xt[:, 1])[0, 1] == pytest.approx(s.correlation, abs=0.03)

    def kurt(v):
        z = (v - v.mean()) / v.std()
        return (z**4).mean()

    assert kurt(xt[:, eq]) > kurt(xn[:, eq]) + 1.0


def test_sampled_returns_never_below_minus_99_percent():
    s = default_scenario()
    heavy = replace(s, mc_distribution="student_t", mc_t_df=3.0)
    _, _, r = _sample_class_returns(heavy, 5_000, 43, seed=1)
    assert r.min() >= -0.99


# ---------- Buckets: an exhausted bucket drains to exactly zero ----------


def test_exhausted_cash_bucket_drains_to_zero_not_below():
    # Cash 100 @ 10%, no GI, spend 200; income + growth hold 1,000 @ 0%.
    (cash, inc, gro), _sold, depleted, realized = _step_buckets(
        (100.0, 1_000.0, 0.0), (0.10, 0.0, 0.0), 0.0, 200.0, 0.0, 0.0
    )
    assert not depleted
    assert realized == pytest.approx(200.0)
    # Cash (grown to 110) funds 110/1.05 of spend and drains to exactly 0; income
    # (r=0) funds the remaining 200 - 110/1.05. The year-end rebalance only moves
    # money between buckets, so the total is grown balances minus what was drawn.
    from_income = 200.0 - 110.0 / 1.05
    assert cash >= 0.0 and inc >= 0.0 and gro >= 0.0
    assert cash + inc + gro == pytest.approx(110.0 + 1_000.0 - 110.0 - from_income)
