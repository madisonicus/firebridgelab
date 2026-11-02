"""Shared pytest configuration.

``requires_optional_classes`` marks tests that need the asset classes whose raw
data is not freely redistributable (FTSE Nareit REITs and the testfol.io
simulated series — see DATA-SOURCES.md). When those series are absent from the
staged CSV, the engine runs without them and these tests are skipped. (The
marker is registered in pyproject.toml.)
"""

import pytest

from firebridgelab.returns.class_series import NEW_CLASS_SERIES, OPTIONAL_CLASS_SERIES

_MISSING_OPTIONAL = sorted(OPTIONAL_CLASS_SERIES - set(NEW_CLASS_SERIES))


def pytest_collection_modifyitems(config, items):
    if not _MISSING_OPTIONAL:
        return
    skip = pytest.mark.skip(
        reason=f"optional asset-class data not installed: {', '.join(_MISSING_OPTIONAL)}"
    )
    for item in items:
        if "requires_optional_classes" in item.keywords:
            item.add_marker(skip)
