"""Deprecated import path: ``firebridgelab.correlation``.

Its contents moved to:

- ``firebridgelab.returns.correlation``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.correlation import (
    MIN_OVERLAP,
    DEFAULT_PRIOR,
    PAIR_PRIORS,
    FROBENIUS_TOLERANCE,
    real_return_series_by_series_id,
    pairwise_correlation,
    estimate_correlation_matrix,
    CorrelationNotEstimable,
    estimate_with_gate,
)

__all__ = [
    "MIN_OVERLAP",
    "DEFAULT_PRIOR",
    "PAIR_PRIORS",
    "FROBENIUS_TOLERANCE",
    "real_return_series_by_series_id",
    "pairwise_correlation",
    "estimate_correlation_matrix",
    "CorrelationNotEstimable",
    "estimate_with_gate",
]
