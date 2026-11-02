"""The old flat module paths still import and re-export the same objects."""

import importlib

import pytest

OLD_MODULES = [
    "_step", "buckets", "class_series", "correlation", "data_build", "federal_tax",
    "federal_tax_core", "fetch", "finance", "historical", "monte_carlo", "paths",
    "scenario", "shiller", "sim_core", "solver", "spending", "ss_claim",
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


def test_simulate_shim_is_removed_and_the_function_is_not_shadowed():
    """0.2.0 removed the ``firebridgelab.simulate`` module. As a submodule it
    replaced the package attribute ``firebridgelab.simulate`` (the public
    function) with the module object the moment anything imported it."""
    import firebridgelab
    from firebridgelab.engine import deterministic

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("firebridgelab.simulate")
    assert firebridgelab.simulate is deterministic.simulate
    assert callable(firebridgelab.simulate)
