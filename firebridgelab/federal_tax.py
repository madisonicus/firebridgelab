"""Federal-tax overlay — now a thin REPACKAGING pass over the dragged
tax-location decomposition (tax-drag design td1
supersedes the ft1/ft2 "display-only accounting pass" role this module used to
play).

WHAT CHANGED (DECISION 4's module-layering fix + td-a scope item 5): under
ft1/ft2 this module OWNED the entire year-tax computation — it read tb1's
untaxed shadow decomposition, derived ordinary/LTCG income, ran
``compute_federal_tax``, applied the penalty, and split the attribution. Under
td1 the portfolio itself pays its tax bill, and computing that bill requires
resolving a per-year FIXED POINT coupled to the internal transfers (the
voluntary bracket-fill lever, the RMD-excess movement) — machinery that has to
live where the account balances live, i.e. ``firebridgelab.tax_location``. So:

  - The pure tax PRIMITIVES (``compute_federal_tax``, the MFJ constants, the
    RMD table) moved to the leaf module ``firebridgelab.federal_tax_core`` (imported
    by BOTH ``tax_location`` and this module — see that module's docstring for
    the layering diagram). This module RE-EXPORTS every one of those names
    unchanged, so every existing ``from firebridgelab.federal_tax import X`` — the
    API layer, ``firebridgelab/__init__.py``, and the five ft1 IRS-anchor tests —
    keeps working without modification.
  - The per-year income/tax/attribution computation moved INTO
    ``firebridgelab.tax_location.step_buckets_dragged`` (called from
    ``simulate_path_decomposition``), because it is now COUPLED to the
    transfers and the account balances, not a separable read-only pass.
  - This module's ``compute_tax_overlay`` shrinks to PURE REPACKAGING: it reads
    the already-resolved per-age series straight off the dragged
    ``TaxLocationPath`` (``tax`` / ``spending_tax`` / ``voluntary_tax`` /
    ``forced_tax`` / ``penalty`` / ``ordinary_income`` / ``ltcg_income`` /
    ``gross_outflow``) and NEVER re-derives them — single source of truth is
    the decomposition (a re-derivation here could silently diverge from the
    fixed point's actual resolved values). td-d retires the one field that
    WAS computed here (not merely copied): ``net_spending`` = ``realized_spend
    - spending_tax`` (design DECISION 7's "interim-compat" field, superseded
    end-to-end by the gross-outflow line — ``api/main.py`` and its tests no
    longer read it).

Two public entry points (unchanged surface from ft1/ft2):

  - ``compute_tax_overlay(scenario, path)`` — maps ONE dragged
    ``TaxLocationPath`` to a per-age ``FederalTaxPath``.
  - ``aggregate_tax_overlay(paths)`` — reduces a list of ``FederalTaxPath``
    (one per cohort/trial) to the Historical/MC headline aggregate, mirroring
    ``firebridgelab.tax_location.aggregate_tax_location_risk``.
"""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.federal_tax_core import (
    ADDITIONAL_DEDUCTION_65_PLUS,
    EARLY_WITHDRAWAL_PENALTY_RATE,
    NIIT_RATE,
    NIIT_THRESHOLD_MFJ,
    RMD_START_AGE,
    STANDARD_DEDUCTION_MFJ,
    FederalTaxYear,
    compute_federal_tax,
    n_65_plus_at_age,
    rmd_factor,
)
from firebridgelab.tax_location import TaxLocationPath

__all__ = [
    "ADDITIONAL_DEDUCTION_65_PLUS",
    "EARLY_WITHDRAWAL_PENALTY_RATE",
    "NIIT_RATE",
    "NIIT_THRESHOLD_MFJ",
    "RMD_START_AGE",
    "STANDARD_DEDUCTION_MFJ",
    "FederalTaxYear",
    "FederalTaxOverlayAggregate",
    "FederalTaxPath",
    "aggregate_tax_overlay",
    "compute_federal_tax",
    "compute_tax_overlay",
    "n_65_plus_at_age",
    "rmd_factor",
]


