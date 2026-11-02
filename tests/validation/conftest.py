"""The validation suite: the model against published references.

Every test here compares a number the model produces with a number a third
party published (a study, a regulation, an IRS worked example) and cites the
source in its docstring. docs/validation.md tabulates the same comparisons.

All tests in this directory carry the ``validation`` marker, so
``pytest -m validation`` runs exactly this suite. Tests that take more than a
few seconds also carry ``slow`` (none do today); CI runs both.
"""

import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/validation/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.validation)
