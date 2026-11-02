"""Pure federal-tax primitives — the LEAF module (tax-drag design, DECISION 4's
module-layering fix).

Why this module exists (the import-cycle this untangles): the dragged
decomposition in ``firebridgelab.tax_location`` needs the FULL tax computation (not
just the RMD factor it already used pre-drag) to resolve its per-year fixed
point — but ``firebridgelab.federal_tax`` has always imported ``firebridgelab.tax_location``
(for ``PRE_595_MAX_AGE`` / ``TaxLocationPath`` / ``resolve_split``). Letting
``tax_location`` import back from ``federal_tax`` would be a cycle. So the pure
primitives that both modules need move here, to a LEAF with no engine
dependencies of its own:

    federal_tax_core   (this module — imports nothing from the other two)
        ^                       ^
        |                       |
    tax_location.py        federal_tax.py
    (imports the leaf       (imports the leaf AND tax_location; re-exports
     for its fixed point)    every name this module defines, unchanged, so
                             every existing ``from firebridgelab.federal_tax import
                             X`` — API and test alike — keeps working)

Two public entry points:

  - ``compute_federal_tax(ordinary_income, ltcg_income, n_65_plus=0)`` — pure
    year-income -> ``FederalTaxYear`` (ordinary tax / LTCG tax / NIIT / total).
    UNCHANGED from the federal-tax overlay design (ft1) — moved verbatim, not
    rewritten; the five IRS-anchor tests in ``tests/test_federal_tax.py`` pin
    it and must not move.
  - ``n_65_plus_at_age(age)`` / the RMD helpers (``RMD_START_AGE``,
    ``rmd_factor``) — the small age-keyed lookups both ``tax_location``'s
    fixed point and ``federal_tax``'s (now much thinner) overlay pass need.

2026 MFJ constants (VERIFIED against IRS Rev. Proc. 2025-32 — the 2026 annual
inflation adjustments) are FROZEN in REAL dollars per ADR 0004: the brackets,
standard deduction, and LTCG breakpoints are CPI-indexed IN LAW, but this
engine runs entirely in real (today's) dollars, so holding them constant in
real terms is the coherent choice. Two exceptions are frozen NOMINAL in law
and held fixed-real here too (documented simplifications, disclosed in the
ft1/td1 design notes): the 85%-flat SS inclusion rule and the NIIT $250k MAGI
threshold.

The 0% LTCG bracket is load-bearing (the most-likely-wrong spot in the PURE
tax layer): LTCG tax is STACKED on top of ordinary taxable income through the
real 0/15/20 brackets, never flat-rated — see ``_stacked_ltcg_tax``.
"""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.tax import params


_PARAMS = params.load()
_MFJ = _PARAMS["mfj"]
_SINGLE = _PARAMS["single"]

# Married filing jointly. The couple is treated as one age, so both get the
# per-person 65+ addition together (``n_65_plus_at_age``). The temporary
# 2025-2028 senior deduction is not modelled.
_ORDINARY_BRACKETS_MFJ: params.Brackets = params.brackets(_MFJ["ordinary_brackets"])
STANDARD_DEDUCTION_MFJ: float = _MFJ["standard_deduction"]
ADDITIONAL_DEDUCTION_65_PLUS: float = _MFJ["additional_deduction_65_plus"]
_LTCG_BREAKPOINTS_MFJ: params.Brackets = params.brackets(_MFJ["ltcg_breakpoints"])

# Net Investment Income Tax: rate x min(net investment income, max(0, MAGI -
# threshold)). Net investment income is approximated by the year's LTCG and
# MAGI by ordinary + LTCG income before deductions. The thresholds are fixed
# in law, so holding them fixed in real terms is mildly optimistic over a
# long horizon.
NIIT_RATE: float = _PARAMS["niit"]["rate"]
NIIT_THRESHOLD_MFJ: float = _MFJ["niit_threshold"]

# Single filer, used from the survivor year on (``Scenario.survivor_age``).
# Conservative by the one year a surviving spouse may still file jointly.
_ORDINARY_BRACKETS_SINGLE: params.Brackets = params.brackets(_SINGLE["ordinary_brackets"])
STANDARD_DEDUCTION_SINGLE: float = _SINGLE["standard_deduction"]
ADDITIONAL_DEDUCTION_65_PLUS_SINGLE: float = _SINGLE["additional_deduction_65_plus"]
_LTCG_BREAKPOINTS_SINGLE: params.Brackets = params.brackets(_SINGLE["ltcg_breakpoints"])
NIIT_THRESHOLD_SINGLE: float = _SINGLE["niit_threshold"]


