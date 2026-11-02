"""The ``fr_guardrail`` spending mode: a band rule on the funded ratio.

A close sibling of ``fr_smooth`` that swaps the damped partial adjustment for a
DISCRETE band trip on the funded ratio: hold while funding stays within ±20% of
fully funded, a ±10% nudge to the untapered base past either band, then the same
activity taper and floor/ceiling clamp as ``guardrails``. Path-dependent and
view-only (the max-spend solver is fenced). Bands and step are fixed presets.
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.spending.base import AdjustmentEvent
from firebridgelab.spending.funded_ratio import FundedRatioStrategyBase
from firebridgelab.spending.lifecycle import activity_multiplier
from firebridgelab.spending.modes import SPENDING_MODE_FR_GUARDRAIL
from firebridgelab.spending.registry import register


# Baked band-strategy preset for the funded-ratio GUARDRAIL strategy. The FR
# analogue of the GUARD/ADJUST guardrails defaults, but the band is on the funded
# ratio itself rather than the withdrawal rate: hold while funding stays within ±20%
# of fully-funded (FR in [0.80, 1.20]); a ±10% nudge to the untapered base past
# either band. FR > 1.20 (over-funded) → raise; FR < 0.80 (under-funded) → cut. Not
# user-editable. Module constants so a unit test can read the same values.
_FR_GR_UPPER: float = 1.20   # FR above this → raise the base
_FR_GR_LOWER: float = 0.80   # FR below this → cut the base
_FR_GR_STEP: float = 0.10    # per-trip proportional adjustment to the untapered base


@register(SPENDING_MODE_FR_GUARDRAIL)
class FundedRatioGuardrailStrategy(FundedRatioStrategyBase):
    """Funded-ratio BAND (guardrail) spending strategy.

    A close sibling of ``FundedRatioSmoothStrategy``: same spine (3-year smoothed
    portfolio, NPV-of-the-baseline-stream denominator), same NPV target
    (``scenario.annual_spending`` → FR = 1 funds the baseline), same UNtapered
    carried base, the same nudge→taper→clamp order, and the same floor/ceiling
    rails. The ONLY difference is the per-year adjustment rule: where fr-smooth
    moves the base a damped fraction toward full funding every year, fr-guardrail
    trips a DISCRETE band on the funded ratio — hold inside the band, a fixed ±%
    nudge outside it (the FR analogue of the Guyton-Klinger rate band):

      - ``fr > _FR_GR_UPPER`` (over-funded) → ``base_spend *= (1 + _FR_GR_STEP)``
        (raise), action ``"raise"``;
      - ``fr < _FR_GR_LOWER`` (under-funded) → ``base_spend *= (1 - _FR_GR_STEP)``
        (cut), action ``"cut"``;
      - else (FR within ±20% of full) → hold, action ``"hold"``.

    Baked preset: ``_FR_GR_UPPER`` = 1.20, ``_FR_GR_LOWER`` = 0.80,
    ``_FR_GR_STEP`` = 0.10 (no user inputs). The new base PERSISTS as the
    carried state. This variant has a genuine discrete cut/raise/hold each year, so
    the ``AdjustmentEvent`` vocabulary fits it natively.

    Degenerate handling (identical to fr-smooth / guardrails): a non-positive
    ``begin_year_portfolio`` SKIPS the adjustment for the year (the base is held;
    the floor clamp governs the dollar spend), which also keeps the base from being
    driven non-positive. The ±10% step on a positive base keeps the base strictly
    positive on every adjusting year.
    """

    def __init__(self, scenario: Scenario) -> None:
        # NPV target = the baseline desired spend → FR = 1 funds the baseline
        # (the SAME invariant fr-smooth uses).
        super().__init__(scenario, npv_target_real=scenario.annual_spending)
        # Carried UNtapered base, seeded at the baseline (the go-go-peak level).
        self.base_spend: float = scenario.annual_spending
        # Adjustment-event log + prior-GI tracking (same shape/discipline as
        # GuardrailStrategy / FundedRatioSmoothStrategy). Purely descriptive:
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
        # 1. BAND TRIP (skipped on a non-positive begin balance — the floor clamp
        # governs there; the base is held so it can't be driven non-positive).
        action = "hold"
        if begin_year_portfolio > 0:
            if fr > _FR_GR_UPPER:
                self.base_spend *= (1.0 + _FR_GR_STEP)   # raise toward the ceiling
                action = "raise"
            elif fr < _FR_GR_LOWER:
                self.base_spend *= (1.0 - _FR_GR_STEP)   # cut toward the floor
                action = "cut"
            # else: hold (FR within the band). Persist self.base_spend.

        # 2. TAPER: activity multiplier as a post-multiplier on the untapered base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 3. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor. (Verbatim from GuardrailStrategy /
        # FundedRatioSmoothStrategy so the rails behave identically.)
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
