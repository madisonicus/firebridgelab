"""Deprecated import path: ``firebridgelab._step``.

Its contents moved to:

- ``firebridgelab.engine.step``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.step import (
    net_return,
    step_year,
    max_fundable_spend,
)

__all__ = [
    "net_return",
    "step_year",
    "max_fundable_spend",
]