def standard_deduction(n_65_plus: int, single: bool = False) -> float:
    """Standard deduction incl. the per-person 65+ addition, by filing status."""
    if single:
        return STANDARD_DEDUCTION_SINGLE + ADDITIONAL_DEDUCTION_65_PLUS_SINGLE * n_65_plus
    return STANDARD_DEDUCTION_MFJ + ADDITIONAL_DEDUCTION_65_PLUS * n_65_plus

# Additional tax on a tax-deferred draw before 59 1/2, applied on top of the
# ordinary tax by ``tax.location`` (to every walled-year tax-deferred draw,
# including draws that fund the tax itself).
EARLY_WITHDRAWAL_PENALTY_RATE: float = _PARAMS["early_withdrawal"]["penalty_rate"]

# Couple-same-age approximation for the extra 65+ standard deduction: both are
# treated as 65+ together once the single engine age axis crosses 65.
_N_65_PLUS_AT_65 = 2


def n_65_plus_at_age(age: int) -> int:
    """The couple-same-age 65+ deduction-multiplier count at ``age``.

    2 once ``age >= 65`` (both members of the couple get the extra per-person
    standard deduction simultaneously — the documented same-age approximation),
    else 0. Factored out so ``tax_location``'s fixed point and ``federal_tax``'s
    overlay pass compute this identically (previously duplicated as a bare
    ``2 if age >= 65 else 0`` inline in ``federal_tax.compute_tax_overlay``).
    """
    return _N_65_PLUS_AT_65 if age >= 65 else 0


def _marginal_tax(taxable: float, brackets: tuple[tuple[float, float], ...]) -> float:
    """Marginal-bracket tax on ``taxable`` income given an ascending (upper,
    rate) bracket table, starting from $0. Shared by the ordinary-income calc;
    LTCG uses the STACKED variant below (``_stacked_ltcg_tax``) since gains
    ride on top of ordinary income rather than from $0.
    """
    if taxable <= 0.0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if taxable <= lower:
            break
        # perf: inline conditional instead of ``min(taxable, upper)`` —
        # byte-identical value (CPython ``min(a, b)`` returns ``b`` only when
        # ``b < a``), no builtin-call overhead; this line runs ~10M times in a
        # split-MC 10k run.
        segment = (upper if upper < taxable else taxable) - lower
        tax += segment * rate
        lower = upper
    return tax


def _stacked_ltcg_tax(
    taxable_ordinary: float,
    taxable_ltcg: float,
    breakpoints: tuple[tuple[float, float], ...],
) -> float:
    """LTCG tax with gains STACKED on top of ordinary taxable income.

    The taxable LTCG occupies the band ``[taxable_ordinary, taxable_ordinary +
    taxable_ltcg]`` on the TOTAL-taxable-income axis; this slices that band by
    the 0/15/20 breakpoints rather than flat-rating it (the most-likely-wrong
    spot in the pure tax layer — in bridge years ordinary income is low and a
    material share of gains lands in the 0% bracket). Both inputs are already
    POST-deduction (the caller resolves the deduction split via
    ``compute_federal_tax``).
    """
    if taxable_ltcg <= 0.0:
        return 0.0
    band_start = taxable_ordinary
    band_end = taxable_ordinary + taxable_ltcg
    tax = 0.0
    lower = 0.0
    for upper, rate in breakpoints:
        if band_end <= lower:
            break
        # perf: inline conditionals instead of ``max``/``min`` builtins —
        # byte-identical values, no call overhead (see ``_marginal_tax``).
        seg_lower = band_start if band_start > lower else lower
        seg_upper = band_end if band_end < upper else upper
        if seg_upper > seg_lower:
            tax += (seg_upper - seg_lower) * rate
        lower = upper
    return tax


@dataclass(frozen=True)
class FederalTaxYear:
    """One year's federal tax, decomposed by source.

    ``total`` = ``ordinary_tax + ltcg_tax + niit``. Does NOT include the 10%
    early-withdrawal penalty — callers (``firebridgelab.tax_location``'s fixed point)
    apply that on top of ``total``, once the walled-year tax-deferred draw
    amount is known; ``compute_federal_tax`` is a pure year-income -> tax
    function with no notion of "forced" or "walled."
    """

    ordinary_tax: float
    ltcg_tax: float
    niit: float
    total: float


