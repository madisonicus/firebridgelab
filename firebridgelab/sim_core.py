"""Deprecated import path: ``firebridgelab.sim_core``.

Its contents moved to:

- ``firebridgelab.engine.sim_core``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.sim_core import (
    AdjustmentStats,
    WorstCohortYear,
    WorstCohort,
    BufferSurvivalStats,
    compute_buffer_survival,
)

__all__ = [
    "AdjustmentStats",
    "WorstCohortYear",
    "WorstCohort",
    "BufferSurvivalStats",
    "compute_buffer_survival",
]
