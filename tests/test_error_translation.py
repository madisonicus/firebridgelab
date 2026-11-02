"""Unit tests for the engine-error → user-facing 422 translation seam.

Two things are pinned here, both without any HTTP round-trip:

1. ``engine_error_to_detail`` maps each of the FIVE typed engine exceptions to
   its exact user-facing 422 sentence, and passes an unmapped ``ValueError``
   through verbatim. The translation is class-dispatched (no substring matching),
   so these strings are the contract the front-end shows.

2. The two NEWLY-typed exceptions (``CorrelationNotEstimable``,
   ``NoReplayableWindow``) are actually RAISED as their typed subclass by the
   engine in their trigger conditions — and each subclasses ``ValueError`` with
   the same technical message it carried before, so existing ``except
   ValueError`` paths and ``pytest.raises(ValueError, match=...)`` tests still
   hold.
"""

import numpy as np
import pytest

from api.error_translation import engine_error_to_detail
from firebridgelab.returns.correlation import CorrelationNotEstimable
from firebridgelab.engine.cohort_windows import NoReplayableWindow, _apply_window_floor
from firebridgelab.spending.base import (
    BucketsGuardrailsNotComposable,
    BucketsNotSolvable,
    SpendingStrategyNotSolvable,
)


# --- 1. Direct translation of each typed exception -------------------------

def test_translate_spending_strategy_not_solvable():
    detail = engine_error_to_detail(SpendingStrategyNotSolvable("anything"))
    assert detail == (
        "Max-spend solve is not available in dynamic-spending (guardrails / "
        "funded-ratio) modes — they're view-only; set your start spend and "
        "read the spending fan."
    )


def test_translate_buckets_not_solvable():
    detail = engine_error_to_detail(BucketsNotSolvable("anything"))
    assert detail == (
        "Max-spend solve is not available in buckets sourcing mode — it's "
        "view-only; set your spend and read the buffer-survival metric."
    )


def test_translate_buckets_guardrails_not_composable():
    detail = engine_error_to_detail(BucketsGuardrailsNotComposable("anything"))
    assert detail == (
        "Dynamic spending (guardrails / funded-ratio) and bucket sourcing "
        "don't compose yet — that's a later slice. Use constant or lifecycle "
        "spending with buckets."
    )


def test_translate_correlation_not_estimable():
    # Carry a realistic technical message; the translation must NOT leak it.
    detail = engine_error_to_detail(
        CorrelationNotEstimable(
            "estimated correlation matrix required a large PSD correction "
            "(Frobenius distance 1.2e-01 ...)"
        )
    )
    assert detail == (
        "Not enough overlapping history to estimate correlations across this "
        "many asset classes — reduce the mix, or use Historical or "
        "Deterministic mode."
    )
    assert "Frobenius" not in detail


def test_translate_no_replayable_window():
    detail = engine_error_to_detail(
        NoReplayableWindow("no cohort has a full 93-year window common ...")
    )
    assert detail == (
        "These asset classes don't share enough overlapping history to replay "
        "this retirement length — shorten the horizon, drop the shortest-"
        "history class, or use Monte Carlo or Deterministic mode."
    )
    assert "no cohort has a full" not in detail


def test_unmapped_value_error_passes_through_verbatim():
    detail = engine_error_to_detail(ValueError("mapped allocation weights sum to zero"))
    assert detail == "mapped allocation weights sum to zero"


# --- 2. The newly-typed exceptions are subclasses of ValueError ------------

@pytest.mark.parametrize(
    "exc_cls", [CorrelationNotEstimable, NoReplayableWindow]
)
def test_new_exceptions_subclass_value_error(exc_cls):
    assert issubclass(exc_cls, ValueError)


# --- 3. The engine RAISES the typed subclass in its trigger condition -------

def test_frobenius_gate_raises_typed_correlation_not_estimable(monkeypatch):
    """The D6 Frobenius gate raises ``CorrelationNotEstimable`` (a ValueError
    subclass) with the unchanged technical message when an estimate is badly
    non-PSD — proving the typed signal, not a bare ValueError, reaches the API."""
    import firebridgelab.returns.correlation as corr

    bad = np.array(
        [
            [1.0, 0.95, -0.95],
            [0.95, 1.0, 0.95],
            [-0.95, 0.95, 1.0],
        ]
    )
    monkeypatch.setattr(
        corr, "estimate_correlation_matrix", lambda series_ids: (bad, {})
    )
    # The message text is unchanged (existing tests match "large PSD correction").
    with pytest.raises(CorrelationNotEstimable, match="large PSD correction"):
        corr.estimate_with_gate(["a", "b", "c"])
    # And it is still catchable as a plain ValueError (backwards-compat).
    with pytest.raises(ValueError, match="large PSD correction"):
        corr.estimate_with_gate(["a", "b", "c"])


def test_window_floor_raises_typed_no_replayable_window():
    """``_apply_window_floor`` raises ``NoReplayableWindow`` (a ValueError
    subclass) with the unchanged message when the floor removes every cohort."""
    with pytest.raises(NoReplayableWindow, match="no cohort has a full window"):
        _apply_window_floor([1990, 1991, 1992], 2050)
    # Still catchable as a plain ValueError.
    with pytest.raises(ValueError, match="no cohort has a full window"):
        _apply_window_floor([1990, 1991, 1992], 2050)
