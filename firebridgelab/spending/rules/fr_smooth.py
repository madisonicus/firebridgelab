"""The ``fr_smooth`` spending mode: funded-ratio partial adjustment.

FR = 1 means the 3-year smoothed portfolio exactly funds the baseline
``annual_spending`` stream (net of GI). Each year the untapered base moves a
damped fraction (alpha) of the way toward full funding, then takes the same
go-go/slow-go/no-go taper and floor/ceiling clamp as ``guardrails``.
Path-dependent and view-only (the max-spend solver is fenced).
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.spending.base import AdjustmentEvent
from firebridgelab.spending.funded_ratio import FundedRatioStrategyBase
from firebridgelab.spending.lifecycle import activity_multiplier
from firebridgelab.spending.modes import SPENDING_MODE_FR_SMOOTH
from firebridgelab.spending.registry import register


# Default smoothness knob for the funded-ratio partial-adjustment strategy
# (fr_smooth). Each year the untapered base moves a fraction ``alpha`` of the way
# toward full funding: ``base *= (1 + alpha * (fr - 1))``. alpha = 1 would snap
# the base straight to the funded level each year (jumpy); alpha = 0 would freeze
# it (constant-equivalent). 0.5 damps the adjustment to roughly half-speed — the
# "smooth" in fr_smooth. Not user-editable. A module constant so a unit test can
# read the same value.
_FR_SMOOTH_ALPHA: float = 0.5


@register(SPENDING_MODE_FR_SMOOTH)
class FundedRatioSmoothStrategy(FundedRatioStrategyBase):
    """Funded-ratio PARTIAL-ADJUSTMENT spending strategy.

    Mirrors ``GuardrailStrategy``'s pinned nudge→taper→clamp order so the two are
    directly A/B-comparable on the spending fan — the ONLY difference is the
    nudge: guardrails trips a discrete band on the withdrawal RATE; fr-smooth moves
    the untapered base a damped fraction of the way toward full funding each year,
    driven by the funded ratio (smoothed portfolio / NPV-of-the-baseline-stream).

    NPV target = ``scenario.annual_spending`` (the baseline desired / go-go-peak
    spend), baked at construction. So FR = 1 means "the smoothed portfolio exactly
    funds the baseline spending stream"; FR > 1 → over-funded (raise toward the
    ceiling); FR < 1 → under-funded (cut toward the floor).

    The carried state is the UNtapered base ``base_spend`` (seeded on the first
    call at ``scenario.annual_spending``). Carrying the untapered level — NOT the
    post-taper number — is what prevents the activity taper from double-applying at
    phase boundaries (the same invariant the guardrail base relies on).

    Per-year ``_decide`` (the executable form of the pinned order):

      1. PARTIAL-ADJUSTMENT::

             base_spend = base_spend * (1 + alpha * (fr - 1))

         with ``alpha = _FR_SMOOTH_ALPHA`` (0.5). FR = 1 → ×1.0 (hold); FR > 1 →
         raise; FR < 1 → cut; alpha damps the speed. The new base PERSISTS as the
         carried state (set on ``self``, not recomputed from scratch).
      2. TAPER: ``tapered = base_spend * activity_multiplier(age)`` — the existing
         go-go/slow-go/no-go taper, a post-multiplier on the untapered base
         (exactly as ``GuardrailStrategy`` applies it).
      3. CLAMP: floor flat (``scenario.spending_floor``); ceiling activity-tapered
         (``scenario.spending_ceiling * activity_multiplier(age)``); FLOOR WINS if
         the tapered ceiling ever falls to/below the floor. (Copied verbatim from
         ``GuardrailStrategy`` so the rails behave identically.)

    Degenerate handling (mirrors guardrails): if ``begin_year_portfolio <= 0`` the
    FR adjustment is SKIPPED for the year (the base is held; the floor clamp
    governs the dollar spend). The FR is also clamped non-negative implicitly — a
    non-positive begin balance can't fund anything, so skipping is the honest move
    and it keeps the base from ever being driven non-positive by a runaway ``fr``.
    The ``(1 + alpha*(fr-1))`` factor with alpha = 0.5 and fr ≥ 0 is ≥ 0.5 > 0, so
    a positive base stays strictly positive on every adjusting year.

    Adjustment-stats readout: fr_smooth emits one
    ``AdjustmentEvent`` per year so the EXISTING cost-of-flexibility readout +
    worst-cohort drill-down work unchanged. The ``action`` label is the SIGN of the
    FR-driven change to the untapered base this year — ``"raise"`` if the base went
    up, ``"cut"`` if down, ``"hold"`` if unchanged (FR ≈ 1, alpha = 0, or a
    non-positive begin balance). This is HONEST: it is a real, per-year, discrete
    fact about the direction the partial-adjustment moved the base — NOT a
    fabricated guardrail-band-trip. The aggregate stats (pct_ever_cut, years at
    floor, deepest cut) are computed from the realized spend SERIES vs the
    start/floor levels, which is mode-agnostic and equally meaningful here. The
    ``gi_stepped_up`` flag is tracked the same way (FR rises when GI lands because
    the NPV-of-gaps denominator shrinks), so a raise in a GI-step year is still
    labeled GI-driven in the drill-down.
    """

    def __init__(self, scenario: Scenario) -> None:
        # NPV target = the baseline desired spend → FR = 1 funds the baseline.
        super().__init__(scenario, npv_target_real=scenario.annual_spending)
        # Carried UNtapered base, seeded at the baseline (the go-go-peak level).
        self.base_spend: float = scenario.annual_spending
        # Adjustment-event log + prior-GI tracking for GI-driven-raise labeling —
        # same shape/discipline as GuardrailStrategy. Purely descriptive:
        # recording never alters the returned spend.
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    def _decide(
        self,
        scenario: Scenario,
        age: int,
        fr: float,
        begin_year_portfolio: float,
        gi: float,
    ) -> tuple[float, str | None]:
        alpha = _FR_SMOOTH_ALPHA

        # 1. PARTIAL-ADJUSTMENT (skipped on a non-positive begin balance — the
        # floor clamp governs there; the base is held so it can't be driven
        # non-positive by a degenerate FR).
        action = "hold"
        if begin_year_portfolio > 0:
            factor = 1.0 + alpha * (fr - 1.0)
            new_base = self.base_spend * factor
            if new_base > self.base_spend + 1e-9:
                action = "raise"
            elif new_base < self.base_spend - 1e-9:
                action = "cut"
            self.base_spend = new_base   # PERSIST the carried state.

        # 2. TAPER: activity multiplier as a post-multiplier on the untapered base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 3. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor. (Verbatim from GuardrailStrategy.)
        floor = scenario.spending_floor
        ceiling = scenario.spending_ceiling * activity_multiplier(scenario, age)
        if ceiling < floor:
            spend = floor
        elif tapered < floor:
            spend = floor
        elif tapered > ceiling:
            spend = ceiling
        else:
            spend = tapered

        # Record the year's event (additive — does not touch the computed spend).
        gi_stepped_up = self._prev_gi is not None and gi > self._prev_gi + 1e-9
        self.events.append(
            AdjustmentEvent(
                age=age,
                action=action,
                spend=spend,
                begin_portfolio=begin_year_portfolio,
                gi=gi,
                gi_stepped_up=gi_stepped_up,
            )
        )
        self._prev_gi = gi
        return spend, action
