"""Federal tax against IRS publications and worked examples.

Sources, all read 2026-11-02:

- Rev. Proc. 2025-32, section 4.01 (Tables 1 and 3, the 2026 tax rate
  schedules), 4.03 (capital-gain breakpoints) and 4.14 (standard deduction
  and the aged amount). https://www.irs.gov/pub/irs-drop/rp-25-32.pdf
- Publication 915 (2025), Worksheet 1 and Examples 1-3.
  https://www.irs.gov/publications/p915
- Instructions for Form 1040 (2025), Qualified Dividends and Capital Gain Tax
  Worksheet - Line 16, p. 38. https://www.irs.gov/pub/irs-pdf/i1040gi.pdf
- Instructions for Form 8960 (2025), "Application to Individuals".
  https://www.irs.gov/pub/irs-pdf/i8960.pdf
- Publication 590-B (2025), chapter 1, required minimum distributions
  (worked example) and the additional tax on early distributions (Tom Jones
  example). https://www.irs.gov/pub/irs-pdf/p590b.pdf
- Treas. Reg. 1.401(a)(9)-9(c), Uniform Lifetime Table (eCFR, current as of
  2026-10-29). https://www.ecfr.gov/current/title-26/part-1/section-1.401(a)(9)-9
"""

from __future__ import annotations

import pytest

from firebridgelab.tax.core import (
    EARLY_WITHDRAWAL_PENALTY_RATE,
    compute_federal_tax,
    standard_deduction,
)
from firebridgelab.tax.location import step_buckets_dragged
from firebridgelab.tax.rmd import RMD_START_AGE, rmd_factor
from firebridgelab.tax.ss_taxation import taxable_social_security

# Rev. Proc. 2025-32 sec. 4.01, as printed: (over, base tax, rate on the excess).
SCHEDULE_MFJ = [  # Table 1, married filing jointly
    (0, 0.0, 0.10),
    (24_800, 2_480.0, 0.12),
    (100_800, 11_600.0, 0.22),
    (211_400, 35_932.0, 0.24),
    (403_550, 82_048.0, 0.32),
    (512_450, 116_896.0, 0.35),
    (768_700, 206_583.50, 0.37),
]
SCHEDULE_SINGLE = [  # Table 3, unmarried individuals
    (0, 0.0, 0.10),
    (12_400, 1_240.0, 0.12),
    (50_400, 5_800.0, 0.22),
    (105_700, 17_966.0, 0.24),
    (201_775, 41_024.0, 0.32),
    (256_225, 58_448.0, 0.35),
    (640_600, 192_979.25, 0.37),
]
# Sec. 4.14(1) and (3).
STD_MFJ, STD_SINGLE = 32_200.0, 16_100.0
AGED_MFJ, AGED_SINGLE = 1_650.0, 2_050.0
# Sec. 4.03: maximum zero-rate and 15%-rate amounts.
CG_MFJ, CG_SINGLE = (98_900.0, 613_700.0), (49_450.0, 545_500.0)


def _schedule_tax(taxable: float, schedule) -> float:
    """The published schedule: base tax plus the rate on the excess."""
    over, base, rate = max(row for row in schedule if row[0] <= max(taxable, 0.0))
    return base + rate * (taxable - over) if taxable > 0 else 0.0


INCOMES = [5_000, 12_400, 24_800, 40_000, 50_400, 75_000, 100_800, 150_000,
           211_400, 256_225, 300_000, 403_550, 512_450, 640_600, 768_700, 1_000_000]


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("taxable", INCOMES)
def test_rate_schedule_rev_proc_2025_32(taxable, single):
    """Ordinary tax on taxable income equals the Rev. Proc. 2025-32 schedule
    (Table 1 joint, Table 3 single) at bracket edges and between them. The
    model is fed gross income = taxable income + the standard deduction.
    Tolerance: half a cent."""
    schedule, std = (SCHEDULE_SINGLE, STD_SINGLE) if single else (SCHEDULE_MFJ, STD_MFJ)
    got = compute_federal_tax(taxable + std, 0.0, single=single)
    assert got.ordinary_tax == pytest.approx(_schedule_tax(taxable, schedule), abs=0.005)


def test_standard_deduction_and_aged_amount():
    """Rev. Proc. 2025-32 sec. 4.14: $32,200 joint / $16,100 single, plus
    $1,650 per spouse 65 or older on a joint return and $2,050 for an
    unmarried filer. Tolerance: exact."""
    assert standard_deduction(0) == STD_MFJ
    assert standard_deduction(2) == STD_MFJ + 2 * AGED_MFJ
    assert standard_deduction(0, single=True) == STD_SINGLE
    assert standard_deduction(1, single=True) == STD_SINGLE + AGED_SINGLE
    # The first dollar over the deduction is taxed at 10%.
    assert compute_federal_tax(STD_MFJ + 2 * AGED_MFJ + 1, 0.0, n_65_plus=2).total == pytest.approx(0.10)