@dataclass(frozen=True)
class FederalTaxPath:
    """One path's (deterministic line, or one cohort/trial) federal-tax overlay.

    Per-age series aligned to ``ages`` (mirrors ``TaxLocationPath``) — ALL
    copied verbatim from the dragged decomposition's own recorded series
    (never re-derived; see the module docstring):

      - ``ordinary_income`` / ``ltcg_income`` — the year's taxable-income
        components (pre-deduction) the fixed point converged on.
      - ``tax`` — total federal tax INCLUDING the early-withdrawal penalty
        (0.0 once the path has already depleted in a prior year — the
        post-depletion convention, see ``firebridgelab.tax_location``).
      - ``spending_tax`` / ``voluntary_tax`` / ``forced_tax`` — DECISION 6's
        exact three-way telescoping attribution:
        ``spending_tax + voluntary_tax + forced_tax == tax`` at every age,
        EXACT ``==``.
      - ``penalty`` — the 10% early-withdrawal penalty alone (a subset of
        ``tax``, not additional to it).
      - ``gross_outflow`` — "what the portfolio actually funds" = ``spending +
        tax`` (DECISION 7's chart line, td-a records it; td-d draws it).

    Path scalars (mirrors the design's Historical/MC readout):

      - ``total_tax`` — sum of ``tax`` across the path.
      - ``avg_annual_tax`` — ``total_tax / len(ages)``.
      - ``effective_rate`` — ``total_tax / total REALIZED spending`` across the
        path (0.0 if total realized spending is 0). td-b: the denominator is
        the realized series — dividing by intended consumption that never
        happened understates the rate on depleting paths. Identical on
        non-depleting paths.
      - ``worst_tax`` / ``worst_tax_age`` — the single highest annual ``tax``
        and the age it occurred at.
      - ``total_forced_tax`` / ``total_voluntary_tax`` — the summed forced /
        voluntary shares of ``total_tax`` (the "of which forced / voluntary"
        readout components).
      - ``pretax_terminal`` / ``dragged_terminal`` — the path's LAST-year
        paired pre-tax total and dragged total (``TaxLocationPath.pretax_total
        [-1]`` and ``taxable[-1]+tax_deferred[-1]+roth[-1]``) — the "estate
        cost of taxes" readout's (DECISION 9, a later slice) two raw inputs,
        recorded here so td-b/c can pair them (median of PER-PATH differences,
        never a difference of medians) without re-deriving from
        ``TaxLocationPath`` at the aggregation layer.
    """

    ages: list[int]
    ordinary_income: list[float]
    ltcg_income: list[float]
    tax: list[float]
    spending_tax: list[float]
    voluntary_tax: list[float]
    forced_tax: list[float]
    penalty: list[float]
    gross_outflow: list[float]
    total_tax: float
    avg_annual_tax: float
    effective_rate: float
    worst_tax: float
    worst_tax_age: int
    total_forced_tax: float
    total_voluntary_tax: float
    pretax_terminal: float
    dragged_terminal: float


def compute_tax_overlay(scenario, path: TaxLocationPath) -> FederalTaxPath:
    """Map one DRAGGED ``TaxLocationPath`` to a per-age federal-tax overlay.

    Pure repackaging (see the module docstring) — reads the already-resolved
    series the fixed point recorded (``tax`` / ``spending_tax`` /
    ``voluntary_tax`` / ``forced_tax`` / ``penalty`` / ``ordinary_income`` /
    ``ltcg_income`` / ``gross_outflow``) straight off ``path`` and NEVER
    recomputes them — every field here is copied, plus the path-level scalar
    reductions (sums / medians / worst-year), unchanged in spirit from ft1/ft2.
    """
    ages = list(path.ages)
    n = len(ages)

    tax = list(path.tax)
    spending_tax = list(path.spending_tax)
    voluntary_tax = list(path.voluntary_tax)
    forced_tax = list(path.forced_tax)
    penalty = list(path.penalty)
    ordinary_income = list(path.ordinary_income)
    ltcg_income = list(path.ltcg_income)
    gross_outflow = list(path.gross_outflow)

    total_tax = sum(tax)
    avg_annual_tax = total_tax / n if n else 0.0
    total_spending = sum(path.realized_spend)
    effective_rate = total_tax / total_spending if total_spending > 0 else 0.0
    if tax:
        worst_idx = max(range(n), key=lambda k: tax[k])
        worst_tax = tax[worst_idx]
        worst_tax_age = ages[worst_idx]
    else:
        worst_tax = 0.0
        worst_tax_age = scenario.start_age

    pretax_terminal = (
        path.pretax_total[-1] if path.pretax_total else scenario.initial_portfolio
    )
    dragged_terminal = (
        path.taxable[-1] + path.tax_deferred[-1] + path.roth[-1]
        if ages
        else scenario.initial_portfolio
    )

    return FederalTaxPath(
        ages=ages,
        ordinary_income=ordinary_income,
        ltcg_income=ltcg_income,
        tax=tax,
        spending_tax=spending_tax,
        voluntary_tax=voluntary_tax,
        forced_tax=forced_tax,
        penalty=penalty,
        gross_outflow=gross_outflow,
        total_tax=total_tax,
        avg_annual_tax=avg_annual_tax,
        effective_rate=effective_rate,
        worst_tax=worst_tax,
        worst_tax_age=worst_tax_age,
        total_forced_tax=sum(forced_tax),
        total_voluntary_tax=sum(voluntary_tax),
        pretax_terminal=pretax_terminal,
        dragged_terminal=dragged_terminal,
    )


