"""Real value of dollar amounts that the law fixes in nominal dollars.

The engine runs in real (plan-start) dollars and treats the 2026 tax tables as
plan-start values. Amounts the law indexes to inflation (the brackets, the
standard deduction, the IRMAA ranges, the wage base) therefore stay constant in
real terms. Amounts the law does NOT index shrink in real terms every year at
``Scenario.assumed_inflation``:

  - the Social Security base amounts, $25,000/$34,000 and $32,000/$44,000
    (IRC 86(c));
  - the net investment income tax thresholds, $200,000/$250,000 (IRC 1411(b));
  - the additional Medicare tax thresholds, $200,000/$250,000 (IRC 3101(b)(2)).

``nominal_scale(scenario, age)`` is the factor those amounts are multiplied by
in the plan year whose person-A age is ``age``: 1.0 in the first plan year.
"""

from __future__ import annotations


def nominal_scale(scenario, age: int) -> float:
    """The real value, in plan-start dollars, of one nominal dollar fixed at
    plan start, ``age - start_age`` years later at ``assumed_inflation``."""
    years = age - scenario.start_age
    if years <= 0:
        return 1.0
    return (1.0 + scenario.assumed_inflation) ** -years
