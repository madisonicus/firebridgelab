"""The deterministic default-scenario canary, and the data it was pinned on.

ADR 0001 pins the default scenario's deterministic output to the float. That
output depends on the fetched Shiller file (the per-class volatilities that set
the rebalanced return come from it), and Shiller revises recent months when he
publishes a new one. So the pins are exact only when ``ie_data.xls`` is the
file they were taken from. Against any other revision they hold to ``rel=1e-4``,
which absorbs data revisions but still catches an engine change of any size
that matters. To re-pin after a deliberate engine change or a new data year:
run ``firebridgelab-fetch-data --force``, update the three constants
below, and say so in the commit.
"""

import hashlib
from functools import lru_cache

import pytest

from firebridgelab.returns.shiller import CACHE_FILE

# sha256 of the ie_data.xls the pins below were taken from (fetched 2026-11-01).
REFERENCE_SHILLER_SHA256 = "98a54927942e842ec07ffe73000998e793953708c345b6b27bb363e9ada8366a"

DET_FINAL_PIN = 6074100.445485487
DET_YEAR0_PIN = 1783932.026989239


@lru_cache(maxsize=1)
def on_reference_data() -> bool:
    try:
        return hashlib.sha256(CACHE_FILE.read_bytes()).hexdigest() == REFERENCE_SHILLER_SHA256
    except OSError:
        return False


def canary(value: float):
    """``value`` itself on the reference data, else ``pytest.approx(value, rel=1e-4)``."""
    return value if on_reference_data() else pytest.approx(value, rel=1e-4)


DET_FINAL_CANARY = canary(DET_FINAL_PIN)
DET_YEAR0_CANARY = canary(DET_YEAR0_PIN)
