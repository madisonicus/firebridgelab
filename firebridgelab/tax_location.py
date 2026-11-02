"""Deprecated import path: ``firebridgelab.tax_location``.

Its contents moved to:

- ``firebridgelab.tax.location``
- ``firebridgelab.tax.location_path``
- ``firebridgelab.tax.location_risk``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.tax.location import (
    TAX_LOCATION_ACCESS_AGE,
    PRE_595_MAX_AGE,
    LEVER_MIN_AGE,
    LEVER_MAX_AGE,
    FIXED_POINT_TOLERANCE,
    FIXED_POINT_MAX_ITERATIONS,
    SPLIT_SUM_TOLERANCE,
    has_tax_split,
    resolve_split,
    DraggedYearStep,
    step_buckets_dragged,
)
from firebridgelab.tax.location_path import (
    TaxLocationPath,
    year_tax_context_table,
    simulate_path_decomposition,
    deterministic_blended_returns,
    simulate_tax_location_deterministic,
    simulate_dragged_depletion_only,
)
from firebridgelab.tax.location_risk import (
    DepletionDistribution,
    TaxLocationRisk,
    aggregate_tax_location_risk,
    EstateCostOfTaxes,
    aggregate_estate_cost,
)

__all__ = [
    "TAX_LOCATION_ACCESS_AGE",
    "PRE_595_MAX_AGE",
    "LEVER_MIN_AGE",
    "LEVER_MAX_AGE",
    "FIXED_POINT_TOLERANCE",
    "FIXED_POINT_MAX_ITERATIONS",
    "SPLIT_SUM_TOLERANCE",
    "has_tax_split",
    "resolve_split",
    "DraggedYearStep",
    "step_buckets_dragged",
    "TaxLocationPath",
    "year_tax_context_table",
    "simulate_path_decomposition",
    "deterministic_blended_returns",
    "simulate_tax_location_deterministic",
    "simulate_dragged_depletion_only",
    "DepletionDistribution",
    "TaxLocationRisk",
    "aggregate_tax_location_risk",
    "EstateCostOfTaxes",
    "aggregate_estate_cost",
]
