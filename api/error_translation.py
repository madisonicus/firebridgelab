"""Engine-error → user-facing 422 detail translation (the API error seam).

The engine raises precise, developer-oriented ``ValueError`` subclasses
(Frobenius distances, cohort-window spans, solver-fence signals). Those are
correct for a log but unhelpful in the UI, so the cases a user can actually hit
by editing the matrix / presets / spending mode get a plain-language sentence
(asset-class design deliverable 3). This module owns that translation so it's a
unit-testable seam, independent of HTTP.

Dispatch is by EXCEPTION CLASS for every mapped case — never by substring-
matching the message text. The engine owns the typed signals:

- ``SpendingStrategyNotSolvable`` (``firebridgelab.spending``) — max-spend solve asked
  of a path-dependent (dynamic-spending) strategy.
- ``BucketsNotSolvable`` (``firebridgelab.spending``) — max-spend solve asked of a
  buckets-sourcing scenario (view-only).
- ``BucketsGuardrailsNotComposable`` (``firebridgelab.spending``) — dynamic spending +
  bucket sourcing don't compose yet.
- ``CorrelationNotEstimable`` (``firebridgelab.returns.correlation``) — the D6 Frobenius gate in
  ``estimate_with_gate``: too many classes with too little overlapping history
  to estimate honest cross-class correlations (broad-MC non-PSD).
- ``NoReplayableWindow`` (``firebridgelab.historical``) — the selected classes share no
  common stretch of history long enough to replay the horizon.
- ``TaxDragMonteCarloNotSolvable`` (``firebridgelab.spending``, tax-drag design td1
  DECISION 8) — max-spend solve asked of a split-active Monte Carlo scenario
  (the dragged MC solve is a later card). Unlike the other mapped cases this
  one passes the ENGINE'S OWN message through verbatim (``str(exc)``) rather
  than a rewritten sentence — the fence message
  (``firebridgelab.analysis.solver.TAX_DRAG_MC_FENCE_MESSAGE``) is already the exact
  user-facing text both the engine and the API test against, so a paraphrase
  here would just be a second copy to keep in sync.

Any other ``ValueError`` (no mapped class, malformed explicit matrix, zero
mapped weight) passes through verbatim — those messages are already actionable.

This module imports the typed exceptions from the engine (api → engine is a
clean one-way dependency, no import cycle).
"""

from firebridgelab.returns.correlation import CorrelationNotEstimable
from firebridgelab.engine.cohort_windows import NoReplayableWindow
from firebridgelab.spending.base import (
    BucketsGuardrailsNotComposable,
    BucketsNotSolvable,
    SpendingStrategyNotSolvable,
    TaxDragMonteCarloNotSolvable,
)
from firebridgelab.analysis.stress import StressNotAvailable

__all__ = ["engine_error_to_detail"]


def engine_error_to_detail(exc: ValueError) -> str:
    """Map a raw engine ``ValueError`` to a clean, user-facing 422 detail string.

    Discriminates on the exception CLASS (all five mapped cases are typed
    ``ValueError`` subclasses), so a future reword of an engine message can't
    silently break the translation. The final fallback returns ``str(exc)`` —
    unmapped engine ValueErrors are already user-actionable.
    """
    if isinstance(exc, SpendingStrategyNotSolvable):
        return (
            "Max-spend solve is not available in dynamic-spending (guardrails / "
            "funded-ratio) modes — they're view-only; set your start spend and "
            "read the spending fan."
        )
    if isinstance(exc, BucketsNotSolvable):
        return (
            "Max-spend solve is not available in buckets sourcing mode — it's "
            "view-only; set your spend and read the buffer-survival metric."
        )
    if isinstance(exc, BucketsGuardrailsNotComposable):
        return (
            "Dynamic spending (guardrails / funded-ratio) and bucket sourcing "
            "don't compose yet — that's a later slice. Use constant or lifecycle "
            "spending with buckets."
        )
    if isinstance(exc, CorrelationNotEstimable):
        return (
            "Not enough overlapping history to estimate correlations across this "
            "many asset classes — reduce the mix, or use Historical or "
            "Deterministic mode."
        )
    if isinstance(exc, NoReplayableWindow):
        return (
            "These asset classes don't share enough overlapping history to replay "
            "this retirement length — shorten the horizon, drop the shortest-"
            "history class, or use Monte Carlo or Deterministic mode."
        )
    if isinstance(exc, StressNotAvailable):
        return (
            "That stress test isn't available for this plan — stress tests "
            "currently run on a single-portfolio, constant allocation. Switch off "
            "buckets / glidepath, or pick a listed stress scenario."
        )
    if isinstance(exc, TaxDragMonteCarloNotSolvable):
        # Explicit typed branch (never fall through to the generic str(exc)
        # tail below by accident) — but the VALUE is the engine's own message
        # verbatim; see the module docstring for why no rewrite is needed here.
        return str(exc)
    return str(exc)