def compute_federal_tax(
    ordinary_income: float, ltcg_income: float, n_65_plus: int = 0, single: bool = False
) -> FederalTaxYear:
    """Federal tax (MFJ, 2026 constants) on one year's ordinary + LTCG income.

    Pure, closed-form, no firebridgelab/scenario dependency — a plain year-income ->
    tax function so it is independently unit-testable against hand-computed /
    external-reference scenario-years, AND so ``tax_location``'s per-year fixed
    point can call it repeatedly (up to 100 times/year) with negligible cost.

    - ``deduction`` = ``STANDARD_DEDUCTION_MFJ + ADDITIONAL_DEDUCTION_65_PLUS *
      n_65_plus``.
    - ``taxable_ordinary`` = ``max(0, ordinary_income - deduction)``; any
      LEFTOVER deduction (when ordinary income doesn't use it all) reduces
      taxable LTCG: ``taxable_ltcg = max(0, ltcg_income - max(0, deduction -
      ordinary_income))``.
    - Ordinary tax: marginal MFJ brackets on ``taxable_ordinary``.
    - LTCG tax: STACKED on top of ``taxable_ordinary`` through the 0/15/20
      breakpoints (``_stacked_ltcg_tax``) — never flat-rated.
    - NIIT: 3.8% x ``min(ltcg_income, max(0, MAGI - $250k))``, MAGI ~=
      ``ordinary_income + ltcg_income`` (pre-deduction, per the design's
      approximation).
    """
    ordinary_tax, ltcg_tax, niit, total = _tax_components(
        ordinary_income, ltcg_income, n_65_plus, single
    )
    return FederalTaxYear(
        ordinary_tax=ordinary_tax, ltcg_tax=ltcg_tax, niit=niit, total=total
    )


def federal_tax_total(
    ordinary_income: float, ltcg_income: float, n_65_plus: int = 0, single: bool = False
) -> float:
    """``compute_federal_tax(...).total`` without the ``FederalTaxYear``
    allocation — the hot-path variant for ``tax_location``'s per-year
    fixed point, which calls this ~3.5M times in a split-MC 10k run and reads
    ONLY the total (the frozen-dataclass construction was pure overhead
    there: four ``object.__setattr__`` calls per iterate).

    Byte-identical to ``compute_federal_tax(...).total`` by construction —
    both are thin wrappers over the SAME ``_tax_components`` arithmetic (a
    single source; the two cannot drift), pinned by a test in
    ``tests/test_federal_tax.py``.
    """
    return _tax_components(ordinary_income, ltcg_income, n_65_plus, single)[3]


def _tax_components(
    ordinary_income: float, ltcg_income: float, n_65_plus: int, single: bool = False
) -> tuple[float, float, float, float]:
    """The shared arithmetic behind ``compute_federal_tax`` /
    ``federal_tax_total``: ``(ordinary_tax, ltcg_tax, niit, total)``.

    Extracted verbatim from ``compute_federal_tax`` so the hot-path
    total and the dataclass-returning entry point share ONE implementation.
    The ``max``/``min`` builtins became inline conditionals — byte-identical
    values (CPython ``max(0.0, x)`` returns ``x`` only when ``x > 0.0``),
    no builtin-call overhead at ~3.5M calls per split-MC 10k run.
    """
    if single:
        brackets = _ORDINARY_BRACKETS_SINGLE
        ltcg_breaks = _LTCG_BREAKPOINTS_SINGLE
        niit_threshold = NIIT_THRESHOLD_SINGLE
    else:
        brackets = _ORDINARY_BRACKETS_MFJ
        ltcg_breaks = _LTCG_BREAKPOINTS_MFJ
        niit_threshold = NIIT_THRESHOLD_MFJ
    deduction = standard_deduction(n_65_plus, single)
    taxable_ordinary = ordinary_income - deduction
    if taxable_ordinary < 0.0:
        taxable_ordinary = 0.0
    leftover_deduction = deduction - ordinary_income
    if leftover_deduction < 0.0:
        leftover_deduction = 0.0
    taxable_ltcg = ltcg_income - leftover_deduction
    if taxable_ltcg < 0.0:
        taxable_ltcg = 0.0

    ordinary_tax = _marginal_tax(taxable_ordinary, brackets)
    ltcg_tax = _stacked_ltcg_tax(
        taxable_ordinary, taxable_ltcg, ltcg_breaks
    )

    magi = ordinary_income + ltcg_income
    niit_base = magi - niit_threshold
    if niit_base < 0.0:
        niit_base = 0.0
    if ltcg_income < niit_base:
        niit_base = ltcg_income
    niit = NIIT_RATE * niit_base

    total = ordinary_tax + ltcg_tax + niit
    return ordinary_tax, ltcg_tax, niit, total
