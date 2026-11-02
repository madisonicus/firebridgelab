"""income.survivor (split from the flat layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario



SURVIVOR_DECEDENTS = ("a", "b")


def is_survivor_year(scenario: Scenario, age: int) -> bool:
    """True once the first death has happened (``survivor_age`` reached)."""
    return scenario.survivor_age is not None and age >= scenario.survivor_age
