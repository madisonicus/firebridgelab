"""Deprecated import path: ``firebridgelab.fetch``.

Its contents moved to:

- ``firebridgelab.returns.fetch``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.returns.fetch import (
    USER_AGENT,
    SOURCES,
    TESTFOL_TICKERS,
    OPTIONAL_INSTRUCTIONS,
    SHILLER_PAGE,
    fetch,
    main,
)

__all__ = [
    "USER_AGENT",
    "SOURCES",
    "TESTFOL_TICKERS",
    "OPTIONAL_INSTRUCTIONS",
    "SHILLER_PAGE",
    "fetch",
    "main",
]

if __name__ == "__main__":
    import sys

    sys.exit(main())
