"""tax.location_risk (split from the flat layout)."""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.tax.location_path import TaxLocationPath


@dataclass(frozen=True)
class DepletionDistribution:
    """The depletion-age distribution among the paths that depleted pre-59½.

    All ages are ints; None when NO path depleted pre-59½. Mirrors the spirit of
    ``BufferSurvivalStats`` (median + worst sentinel) for the tax-location metric.

      - ``median`` — median depletion age among depleting paths.
      - ``p10`` — the 10th-percentile (earliest-tail) depletion age.
      - ``earliest`` — the single earliest depletion age across all paths.
    """

    median: int | None
    p10: int | None
    earliest: int | None


@dataclass(frozen=True)
class TaxLocationRisk:
    """Aggregated tax-location risk metric across cohorts / trials (the headline).

    The Historical / Monte Carlo analogue of ``BufferSurvivalStats``: across all
    paths, how often does the FREELY-REACHABLE money run dry before the penalty
    door opens at 59½, and how early.

      - ``count`` — number of paths covered.
      - ``pct_taxable_depleted_pre_595`` — % (0..100) of paths whose taxable
        balance hit 0 at age <= 59.
      - ``pct_reachable_depleted_pre_595`` — % (0..100) of paths whose reachable
        money (taxable + roth) hit 0 at age <= 59 (forced tax-deferred draw).
      - ``taxable_depletion`` — the depletion-age distribution among paths whose
        TAXABLE depleted pre-59½ (median / p10 / earliest).
      - ``reachable_depletion`` — same distribution for REACHABLE depletion.
    """

    count: int
    pct_taxable_depleted_pre_595: float
    pct_reachable_depleted_pre_595: float
    taxable_depletion: DepletionDistribution
    reachable_depletion: DepletionDistribution

    # Optional taxable percentile bands for charting the depletion spread (the
    # design's "optionally taxable percentile bands"). Per-age percentiles of the
    # taxable balance across paths; None on the deterministic single-path side
    # (which carries the explicit balance series instead).
    ages: list[int] | None = None
    taxable_p10: list[float] | None = None
    taxable_median: list[float] | None = None
    taxable_p90: list[float] | None = None

    # Per-account median balance series + the TOTAL-investments fan (txlb
    # hotfix). Median is not additive, so the three account medians do NOT stack
    # to ``total_median`` — that is expected and correct; the chart draws them as
    # LINES (typical composition) while the total fan carries the uncertainty.
    #   - ``tax_deferred_median`` / ``roth_median`` — per-age median across paths
    #     of each account's reconstructed end-of-year balance (taxable's median is
    #     ``taxable_median`` above).
    #   - ``total_p10`` / ``total_median`` / ``total_p90`` — per-age percentiles of
    #     the per-path TOTAL (= taxable + tax_deferred + roth). Under td1 this is
    #     the DRAGGED total (DIES: this used to match the still-pre-tax overall
    #     portfolio fan exactly; re-pointing the portfolio fan ITSELF to be the
    #     dragged total is a td-b deliverable — see
    #     ``tests/test_tax_location.py``'s re-pointed consistency test).
    tax_deferred_median: list[float] | None = None
    roth_median: list[float] | None = None
    total_p10: list[float] | None = None
    total_median: list[float] | None = None
    total_p90: list[float] | None = None


def _distribution(ages: list[int]) -> DepletionDistribution:
    """Median / p10 / earliest of a list of depletion ages (empty → all None)."""
    if not ages:
        return DepletionDistribution(median=None, p10=None, earliest=None)
    import numpy as np

    arr = np.array(ages, dtype=float)
    return DepletionDistribution(
        median=int(round(float(np.median(arr)))),
        p10=int(round(float(np.percentile(arr, 10)))),
        earliest=int(min(ages)),
    )


def aggregate_tax_location_risk(
    paths: list[TaxLocationPath],
) -> TaxLocationRisk:
    """Aggregate per-path decomposition facts into the headline risk metric.

    Shared by the Historical and Monte Carlo bucket paths (the SAME aggregation, so the
    two readouts can't drift — same discipline as ``compute_buffer_survival``).
    Percentages are 0..100. An empty input returns a zeroed metric rather than
    dividing by zero.
    """
    n = len(paths)
    if n == 0:
        empty = DepletionDistribution(median=None, p10=None, earliest=None)
        return TaxLocationRisk(
            count=0,
            pct_taxable_depleted_pre_595=0.0,
            pct_reachable_depleted_pre_595=0.0,
            taxable_depletion=empty,
            reachable_depletion=empty,
        )

    taxable_ages = [
        p.taxable_depletion_age for p in paths if p.taxable_depletion_age is not None
    ]
    reachable_ages = [
        p.reachable_depletion_age
        for p in paths
        if p.reachable_depletion_age is not None
    ]

    # Optional per-age taxable percentile bands (paths share the same age axis).
    ages = paths[0].ages
    import numpy as np

    taxable_matrix = np.array([p.taxable for p in paths], dtype=float)
    p10, median, p90 = (
        row.tolist()
        for row in np.percentile(taxable_matrix, (10, 50, 90), axis=0)
    )

    # Per-account median series (same per-age percentile machinery as taxable).
    tax_deferred_matrix = np.array([p.tax_deferred for p in paths], dtype=float)
    roth_matrix = np.array([p.roth for p in paths], dtype=float)
    tax_deferred_median = np.percentile(tax_deferred_matrix, 50, axis=0).tolist()
    roth_median = np.percentile(roth_matrix, 50, axis=0).tolist()

    # The TOTAL-investments fan: per-age percentiles of the per-path TOTAL
    # (= taxable + tax_deferred + roth). Under td1 this is the DRAGGED total by
    # construction (see the TaxLocationRisk field docstring).
    total_matrix = taxable_matrix + tax_deferred_matrix + roth_matrix
    total_p10, total_median, total_p90 = (
        row.tolist()
        for row in np.percentile(total_matrix, (10, 50, 90), axis=0)
    )

    return TaxLocationRisk(
        count=n,
        pct_taxable_depleted_pre_595=100.0 * len(taxable_ages) / n,
        pct_reachable_depleted_pre_595=100.0 * len(reachable_ages) / n,
        taxable_depletion=_distribution(taxable_ages),
        reachable_depletion=_distribution(reachable_ages),
        ages=ages,
        taxable_p10=p10,
        taxable_median=median,
        taxable_p90=p90,
        tax_deferred_median=tax_deferred_median,
        roth_median=roth_median,
        total_p10=total_p10,
        total_median=total_median,
        total_p90=total_p90,
    )


