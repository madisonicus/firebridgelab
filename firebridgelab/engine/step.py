"""Shared per-year portfolio step (the engine's atomic move).

One function — ``step_year`` — applies one year of the midpoint-withdrawal
mechanic to a portfolio given the year's real return, guaranteed income, and
spending. All three engines (Deterministic, Historical cohort replay, Monte
Carlo trial replay) call it, so the year-step math has ONE definition and
cannot drift between modes. ``net_return`` nets the annual fee out of a year's
return and ``max_fundable_spend`` inverts the step.

Keep this module narrow: no scenario, no strategy, no GI accumulation, no
per-year reporting — just the portfolio step itself. Anything broader belongs in
the calling engine.
"""

from __future__ import annotations



def net_return(r: float, annual_fee: float) -> float:
    """A year's real return net of an all-in annual cost: ``(1+r)(1-fee) - 1``.
    ``annual_fee == 0`` returns ``r`` itself, so a zero fee is an exact no-op."""
    if annual_fee == 0.0:
        return r
    return (1.0 + r) * (1.0 - annual_fee) - 1.0


def step_year(
    start_portfolio: float, r: float, gi: float, spending: float
) -> tuple[float, bool]:
    """Apply one year of the mid-year cash-flow mechanic.

    Pure portfolio step: given the year's inputs, return the end-of-year
    balance (floored at 0) and a flag for whether the floor was touched this
    year.

    Timing convention: spending AND guaranteed income are both treated as
    flows at mid-year, so each earns (or forgoes) half a year of return:

        growth = start_portfolio * r
        end = start_portfolio + growth + gi * (1 + r / 2) - spending * (1 + r / 2)

    Crediting GI with no growth while charging spending half a year would be a
    conservative asymmetry (~8% of terminal wealth on the Plan of Record). The
    form above is the canonical operand order the regression tests pin
    (ADR 0001/0002).

    Spending and GI are inputs to the step — the caller decides how to compute
    them (deterministic vs cohort vs guardrail). This helper just steps the
    portfolio.
    """
    growth = start_portfolio * r
    end_portfolio = start_portfolio + growth + gi * (1 + r / 2) - spending * (1 + r / 2)
    depleted_this_year = False
    if end_portfolio < 0:
        end_portfolio = 0.0
        depleted_this_year = True
    return end_portfolio, depleted_this_year


def max_fundable_spend(start_portfolio: float, r: float, gi: float) -> float:
    """The largest spend that brings end-of-year to exactly 0 under the mid-year
    convention — i.e. what the household can actually fund this year.

    Solving ``0 == start + start*r + (gi - spend)*(1 + r/2)`` for ``spend``:

        max_fundable = start*(1 + r) / (1 + r / 2) + gi

    Used to derive the REALIZED spend (``min(intended, max_fundable)``) that the
    engine RECORDS — distinct from the spend it steps the portfolio with. In a
    non-depleting year ``intended <= max_fundable`` so realized == intended; in
    a depleting year the recorded spend is capped to what was actually
    affordable (exactly ``gi`` once the portfolio is gone). The portfolio path
    is unchanged either way: stepping with ``intended`` floors end to 0, and
    stepping with ``max_fundable`` lands on 0. ``1 + r/2 > 0`` for every
    admissible return (r > -1).
    """
    return start_portfolio * (1.0 + r) / (1.0 + r / 2.0) + gi
