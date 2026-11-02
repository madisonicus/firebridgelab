"""The canonical guaranteed-income (GI) sum: pensions plus Social Security."""

from __future__ import annotations

from typing import TYPE_CHECKING

from firebridgelab.income.pension import pension_income_at_age
from firebridgelab.income.social_security import ss_income_at_age

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario


def gi_at_age(scenario: Scenario, age: int) -> float:
    """Summed guaranteed income active at ``age`` (the four per-person streams).

    The single canonical GI sum: the engines (deterministic, historical, Monte
    Carlo), the guardrail's ``initial_gi`` anchor, the NPV gap table, the bucket
    forward-gap helpers and the tax split (``pension_income_at_age`` /
    ``ss_income_at_age``) all read these functions rather than re-deriving the
    sum. A stream is active iff ``age >= its start age``. Pension real value
    follows ``_pension_multiplier``; the survivor rules follow
    ``is_survivor_year``. With the pension-inflation and survivor fields at their
    defaults this is the plain four-stream sum.
    """
    return pension_income_at_age(scenario, age) + ss_income_at_age(scenario, age)
