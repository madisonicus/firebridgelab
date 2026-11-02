"""Spending rules.

Each rule is a stateful ``SpendingStrategy`` (``spending.base``) built once per
run by ``make_strategy`` (``spending.registry``) from ``Scenario.spending_mode``.
The built-in rules live one per module in ``spending.rules``.

The names below are also importable from ``firebridgelab.spending`` directly,
which was a single module before the package split. They resolve lazily so that
low-level modules (``engine.scenario`` reads ``spending.modes``) can import
from this package without pulling in every rule.
"""

import importlib

_EXPORTS = {
    "AdjustmentEvent": "base",
    "SpendingStrategy": "base",
    "SpendingStrategyNotSolvable": "base",
    "BucketsGuardrailsNotComposable": "base",
    "BucketsNotSolvable": "base",
    "TaxDragMonteCarloNotSolvable": "base",
    "FundedRatioStrategyBase": "funded_ratio",
    "PureAgeStrategy": "rules.constant",
    "GuardrailStrategy": "rules.guardrails",
    "FundedRatioSmoothStrategy": "rules.fr_smooth",
    "FundedRatioGuardrailStrategy": "rules.fr_guardrail",
    "FundedRatioTierStrategy": "rules.fr_tiers",
    "AmortizationFrontierStrategy": "rules.amortization",
    "make_strategy": "registry",
    "register": "registry",
    "registered_modes": "registry",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    try:
        module = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