@pytest.mark.parametrize(
    "example, benefits, other_income, single, published",
    [
        # Example 1: single; pension 18,600 + wages 9,400 + interest 990.
        ("Example 1", 5_980.0, 28_990.0, True, 2_990.0),
        # Example 2: joint; 15,500 + 14,000 + 250 = 29,750 on line 3, less the
        # $1,000 IRA deduction on line 6 (the model's other income is after
        # adjustments).
        ("Example 2", 5_600.0, 29_750.0 - 1_000.0, False, 0.0),
        # Example 3: joint; line 3 = 40,500; line 8 = 45,500 > 44,000.
        ("Example 3", 10_000.0, 40_500.0, False, 6_275.0),
    ],
)
def test_pub_915_worksheet_1_examples(example, benefits, other_income, single, published):
    """IRS Pub. 915 (2025), Worksheet 1 filled-in examples: the taxable part of
    the benefits (line 19). Example 4 (married filing separately, lived
    together) is out of scope: the model has no separate returns.
    Tolerance: one dollar (the worksheet rounds to whole dollars)."""
    got = taxable_social_security(benefits, other_income, single)
    assert got == pytest.approx(published, abs=1.0), example


def _qdcg_worksheet(taxable_income: float, gains: float, single: bool) -> float:
    """Form 1040 instructions (2025), Qualified Dividends and Capital Gain Tax
    Worksheet, lines 1-25, with the 2026 amounts of Rev. Proc. 2025-32 sec.
    4.03 on lines 6 and 13, and the 2026 schedule on lines 22 and 24 (the
    Tax Computation Worksheet; the Tax Table below $100,000 rounds to $50
    bands, which the model does not)."""
    schedule = SCHEDULE_SINGLE if single else SCHEDULE_MFJ
    zero_max, fifteen_max = CG_SINGLE if single else CG_MFJ
    l1 = taxable_income
    l4 = gains
    l5 = max(0.0, l1 - l4)
    l6 = zero_max
    l7 = min(l1, l6)
    l8 = min(l5, l7)
    l9 = l7 - l8
    l10 = min(l1, l4)
    l12 = l10 - l9
    l13 = fifteen_max
    l14 = min(l1, l13)
    l15 = l5 + l9
    l16 = max(0.0, l14 - l15)
    l17 = min(l12, l16)
    l18 = 0.15 * l17
    l19 = l9 + l17
    l20 = l10 - l19
    l21 = 0.20 * l20
    l22 = _schedule_tax(l5, schedule)
    l23 = l18 + l21 + l22
    l24 = _schedule_tax(l1, schedule)
    return min(l23, l24)


QDCG_CASES = [
    # (ordinary taxable income, long-term gain)
    (0.0, 50_000.0),
    (40_000.0, 40_000.0),
    (60_000.0, 80_000.0),
    (98_000.0, 1_500.0),     # the 15% band starts inside the 12% bracket
    (98_900.0, 1_000.0),     # joint: line 24 is smaller than line 23
    (99_500.0, 1_000.0),
    (150_000.0, 100_000.0),
    (500_000.0, 300_000.0),
    (700_000.0, 50_000.0),
    (48_000.0, 2_000.0),     # single: the 15% band starts inside the 12% bracket
    (49_450.0, 900.0),       # single: line 24 is smaller than line 23
    (200_000.0, 400_000.0),
]


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("ordinary, gains", QDCG_CASES)
def test_qualified_dividends_capital_gain_worksheet(ordinary, gains, single):
    """Gains stacked on ordinary income through the 0/15/20% bands, as the
    IRS worksheet computes them, including line 25: the smaller of the
    stacked tax and the regular tax on all taxable income. (Line 25 binds
    only just above $98,900 joint / $49,450 single, where the 15% band
    starts below the top of the 12% bracket; the model missed it until
    0.2.0, overstating tax there by up to $57.) Tolerance: half a cent."""
    std = STD_SINGLE if single else STD_MFJ
    got = compute_federal_tax(ordinary + std, gains, single=single)
    expected = _qdcg_worksheet(ordinary + gains, gains, single)
    assert got.ordinary_tax + got.ltcg_tax == pytest.approx(expected, abs=0.005)