@dataclass(frozen=True)
class FederalTaxOverlayAggregate:
    """Aggregated federal-tax overlay across cohorts / trials (Historical/MC).

    The tax-overlay analogue of ``firebridgelab.tax_location.TaxLocationRisk``: median
    per-age series + median-of-scalars across paths (mirrors
    ``aggregate_tax_location_risk``'s discipline — same aggregator shared by
    both stochastic engines so the two readouts can't drift).
    """

    ages: list[int]
    tax_median: list[float]
    forced_tax_median: list[float]
    voluntary_tax_median: list[float]
    gross_outflow_median: list[float]
    median_avg_annual_tax: float
    median_effective_rate: float
    median_worst_tax: float
    median_worst_tax_age: int
    median_total_forced_tax: float
    median_total_voluntary_tax: float


def aggregate_tax_overlay(paths: list[FederalTaxPath]) -> FederalTaxOverlayAggregate:
    """Aggregate per-path federal-tax overlays into the Historical/MC headline.

    Shared by the Historical and Monte Carlo callers (same discipline as
    ``aggregate_tax_location_risk``). An empty input returns a zeroed aggregate
    rather than dividing by zero (defensive; callers only invoke this with a
    non-empty ``paths`` in practice, since it is gated the same way
    ``tax_location_risk`` is).
    """
    if not paths:
        return FederalTaxOverlayAggregate(
            ages=[],
            tax_median=[],
            forced_tax_median=[],
            voluntary_tax_median=[],
            gross_outflow_median=[],
            median_avg_annual_tax=0.0,
            median_effective_rate=0.0,
            median_worst_tax=0.0,
            median_worst_tax_age=0,
            median_total_forced_tax=0.0,
            median_total_voluntary_tax=0.0,
        )

    import numpy as np

    ages = paths[0].ages
    tax_matrix = np.array([p.tax for p in paths], dtype=float)
    forced_tax_matrix = np.array([p.forced_tax for p in paths], dtype=float)
    voluntary_tax_matrix = np.array([p.voluntary_tax for p in paths], dtype=float)
    gross_outflow_matrix = np.array([p.gross_outflow for p in paths], dtype=float)
    tax_median = np.percentile(tax_matrix, 50, axis=0).tolist()
    forced_tax_median = np.percentile(forced_tax_matrix, 50, axis=0).tolist()
    voluntary_tax_median = np.percentile(voluntary_tax_matrix, 50, axis=0).tolist()
    gross_outflow_median = np.percentile(gross_outflow_matrix, 50, axis=0).tolist()

    avg_annual_taxes = np.array([p.avg_annual_tax for p in paths], dtype=float)
    effective_rates = np.array([p.effective_rate for p in paths], dtype=float)
    worst_taxes = np.array([p.worst_tax for p in paths], dtype=float)
    worst_tax_ages = np.array([p.worst_tax_age for p in paths], dtype=float)
    total_forced_taxes = np.array([p.total_forced_tax for p in paths], dtype=float)
    total_voluntary_taxes = np.array(
        [p.total_voluntary_tax for p in paths], dtype=float
    )

    return FederalTaxOverlayAggregate(
        ages=ages,
        tax_median=tax_median,
        forced_tax_median=forced_tax_median,
        voluntary_tax_median=voluntary_tax_median,
        gross_outflow_median=gross_outflow_median,
        median_avg_annual_tax=float(np.median(avg_annual_taxes)),
        median_effective_rate=float(np.median(effective_rates)),
        median_worst_tax=float(np.median(worst_taxes)),
        median_worst_tax_age=int(round(float(np.median(worst_tax_ages)))),
        median_total_forced_tax=float(np.median(total_forced_taxes)),
        median_total_voluntary_tax=float(np.median(total_voluntary_taxes)),
    )
