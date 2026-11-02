"""The canonical guaranteed-income (GI) sum and the household flows around it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from firebridgelab.income.household import (
    cash_event_at_age,
    household,
    net_income_at_age,
    required_spending_at_age,
)

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario

__all__ = [
    "gi_at_age",
    "net_income_at_age",
    "required_spending_at_age",
    "cash_event_at_age",
]


def gi_at_age(scenario: Scenario, age: int) -> float:
    """Summed guaranteed income active at ``age`` (every income stream).

    The single canonical GI sum: the engines (deterministic, historical, Monte
    Carlo), the guardrail's ``initial_gi`` anchor, the NPV gap table, the bucket
    forward-gap helpers and the tax split (``pension_income_at_age`` /
    ``ss_income_at_age``) all read it, and all of it comes from the one resolved
    stream list in ``income.household`` (the legacy person_* fields are converted
    into that list). A stream pays in owner-ages ``[start_age, end_age)``; its
    real value follows its COLA rule and the survivor rules. Spending rules read
    ``net_income_at_age`` (GI minus required spending) instead.
    """
    return household(scenario).year(age).gi
