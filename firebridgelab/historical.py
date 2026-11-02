"""Deprecated import path: ``firebridgelab.historical``.

Its contents moved to:

- ``firebridgelab.engine.cohort_aggregate``
- ``firebridgelab.engine.cohort_windows``
- ``firebridgelab.engine.historical``
- ``firebridgelab.returns.series``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.cohort_aggregate import HistoricalResult, WorstBucketCohort
from firebridgelab.engine.cohort_windows import NoReplayableWindow, cohort_return_sequences
from firebridgelab.engine.historical import simulate_historical
from firebridgelab.returns.series import (
    SERIES_REGISTRY,
    SERIES_REGISTRY_NEW,
    SERIES_FOR_CLASS,
    MC_SERIES_REGISTRY,
    HIST_SERIES_REGISTRY,
    HISTORICAL_SELECTION_EPSILON,
    mapped_series_for,
    renormalized_weights,
    historical_series_by_id,
    selected_series_for,
    common_overlap_start_years,
    glidepath_phase_weights,
)

__all__ = [
    "NoReplayableWindow",
    "SERIES_REGISTRY",
    "SERIES_REGISTRY_NEW",
    "SERIES_FOR_CLASS",
    "HistoricalResult",
    "WorstBucketCohort",
    "MC_SERIES_REGISTRY",
    "HIST_SERIES_REGISTRY",
    "HISTORICAL_SELECTION_EPSILON",
    "mapped_series_for",
    "renormalized_weights",
    "historical_series_by_id",
    "selected_series_for",
    "common_overlap_start_years",
    "glidepath_phase_weights",
    "simulate_historical",
    "cohort_return_sequences",
]
