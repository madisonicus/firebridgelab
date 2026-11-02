"""NPV of future spending gaps — the funded-ratio denominator.

The funded-ratio spending strategies discount a per-year *gap* between a real
target spend and the year's guaranteed income, then divide a smoothed portfolio
by that NPV (GI here is net of required spending, ``net_income_at_age``, so a
spending adjustment is part of every gap); they compute the table once at
construction with
``compute_npv_gaps_by_age`` and look up the value per year. The amortization
frontier bisects on the single-age form, ``npv_gaps_at_age``.

Pure math — no I/O, no mutation, no dataclass state. The GI summation reuses
``net_income_at_age`` (``income.guaranteed``) so the NPV's per-year GI can never
drift from the engine's per-year GI. Ages are person A's age (the engine axis).
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.income.guaranteed import net_income_at_age


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
    SAME household sum the engines hand the spending rule each year, net of
    required spending (``income.guaranteed.net_income_at_age``), so the NPV
    denominator can't drift from the engine's per-year income.

    Args:
        scenario: the run's Scenario (supplies the horizon + the household flows).
        target_real: the per-year real spending level whose gap above GI is
            discounted. This is the FR *numerator-equivalent* target — its choice
            is load-bearing (the ``fr_tiers`` variant discounts the MAX go-go
            tier amount, NOT the floor; ``fr_smooth`` / ``fr_guardrail`` discount
            ``scenario.annual_spending``). See ``FundedRatioStrategyBase``.
        discount_rate: real discount rate (default 0.01).

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
        gi = net_income_at_age(scenario, age)
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
        gap = max(target_real - net_income_at_age(scenario, a), 0.0)
        npv += gap * factor
        factor /= discount
    return npv
