"""Deprecated import path: ``firebridgelab.simulate``.

Its contents moved to:

- ``firebridgelab.engine.deterministic``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.deterministic import (
    YearPoint,
    Trajectory,
    simulate,
)

__all__ = [
    "YearPoint",
    "Trajectory",
    "simulate",
]
