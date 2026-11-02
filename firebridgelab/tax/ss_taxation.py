"""How much Social Security is taxable: the provisional-income rule.

IRC 86 and IRS Publication 915 (Worksheet 1). Provisional income is the year's
other income (here: every ordinary and capital-gain item in federal AGI apart
from the benefits) plus half the benefits. Below the base amount nothing is
taxable; between the base and the adjusted base amount up to 50% is; above the
adjusted base up to 85% is:

    line 10 = provisional - base               (nothing taxable if <= 0)
    line 11 = adjusted_base - base             (9,000 single, 12,000 joint)
    line 12 = max(0, line 10 - line 11)
    line 15 = min(benefits / 2, min(line 10, line 11) / 2)
    taxable = min(0.85 * benefits, line 15 + 0.85 * line 12)

The base amounts are not indexed in law, so callers pass ``scale``
(``tax.thresholds.nominal_scale``), and they shrink in real terms.

``Scenario.ss_taxation`` selects the rule: ``"provisional"`` (this module) or
``"flat85"`` (85% of every benefit is taxable whatever the income: the
pre-M4 behaviour, which overstates tax for low-income years).
"""

from __future__ import annotations

from firebridgelab.tax import params

_P = params.load()["social_security_taxation"]

BASE_SINGLE: float = _P["base_amount_single"]
BASE_MFJ: float = _P["base_amount_mfj"]
ADJUSTED_BASE_SINGLE: float = _P["adjusted_base_amount_single"]
ADJUSTED_BASE_MFJ: float = _P["adjusted_base_amount_mfj"]
TIER1_RATE: float = _P["tier1_rate"]
TIER2_RATE: float = _P["tier2_rate"]
FLAT_INCLUSION: float = _P["flat_inclusion"]

SS_TAXATION_PROVISIONAL = "provisional"
SS_TAXATION_FLAT85 = "flat85"
SS_TAXATION_MODES = (SS_TAXATION_PROVISIONAL, SS_TAXATION_FLAT85)


def taxable_social_security(
    benefits: float, other_income: float, single: bool, scale: float = 1.0
) -> float:
    """The taxable part of ``benefits`` (Pub. 915 Worksheet 1).

    ``other_income`` is every other item of federal AGI (before the benefits);
    ``scale`` is the real value of the 2026 base amounts this year.
    """
    if benefits <= 0.0:
        return 0.0
    if single:
        base, adjusted = BASE_SINGLE * scale, ADJUSTED_BASE_SINGLE * scale
    else:
        base, adjusted = BASE_MFJ * scale, ADJUSTED_BASE_MFJ * scale
    over_base = other_income + TIER1_RATE * benefits - base          # line 10
    if over_base <= 0.0:
        return 0.0
    span = adjusted - base                                           # line 11
    over_adjusted = over_base - span if over_base > span else 0.0    # line 12
    tier1 = TIER1_RATE * (over_base if over_base < span else span)   # line 14
    half = TIER1_RATE * benefits
    if half < tier1:                                                 # line 15
        tier1 = half
    taxable = tier1 + TIER2_RATE * over_adjusted                     # line 17
    cap = TIER2_RATE * benefits                                      # line 18
    return taxable if taxable < cap else cap


def fill_amount(
    other_ordinary: float,
    ltcg: float,
    benefits: float,
    target_ordinary: float,
    single: bool,
    scale: float = 1.0,
) -> float:
    """The extra ordinary income ``c >= 0`` that brings total ordinary income,
    ``other_ordinary + c + taxable_social_security(...)``, up to
    ``target_ordinary``, with the benefits taxed by the provisional rule.

    Used by the bracket-fill lever and the Roth conversion policy: every extra
    dollar of conversion can also make up to 85 cents of benefits taxable, so
    the fill is smaller than ``target - other`` by that amount. The function
    ``g(c) = other + c + taxable(other + c + ltcg)`` is piecewise linear and
    increasing (slope 1, 1.5 or 1.85), so the root is found exactly from its
    breakpoints.
    """

    def g(c: float) -> float:
        return other_ordinary + c + taxable_social_security(
            benefits, other_ordinary + c + ltcg, single, scale
        )

    if g(0.0) >= target_ordinary:
        return 0.0
    if benefits <= 0.0:
        return target_ordinary - other_ordinary
    if single:
        base, adjusted = BASE_SINGLE * scale, ADJUSTED_BASE_SINGLE * scale
    else:
        base, adjusted = BASE_MFJ * scale, ADJUSTED_BASE_MFJ * scale
    half = TIER1_RATE * benefits
    span = adjusted - base
    tier1_max = min(half, TIER1_RATE * span)
    # Provisional-income levels where the slope of g can change.
    kinks = [base, min(base + benefits, adjusted), adjusted]
    kinks.append(adjusted + max(0.0, TIER2_RATE * benefits - tier1_max) / TIER2_RATE)
    offset = other_ordinary + ltcg + half
    prev_c, prev_g = 0.0, g(0.0)
    for pi in sorted(kinks):
        c = pi - offset
        if c <= prev_c:
            continue
        gc = g(c)
        if gc >= target_ordinary:
            return prev_c + (target_ordinary - prev_g) * (c - prev_c) / (gc - prev_g)
        prev_c, prev_g = c, gc
    return prev_c + (target_ordinary - prev_g)
