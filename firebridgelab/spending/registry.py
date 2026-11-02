"""Spending-rule registry: ``Scenario.spending_mode`` → strategy factory.

A rule registers its factory with ``@register(mode, ...)``; the factory takes
the ``Scenario`` and returns a fresh ``SpendingStrategy``. Every module in
``firebridgelab.spending.rules`` is imported the first time the registry is
read, so a new rule is one new module there (plus, for the web API, an entry
in ``api/spending_modes.py``).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Callable

from firebridgelab.spending.modes import SPENDING_MODE_CONSTANT

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario
    from firebridgelab.spending.base import SpendingStrategy

Factory = Callable[["Scenario"], "SpendingStrategy"]

_FACTORIES: dict[str, Factory] = {}
# The rule an unrecognised mode falls back to (validate_scenario rejects
# unknown modes before a run; the engines themselves never raise on one).
_FALLBACK_MODE = SPENDING_MODE_CONSTANT
_rules_loaded = False


def register(*modes: str) -> Callable[[Factory], Factory]:
    """Class/function decorator: build strategies for ``modes`` with the
    decorated factory."""

    def decorator(factory: Factory) -> Factory:
        for mode in modes:
            if mode in _FACTORIES:
                raise ValueError(f"spending mode {mode!r} is already registered")
            _FACTORIES[mode] = factory
        return factory

    return decorator


def _load_rules() -> None:
    global _rules_loaded
    if _rules_loaded:
        return
    from firebridgelab.spending import rules

    for info in sorted(pkgutil.iter_modules(rules.__path__), key=lambda m: m.name):
        importlib.import_module(f"{rules.__name__}.{info.name}")
    _rules_loaded = True


def registered_modes() -> frozenset[str]:
    """Every ``spending_mode`` value a registered rule handles."""
    _load_rules()
    return frozenset(_FACTORIES)


def make_strategy(scenario: Scenario) -> SpendingStrategy:
    """Build the spending strategy for one run from ``scenario.spending_mode``.

    Call it once per run (the deterministic path, each historical cohort, each
    Monte Carlo trial) before the year loop, so a stateful strategy's anchors
    and carried state belong to that run alone. An unregistered mode gets the
    ``constant`` rule.
    """
    _load_rules()
    factory = _FACTORIES.get(scenario.spending_mode) or _FACTORIES[_FALLBACK_MODE]
    return factory(scenario)
