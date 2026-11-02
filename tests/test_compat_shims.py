"""The pre-0.2 flat module paths still import and re-export the same objects."""

import importlib

import pytest

OLD_MODULES = [
    "_step", "buckets", "class_series", "correlation", "data_build", "federal_tax",
    "federal_tax_core", "fetch", "finance", "historical", "monte_carlo", "paths",
    "scenario", "shiller", "sim_core", "simulate", "solver", "spending", "ss_claim",
    "stress", "tax_location",
]


@pytest.mark.parametrize("name", OLD_MODULES)
def test_old_module_path_reexports_new_objects(name):
    module = importlib.import_module(f"firebridgelab.{name}")
    assert module.__all__
    for attr in module.__all__:
        value = getattr(module, attr)
        owner = getattr(value, "__module__", None)
        if owner and owner.startswith("firebridgelab."):
            assert getattr(importlib.import_module(owner), attr) is value
