"""The survivor case: whether a given age falls after the first death.

Person ``survivor_decedent`` dies at their own age ``survivor_age``; the year
axis is person A's age, so the death year is converted with the age offset
(``income.household``). The pension, Social Security, spending and tax rules
that change after it live with their own streams and read ``is_survivor_year``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from firebridgelab.income.household import household

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario


SURVIVOR_DECEDENTS = ("a", "b")


def is_survivor_year(scenario: Scenario, age: int) -> bool:
    """True once the first death has happened (A-age ``age`` at or after the
    decedent's death year). Always False for a one-person household."""
    if scenario.survivor_age is None:
        return False
    return household(scenario).is_survivor_year(age)
