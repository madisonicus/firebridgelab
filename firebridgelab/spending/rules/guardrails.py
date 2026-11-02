"""spending.rules.guardrails (split from the flat layout)."""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.income.survivor import is_survivor_year
from firebridgelab.spending.base import AdjustmentEvent, SpendingStrategy
from firebridgelab.spending.lifecycle import activity_multiplier
from firebridgelab.spending.modes import SPENDING_MODE_GUARDRAILS
from firebridgelab.spending.registry import register


@register(SPENDING_MODE_GUARDRAILS)
class GuardrailStrategy(SpendingStrategy):
    """Two-sided Guyton-Klinger guardrail (the new stateful strategy).

    Captures its init-time anchors at construction (design):

      - ``initial_portfolio`` = ``scenario.initial_portfolio``
      - ``base_spend_0``      = ``scenario.annual_spending`` (the UNtapered base)
      - ``initial_gi``        = the GI active at ``start_age`` (0 for the default household)
      - ``initial_rate``      = ``max(0, base_spend_0 - initial_gi) / initial_portfolio``

    and carries mutable state ``base_spend`` (the nudged, untapered level) across
    years. If ``initial_rate == 0`` the guardrail is DISABLED (the run behaves as
    plain tapered/clamped constant) — ``enabled`` records that.

    The nudge→taper→clamp arithmetic per year is pinned in the module docstring;
    this is the executable form.
    """

    is_solvable: bool = False
    reason_not_solvable: str = (
        "max-spend solve is not available in guardrails mode (view-only)"
    )
    # Guardrails' spend varies across cohorts/trials → the engines collect the
    # per-year spend fan + adjustment events (the predicate that replaced the
    # ``spending_mode == "guardrails"`` gate at the historical/MC call sites).
    collects_spends: bool = True

    def __init__(self, scenario: Scenario) -> None:
        self.initial_portfolio = scenario.initial_portfolio
        self.base_spend_0 = scenario.annual_spending
        self.initial_gi = gi_at_age(scenario, scenario.start_age)
        # Portfolio-funded withdrawal rate at the start age, on the untapered base.
        # A non-positive initial portfolio (guarded at the API as >0) or a base ≤ GI
        # both yield initial_rate == 0 → guardrail disabled.
        if self.initial_portfolio > 0:
            self.initial_rate = max(0.0, self.base_spend_0 - self.initial_gi) / self.initial_portfolio
        else:
            self.initial_rate = 0.0
        # Degenerate: initial_rate == 0 disables the guardrail for the run.
        self.enabled = self.initial_rate > 0.0
        # Mutable carried state: the nudged, UNtapered base.
        self.base_spend = self.base_spend_0
        # Adjustment-event log (variable-spending design slice 2): one
        # AdjustmentEvent per spend_for_year call, in age order. Purely
        # descriptive — recording it never alters the returned spend, so the
        # canary holds. ``_prev_gi`` carries the prior year's GI so a raise in a
        # GI-step year can be flagged GI-driven (the resolved design decision).
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        guard = scenario.guard_band
        adjust = scenario.guard_adjust

        # 1+2. NUDGE (only when enabled and the begin-of-year balance is positive).
        # The rate is on the UNtapered nudged base, net of GI, over the begin-of-
        # year portfolio. begin_year_portfolio <= 0 skips nudging (floor governs).
        action = "hold"
        if self.enabled and begin_year_portfolio > 0:
            # After a first death the household spends ``survivor_spending_fraction``
            # of the rule's output (applied post-strategy by the engine loop), so
            # the withdrawal the rule sees must carry the same scale.
            household = (
                scenario.survivor_spending_fraction
                if is_survivor_year(scenario, age)
                else 1.0
            )
            rate = max(0.0, self.base_spend * household - gi) / begin_year_portfolio
            if rate > self.initial_rate * (1.0 + guard):
                self.base_spend *= (1.0 - adjust)   # cut toward the floor
                action = "cut"
            elif rate < self.initial_rate * (1.0 - guard):
                self.base_spend *= (1.0 + adjust)   # raise toward the ceiling
                action = "raise"
            # else: hold. Persist self.base_spend (it is the carried state).

        # Optional base clamp (review 2026-10-11): keep the carried state inside
        # the rails it is clamped to, so a long run of cuts at the floor (or
        # raises at the ceiling) cannot leave a hidden budget far outside what is
        # actually spent. Off by default (the original rule).
        if scenario.guard_clamp_base:
            taper = activity_multiplier(scenario, age)
            lo = scenario.spending_floor / taper if taper > 0 else self.base_spend
            hi = scenario.spending_ceiling
            self.base_spend = min(max(self.base_spend, lo), max(lo, hi))
        # 3. TAPER: the activity multiplier is a post-multiplier on the nudged base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 4. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor.
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
        # GI stepped up iff it rose vs the prior year; the first year has no prior,
        # so it is never a "step" (the start GI is the baseline, not an arrival).
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
        return spend
