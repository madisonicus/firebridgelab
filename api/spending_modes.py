"""Registry of API-presentation properties for spending modes.

Consolidates the spending-mode metadata that the API needs (valid mode IDs, which
modes the saved-scenario view accepts, which modes compose with bucket sourcing).
This is the single source of truth those three lists used to be scattered across
``api/main.py``.

Engine *capabilities* (``is_solvable``, ``collects_spends``) are deliberately NOT
here — they are predicates on the SpendingStrategy instance (ADR 0006), read via
``make_strategy`` and the typed ``SpendingStrategyNotSolvable`` exception. This
registry holds only API-presentation properties.
"""

from dataclasses import dataclass

from firebridgelab.scenario import (
    SPENDING_MODE_AMORTIZATION,
    SPENDING_MODE_CONSTANT,
    SPENDING_MODE_FR_GUARDRAIL,
    SPENDING_MODE_FR_SMOOTH,
    SPENDING_MODE_FR_TIERS,
    SPENDING_MODE_GUARDRAILS,
    SPENDING_MODE_LIFECYCLE,
)


@dataclass(frozen=True)
class SpendingModeDescriptor:
    mode_id: str                   # matches a SPENDING_MODE_* constant
    composable_with_buckets: bool  # False -> 422 when sourcing_mode == buckets
    save_eligible: bool            # whether the saved-scenario view accepts it
    # is_solvable / collects_spends are NOT here — they are engine predicates
    # on the strategy instance (ADR 0006), read via make_strategy / the typed
    # SpendingStrategyNotSolvable exception. Do not duplicate them.


_SPENDING_MODE_REGISTRY: tuple[SpendingModeDescriptor, ...] = (
    SpendingModeDescriptor(SPENDING_MODE_CONSTANT,     composable_with_buckets=True,  save_eligible=True),
    SpendingModeDescriptor(SPENDING_MODE_LIFECYCLE,    composable_with_buckets=True,  save_eligible=True),
    SpendingModeDescriptor(SPENDING_MODE_GUARDRAILS,   composable_with_buckets=False, save_eligible=True),
    SpendingModeDescriptor(SPENDING_MODE_FR_SMOOTH,    composable_with_buckets=False, save_eligible=True),
    SpendingModeDescriptor(SPENDING_MODE_FR_GUARDRAIL, composable_with_buckets=False, save_eligible=True),
    SpendingModeDescriptor(SPENDING_MODE_FR_TIERS,     composable_with_buckets=False, save_eligible=True),
    # The amortization frontier (design decision 8): dynamic / path-dependent like every
    # other non-pure strategy, so NOT composable with buckets; save-eligible.
    SpendingModeDescriptor(SPENDING_MODE_AMORTIZATION, composable_with_buckets=False, save_eligible=True),
)
SPENDING_MODE_IDS: tuple[str, ...] = tuple(d.mode_id for d in _SPENDING_MODE_REGISTRY)
_SPENDING_MODE_BY_ID: dict[str, SpendingModeDescriptor] = {d.mode_id: d for d in _SPENDING_MODE_REGISTRY}
SAVE_VIEW_SPENDING_MODE_IDS: frozenset[str] = frozenset(
    d.mode_id for d in _SPENDING_MODE_REGISTRY if d.save_eligible
)
