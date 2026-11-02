"""Deprecated import path: ``firebridgelab.stress``.

Its contents moved to:

- ``firebridgelab.analysis.stress``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.analysis.stress import (
    StressNotAvailable,
    STRESS_SCENARIOS,
    STRESS_PERTURBATIONS,
    simulate_stress,
    effective_spending_rails,
)

__all__ = [
    "StressNotAvailable",
    "STRESS_SCENARIOS",
    "STRESS_PERTURBATIONS",
    "simulate_stress",
    "effective_spending_rails",
]
