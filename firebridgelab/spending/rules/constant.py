"""The ``constant`` and ``lifecycle`` spending modes.

Both are pure functions of age, served by ``spending_for_age`` in
``spending.lifecycle``: ``constant`` spends ``annual_spending`` every year;
``lifecycle`` applies the age-anchored go-go/slow-go/no-go phase curve.
Neither looks at the portfolio or carries state.
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.spending.base import SpendingStrategy
from firebridgelab.spending.lifecycle import spending_for_age
from firebridgelab.spending.modes import SPENDING_MODE_CONSTANT, SPENDING_MODE_LIFECYCLE
from firebridgelab.spending.registry import register


class PureAgeStrategy(SpendingStrategy):
    """The ``constant`` / ``lifecycle`` strategy — a thin wrapper over the pure
    ``spending_for_age`` helper.

    Ignores ``begin_year_portfolio`` and ``gi`` and carries NO mutable state, so a
    run under this strategy is numerically identical to calling
    ``spending_for_age`` directly.
    """

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        return spending_for_age(scenario, age)


@register(SPENDING_MODE_CONSTANT, SPENDING_MODE_LIFECYCLE)
def _pure_age_strategy(scenario: Scenario) -> PureAgeStrategy:
    return PureAgeStrategy()
