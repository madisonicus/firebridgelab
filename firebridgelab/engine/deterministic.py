"""Deterministic year-by-year projection (``simulate``).

One constant real return per allocation (per phase under a glidepath), the
spending strategy selected by ``spending_mode``, and the four per-person GI streams
(two pensions + two Social Security) that activate at fixed ages. No randomness and
no historical sequence. ``sourcing_mode == "buckets"`` routes to the 3-bucket
mechanic in ``engine.buckets`` and returns the same ``Trajectory`` shape. All values
are real (today's) dollars.

Real-return assumption
----------------------
The year's return is the annually-rebalanced compound return of the allocation
(``engine.allocation.blended_real_return_at_age``), net of ``annual_fee``. The
default 60/40 class knobs are 7%/2% real, a plain blend of
0.60·0.07 + 0.40·0.02 = 5.0% real (``blended_real_return``), in the middle of the
commonly-cited range for historical 60/40 real returns:

  - Vanguard's "Principles for Investing Success" cites ~5% real for a
    balanced US portfolio over the long historical record.
  - The Bogleheads wiki summary of historical US 60/40 real returns
    (1926-present) lands in the 4.5-5.5% range depending on window.
  - Bengen's original 4% SWR work assumed comparable real returns
    (~5.0% stocks, ~2.5% bonds → blend near 4%).

Conservatism is a user knob (edit the class returns), not a structural default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from firebridgelab.engine.allocation import blended_real_return_at_age
from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS, Scenario
from firebridgelab.engine.step import (
    max_fundable_spend,
    net_return,
    step_year,
)
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.spending.base import SpendingStrategy
from firebridgelab.spending.lifecycle import spending_multiplier
from firebridgelab.spending.registry import make_strategy


@dataclass(frozen=True)
class YearPoint:
    """One year's snapshot of the projection."""

    age: int
    start_portfolio: float  # portfolio at start of year (before growth)
    growth: float           # real-return growth during year
    spending: float         # spending withdrawn this year
    guaranteed_income: float  # sum of all active pension + SS streams this year
    end_portfolio: float    # portfolio at end of year (floored at 0)


@dataclass(frozen=True)
class Trajectory:
    """Result of one deterministic projection."""

    points: List[YearPoint]
    depleted_age: int | None  # first age at which end_portfolio == 0, or None
    final_portfolio: float    # end_portfolio at end_age


