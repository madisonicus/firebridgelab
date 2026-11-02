"""Deprecated import path: ``firebridgelab.finance``.

Its contents moved to:

- ``firebridgelab.spending.npv``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.spending.npv import compute_npv_gaps_by_age, npv_gaps_at_age

__all__ = [
    "compute_npv_gaps_by_age",
    "npv_gaps_at_age",
]
