"""Deprecated import path: ``firebridgelab.buckets``.

Its contents moved to:

- ``firebridgelab.engine.buckets``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.buckets import (
    UP_TRIGGER_FLOOR,
    SYMMETRIC_REFILL,
    EQUITY_CLASS_IDS,
    BucketYear,
    BucketTrajectory,
    bucket_underfunded,
    simulate_buckets_cohort,
    simulate_buckets_deterministic,
    effective_equity_glidepath,
)

__all__ = [
    "UP_TRIGGER_FLOOR",
    "SYMMETRIC_REFILL",
    "EQUITY_CLASS_IDS",
    "BucketYear",
    "BucketTrajectory",
    "bucket_underfunded",
    "simulate_buckets_cohort",
    "simulate_buckets_deterministic",
    "effective_equity_glidepath",
]
