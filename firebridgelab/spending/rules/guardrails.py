"""The ``guardrails`` spending mode: a two-sided Guyton-Klinger guardrail.

The spend responds to the portfolio-funded withdrawal rate: when the rate drifts
more than ``guard_band`` above or below its starting value, the carried base is
cut or raised by ``guard_adjust``. Guyton & Klinger (2006), run in real dollars;
GK's nominal COLA rule does not map onto a real-dollar engine and is not modelled.

Per-year order: nudge → taper → clamp
-------------------------------------
Each year, on the BEGIN-of-year portfolio (the balance BEFORE this year's growth
and withdrawal — ``portfolio`` at the top of the iteration in
``engine.sim_core._simulate_cohort``):

  1. ``rate = max(0, base_spend - gi) / begin_year_portfolio`` — the portfolio-
     funded withdrawal rate on the UNtapered nudged base (scaled by the survivor
     spending fraction after a first death). If ``begin_year_portfolio <= 0``,
     SKIP nudging (the floor clamp governs).
  2. If ``rate > initial_rate*(1+GUARD)`` → ``base_spend *= (1 - ADJUST)`` (cut);
     elif ``rate < initial_rate*(1-GUARD)`` → ``base_spend *= (1 + ADJUST)``
     (raise); else hold. PERSIST the new ``base_spend`` as the carried state.
  3. ``tapered = base_spend * activity_multiplier(age)`` — the go-go/slow-go/no-go
     taper, composed as a post-multiplier on the nudged base.
  4. ``spend = clamp(tapered, floor(age), ceiling(age))``. FLOOR WINS if a tapered
     ceiling ever falls to/below the floor.

Carrying the *untapered* base as the state (not the post-taper number) is what
prevents the taper from double-applying at phase boundaries.

The rails
---------
  - **Floor**: the OPERATIONAL ``spending_floor`` ($85k default — the lowest
    level the household will accept), FLAT — NOT activity-tapered: spending can
    never drop below it. ($45k is the ABSOLUTE / survivable floor — context
    guaranteed income roughly reaches, drawn as a fainter reference line on the
    fan — NOT the clamp; see ``engine.scenario.ABSOLUTE_FLOOR``.)
  - **Ceiling**: ``spending_ceiling`` ($150k default), the satiation cap, IS
    activity-tapered, so raises land mostly in the active years.

When ``spending_floor == annual_spending`` (e.g. an $85k floor with an $85k
start spend) the operational floor coincides with the start level, so a cut clamps
straight back to the floor and never registers as a spend below the start — the
adjustment stats then read ``pct_ever_cut`` → 0 and the deepest cut → the floor.
That is correct, not a bug: with the floor at the start level there is no room to
spend below it, and the squeeze shows up as years pinned AT the floor
(``median_years_at_floor``), the key stat in that regime.

``guard_clamp_base`` (off by default) additionally keeps the carried base inside
the rails, so a long run of cuts at the floor (or raises at the ceiling) cannot
leave a hidden budget far outside what is actually spent.

Degenerate cases
----------------
  - ``initial_rate == 0`` (start spend ≤ start GI) → DISABLE the guardrail for the
    run: behave as plain tapered/clamped constant (nudging would otherwise make
    every nonzero rate a spurious cut, since both bands collapse to 0).
  - ``ADJUST == 0`` → the nudge is a no-op multiply (×1.0), so spend never moves:
    exactly tapered/clamped constant.
  - ``begin_year_portfolio <= 0`` (deep depletion) → step 1 skips nudging; the
    floor clamp governs the dollar spend.
"""

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
    """Two-sided Guyton-Klinger guardrail.

    Captures its init-time anchors at construction:

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
    # per-year spend fan + adjustment events.
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
        # Adjustment-event log: one AdjustmentEvent per spend_for_year call, in
        # age order. Purely descriptive — recording it never alters the returned
        # spend. ``_prev_gi`` carries the prior year's GI so a raise in a
        # GI-step year can be flagged GI-driven.
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

        # Optional base clamp: keep the carried state inside the rails it is
        # clamped to, so a long run of cuts at the floor (or raises at the
        # ceiling) cannot leave a hidden budget far outside what is actually
        # spent. Off by default (the original rule).
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
