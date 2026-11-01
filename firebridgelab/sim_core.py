"""Shared cohort-simulation primitives, used by every return mode + the solver.

This module holds the simulation primitives that are NOT specific to any one
return mode — the pieces all three modes (and the solver) run in common:

  - ``_simulate_cohort`` — the per-**cohort** sim loop. A *cohort* here is one
    full retirement run driven by a single per-year real-return sequence (a
    Historical start-year window, a Monte Carlo *trial*, or the solver's
    candidate path). Each return mode constructs its own return sequences, but
    they all hand them to this one loop, so the per-year withdrawal math
    (midpoint convention, four GI streams, strategy-shaped spending, floor at
    zero) is identical across modes and can't drift.
  - The shared result dataclasses (``AdjustmentStats``, ``WorstCohortYear``,
    ``WorstCohort``, ``BufferSurvivalStats``) that a cohort/trial run reduces to,
    reused field-for-field by the Historical and Monte Carlo public results.
  - The aggregators that build those dataclasses from per-cohort/per-trial data
    (``_compute_adjustment_stats``, ``compute_buffer_survival``) — defined ONCE
    so the Historical and Monte Carlo readouts use the SAME math.

A *return mode* (deterministic / historical / monte_carlo) decides HOW the
per-year returns are drawn; this module owns WHAT happens once you have them.

Dependencies point DOWNWARD only — this module imports from ``firebridgelab.scenario``,
``firebridgelab._step``, and ``firebridgelab.spending``. ``historical.py``, ``monte_carlo.py``,
and ``solver.py`` import FROM here, never the reverse, so there is no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from firebridgelab._step import max_fundable_spend, net_return, step_year
from firebridgelab.scenario import Scenario, gi_at_age, is_survivor_year, spending_multiplier
from firebridgelab.spending import AdjustmentEvent, SpendingStrategy, make_strategy

# Tolerance for "at the floor" / "below a reference" comparisons on real-dollar
# spends — well below a $1 difference, so floating-point clamp residue doesn't
# miscount a year that landed exactly on a rail.
_SPEND_EPSILON: float = 1.0


@dataclass(frozen=True)
class AdjustmentStats:
    """Cost-of-flexibility readout, aggregated across all cohorts (slice 2).

    Guardrails + Historical only. Frames the *shape of flexibility*, not a
    pass/fail. All fractions are 0..1; ages are ints; dollars are real.

      - ``cohort_count``        — number of cohorts the stats cover.
      - ``pct_ever_cut``        — fraction of cohorts whose spend EVER dropped
        below the start level (``annual_spending``) at any age.
      - ``pct_ever_raised``     — fraction whose spend EVER rose above the start
        level. (Cut/raise are vs the START spend, not each other — a cohort can
        do both over a long horizon.)
      - ``median_first_cut_age``— median age of the FIRST ``cut`` event, over the
        cohorts that cut at all; None if no cohort ever cut.
      - ``median_years_at_floor``  — median (across ALL cohorts) of the count of
        years a cohort spent AT the operational floor (``scenario.spending_floor``,
        within ``_SPEND_EPSILON``). With the default $85k floor == $85k start
        spend this is THE key squeeze stat — years pinned at the minimum (cuts
        can't register below a floor that equals the start level).
      - ``deepest_cut``         — the DEEPEST CUT, defined as the p10 of each
        cohort's MINIMUM real spend across the horizon: a typical-bad-case floor
        on lifestyle (10% of cohorts get squeezed at least this hard). Chosen
        over the single worst-cohort minimum so one extreme sequence doesn't set
        the headline (design: "pick one, document it").
      - ``lowest_single_year_spend`` — the GLOBAL single-year minimum spend across
        ALL cohorts (the absolute lowest any cohort reached in any one year),
        reported alongside ``deepest_cut`` for context. NOTE: this is NOT the
        displayed ``worst_cohort``'s minimum — ``worst_cohort`` is selected by
        lowest TOTAL spend, so its own lowest year (``WorstCohort.min_spend``) is
        usually higher than this global single-year floor. Named to make that
        distinction unambiguous (the old name implied it belonged to the displayed
        worst cohort, which read as a $15k self-contradiction in the UI).
      - ``pct_ever_forced_to_floor`` — fraction of cohorts that EVER got forced
        down to the operational floor (``scenario.spending_floor``, default $85k).
        With the default $85k floor at the start level this is the "% of histories
        ever forced down to your floor" headline. (Confirmed safe: 0 of 113 cohorts
        deplete holding a $85k floor.)
    """

    cohort_count: int
    pct_ever_cut: float
    pct_ever_raised: float
    median_first_cut_age: int | None
    median_years_at_floor: float
    deepest_cut: float
    lowest_single_year_spend: float
    pct_ever_forced_to_floor: float


@dataclass(frozen=True)
class WorstCohortYear:
    """One year of the worst cohort's annotated spending path (slice 2).

    The DISPLAY-ready form of an ``AdjustmentEvent`` for the drill-down narrative:
    the engine logic is rate-based, but every field here is in the dollars the user
    thinks in. ``label`` is the human action tag the UI renders verbatim, with
    GI-driven raises distinguished from market-driven ones (design resolved
    decision): a raise in a GI-step year is "raise — guaranteed income started",
    a raise in a non-GI year is "raise — strong returns".
    """

    age: int
    action: str            # "cut" | "raise" | "hold"
    gi_driven: bool        # raise caused by a GI step-up (not market)
    spend: float           # withdrawn real-dollar spend this year
    begin_portfolio: float
    gi: float
    label: str             # display tag, e.g. "raise — guaranteed income started"


@dataclass(frozen=True)
class WorstCohort:
    """The most-squeezed historical cohort, drilled (slice 2).

    "Worst" = the cohort with the LOWEST TOTAL real spending over the full horizon
    (the most-squeezed retirement) — documented, with a deterministic tie-break to
    the earliest start year. Labeled by its start year ("the 1966 cohort").

      - ``start_year``  — the cohort's start year (its label).
      - ``total_spend`` — its summed real spend over the horizon (the selection key).
      - ``min_spend``   — its lowest single-year spend.
      - ``depleted_age``— the age it hit zero, or None.
      - ``path``        — the year-by-year annotated path (WorstCohortYear list).
    """

    start_year: int
    total_spend: float
    min_spend: float
    depleted_age: int | None
    path: list[WorstCohortYear]


@dataclass(frozen=True)
class BufferSurvivalStats:
    """The buffer-survival readout for the 3-bucket strategy (bucket-strategy design
    "Output / what the user sees" — the thesis payoff, the analogue of guardrails'
    ``adjustment_stats``).

    A "forced growth sale" = a year where spending had to be sourced from the
    GROWTH bucket because a sustained drawdown exhausted the cash + income buffer
    (a down-year cash+income exhaustion — the real failure mode the strategy is
    meant to avoid). Aggregated across all cohorts (Historical) or trials (MC):

      - ``count``                            — number of cohorts / trials covered.
      - ``pct_cohorts_forced_to_sell_growth``— fraction whose buffer was EVER
        breached (a forced growth sale happened at any age). 0.0 = the buffer
        carried every history through; this is the headline.
      - ``median_years_buffer_lasted``       — the MEDIAN AGE of the FIRST forced
        growth sale across the histories that had one (design: "median age of first
        forced growth sale"); None when NO history ever forced a sale (the
        "never" sentinel — the buffer always held).
      - ``worst_forced_sale_age``            — the EARLIEST age any history forced a
        growth sale (the worst case — the soonest the buffer broke); None when
        none did.
    """

    count: int
    pct_cohorts_forced_to_sell_growth: float
    median_years_buffer_lasted: int | None
    worst_forced_sale_age: int | None


def compute_buffer_survival(
    first_forced_sale_ages: list[int | None],
) -> BufferSurvivalStats:
    """Aggregate the buffer-survival stats from per-history first-forced-sale ages.

    ``first_forced_sale_ages[i]`` is the first age history ``i`` forced a growth
    sale, or ``None`` if it never did (the buffer held all horizon). Shared by the
    Historical and Monte Carlo bucket paths (the SAME aggregation, so the two
    readouts can't drift — same discipline as ``_compute_adjustment_stats``).

    - ``pct_cohorts_forced_to_sell_growth`` = (# with a non-None age) / count.
    - ``median_years_buffer_lasted`` = median of the non-None ages, rounded to an
      int age, or None when none forced a sale ("never").
    - ``worst_forced_sale_age`` = the minimum non-None age (soonest breach), or
      None.

    An empty input (no histories) returns a zeroed object rather than dividing by
    zero (cheap insurance — a short-window multi-class bucket run could in
    principle produce an empty cohort set).
    """
    n = len(first_forced_sale_ages)
    if n == 0:
        return BufferSurvivalStats(
            count=0,
            pct_cohorts_forced_to_sell_growth=0.0,
            median_years_buffer_lasted=None,
            worst_forced_sale_age=None,
        )
    forced_ages = [a for a in first_forced_sale_ages if a is not None]
    if not forced_ages:
        return BufferSurvivalStats(
            count=n,
            pct_cohorts_forced_to_sell_growth=0.0,
            median_years_buffer_lasted=None,
            worst_forced_sale_age=None,
        )
    median_age = int(round(_median([float(a) for a in forced_ages])))
    return BufferSurvivalStats(
        count=n,
        pct_cohorts_forced_to_sell_growth=len(forced_ages) / n,
        median_years_buffer_lasted=median_age,
        worst_forced_sale_age=min(forced_ages),
    )


def _simulate_cohort(
    scenario: Scenario,
    returns: np.ndarray,
    strategy: SpendingStrategy | None = None,
) -> tuple[list[float], int | None, list[float]]:
    """Run one cohort: the deterministic sim with a per-year return sequence.

    Identical math to firebridgelab.simulate.simulate (midpoint withdrawal, four GI
    streams, strategy-shaped spending, floor at zero) except ``r`` comes from
    ``returns[step]`` each year instead of a constant blend. ``returns`` has one
    entry per age-step (start_age .. end_age inclusive).

    Spending comes from a ``SpendingStrategy`` constructed ONCE per cohort, BEFORE
    the year loop (variable-spending design). When ``strategy`` is None one is built
    from ``scenario.spending_mode`` (``make_strategy``): constant/lifecycle resolve
    to the byte-stable wrapper over ``spending_for_age`` (so the cohort math is
    unchanged and the canary holds), guardrails to the stateful GK rule that reads
    the begin-of-year portfolio. Callers that re-run the same scenario across many
    cohorts (the solver / MC) pass None and let each call build its own (cheap,
    and correctly resets the guardrail's carried state per run).

    Returns ``(end_portfolios, depleted_age, spends)`` where ``end_portfolios[k]``
    is the end-of-year balance and ``spends[k]`` is the spend withdrawn at age
    ``start_age + k`` (the spends feed the Historical spending fan), and
    ``depleted_age`` is the first age the balance hit zero (or ``None``).
    """
    if strategy is None:
        strategy = make_strategy(scenario)

    end_portfolios: list[float] = []
    spends: list[float] = []
    portfolio = scenario.initial_portfolio
    depleted_age: int | None = None

    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        r = net_return(float(returns[step]), scenario.annual_fee)
        gi = gi_at_age(scenario, age)
        # Spending from the strategy. The begin-of-year portfolio is ``portfolio``
        # at the TOP of this iteration (the prior year's ``end``, before this
        # year's growth/withdrawal) — the denominator the guardrail rate uses, per
        # the design. constant/lifecycle ignore it (byte-stable).
        spending = strategy.spend_for_year(scenario, age, portfolio, gi)
        # Cost-of-living real drift (stress-test design slice 4): a post-strategy
        # multiplier on the funded spend, applied in BOTH loops for consistency.
        # Default spending_real_drift 0.0 ⇒ (1+0)**k == 1.0 ⇒ byte-identical (the
        # Historical + MC default-scenario canaries hold).
        spending *= spending_multiplier(scenario, age)
        # Realized-spend cap: the strategy returns the INTENDED spend; a depleting
        # cohort year can only fund what the portfolio + GI cover. The RECORDED
        # spend (the ``spends`` list that feeds the fan + adjustment stats) is capped
        # at ``max_fundable_spend`` (non-depleting: intended <= max_fundable, no-op).
        # This is a REPORTING value only — the portfolio is STILL stepped with the
        # INTENDED ``spending`` so ``end`` AND the depletion flag stay byte-identical
        # (the flag fires on end < 0; max_fundable lands exactly ON 0 and would NOT
        # flag, so it must not be the value stepped). The strategy keeps its own
        # intended base, so the guardrail nudge is unaffected.
        realized_spend = min(spending, max_fundable_spend(portfolio, r, gi))
        # Midpoint convention via the shared engine step (same canonical form
        # the deterministic engine uses). Slice 3: rewired off the
        # ``portfolio * (1.0 + r)`` expression onto the canonical
        # ``portfolio + portfolio*r`` form — byte-identical against the
        # Historical + MC default-scenario canaries on the live cohort grid. Stepped
        # with the INTENDED spend to preserve the depletion flag (see above).
        already_depleted = depleted_age is not None
        end, depleted_this_year = step_year(portfolio, r, gi, spending)
        if depleted_this_year and depleted_age is None:
            depleted_age = age
        # Absorbing depletion: once a path has depleted, the portfolio
        # stays at 0 for every later year — a GI stream starting after depletion
        # must not resurrect the balance. ``step_year`` is a memoryless per-year
        # helper (post-depletion it computes ``end = 0 + 0 + (gi − spend)·(1+r/2)``,
        # which goes positive whenever GI exceeds the funded gap), so the absorbing
        # state lives HERE, in the one loop all return modes share. The recorded
        # ``realized_spend`` above already caps at ``max_fundable_spend(0, r, gi)``
        # = ``gi`` — the GI-funded level — so post-depletion spend semantics
        # are unchanged. ``depleted_age`` (FIRST crossing) is set before this
        # override and never moves. Byte-identical for any path that never recovers
        # post-depletion (end is already 0.0 then) — verified at defaults, so the
        # Historical/MC canaries hold.
        if already_depleted:
            end = 0.0
        end_portfolios.append(end)
        spends.append(realized_spend)
        portfolio = end

    return end_portfolios, depleted_age, spends


def _median(values: list[float]) -> float:
    """Plain median of a non-empty list (NumPy, so it matches the fan's p50)."""
    return float(np.median(np.array(values, dtype=float)))


def _compute_adjustment_stats(
    scenario: Scenario,
    cohort_spends: list[list[float]],
    cohort_events: list[list[AdjustmentEvent]],
) -> AdjustmentStats:
    """Aggregate the cost-of-flexibility stats across all cohorts (slice 2).

    Definitions are pinned in ``AdjustmentStats``. "Below the start level" /
    "above" are vs ``scenario.annual_spending`` (the guardrail's start spend);
    "at / forced down to the floor" uses ``scenario.spending_floor`` within
    ``_SPEND_EPSILON``. ``deepest_cut`` is the p10 of per-cohort minimum spend;
    ``lowest_single_year_spend`` is the GLOBAL single-year minimum across all
    cohorts (NOT the displayed worst cohort's minimum — see ``AdjustmentStats``).

    Guard: an empty ``cohort_spends`` returns a zeroed stats object rather than
    dividing by zero / calling ``min``/``np.percentile`` on an empty list. Today
    this path is only reached behind a ``cohort_spends``-truthy gate, but slice 3's
    short-window multi-class cohorts under guardrails could combine into a small (or
    empty) cohort set — cheap insurance so that can never raise.
    """
    n = len(cohort_spends)
    if n == 0:
        return AdjustmentStats(
            cohort_count=0,
            pct_ever_cut=0.0,
            pct_ever_raised=0.0,
            median_first_cut_age=None,
            median_years_at_floor=0.0,
            deepest_cut=0.0,
            lowest_single_year_spend=0.0,
            pct_ever_forced_to_floor=0.0,
        )
    start_spend = scenario.annual_spending
    floor = scenario.spending_floor

    per_cohort_min = [min(spends) for spends in cohort_spends]

    if scenario.survivor_age is None:
        ever_cut = sum(1 for m in per_cohort_min if m < start_spend - _SPEND_EPSILON)
        ever_raised = sum(
            1 for spends in cohort_spends if max(spends) > start_spend + _SPEND_EPSILON
        )
        ever_hit_floor = sum(1 for m in per_cohort_min if m <= floor + _SPEND_EPSILON)
        years_at_floor = [
            sum(1 for s in spends if s <= floor + _SPEND_EPSILON) for spends in cohort_spends
        ]
    else:
        # Survivor years spend ``survivor_spending_fraction`` of the couple's
        # level, so compare each year against the rails scaled the same way —
        # otherwise every post-death year would read as a cut to the floor.
        scale = [
            scenario.survivor_spending_fraction if is_survivor_year(scenario, scenario.start_age + k) else 1.0
            for k in range(len(cohort_spends[0]))
        ]

        def _norm(spends: list[float]) -> list[float]:
            return [s_ / f if f > 0 else s_ for s_, f in zip(spends, scale)]

        normed = [_norm(spends) for spends in cohort_spends]
        ever_cut = sum(1 for sp in normed if min(sp) < start_spend - _SPEND_EPSILON)
        ever_raised = sum(1 for sp in normed if max(sp) > start_spend + _SPEND_EPSILON)
        ever_hit_floor = sum(1 for sp in normed if min(sp) <= floor + _SPEND_EPSILON)
        years_at_floor = [
            sum(1 for s_ in sp if s_ <= floor + _SPEND_EPSILON) for sp in normed
        ]

    # Median age of the FIRST cut, over the cohorts that cut at all.
    first_cut_ages: list[int] = []
    for events in cohort_events:
        for ev in events:
            if ev.action == "cut":
                first_cut_ages.append(ev.age)
                break
    median_first_cut_age = (
        int(round(_median([float(a) for a in first_cut_ages])))
        if first_cut_ages
        else None
    )

    deepest_cut = float(np.percentile(np.array(per_cohort_min, dtype=float), 10))
    lowest_single_year_spend = float(min(per_cohort_min))

    return AdjustmentStats(
        cohort_count=n,
        pct_ever_cut=ever_cut / n,
        pct_ever_raised=ever_raised / n,
        median_first_cut_age=median_first_cut_age,
        median_years_at_floor=_median([float(y) for y in years_at_floor]),
        deepest_cut=deepest_cut,
        lowest_single_year_spend=lowest_single_year_spend,
        pct_ever_forced_to_floor=ever_hit_floor / n,
    )
