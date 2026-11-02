"""Deprecated import path: ``firebridgelab.shiller``.

Its contents moved to:

- ``firebridgelab.returns.shiller``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.shiller import (
    CACHE_FILE,
    ShillerData,
    parse_shiller,
    load_shiller,
)

__all__ = [
    "CACHE_FILE",
    "ShillerData",
    "parse_shiller",
    "load_shiller",
]
