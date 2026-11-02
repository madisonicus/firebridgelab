"""Deprecated import path: ``firebridgelab.solver``.

Its contents moved to:

- ``firebridgelab.analysis.solver``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.analysis.solver import (
    TAX_DRAG_MC_FENCE_MESSAGE,
    MODE_DETERMINISTIC,
    MODE_HISTORICAL,
    MODE_MONTE_CARLO,
    SolverResult,
    evaluate,
    solve_max_spend,
)

__all__ = [
    "TAX_DRAG_MC_FENCE_MESSAGE",
    "MODE_DETERMINISTIC",
    "MODE_HISTORICAL",
    "MODE_MONTE_CARLO",
    "SolverResult",
    "evaluate",
    "solve_max_spend",
]
