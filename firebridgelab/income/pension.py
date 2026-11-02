"""income.pension (split from the flat layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from firebridgelab.income.survivor import is_survivor_year

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario


PENSION_COLA_FULL = "full"
PENSION_COLA_FERS_DIET = "fers_diet"
PENSION_COLAS = (PENSION_COLA_FULL, PENSION_COLA_FERS_DIET)


def fers_diet_cola(inflation: float) -> float:
    """FERS annual COLA for a given CPI change (5 U.S.C. 8462): full CPI up to
    2%, flat 2% between 2% and 3%, CPI minus 1% above 3%."""
    if inflation <= 0.02:
        return inflation
    if inflation <= 0.03:
        return 0.02
    return inflation - 0.01


def _pension_multiplier(scenario: Scenario, age: int, start_age: int) -> float:
    """Real value of $1 of a pension's entered amount at ``age`` (it began at
    ``start_age``): pre-commencement nominal freeze, post-commencement COLA
    shortfall, and the stress-only ``pension_real_drift``."""
    years_paid = age - start_age
    mult = (1.0 - scenario.pension_real_drift) ** years_paid
    infl = scenario.assumed_inflation
    if scenario.pension_nominal_until_start:
        mult /= (1.0 + infl) ** max(0, start_age - scenario.start_age)
    if scenario.pension_cola == PENSION_COLA_FERS_DIET:
        mult *= ((1.0 + fers_diet_cola(infl)) / (1.0 + infl)) ** years_paid
    return mult


def pension_income_at_age(scenario: Scenario, age: int) -> float:
    """Summed real pension income at ``age`` (both pensions; see the Scenario
    pension-inflation and survivor fields)."""
    streams = (
        ("a", scenario.person_a_pension_amount, scenario.person_a_pension_start_age),
        ("b", scenario.person_b_pension_amount, scenario.person_b_pension_start_age),
    )
    survivor = is_survivor_year(scenario, age)
    total = 0.0
    for who, amount, start in streams:
        if age < start:
            continue
        value = amount * _pension_multiplier(scenario, age, start)
        if survivor and who == scenario.survivor_decedent:
            value *= scenario.survivor_pension_fraction
        total += value
    return total