@dataclass(frozen=True)
class EstateCostOfTaxes:
    """"Estate cost of taxes" readout (td-b, DECISION 9) — framed as "how
    much smaller the estate ends because the portfolio paid the IRS", paired
    with the success-rate delta so a $0 estate cost can never misread as "taxes
    are free" in exactly the scenarios (both runs deplete) where they broke the
    plan.

    ``estate_cost = pretax_terminal - dragged_terminal`` PER PATH, where
    ``pretax_terminal`` comes from an INDEPENDENT run of the scenario's
    pre-tax/legacy world — a FRESH spending-strategy instance reading the
    UNTAXED balance, over the SAME return sequence — never the same-spend
    ``TaxLocationPath.pretax_total`` td-a recorded (that series is paired
    against the DRAGGED run's OWN spend decisions, kept for the drag-direction
    BINDS invariant; DECISION 9 wants the two runs to be honestly independent,
    "each is the honest run of its own world" — under a dynamic strategy the
    two can spend, and therefore deplete, at different times). Aggregation is
    the MEDIAN of per-path differences (paired by return sequence), never a
    difference of medians (that would let the two worlds' medians come from
    DIFFERENT paths).

      - ``count`` — number of paths (1 for the deterministic single pair).
      - ``median_estate_cost`` — median across paths of
        ``pretax_terminal[i] - dragged_terminal[i]``; the single value at
        ``count == 1``.
      - ``pretax_success_rate`` / ``dragged_success_rate`` — fraction (0..1) of
        paths that did NOT deplete by ``end_age`` in each independent world.
      - ``success_rate_delta`` — ``dragged_success_rate - pretax_success_rate``
        (typically <= 0: drag can only add outflow, never remove it, on a
        STATIC-spend fixture; a dynamic strategy's own reactive cuts can narrow
        or, in principle, flip this locally — the copy discloses that the two
        runs spend slightly differently under dynamic strategies).
    """

    count: int
    median_estate_cost: float
    pretax_success_rate: float
    dragged_success_rate: float
    success_rate_delta: float


def aggregate_estate_cost(
    pretax_terminals: list[float],
    dragged_terminals: list[float],
    pretax_success: list[bool],
    dragged_success: list[bool],
) -> EstateCostOfTaxes:
    """Aggregate the per-path (pretax, dragged) terminal pairs into the DECISION 9
    readout. All four lists must be the SAME length and index-aligned (path i's
    pretax terminal / dragged terminal / pretax success / dragged success), so
    the per-path DIFFERENCE (never a difference of medians) is well-defined.
    ``count == 1`` (the deterministic mode caller) degenerates correctly: the
    "median" of a single value is that value, so the same function serves both
    the deterministic single-pair case and the Historical/MC many-path case.

    An empty input returns a zeroed readout rather than dividing by zero /
    calling ``np.median`` on an empty array (defensive; callers only invoke
    this behind the same ``has_tax_split`` gate the rest of the split-mode
    machinery uses).
    """
    n = len(pretax_terminals)
    if n == 0:
        return EstateCostOfTaxes(
            count=0,
            median_estate_cost=0.0,
            pretax_success_rate=0.0,
            dragged_success_rate=0.0,
            success_rate_delta=0.0,
        )
    import numpy as np

    diffs = [pretax_terminals[i] - dragged_terminals[i] for i in range(n)]
    median_estate_cost = float(np.median(np.array(diffs, dtype=float)))
    pretax_rate = sum(1 for s in pretax_success if s) / n
    dragged_rate = sum(1 for s in dragged_success if s) / n
    return EstateCostOfTaxes(
        count=n,
        median_estate_cost=median_estate_cost,
        pretax_success_rate=pretax_rate,
        dragged_success_rate=dragged_rate,
        success_rate_delta=dragged_rate - pretax_rate,
    )
