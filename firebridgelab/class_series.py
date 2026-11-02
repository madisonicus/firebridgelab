"""Deprecated import path: ``firebridgelab.class_series``.

Its contents moved to:

- ``firebridgelab.returns.class_series``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.class_series import (
    CACHE_FILE,
    ALL_NEW_CLASS_SERIES,
    OPTIONAL_CLASS_SERIES,
    ClassSeries,
    parse_class_series,
    load_class_series,
    NEW_CLASS_SERIES,
)

__all__ = [
    "CACHE_FILE",
    "ALL_NEW_CLASS_SERIES",
    "OPTIONAL_CLASS_SERIES",
    "ClassSeries",
    "parse_class_series",
    "load_class_series",
    "NEW_CLASS_SERIES",
]
