"""Deprecated import path: ``firebridgelab.monte_carlo``.

Its contents moved to:

- ``firebridgelab.engine.monte_carlo``
- ``firebridgelab.returns.sampling``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.monte_carlo import (
    DEFAULT_SEED,
    DEFAULT_N_TRIALS,
    SPLIT_PARALLEL_MAX_WORKERS,
    SPLIT_PARALLEL_MIN_TRIALS,
    MonteCarloResult,
    simulate_monte_carlo,
)
from firebridgelab.returns.sampling import (
    MIN_SIMPLE_RETURN,
    CORRELATION_LIMIT,
    EIGENVALUE_FLOOR,
    rebalanced_compound_return,
)

__all__ = [
    "DEFAULT_SEED",
    "MIN_SIMPLE_RETURN",
    "DEFAULT_N_TRIALS",
    "CORRELATION_LIMIT",
    "EIGENVALUE_FLOOR",
    "SPLIT_PARALLEL_MAX_WORKERS",
    "SPLIT_PARALLEL_MIN_TRIALS",
    "MonteCarloResult",
    "rebalanced_compound_return",
    "simulate_monte_carlo",
]
