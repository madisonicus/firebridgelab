"""Pure age-based spending shapes and the multipliers the engines apply.

``spending_for_age`` serves the ``constant`` and ``lifecycle`` modes;
``activity_multiplier`` is the go-go/slow-go/no-go taper the dynamic rules
compose with their own adjustment; ``spending_multiplier`` is the
post-strategy cost-of-living drift and survivor scaling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from firebridgelab.income.survivor import is_survivor_year
from firebridgelab.spending.modes import SPENDING_MODE_LIFECYCLE

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario


def spending_multiplier(scenario: Scenario, age: int) -> float:
    """Post-strategy multiplier on a year's spend: cost-of-living drift
    (``spending_real_drift``) times the survivor spending fraction."""
    mult = (1.0 + scenario.spending_real_drift) ** (age - scenario.start_age)
    if is_survivor_year(scenario, age):
        mult *= scenario.survivor_spending_fraction
    return mult


def activity_multiplier(scenario: Scenario, age: int) -> float:
    """The go-go / slow-go / no-go activity taper factor at a given age.

    The pure age→multiplier curve underneath ``lifecycle`` spending: go-go (below
    ``slow_go_start_age``) → 1.00, slow-go (``slow_go_start_age``..``no_go_start_age``-1)
    → ``slow_go_multiplier``, no-go (``no_go_start_age``+) → ``no_go_multiplier``.
    With the defaults: ≤74 → ×1.00, 75-84 → ×0.85, ≥85 → ×0.75.

    Factored out so the dynamic strategies can COMPOSE the same activity taper
    with their own adjustment (nudge→taper→clamp: the taper is a post-multiplier
    on the nudged base). ``spending_for_age`` reads the SAME factor.
    """
    if age >= scenario.no_go_start_age:
        return scenario.no_go_multiplier
    if age >= scenario.slow_go_start_age:
        return scenario.slow_go_multiplier
    return 1.0


def spending_for_age(scenario: Scenario, age: int) -> float:
    """Real annual spending at a given age — the pure (path-independent) shapes.

    The deterministic and historical/MC engines call this (via ``PureAgeStrategy``
    in ``spending.rules.constant``) for the ``constant`` and ``lifecycle`` modes, so
    the spending math can't drift between engines. It is a PURE function of age —
    it does NOT serve the path-dependent modes (those are stateful strategies).

    - ``constant`` mode → returns ``scenario.annual_spending`` unchanged.
    - ``lifecycle`` mode → ``annual_spending`` is the GO-GO (peak) level scaled by
      the ``activity_multiplier`` taper. With the defaults: go-go ≤74 → ×1.00,
      slow-go 75-84 → ×0.85, no-go ≥85 → ×0.75.

    Any non-lifecycle mode (including ``guardrails``, whose dollar path comes from
    its own strategy and never flows through here) returns the flat
    ``annual_spending`` — but the engines never call this for guardrail mode.
    """
    if scenario.spending_mode != SPENDING_MODE_LIFECYCLE:
        return scenario.annual_spending
    return scenario.annual_spending * activity_multiplier(scenario, age)
