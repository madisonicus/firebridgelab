"""The survivor case: whether a given age falls after the first death.

The pension, Social Security, spending and tax rules that change after it live
with their own streams and read ``is_survivor_year``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario



SURVIVOR_DECEDENTS = ("a", "b")


def is_survivor_year(scenario: Scenario, age: int) -> bool:
    """True once the first death has happened (``survivor_age`` reached)."""
    return scenario.survivor_age is not None and age >= scenario.survivor_age
