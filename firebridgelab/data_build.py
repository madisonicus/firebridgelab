"""Deprecated import path: ``firebridgelab.data_build``.

Its contents moved to:

- ``firebridgelab.returns.data_build``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.data_build import (
    CACHE,
    OUT,
    build_st_treasury,
    build_lt_treasury,
    build_us_small_value,
    build_it_treasury,
    build_gold,
    build_us_reit,
    main,
)

__all__ = [
    "CACHE",
    "OUT",
    "build_st_treasury",
    "build_lt_treasury",
    "build_us_small_value",
    "build_it_treasury",
    "build_gold",
    "build_us_reit",
    "main",
]
