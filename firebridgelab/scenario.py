"""Deprecated import path: ``firebridgelab.scenario``.

Its contents moved to:

- ``firebridgelab.engine.allocation``
- ``firebridgelab.engine.buckets``
- ``firebridgelab.engine.scenario``
- ``firebridgelab.engine.validation``
- ``firebridgelab.income.guaranteed``
- ``firebridgelab.income.pension``
- ``firebridgelab.income.social_security``
- ``firebridgelab.income.survivor``
- ``firebridgelab.spending.lifecycle``
- ``firebridgelab.spending.modes``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.engine.allocation import (
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    ALLOCATION_401K_ACCESS_AGE,
    DEFAULT_CORRELATION,
    AssetClass,
    CLASS_MENU,
    glidepath_boundaries,
    phase_index_for_age,
    blended_real_return,
    allocation_at_age,
    blended_real_return_at_age,
)
from firebridgelab.engine.buckets import (
    gap_for_age,
    cash_target,
    income_target,
)
from firebridgelab.engine.scenario import (
    MC_DISTRIBUTION_NORMAL,
    MC_DISTRIBUTION_STUDENT_T,
    MC_DISTRIBUTIONS,
    SOURCING_MODE_SINGLE,
    SOURCING_MODE_BUCKETS,
    ABSOLUTE_FLOOR,
    DEFAULT_PERSON_A_SS_FRA,
    DEFAULT_PERSON_B_SS_FRA,
    Bucket,
    BucketConfig,
    default_bucket_config,
    Scenario,
    default_scenario,
)
from firebridgelab.engine.validation import (
    ScenarioValidationError,
    validate_scenario,
    validate_glidepath_class_consistency,
)
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.income.pension import (
    PENSION_COLA_FULL,
    PENSION_COLA_FERS_DIET,
    PENSION_COLAS,
    fers_diet_cola,
    pension_income_at_age,
)
from firebridgelab.income.social_security import ss_income_at_age
from firebridgelab.income.survivor import SURVIVOR_DECEDENTS, is_survivor_year
from firebridgelab.spending.lifecycle import (
    spending_multiplier,
    activity_multiplier,
    spending_for_age,
)
from firebridgelab.spending.modes import (
    SPENDING_MODE_CONSTANT,
    SPENDING_MODE_LIFECYCLE,
    SPENDING_MODE_GUARDRAILS,
    SPENDING_MODE_FR_SMOOTH,
    SPENDING_MODE_FR_GUARDRAIL,
    SPENDING_MODE_FR_TIERS,
    SPENDING_MODE_AMORTIZATION,
)

__all__ = [
    "SPENDING_MODE_CONSTANT",
    "SPENDING_MODE_LIFECYCLE",
    "SPENDING_MODE_GUARDRAILS",
    "SPENDING_MODE_FR_SMOOTH",
    "SPENDING_MODE_FR_GUARDRAIL",
    "SPENDING_MODE_FR_TIERS",
    "SPENDING_MODE_AMORTIZATION",
    "ALLOCATION_MODE_CONSTANT",
    "MC_DISTRIBUTION_NORMAL",
    "MC_DISTRIBUTION_STUDENT_T",
    "MC_DISTRIBUTIONS",
    "PENSION_COLA_FULL",
    "PENSION_COLA_FERS_DIET",
    "PENSION_COLAS",
    "SURVIVOR_DECEDENTS",
    "ALLOCATION_MODE_GLIDEPATH",
    "SOURCING_MODE_SINGLE",
    "SOURCING_MODE_BUCKETS",
    "ALLOCATION_401K_ACCESS_AGE",
    "ABSOLUTE_FLOOR",
    "DEFAULT_PERSON_A_SS_FRA",
    "DEFAULT_PERSON_B_SS_FRA",
    "DEFAULT_CORRELATION",
    "AssetClass",
    "CLASS_MENU",
    "Bucket",
    "BucketConfig",
    "default_bucket_config",
    "Scenario",
    "fers_diet_cola",
    "is_survivor_year",
    "pension_income_at_age",
    "ss_income_at_age",
    "gi_at_age",
    "spending_multiplier",
    "gap_for_age",
    "cash_target",
    "income_target",
    "glidepath_boundaries",
    "phase_index_for_age",
    "activity_multiplier",
    "spending_for_age",
    "ScenarioValidationError",
    "validate_scenario",
    "default_scenario",
    "blended_real_return",
    "allocation_at_age",
    "validate_glidepath_class_consistency",
    "blended_real_return_at_age",
]
