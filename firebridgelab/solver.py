"""Spending solver — the INVERSE of a projection.

A normal run takes a spend and (could) read off a success rate. The solver
flips that: you set a confidence target and it finds the **most you can spend**
that still clears it, then writes that number back into the scenario's
``annual_spending`` so the normal projection renders at the solved level.

The solver is a **search, not a spending strategy**. It binary-searches a
single scalar — ``annual_spending`` — which ``spending_for_age`` already
interprets as the flat spend (constant mode) or the **go-go peak** the tapered
curve hangs from (lifecycle mode). So the solver is **shape-agnostic**: it never
knows or cares about the spending shape (the design notes
§"Core concept").

Held fixed during a solve: portfolio, allocation, GI streams, ages, spending
*shape* (mode + multipliers + phase ages), confidence target. Solved: the single
``annual_spending`` level dial.

Survival (locked MVP definition)
--------------------------------
Per cohort, survival = the portfolio never hits zero before ``end_age`` (the
engine's existing depletion: ``depleted_age is None``). Confidence = success
rate = ``1 - depletion_count / cohort_count``. This is conservative and labeled
— it ignores that GI keeps flowing after the portfolio drains (the income-floor
redefinition is a separate backlog card, deliberately not smuggled in here).

Why bisection is valid (monotonicity)
--------------------------------------
``end = portfolio*(1+r) + (GI - spending)*(1+r/2)``; the spend coefficient
``(1 + r/2) > 0`` for all admissible ``r > -1`` and GI is spend-independent, so
``dend/dspend <= 0`` for every cohort-year. Lifecycle phase multipliers (>= 0)
preserve it. So ``success_fraction`` is weakly non-increasing in spend — a clean
bisection target. **Any future spend-dependent term (taxes, ACA cliffs, RMDs)
must re-derive this or bisection breaks silently** (the property test guards it).

Performance
-----------
``evaluate()`` runs a LEAN, depletion-only inner loop: it reuses the SAME
per-cohort math as ``historical._simulate_cohort`` (so the math can't drift) but
counts only depletions — it never builds the percentile fan ``simulate_historical``
does. The cohorts' blended-return arrays are spend-independent, so the historical
solve precomputes them once and re-runs only the cheap loop per probe (~10-15
probes). Sub-second.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from firebridgelab.historical import (
    cohort_return_sequences,
)
from firebridgelab.monte_carlo import _monte_carlo_evaluator
from firebridgelab.sim_core import _simulate_cohort
from firebridgelab.scenario import SOURCING_MODE_BUCKETS, Scenario
from firebridgelab.shiller import ShillerData
from firebridgelab.simulate import simulate
from firebridgelab.spending import (
    BucketsNotSolvable,
    SpendingStrategyNotSolvable,
    TaxDragMonteCarloNotSolvable,
    make_strategy,
)
from firebridgelab.tax_location import (
    deterministic_blended_returns,
    has_tax_split,
    simulate_dragged_depletion_only,
    year_tax_context_table,
)

# tax-drag design td1, DECISION 8: the MC solve fence message (pinned so the API
# and the tests both read the exact same sentence, never a paraphrase).
TAX_DRAG_MC_FENCE_MESSAGE = (
    "max-spend solve is not yet available with tax drag in Monte Carlo mode"
)

# Solver modes mirror the projection modes (firebridgelab.simulate vs firebridgelab.historical
# vs firebridgelab.monte_carlo).
MODE_DETERMINISTIC = "deterministic"
MODE_HISTORICAL = "historical"
MODE_MONTE_CARLO = "monte_carlo"

# Bisection tolerance ($/yr): refine the bracket until it's this narrow. A
# couple extra iterations vs a coarser tolerance, negligible cost.
_TOLERANCE = 100.0

# Hard iteration caps so the search can never hang (the expansion converges in
# ~log2(cap/guess) steps and the bisection in ~log2(span/tolerance); both are
# well under 40 for any admissible scenario — the cap is a safety net).
_MAX_EXPANSION_STEPS = 40
_MAX_BISECTION_STEPS = 40

# Starting guess for the exponential bracket expansion ($/yr). Doubled until the
# solve fails (or the ceiling cap is hit).
_INITIAL_GUESS = 50_000.0

# Display rounding: report the boundary spend rounded DOWN to this granularity,
# so the shown figure still clears the target (round-to-nearest could surface a
# number that misses the bar). Consistency, not conservatism.
_DISPLAY_ROUNDING = 1_000.0

# Path-dependent spending strategies (currently GuardrailStrategy) break the
# monotonicity proof the bisection rests on (see "Why bisection is valid" above):
# their floor clamp drops spend to the floor in bad sequences, so survival is
# non-monotonic in the spend dial and the solver would return a confidently-wrong
# number. The two public entry points ask the constructed strategy whether it's
# solvable and raise ``SpendingStrategyNotSolvable`` BEFORE any bisection runs —
# the strategy owns the fence and the reason text, so the solver no longer needs
# to know which spending modes are view-only.


@dataclass(frozen=True)
class SolverResult:
    """Outcome of a max-sustainable-spend solve.

    ``solved_spend`` is the reported boundary spend, rounded DOWN to the nearest
    $1,000. ``achieved_fraction`` is the survival fraction recomputed AT that
    displayed number, so ``achieved_fraction >= confidence_target`` holds for the
    reported figure by construction. ``hit_ceiling`` is True iff the scenario
    still survived at the search cap (GI alone covers all spend); then
    ``solved_spend`` is that cap. ``cohort_count`` is the number of stochastic
    paths evaluated — historical cohorts in ``historical`` mode, sampled trials in
    ``monte_carlo`` mode (``None`` for deterministic, which is a single path).
    """

    solved_spend: float
    achieved_fraction: float
    confidence_target: float
    hit_ceiling: bool
    mode: str
    cohort_count: int | None


def _historical_evaluator(scenario: Scenario, data: ShillerData | None = None):
    """Build a fast survival-fraction function over a spend.

    The cohorts' blended-return arrays are spend-independent, so we compute them
    ONCE here, then the returned ``evaluate(spend)`` re-runs only the lean
    depletion-only cohort loop per probe. Returns ``(evaluate, cohort_count)``.

    Built via ``firebridgelab.historical.cohort_return_sequences`` — the SAME cohort
    construction ``simulate_historical`` uses — so the solver's window (legacy
    Shiller for the 2-class default, the D3 multi-class common-overlap window
    otherwise) always matches the display engine's. Raises ``ValueError`` (no
    mapped class / no common-overlap window) on a scenario that can't run
    historically — the API routes that to a 422.
    """
    start_years, cohort_returns = cohort_return_sequences(scenario, data)
    cohort_count = len(cohort_returns)

    def evaluate(spend: float) -> float:
        probe = replace(scenario, annual_spending=spend)
        depletion_count = 0
        for returns in cohort_returns:
            # Reuse the SAME per-cohort math as simulate_historical (midpoint
            # withdrawal, four GI streams, spending_for_age, floor at zero); we
            # take only depleted_age and discard the trajectory — no percentile
            # fan, no trajectory list retained.
            _ends, depleted_age, _spends = _simulate_cohort(probe, returns)
            if depleted_age is not None:
                depletion_count += 1
        return 1.0 - depletion_count / cohort_count

    return evaluate, cohort_count


def _historical_evaluator_dragged(scenario: Scenario, data: ShillerData | None = None):
    """The DRAGGED analogue of ``_historical_evaluator`` (tax-drag design td1,
    DECISION 8): survival = the DRAGGED path never depletes, not the pre-tax
    scalar.

    Same precompute-once shape: the cohorts' blended-return arrays are
    spend-independent, built ONCE via ``cohort_return_sequences`` — the exact
    SAME cohort construction ``simulate_historical`` and the plain
    ``_historical_evaluator`` use, so a dragged solve always runs over the
    SAME cohort set the dragged display fan does. Each probe re-runs only the
    lean ``simulate_dragged_depletion_only`` per cohort (skips series
    recording, breaks on first depletion — see that function's docstring).
    A FRESH spending-strategy instance per cohort per probe (mirrors the plain
    evaluator + the display path: a stateful strategy's carried state must not
    leak across cohorts). The per-age tax context (pension/SS/deduction
    lookups — never a function of ``annual_spending``) is precomputed ONCE for
    the whole solve, not once per probe: every one of the ~15 bisection probes
    shares it.
    """
    start_years, cohort_returns = cohort_return_sequences(scenario, data)
    cohort_count = len(cohort_returns)
    year_context = year_tax_context_table(scenario)

    def evaluate(spend: float) -> float:
        probe = replace(scenario, annual_spending=spend)
        depletion_count = 0
        for returns in cohort_returns:
            strategy = make_strategy(probe)
            depleted_age = simulate_dragged_depletion_only(
                probe, returns, strategy, year_context=year_context
            )
            if depleted_age is not None:
                depletion_count += 1
        return 1.0 - depletion_count / cohort_count

    return evaluate, cohort_count


def evaluate(scenario: Scenario, mode: str) -> float:
    """Survival fraction for ``scenario`` under ``mode``.

    - ``historical``: the fraction of historical cohorts that never deplete
      (``1 - depletion_count / cohort_count``), via the lean depletion-only loop.
    - ``monte_carlo``: the fraction of sampled trials that never deplete, via the
      SAME shared sampler the display path uses (fixed seed/count → reproducible),
      again through a lean depletion-only loop.
    - ``deterministic``: a single path, so survival is binary — ``1.0`` if the
      one trajectory never depletes, else ``0.0``.

    Raises ``ValueError`` for an unknown mode, or (historical / monte_carlo) for a
    scenario that can't run (no mapped allocation class / no full-window cohort).
    Raises ``SpendingStrategyNotSolvable`` (a ``ValueError`` subclass) when the
    scenario's spending strategy is path-dependent (currently guardrails — view-
    only): the monotonicity that makes the spend search valid does not hold there.
    Raises ``BucketsNotSolvable`` (also a ``ValueError`` subclass) when
    ``sourcing_mode == "buckets"`` — the bucket sourcing is view-only in slice 1a
    (fenced like guardrails), so the solver never runs over it.

    Tax-drag (design td1, DECISION 8): when a tax-location split is supplied
    (single sourcing — buckets already fenced above), ``historical`` and
    ``deterministic`` ride the DRAGGED engine (survival = the dragged path
    never depletes). ``monte_carlo`` + a split raises
    ``TaxDragMonteCarloNotSolvable`` (a ``ValueError`` subclass) BEFORE any
    evaluation — the MC dragged solve is a later card (see that exception's
    docstring).
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        raise BucketsNotSolvable(
            "max-spend solve is not available in buckets sourcing mode (view-only)"
        )
    strategy = make_strategy(scenario)
    if not strategy.is_solvable:
        raise SpendingStrategyNotSolvable(strategy.reason_not_solvable)
    split_on = has_tax_split(scenario)
    if split_on and mode == MODE_MONTE_CARLO:
        raise TaxDragMonteCarloNotSolvable(TAX_DRAG_MC_FENCE_MESSAGE)
    if mode == MODE_HISTORICAL:
        if split_on:
            evaluator, _ = _historical_evaluator_dragged(scenario)
        else:
            evaluator, _ = _historical_evaluator(scenario)
        return evaluator(scenario.annual_spending)
    if mode == MODE_MONTE_CARLO:
        evaluator, _ = _monte_carlo_evaluator(scenario)
        return evaluator(scenario.annual_spending)
    if mode == MODE_DETERMINISTIC:
        if split_on:
            depleted_age = simulate_dragged_depletion_only(
                scenario, deterministic_blended_returns(scenario), make_strategy(scenario)
            )
            return 1.0 if depleted_age is None else 0.0
        return 1.0 if simulate(scenario).depleted_age is None else 0.0
    raise ValueError(
        f"mode must be one of {MODE_HISTORICAL}, {MODE_MONTE_CARLO}, "
        f"{MODE_DETERMINISTIC} (got '{mode}')"
    )


