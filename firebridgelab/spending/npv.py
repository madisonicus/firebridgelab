"""Pure financial math helpers used across the engine.

So far: NPV of future spending gaps. Strategies that need a "funded ratio"
(portfolio / NPV-of-gaps) compute the denominator once at construction time
using ``compute_npv_gaps_by_age``, then look up the value per year.

Kept separate from ``firebridgelab.income`` concerns and the withdrawal mechanics: this
module is pure-math — no I/O, no mutation, no dataclass state. It is the FR
foundation (funded-ratio feature slice 1): the funded-ratio spending strategies
(slices 2-4) discount a per-year *gap* between a real target spend and the
year's guaranteed income, then divide a smoothed portfolio by that NPV.

Ported from the archived ``firebridgelab/finance.py`` (the legacy v6 oracle), adapted to
the CURRENT ``Scenario``/GI model: the GI summation reuses
``firebridgelab.scenario.gi_at_age`` so the NPV's per-year GI can never drift from the
engine's per-year GI (the same anti-drift discipline the guardrail's ``initial_gi``
anchor follows). The single-age-axis convention (both people share one age
timeline; every GI stream fires on ``age >= start_age``) is the same one the rest
of the current engine uses — there is no separate spouse-age offset here, unlike
the archived two-age version.
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.income.guaranteed import gi_at_age


def compute_npv_gaps_by_age(
    scenario: Scenario,
    target_real: float,
    discount_rate: float = 0.01,
) -> dict[int, float]:
    """Precompute the NPV of (target_real − GI) from each age to ``end_age``.

    For each ``person_a_age`` in ``[scenario.start_age, scenario.end_age]``, walks
    forward year by year and accumulates the discounted gap::

        NPV(age) = Σ_{t=0..end_age-age} max(target_real - GI(age + t), 0)
                   / (1 + discount_rate)^t

    where ``GI(a)`` is the summed guaranteed income active at age ``a`` — the
    SAME four-stream sum the engines use per year (via
    ``firebridgelab.scenario.gi_at_age``), so the NPV denominator can't drift from the
    engine's per-year GI.

    Args:
        scenario: the run's Scenario (supplies the horizon + the four GI streams).
        target_real: the per-year real spending level whose gap above GI is
            discounted. This is the FR *numerator-equivalent* target — its choice
            is load-bearing (the ``fr-xmastree`` variant discounts the MAX go-go
            tier amount, NOT the floor; ``fr-smooth`` / ``fr-guardrail`` discount
            ``scenario.annual_spending``). See ``FundedRatioStrategyBase``.
        discount_rate: real discount rate (default 0.01, the legacy 1%).

    Returns:
        ``dict[int, float]`` mapping ``age -> NPV`` at that age, keys covering
        ``[start_age, end_age]`` inclusive. NPV declines monotonically toward the
        horizon; at ``end_age`` it is just that year's gap (no future terms).

    Pure: no I/O, no mutation of ``scenario``.
    """
    if discount_rate < -0.99:
        raise ValueError(f"discount_rate {discount_rate} unreasonably negative")

    start_age = scenario.start_age
    end_age = scenario.end_age

    # Per-age gap first; then a single backward Bellman pass yields all NPVs in
    # O(n) and is slightly more accurate numerically than re-summing each age.
    gaps_by_age: dict[int, float] = {}
    for age in range(start_age, end_age + 1):
        gi = gi_at_age(scenario, age)
        gaps_by_age[age] = max(target_real - gi, 0.0)

    # NPV(a) = gap(a) + NPV(a+1) / (1 + r). Walk from the horizon backward.
    npv_by_age: dict[int, float] = {}
    discount = 1.0 + discount_rate
    npv_future = 0.0
    for age in range(end_age, start_age - 1, -1):
        npv_here = gaps_by_age[age] + npv_future / discount
        npv_by_age[age] = npv_here
        npv_future = npv_here

    return npv_by_age


def npv_gaps_at_age(
    scenario: Scenario,
    age: int,
    target_real: float,
    discount_rate: float = 0.01,
) -> float:
    """NPV at a SINGLE age of the GI-netted gap stream from ``age`` to ``end_age``.

    The focused, single-age form of ``compute_npv_gaps_by_age`` — it returns exactly
    ``compute_npv_gaps_by_age(scenario, target_real, discount_rate)[age]`` without
    building the whole ``[start_age, end_age]`` table::

        NPV(age) = Σ_{t=0..end_age-age} max(target_real - GI(age + t), 0)
                   / (1 + discount_rate)^t

    The amortization-frontier strategy bisects on ``target_real`` (= the constant-real
    spend ``L``) each year: the LHS is monotonically INCREASING in ``target_real``
    (a higher target → larger per-year gaps → larger NPV), so a single root exists and
    bisection is valid. Calling this once per bisection step over the forward window
    from the current age is cheaper than rebuilding the full backward table each step,
    and keeps the solve self-contained.

    Pure: no I/O, no mutation of ``scenario``. ``age`` is assumed in
    ``[start_age, end_age]`` (the strategy only ever asks for in-horizon ages).
    """
    if discount_rate < -0.99:
        raise ValueError(f"discount_rate {discount_rate} unreasonably negative")

    end_age = scenario.end_age
    discount = 1.0 + discount_rate
    npv = 0.0
    factor = 1.0  # 1 / (1 + r)^t, accumulated forward from t = 0
    for a in range(age, end_age + 1):
        gap = max(target_real - gi_at_age(scenario, a), 0.0)
        npv += gap * factor
        factor /= discount
    return npv
