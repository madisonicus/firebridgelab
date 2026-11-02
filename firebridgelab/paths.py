"""Deprecated import path: ``firebridgelab.paths``.

Its contents moved to:

- ``firebridgelab.returns.paths``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.paths import (
    ENV_VAR,
    checkout_root,
    user_cache_dir,
    data_dir,
    missing_data_message,
)

__all__ = [
    "ENV_VAR",
    "checkout_root",
    "user_cache_dir",
    "data_dir",
    "missing_data_message",
]