def solve_max_spend(
    scenario: Scenario,
    threshold: float,
    mode: str,
    data: ShillerData | None = None,
) -> SolverResult:
    """Find the max ``annual_spending`` whose survival fraction clears ``threshold``.

    Binary search on the spend level:

    - **Bracket.** ``lo = 0.0`` is a guaranteed-surviving floor (zero spend can't
      deplete any cohort, so its survival fraction is 1.0 >= any threshold). Find
      ``hi`` by exponential expansion (double a starting guess until the solve
      fails). **Ceiling guard:** cap expansion at ``10 * initial_portfolio``; if
      the scenario still survives at the cap (GI alone covers all spend), return
      ``hit_ceiling=True`` with ``solved_spend`` = the cap.
    - **Bisect** ``[lo, hi]`` to +/-$100, holding the invariant
      ``survival(lo) >= threshold > survival(hi)``.
    - **Report** the boundary spend rounded DOWN to the nearest $1,000, and
      recompute ``achieved_fraction`` AT that displayed number so it clears the
      target by construction.

    Historical and Monte Carlo solve against the confidence ``threshold`` via
    their precompute-once evaluator (cohorts' / trials' return arrays are
    spend-independent, so they're sampled ONCE and only the lean depletion loop
    re-runs per probe — Monte Carlo reuses the SAME shared sampler the display
    path uses, with the fixed seed/count, so a solved number always matches the
    fan shown and re-solving the same scenario is reproducible). Deterministic
    uses ``threshold = 1.0`` (the single path must survive) and reports
    ``cohort_count = None``.

    Raises ``SpendingStrategyNotSolvable`` (a ``ValueError`` subclass), BEFORE any
    bisection, when the scenario's spending strategy is path-dependent (currently
    guardrails): survival is non-monotonic in the spend dial and the bisection's
    correctness proof does not hold. The strategy owns the verdict, so the fence
    is mode-agnostic (it fences off deterministic, historical, and monte_carlo
    alike) and mode-string-agnostic (it asks the strategy, not the scenario).

    Also raises ``BucketsNotSolvable`` (a ``ValueError`` subclass), BEFORE any
    bisection, when ``sourcing_mode == "buckets"`` — the bucket sourcing is fenced
    from the solver in slice 1a (view-only), mirroring the guardrails fence.

    Tax-drag (design td1, DECISION 8): with a split supplied (single sourcing),
    ``historical`` and ``deterministic`` ride the DRAGGED engine — the solved
    number becomes after-tax honest, via the lean dragged evaluators
    (``_historical_evaluator_dragged`` / ``simulate_dragged_depletion_only``).
    ``monte_carlo`` + a split raises ``TaxDragMonteCarloNotSolvable`` (a
    ``ValueError`` subclass) BEFORE any bisection — ~15 probes x 10k dragged
    trials (each a per-year fixed point, not a cheap scalar step) is a
    minutes-scale solve, deferred to a follow-up card.
    """
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        raise BucketsNotSolvable(
            "max-spend solve is not available in buckets sourcing mode (view-only)"
        )
    strategy = make_strategy(scenario)
    if not strategy.is_solvable:
        raise SpendingStrategyNotSolvable(strategy.reason_not_solvable)
    split_on = has_tax_split(scenario)
    if split_on and mode == MODE_MONTE_CARLO:
        raise TaxDragMonteCarloNotSolvable(TAX_DRAG_MC_FENCE_MESSAGE)

    if mode == MODE_HISTORICAL:
        if split_on:
            survives, cohort_count = _historical_evaluator_dragged(scenario, data)
        else:
            survives, cohort_count = _historical_evaluator(scenario, data)
    elif mode == MODE_MONTE_CARLO:
        # Mirror historical exactly: precompute the seeded trials ONCE, then
        # bisect on spend over those FIXED draws across all probes (reproducible,
        # and the monotonicity that makes bisection valid). Same narrow
        # evaluate(spend) → survival_fraction interface.
        survives, cohort_count = _monte_carlo_evaluator(scenario, data=data)
    elif mode == MODE_DETERMINISTIC:
        if split_on:
            # Neither the blended-return sequence nor the per-age tax context
            # depends on ``annual_spending`` — precompute both ONCE, outside
            # the bisection closure, instead of every one of the ~15-40
            # probes rebuilding them.
            det_returns = deterministic_blended_returns(scenario)
            det_year_context = year_tax_context_table(scenario)

            def survives(spend: float) -> float:
                probe = replace(scenario, annual_spending=spend)
                depleted_age = simulate_dragged_depletion_only(
                    probe,
                    det_returns,
                    make_strategy(probe),
                    year_context=det_year_context,
                )
                return 1.0 if depleted_age is None else 0.0
        else:
            def survives(spend: float) -> float:
                probe = replace(scenario, annual_spending=spend)
                return 1.0 if simulate(probe).depleted_age is None else 0.0
        cohort_count = None
    else:
        raise ValueError(
            f"mode must be one of {MODE_HISTORICAL}, {MODE_MONTE_CARLO}, "
            f"{MODE_DETERMINISTIC} (got '{mode}')"
        )

    ceiling = 10.0 * scenario.initial_portfolio

    def clears(spend: float) -> bool:
        return survives(spend) >= threshold

    # Bracket expansion: lo always clears (0 spend survives), grow hi until it
    # fails. Cap at the ceiling; if even the ceiling clears, GI covers all spend.
    lo = 0.0
    hi = _INITIAL_GUESS
    hit_ceiling = False
    for _ in range(_MAX_EXPANSION_STEPS):
        if hi >= ceiling:
            hi = ceiling
            if clears(hi):
                hit_ceiling = True
            break
        if not clears(hi):
            break
        lo = hi
        hi *= 2.0
    else:
        # Exhausted the step cap without bracketing — clamp to the ceiling and
        # report the ceiling outcome, so a hit_ceiling result truly reports the
        # cap rather than the (uncapped, last-doubled) hi (defensive; not
        # reachable for admissible scenarios).
        hi = ceiling
        hit_ceiling = clears(ceiling)

    if hit_ceiling:
        solved_spend = _round_down(hi)
        return SolverResult(
            solved_spend=solved_spend,
            achieved_fraction=survives(solved_spend),
            confidence_target=threshold,
            hit_ceiling=True,
            mode=mode,
            cohort_count=cohort_count,
        )

    # Bisect [lo, hi] to tolerance, holding clears(lo) and not clears(hi).
    for _ in range(_MAX_BISECTION_STEPS):
        if hi - lo <= _TOLERANCE:
            break
        mid = (lo + hi) / 2.0
        if clears(mid):
            lo = mid
        else:
            hi = mid

    # Report the boundary rounded DOWN to the nearest $1k and recompute achieved
    # AT that displayed number, so achieved >= threshold holds for what's shown.
    solved_spend = _round_down(lo)
    return SolverResult(
        solved_spend=solved_spend,
        achieved_fraction=survives(solved_spend),
        confidence_target=threshold,
        hit_ceiling=False,
        mode=mode,
        cohort_count=cohort_count,
    )


def _round_down(spend: float) -> float:
    """Round a spend DOWN to the nearest $1,000 (the display granularity)."""
    return float(np.floor(spend / _DISPLAY_ROUNDING) * _DISPLAY_ROUNDING)
