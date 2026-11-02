"""Deprecated import path: ``firebridgelab.ss_claim``.

Its contents moved to:

- ``firebridgelab.income.social_security``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.income.social_security import (
    DEFAULT_FRA_AGE,
    MIN_CLAIM_AGE,
    MAX_CLAIM_AGE,
    ss_factor,
    effective_ss_amount,
)

__all__ = [
    "DEFAULT_FRA_AGE",
    "MIN_CLAIM_AGE",
    "MAX_CLAIM_AGE",
    "ss_factor",
    "effective_ss_amount",
]
