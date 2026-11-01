"""Tests for the SS claim-age actuarial helper (firebridgelab.ss_claim).

Pins the ``ss_factor`` table for FRA 67 against the EXACT SSA values (the
acceptance contract — these match SSA's published reduction/credit factors): whole-percent
ages by exact equality, the two fractional ages (65/66) by their closed form.
Also covers the out-of-range guard, ``effective_ss_amount`` = fra × factor, and
a non-67 FRA to prove the per-month formula generalizes (it is NOT a hardcoded
per-age table).
"""

import pytest

from firebridgelab.ss_claim import (
    DEFAULT_FRA_AGE,
    MAX_CLAIM_AGE,
    MIN_CLAIM_AGE,
    effective_ss_amount,
    ss_factor,
)


# The acceptance contract: exact factors for the whole-percent ages, closed-form
# for the two fractional ones. (5/9 of 1% = 1/180 per month.)
_FRACTION_5_9 = (5.0 / 9.0) / 100.0  # per-month early reduction, first 36 months


@pytest.mark.parametrize(
    "claim_age, expected",
    [
        (62, 0.70),
        (63, 0.75),
        (64, 0.80),
        (67, 1.00),
        (68, 1.08),
        (69, 1.16),
        (70, 1.24),
    ],
)
def test_ss_factor_exact_whole_percent_ages(claim_age, expected):
    """The whole-percent ages (62/63/64/67/68/69/70) are EXACT at FRA 67."""
    assert ss_factor(claim_age) == expected


def test_ss_factor_age_65_closed_form():
    """Age 65 = 24 months early (all in the first-36 tier): 1 − 24·(5/9)/100."""
    assert ss_factor(65) == pytest.approx(1.0 - 24 * _FRACTION_5_9)
    # And the published rounded value.
    assert ss_factor(65) == pytest.approx(0.8666666666, abs=1e-9)


def test_ss_factor_age_66_closed_form():
    """Age 66 = 12 months early: 1 − 12·(5/9)/100."""
    assert ss_factor(66) == pytest.approx(1.0 - 12 * _FRACTION_5_9)
    assert ss_factor(66) == pytest.approx(0.9333333333, abs=1e-9)


def test_ss_factor_default_fra_is_67():
    """The default FRA is 67 (both people assumed born 1960+), so the default-arg call
    matches the explicit-67 call at every age."""
    assert DEFAULT_FRA_AGE == 67
    for age in range(MIN_CLAIM_AGE, MAX_CLAIM_AGE + 1):
        assert ss_factor(age) == ss_factor(age, 67)


@pytest.mark.parametrize("claim_age", [61, 71, 50, 100, 0])
def test_ss_factor_out_of_range_raises(claim_age):
    """A claim age outside [62, 70] raises ValueError (→ 422 at the API)."""
    with pytest.raises(ValueError):
        ss_factor(claim_age)


def test_effective_ss_amount_applies_factor():
    """effective_ss_amount = fra_amount × ss_factor for a couple of cases."""
    # At FRA the factor is 1.0 → amount unchanged.
    assert effective_ss_amount(33_000.0, 67) == pytest.approx(33_000.0)
    # Claim at 64 → factor 0.80 → 33000 × 0.80 = 26400.0 (the API-test contract).
    assert effective_ss_amount(33_000.0, 64) == pytest.approx(26_400.0)
    # Claim at 70 → factor 1.24.
    assert effective_ss_amount(32_000.0, 70) == pytest.approx(32_000.0 * 1.24)


def test_effective_ss_amount_propagates_range_error():
    """An out-of-range claim age raises through effective_ss_amount too."""
    with pytest.raises(ValueError):
        effective_ss_amount(33_000.0, 61)


def test_ss_factor_generalizes_to_non_67_fra():
    """The per-month formula generalizes to a different FRA (NOT a hardcoded
    per-age table). With FRA 66, claiming at 62 is 48 months early = the first 36
    months at 5/9-of-1% PLUS 12 months at 5/12-of-1%:
        1 − (36·(5/9) + 12·(5/12))/100 = 1 − (20 + 5)/100 = 0.75.
    """
    assert ss_factor(62, fra_age=66) == pytest.approx(0.75)
    # At FRA itself the factor is exactly 1.0 regardless of FRA.
    assert ss_factor(66, fra_age=66) == 1.0
