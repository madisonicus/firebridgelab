"""Tests for the ``validate_scenario`` construction-invariant seam.

Each test anchors ONE invariant: a valid scenario passes, and a single mutated
field raises ``ScenarioValidationError`` naming that field. The base is always
``default_scenario()`` (which MUST validate cleanly), mutated with
``dataclasses.replace`` because the Scenario is frozen.

The seam turns the previously-IMPLICIT preconditions (buckets-mode requires a
bucket config; glidepath requires enough phases; the tax split must sum) into one
explicit, testable function with named-field errors — replacing the opaque deep
crashes those violations used to produce.
"""

from dataclasses import replace

import pytest

from firebridgelab import (
    AssetClass,
    ScenarioValidationError,
    default_bucket_config,
    default_scenario,
    glidepath_boundaries,
    validate_scenario,
)
from firebridgelab.scenario import (
    ALLOCATION_MODE_GLIDEPATH,
    SOURCING_MODE_BUCKETS,
)


def test_default_scenario_passes():
    """The canary base scenario validates cleanly (the green-suite constraint)."""
    validate_scenario(default_scenario())  # must not raise


# --- Recognized mode constants -------------------------------------------------


def test_unknown_spending_mode_rejected():
    bad = replace(default_scenario(), spending_mode="bogus")
    with pytest.raises(ScenarioValidationError, match="spending_mode"):
        validate_scenario(bad)


def test_unknown_allocation_mode_rejected():
    bad = replace(default_scenario(), allocation_mode="bogus")
    with pytest.raises(ScenarioValidationError, match="allocation_mode"):
        validate_scenario(bad)


def test_unknown_sourcing_mode_rejected():
    bad = replace(default_scenario(), sourcing_mode="bogus")
    with pytest.raises(ScenarioValidationError, match="sourcing_mode"):
        validate_scenario(bad)


# --- Portfolio / spending magnitudes -------------------------------------------


def test_nonpositive_initial_portfolio_rejected():
    bad = replace(default_scenario(), initial_portfolio=0.0)
    with pytest.raises(ScenarioValidationError, match="initial_portfolio"):
        validate_scenario(bad)


def test_negative_annual_spending_rejected():
    bad = replace(default_scenario(), annual_spending=-1.0)
    with pytest.raises(ScenarioValidationError, match="annual_spending"):
        validate_scenario(bad)


# --- Horizon / ages ------------------------------------------------------------


def test_start_age_not_less_than_end_age_rejected():
    bad = replace(default_scenario(), start_age=95, end_age=95)
    with pytest.raises(ScenarioValidationError, match="start_age"):
        validate_scenario(bad)


def test_out_of_range_age_rejected():
    bad = replace(default_scenario(), person_a_ss_start_age=999)
    with pytest.raises(ScenarioValidationError, match="person_a_ss_start_age"):
        validate_scenario(bad)


# --- Allocation ----------------------------------------------------------------


def test_empty_allocation_rejected():
    bad = replace(default_scenario(), allocation=())
    with pytest.raises(ScenarioValidationError, match="allocation"):
        validate_scenario(bad)


def test_allocation_weights_not_summing_to_one_rejected():
    bad = replace(
        default_scenario(),
        allocation=(
            AssetClass("us_total_market", "US Total Stock Market", 0.60, 0.07),
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.30, 0.02),
        ),
    )
    with pytest.raises(ScenarioValidationError, match="allocation weights"):
        validate_scenario(bad)


# --- Spending rails ------------------------------------------------------------


def test_floor_above_ceiling_rejected():
    bad = replace(
        default_scenario(), spending_floor=300_000.0, spending_ceiling=150_000.0
    )
    with pytest.raises(ScenarioValidationError, match="spending_floor"):
        validate_scenario(bad)


# --- Bucket sourcing -----------------------------------------------------------


def test_buckets_mode_without_config_rejected():
    """The deep ``scenario.buckets.n1`` AttributeError, surfaced as a named error."""
    bad = replace(default_scenario(), sourcing_mode=SOURCING_MODE_BUCKETS, buckets=None)
    with pytest.raises(ScenarioValidationError, match="buckets"):
        validate_scenario(bad)


def test_buckets_mode_with_config_passes():
    ok = replace(
        default_scenario(),
        sourcing_mode=SOURCING_MODE_BUCKETS,
        buckets=default_bucket_config(),
    )
    validate_scenario(ok)  # must not raise


def test_negative_bucket_years_rejected():
    bad = replace(
        default_scenario(),
        sourcing_mode=SOURCING_MODE_BUCKETS,
        buckets=replace(default_bucket_config(), n1=-1),
    )
    with pytest.raises(ScenarioValidationError, match="bucket sizing years"):
        validate_scenario(bad)


# --- Glidepath allocation ------------------------------------------------------


def _full_glidepath_phases(scenario):
    """One allocation per derived phase (the legal degenerate glide)."""
    n = len(glidepath_boundaries(scenario))
    return tuple(scenario.allocation for _ in range(n))


def test_glidepath_mode_without_phase_allocations_rejected():
    bad = replace(
        default_scenario(),
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=None,
    )
    with pytest.raises(ScenarioValidationError, match="phase_allocations"):
        validate_scenario(bad)


def test_glidepath_mode_with_too_few_phases_passes():
    # The engine CLAMPS a short phase list to the last supplied phase rather
    # than crashing (see allocation_at_age), so a phase_allocations list with
    # FEWER entries than derived phases is a legal run and must NOT be rejected.
    base = default_scenario()
    # Default derives 5 phases ([50, 60, 65, 67, 70]); supply only 2.
    assert len(glidepath_boundaries(base)) > 2
    ok = replace(
        base,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=(base.allocation, base.allocation),
    )
    validate_scenario(ok)  # must not raise


def test_glidepath_mode_with_full_phases_passes():
    base = default_scenario()
    ok = replace(
        base,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=_full_glidepath_phases(base),
    )
    validate_scenario(ok)  # must not raise


def test_glidepath_empty_phase_rejected():
    base = default_scenario()
    phases = list(_full_glidepath_phases(base))
    phases[1] = ()  # an empty phase
    bad = replace(
        base,
        allocation_mode=ALLOCATION_MODE_GLIDEPATH,
        phase_allocations=tuple(phases),
    )
    with pytest.raises(ScenarioValidationError, match="phase_allocations"):
        validate_scenario(bad)


# --- Tax-location split --------------------------------------------------------


def test_tax_split_summing_to_portfolio_passes():
    base = default_scenario()
    ok = replace(
        base,
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,  # sums to initial_portfolio (1.8M)
    )
    validate_scenario(ok)  # must not raise


def test_tax_split_not_summing_rejected():
    base = default_scenario()
    bad = replace(
        base,
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=100_000.0,  # 1.63M != 1.8M initial_portfolio
    )
    with pytest.raises(ScenarioValidationError, match="tax-location balances"):
        validate_scenario(bad)
