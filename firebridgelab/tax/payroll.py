"""Payroll tax on work income: employee FICA or self-employment tax.

Read only when ``Scenario.payroll_tax`` is on, for income streams of kind
``work``. Each person's wages and self-employment income are taxed on their
own; the additional Medicare tax uses the household's filing status.

  - Wages (IRC 3101): 6.2% Social Security up to the wage base, 1.45%
    Medicare on all wages (the employee's share; the employer's share is not
    the household's cost).
  - Self-employment (IRC 1401/1402), for streams with ``self_employed``:
    net earnings are 92.35% of the income (IRC 1402(a)(12)); nothing is due
    below $400 of net earnings (IRC 1402(b)(2)); 12.4% Social Security on net
    earnings up to the wage base less the person's wages, 2.9% Medicare on all
    of it. Half the self-employment tax (without the additional Medicare tax)
    is deducted from income (IRC 164(f)).
  - Additional Medicare tax (IRC 3101(b)(2), 1401(b)(2), Form 8959): 0.9% of
    wages over the threshold, and of net self-employment earnings over the
    threshold less wages. The threshold ($250,000 joint, $200,000 otherwise)
    is not indexed and shrinks in real terms (``scale``).

Values: ``tax/data/federal_2026.toml`` table ``[payroll]`` (the 2026 wage base
from SSA's determinations notice). The wage base is indexed to the national
average wage, so the model holds it constant in real terms.
"""

from __future__ import annotations

from typing import NamedTuple

from firebridgelab.tax import params

_P = params.load()["payroll"]

WAGE_BASE: float = _P["social_security_wage_base"]
EMPLOYEE_SS_RATE: float = _P["employee_social_security_rate"]
EMPLOYEE_MEDICARE_RATE: float = _P["employee_medicare_rate"]
SE_SS_RATE: float = _P["se_social_security_rate"]
SE_MEDICARE_RATE: float = _P["se_medicare_rate"]
SE_NET_FACTOR: float = _P["se_net_earnings_factor"]
SE_MINIMUM: float = _P["se_minimum_net_earnings"]
ADDITIONAL_MEDICARE_RATE: float = _P["additional_medicare_rate"]
ADDITIONAL_MEDICARE_THRESHOLD_MFJ: float = _P["additional_medicare_threshold_mfj"]
ADDITIONAL_MEDICARE_THRESHOLD_SINGLE: float = _P["additional_medicare_threshold_single"]


class PayrollYear(NamedTuple):
    """One year's payroll tax. ``tax`` = ``fica + se_tax + additional_medicare``;
    ``se_deduction`` is the half of the self-employment tax deducted from
    income (IRC 164(f))."""

    fica: float
    se_tax: float
    additional_medicare: float
    se_deduction: float

    @property
    def tax(self) -> float:
        return self.fica + self.se_tax + self.additional_medicare


ZERO = PayrollYear(0.0, 0.0, 0.0, 0.0)


def payroll_tax(
    wages: dict[str, float] | tuple,
    self_employment: dict[str, float] | tuple,
    single: bool,
    scale: float = 1.0,
) -> PayrollYear:
    """Payroll tax for one year. ``wages`` / ``self_employment`` map a person
    id to that person's income of each kind (or are ``(id, amount)`` pairs)."""
    wages = dict(wages)
    self_employment = dict(self_employment)
    if not wages and not self_employment:
        return ZERO
    fica = 0.0
    se_tax = 0.0
    total_wages = 0.0
    total_net = 0.0
    for person in set(wages) | set(self_employment):
        w = wages.get(person, 0.0)
        total_wages += w
        fica += EMPLOYEE_SS_RATE * min(w, WAGE_BASE) + EMPLOYEE_MEDICARE_RATE * w
        net = SE_NET_FACTOR * self_employment.get(person, 0.0)
        if net < SE_MINIMUM:
            continue
        total_net += net
        room = WAGE_BASE - w
        se_tax += SE_SS_RATE * min(net, room if room > 0.0 else 0.0) + SE_MEDICARE_RATE * net
    threshold = (
        ADDITIONAL_MEDICARE_THRESHOLD_SINGLE if single else ADDITIONAL_MEDICARE_THRESHOLD_MFJ
    ) * scale
    additional = ADDITIONAL_MEDICARE_RATE * max(0.0, total_wages - threshold)
    additional += ADDITIONAL_MEDICARE_RATE * max(
        0.0, total_net - max(0.0, threshold - total_wages)
    )
    return PayrollYear(fica, se_tax, additional, 0.5 * se_tax)