@pytest.mark.parametrize(
    "ordinary, gains, single, published_rule",
    [
        # Joint, MAGI 300,000, NII 20,000: 3.8% of min(20,000, 50,000).
        (280_000.0, 20_000.0, False, 0.038 * 20_000),
        # Joint, MAGI 330,000, NII 80,000: 3.8% of min(80,000, 80,000).
        (250_000.0, 80_000.0, False, 0.038 * 80_000),
        # Joint, MAGI 300,000, NII 80,000: 3.8% of min(80,000, 50,000).
        (220_000.0, 80_000.0, False, 0.038 * 50_000),
        # Single, MAGI 210,000, NII 50,000: 3.8% of min(50,000, 10,000).
        (160_000.0, 50_000.0, True, 0.038 * 10_000),
        # Below the threshold: none.
        (150_000.0, 40_000.0, False, 0.0),
    ],
)
def test_niit_form_8960_rule(ordinary, gains, single, published_rule):
    """Form 8960 instructions (2025), "Application to Individuals": the tax is
    3.8% of the smaller of net investment income and MAGI over $250,000
    (joint) or $200,000 (single). The instructions have no numeric worked
    example for this computation, so the cases apply the stated rule. In the
    model, net investment income is the year's long-term gains (it does not
    tax interest or dividends) and MAGI is AGI. Tolerance: half a cent."""
    got = compute_federal_tax(ordinary, gains, single=single)
    assert got.niit == pytest.approx(published_rule, abs=0.005)


# Treas. Reg. 1.401(a)(9)-9(c), Table 2 (Uniform Lifetime Table), ages 75-120+.
ULT = {
    75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2, 81: 19.4, 82: 18.5,
    83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2,
    91: 11.5, 92: 10.8, 93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3,
    99: 6.8, 100: 6.4, 101: 6.0, 102: 5.6, 103: 5.2, 104: 4.9, 105: 4.6, 106: 4.3,
    107: 4.1, 108: 3.9, 109: 3.7, 110: 3.5, 111: 3.4, 112: 3.3, 113: 3.1, 114: 3.0,
    115: 2.9, 116: 2.8, 117: 2.7, 118: 2.5, 119: 2.3, 120: 2.0,
}


def test_uniform_lifetime_table():
    """Every divisor from the first RMD age (75, born 1960 or later) to "120
    and over" equals Treas. Reg. 1.401(a)(9)-9(c). Tolerance: exact."""
    assert RMD_START_AGE == 75
    for age, divisor in ULT.items():
        assert rmd_factor(age) == divisor, age
    assert rmd_factor(125) == ULT[120]


def test_pub_590b_rmd_example():
    """Pub. 590-B (2025), chapter 1: an owner who turns 75 in 2026 with a
    $100,000 balance at the end of 2025 must take $4,065 ($100,000 / 24.6).
    The model's yearly step, with nothing else drawn that year, moves exactly
    that much out of the tax-deferred account and taxes it. Tolerance: one
    dollar (the publication rounds to whole dollars)."""
    step = step_buckets_dragged(
        500_000.0, 100_000.0, 0.0, 0.0, 0.0, 0.0, True,
        pension_ss_ordinary=0.0, gains_fraction=0.0, n_65_plus=2, age=75,
        bracket_fill_target=None, prior_tax_deferred=100_000.0,
    )
    assert step.rmd_excess == pytest.approx(4_065.0, abs=1.0)
    assert step.ordinary_income == pytest.approx(step.rmd_excess)


def test_early_withdrawal_additional_tax():
    """Pub. 590-B (2025), chapter 1 (Tom Jones, 35): a $3,000 early
    distribution with no exception owes "an additional tax of $300 (10% (0.10)
    x $3,000)". In the model's yearly step at 35, the penalty is 10% of every
    tax-deferred dollar drawn: the $3,000 of spending and the draw that pays
    the tax on it, which is itself an early distribution. At 60 it is zero.
    Tolerance: one cent."""
    assert EARLY_WITHDRAWAL_PENALTY_RATE * 3_000.0 == pytest.approx(300.0)
    kwargs = dict(pension_ss_ordinary=40_000.0, gains_fraction=0.0, n_65_plus=0,
                  bracket_fill_target=None, prior_tax_deferred=100_000.0)
    early = step_buckets_dragged(0.0, 100_000.0, 0.0, 0.0, 0.0, 3_000.0, False, age=35, **kwargs)
    assert early.tax_deferred_draw == pytest.approx(3_000.0)
    drawn = early.tax_deferred_draw + early.tf_tax_deferred_draw
    assert early.penalty == pytest.approx(0.10 * drawn, abs=0.01)
    late = step_buckets_dragged(0.0, 100_000.0, 0.0, 0.0, 0.0, 3_000.0, True, age=60, **kwargs)
    assert late.penalty == 0.0