def simulate(
    scenario: Scenario, strategy: SpendingStrategy | None = None
) -> Trajectory:
    """Run a deterministic real-dollar projection.

    For each year from start_age to end_age inclusive, apply the midpoint
    withdrawal convention:

        end = start + start*r + (GI - spending) * (1 + r/2)

    Spending and GI are both mid-year flows (``engine.step.step_year``).
    End-of-year balance is floored at zero; the first age that hits the floor
    is recorded as depletion.

    Spending comes from a ``SpendingStrategy`` constructed ONCE before the year
    loop (ADR 0006). When ``strategy`` is None one is built from
    ``scenario.spending_mode`` (``make_strategy``): for ``constant`` / ``lifecycle``
    that is a wrapper over ``spending_for_age``; for ``guardrails`` it is the
    stateful GK rule that reads the begin-of-year portfolio. The optional
    ``strategy`` arg lets a caller inject a pre-built strategy (parity with the
    cohort/trial loops); the solver, which never runs guardrail mode, passes none.

    Buckets: when ``sourcing_mode == "buckets"`` the
    deterministic projection routes to the 3-bucket mechanic and returns the SAME
    ``Trajectory`` shape — each ``YearPoint``'s ``end_portfolio`` is the SUMMED
    bucket balance (cash + income + growth) that year, so the existing deterministic
    chart renders unchanged. In Deterministic mode every year is an up year (no down
    years), so the one-sided "ride" branch never fires and the buffer is never
    tested — surface that caveat in the UI (point the user to Historical / MC). The
    single-bucket degenerate config reproduces the single-balance terminal exactly.
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        return _simulate_buckets_as_trajectory(scenario)

    if strategy is None:
        strategy = make_strategy(scenario)

    points: List[YearPoint] = []
    portfolio = scenario.initial_portfolio
    depleted_age: int | None = None

    for age in range(scenario.start_age, scenario.end_age + 1):
        start_portfolio = portfolio
        # Per-age return resolution: under constant allocation_mode this returns
        # the SAME value for every age; under glidepath it returns the active
        # phase's blend.
        r = net_return(blended_real_return_at_age(scenario, age), scenario.annual_fee)

        # growth computed inline for YearPoint; step_year recomputes the same
        # product from the same operands, so the two agree exactly.
        growth = start_portfolio * r

        # Guaranteed income is the sum of the four named streams that are
        # active at this age (age >= the stream's start age), from the canonical
        # sum in ``income.guaranteed.gi_at_age``.
        gi = gi_at_age(scenario, age)

        # Spending comes from the strategy. begin_year_portfolio is the balance
        # BEFORE this year's growth/withdrawal (start_portfolio) — the denominator
        # the guardrail rate uses. constant/lifecycle ignore it.
        spending = strategy.spend_for_year(scenario, age, start_portfolio, gi)
        # Cost-of-living real drift: a post-strategy multiplier on the funded
        # spend, composing with ANY strategy. Default spending_real_drift 0.0
        # makes it exactly 1.0.
        spending *= spending_multiplier(scenario, age)

        # Realized-spend cap: the strategy returns the INTENDED spend, but a
        # depleting year can't fund it — the household can only spend what the
        # portfolio + GI cover. The RECORDED/charted spend is capped at what brings
        # the end balance to exactly 0 (``max_fundable_spend``); in a non-depleting
        # year intended <= max_fundable so realized == intended (a no-op). This is a
        # REPORTING value only — the portfolio is STILL stepped with ``spending``
        # (the intended request) so end_portfolio AND the depletion flag are
        # unaffected (the flag fires on end < 0; max_fundable would land exactly
        # ON 0 and NOT flag, so it must not be the value stepped). The
        # strategy keeps its own intended base, so there is no feedback loop.
        realized_spend = min(spending, max_fundable_spend(start_portfolio, r, gi))

        # Midpoint convention: half the year's spending earns a half-year
        # of growth before being consumed (ADR 0002). Stepped with
        # the INTENDED spend to preserve the depletion flag (see above).
        already_depleted = depleted_age is not None
        end_portfolio, depleted_this_year = step_year(
            start_portfolio, r, gi, spending
        )
        if depleted_this_year and depleted_age is None:
            depleted_age = age
        # Absorbing depletion: once depleted, the portfolio stays at 0
        # — a GI stream starting later must not resurrect it (``step_year`` is
        # memoryless: ``0 + 0 + (gi − spend)·(1+r/2)`` goes positive once GI exceeds
        # the funded gap). Mirrors the same override in the shared cohort loop
        # (``engine.sim_core._simulate_cohort``) so the three return modes can't
        # drift. ``realized_spend`` above already caps at the GI-funded ``gi``;
        # ``depleted_age`` (first crossing) is set before this and never moves.
        # No effect on paths that never recover.
        if already_depleted:
            end_portfolio = 0.0

        points.append(
            YearPoint(
                age=age,
                start_portfolio=start_portfolio,
                growth=growth,
                spending=realized_spend,
                guaranteed_income=gi,
                end_portfolio=end_portfolio,
            )
        )

        portfolio = end_portfolio

    return Trajectory(
        points=points,
        depleted_age=depleted_age,
        final_portfolio=portfolio,
    )


def _simulate_buckets_as_trajectory(scenario: Scenario) -> Trajectory:
    """Run the deterministic 3-bucket sim, projected onto the single ``Trajectory``.

    The deterministic bucket run renders on the single portfolio line — one
    ``YearPoint`` per age whose ``end_portfolio`` is the SUMMED bucket balance,
    ``start_portfolio`` the prior year's summed end (``initial_portfolio`` for year 0),
    ``guaranteed_income`` and ``spending`` the same flows the bucket sim recorded.
    ``growth`` is the net market move backed out of the accounting identity (``end −
    start + spending − GI``) so the tooltip stays self-consistent — it is the combined
    growth across buckets, not a single blended rate (there isn't one in buckets mode).
    """
    from firebridgelab.engine.buckets import simulate_buckets_deterministic

    bt = simulate_buckets_deterministic(scenario)
    points: List[YearPoint] = []
    start = scenario.initial_portfolio
    for y in bt.years:
        end = y.cash + y.income + y.growth
        growth = end - start + y.spending - y.guaranteed_income
        points.append(
            YearPoint(
                age=y.age,
                start_portfolio=start,
                growth=growth,
                spending=y.spending,
                guaranteed_income=y.guaranteed_income,
                end_portfolio=end,
            )
        )
        start = end
    return Trajectory(
        points=points,
        depleted_age=bt.depleted_age,
        final_portfolio=bt.final_portfolio,
    )
