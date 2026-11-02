"""spending.lifecycle (split from the flat layout)."""

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
    With the v1 defaults: ≤74 → ×1.00, 75-84 → ×0.85, ≥85 → ×0.75.

    Factored out so the ``guardrails`` strategy can COMPOSE the same activity taper
    with its dynamic nudge (variable-spending design nudge→taper→clamp order: the
    taper is a post-multiplier on the nudged base). ``spending_for_age`` reads the
    SAME factor, so the lifecycle math is unchanged (byte-for-byte — the inline
    branch it replaced computed the identical multiplier).
    """
    if age >= scenario.no_go_start_age:
        return scenario.no_go_multiplier
    if age >= scenario.slow_go_start_age:
        return scenario.slow_go_multiplier
    return 1.0


def spending_for_age(scenario: Scenario, age: int) -> float:
    """Real annual spending at a given age — the pure (path-independent) shapes.

    The deterministic and historical/MC engines call this (via the strategy
    wrapper in firebridgelab.spending) for the ``constant`` and ``lifecycle`` modes, so
    the spending math can't drift between modes (same discipline as the
    midpoint-convention equivalence test). It is a PURE function of age — it does
    NOT serve the path-dependent ``guardrails`` mode (that's GuardrailStrategy).

    - ``constant`` mode → returns ``scenario.annual_spending`` unchanged (today's
      behavior, byte-for-byte).
    - ``lifecycle`` mode → ``annual_spending`` is the GO-GO (peak) level scaled by
      the ``activity_multiplier`` taper. With the v1 defaults: go-go ≤74 → ×1.00,
      slow-go 75-84 → ×0.85, no-go ≥85 → ×0.75.

    Any non-lifecycle mode (including ``guardrails``, whose dollar path comes from
    its own strategy and never flows through here) returns the flat
    ``annual_spending`` — but the engines never call this for guardrail mode.
    """
    if scenario.spending_mode != SPENDING_MODE_LIFECYCLE:
        return scenario.annual_spending
    return scenario.annual_spending * activity_multiplier(scenario, age)
