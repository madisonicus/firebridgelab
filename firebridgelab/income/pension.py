"""Pension income in real dollars.

Amounts are entered in today's dollars, or, with ``pension_nominal_until_start``, as
the nominal check at commencement (deflated at ``assumed_inflation``). The real path
after commencement depends on the COLA (``full`` keeps it level; ``fers_diet`` applies
the FERS rule of 5 U.S.C. 8462), the stress-only ``pension_real_drift``, and the
survivor fraction after the first death. The arithmetic is shared by every
stream kind and lives in ``income.household``; this module keeps the COLA vocabulary
and the FERS rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


def pension_income_at_age(scenario: Scenario, age: int) -> float:
    """Summed real pension income at ``age`` (every pension-kind stream, after
    the COLA, nominal and survivor rules; see ``income.household``)."""
    from firebridgelab.income.household import household

    return household(scenario).year(age).pension
