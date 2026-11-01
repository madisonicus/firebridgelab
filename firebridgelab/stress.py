"""Synthetic-cohort stress tests (Family A — authored return paths).

A STRESS is an OVERLAY on the current plan, never a replacement (stress-test
design "Core design decisions" #1): the user's allocation, spending target,
spending strategy, and guaranteed income stay fixed; the stress swaps ONLY the
return environment. Mechanically a Family-A stress is a *Cohort whose per-year
returns are AUTHORED rather than sampled* — not a fourth return mode. It reuses
the EXISTING per-cohort loop (``firebridgelab.sim_core._simulate_cohort``) in place of a
Shiller window, exactly the way ``firebridgelab.historical.simulate_historical`` builds
a cohort, and renders as a SINGLE deterministic trajectory (the
``firebridgelab.simulate.Trajectory`` shape), not a 113-cohort fan — it's one specified
adverse world.

Slice 2 (stress-test design §Slicing) transcribes the rest of Family A's ADVERSE
scenarios from the framework appendix and wires the full selector. The battery is
the SIX adverse worlds (the three tailwind/false-positive scenarios — Average
Markets, 1990s Bull Run, March 2020 V-Drop — are deliberately OUT of scope):

  * ``stagflation_1970s``        — framework appendix §4
  * ``historical_depression_1929`` — framework appendix §9
  * ``slow_grind_2000``          — framework appendix §6
  * ``gfc_2008``                 — framework appendix §3
  * ``valuation_unwind_2025``    — framework appendix §7
  * ``depression_extreme``       — framework appendix §8 (Japan-style no-recovery)

Authored return table (the "6-column framework" shape)
------------------------------------------------------
The source framework (a retirement spending and portfolio-management framework
document) authors its scenarios as a year-by-year per-asset-class REAL-return table over 6
columns: US Total, US SCV, Int'l, Bonds, Gold, Cash. A stress here carries the
same shape, as a ``_StressTable`` holding:

  * ``rows`` — the EXPLICIT per-year rows (transcribed real returns, as
    fractions). Year *k* (1-indexed) takes ``rows[k-1]``.
  * ``normal_row`` — the framework's "11+" / "6+" / "9+" NORMAL row. Every year
    BEYOND the explicit rows takes the normal row's per-column value. If a class's
    column isn't in the normal row, it falls back to that class's OWN
    deterministic default ``real_return`` (the slice-1 tail behavior — preserved).

Transcription conventions (stress-test design "correctness-guarantor discipline"):

  * EXPLICIT numeric cell → verbatim as a fraction (−37% → ``-0.37``).
  * RANGE cell ("+3–4%") → the MIDPOINT (``0.035``), commented at the cell.
  * UNSPECIFIED gap years ("6–10 varied" with no numbers) → LINEAR-INTERPOLATE
    each class from its last explicit year's value to its normal-row value across
    the gap (``_interp_gap``), commented at the call site.

6→12 class mapping
------------------
The framework names 6 columns; the modeler has 12 class ids. Each modeler class
is mapped to a framework column by ``_CLASS_TO_COLUMN`` (proxy choices documented
inline). Every one of the 12 maps, so a stress can be applied to any allocation.

Scope guard
-----------
Stress supports SINGLE sourcing + CONSTANT allocation only. A glidepath or
buckets scenario raises a clear ``StressNotAvailable`` (their per-phase /
per-bucket return composition is a later slice). An unknown ``stress_id`` likewise
raises.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from firebridgelab.scenario import (
    ALLOCATION_MODE_CONSTANT,
    SOURCING_MODE_SINGLE,
    AssetClass,
    Scenario,
    gi_at_age,
)
from firebridgelab._step import net_return
from firebridgelab.sim_core import _simulate_cohort
from firebridgelab.simulate import Trajectory, YearPoint, simulate
from firebridgelab.spending import make_strategy
from firebridgelab.tax_location import (
    deterministic_blended_returns,
    has_tax_split,
    simulate_path_decomposition,
)


class StressNotAvailable(ValueError):
    """Raised when a stress run can't be served for the given request.

    Covers the cases a user can hit: an UNKNOWN ``stress_id``, the SCOPE GUARD (a
    glidepath / buckets scenario, which stress doesn't compose with yet), and an
    asset class with no framework-column mapping. A ``ValueError`` subclass (so
    callers' ``except ValueError`` fallbacks still catch it) but TYPED — the same
    pattern as ``NoReplayableWindow`` / ``SpendingStrategyNotSolvable`` — so the API
    maps it to a friendly 422 by class, not by string-matching the message. The
    carried message is the technical detail (correct for a log), mapped to plain
    language at the API seam (``api.error_translation``).
    """

# The 6 framework columns (stress-test design "The battery"). A stress table is
# keyed by these column names; the modeler's 12 class ids each map to one.
_COL_US_TOTAL = "US Total"
_COL_US_SCV = "US SCV"
_COL_INTL = "Int'l"
_COL_BONDS = "Bonds"
_COL_GOLD = "Gold"
_COL_CASH = "Cash"

# 6→12 class-id → framework-column map (stress-test design "Build-time decisions /
# 6→12 class mapping"). Proxy choices (the modeler classes the framework's 6
# columns don't name a column for) are documented per line:
#   - us_reit → US Total: REITs are an equity proxy (no REIT column in the 6).
#   - emerging_markets → Int'l: EM rides the international-equity column.
#   - lt_treasury / it_treasury / tips → Bonds: all duration/credit collapse to
#     the single Bonds column (TIPS is an inflation-linked bond, still Bonds here).
#   - commodities → Gold: the framework's only real-asset column is Gold; broad
#     commodities proxy onto it.
#   - st_treasury → Cash: short Treasuries / T-bills ARE the cash column.
_CLASS_TO_COLUMN: dict[str, str] = {
    "us_total_market": _COL_US_TOTAL,
    "us_reit": _COL_US_TOTAL,          # proxy: REITs → US equity
    "us_small_value": _COL_US_SCV,
    "intl_developed": _COL_INTL,
    "emerging_markets": _COL_INTL,     # proxy: EM → international equity
    "us_agg_bonds": _COL_BONDS,
    "lt_treasury": _COL_BONDS,
    "it_treasury": _COL_BONDS,
    "tips": _COL_BONDS,                # proxy: TIPS → bonds (inflation-linked bond)
    "st_treasury": _COL_CASH,
    "gold": _COL_GOLD,
    "commodities": _COL_GOLD,          # proxy: broad commodities → gold (real asset)
}


@dataclass(frozen=True)
class _StressTable:
    """One authored stress: explicit per-year rows + the framework normal row.

    ``rows`` are the EXPLICIT years (year *k* = ``rows[k-1]``). ``normal_row`` is the
    framework's "11+"/"6+"/"9+" tail applied to EVERY year beyond ``rows``; a
    column absent from ``normal_row`` falls back to that class's own default
    ``real_return`` (the slice-1 tail behavior). Each entry is a per-column dict of
    real returns as fractions.
    """

    rows: list[dict[str, float]]
    normal_row: dict[str, float]


def _interp_gap(
    last_explicit: dict[str, float],
    normal_row: dict[str, float],
    n_gap: int,
) -> list[dict[str, float]]:
    """Linear-interpolate ``n_gap`` unspecified gap rows per the design rule.

    For a "6–10 varied" style gap with no published numbers, each class glides
    LINEARLY from its last explicit year's value to its normal-row value. With the
    last explicit year at value ``a`` and the normal row at ``b``, the ``n_gap``
    gap years take ``a + (b - a) * k / (n_gap + 1)`` for ``k = 1 .. n_gap`` — so the
    year immediately AFTER the gap (handled by the normal-row tail) lands on ``b``.
    Documented, not invented: a principled straight-line glide back to normal.
    """
    out: list[dict[str, float]] = []
    cols = set(last_explicit) | set(normal_row)
    for k in range(1, n_gap + 1):
        row: dict[str, float] = {}
        for col in cols:
            a = last_explicit[col]
            b = normal_row[col]
            row[col] = a + (b - a) * k / (n_gap + 1)
        out.append(row)
    return out


# 1970s Stagflation (framework appendix §4): stocks and bonds down together while
# gold explodes — the classic stagflation regime. Years 1-5 EXPLICIT; the
# framework's "6–10 varied" gap is LINEAR-INTERPOLATED (slice-2 finalization of the
# slice-1 PROVISIONAL placeholder) from each class's year-5 value to its 11+
# normal-row value; year 11+ is the normal row.
_STAGFLATION_1970S_NORMAL = {
    _COL_US_TOTAL: +0.10, _COL_US_SCV: +0.12, _COL_INTL: +0.07,
    _COL_BONDS: +0.03, _COL_GOLD: +0.02, _COL_CASH: +0.02,
}
_STAGFLATION_1970S_EXPLICIT: list[dict[str, float]] = [
    #          US Total  US SCV   Int'l    Bonds    Gold     Cash
    {_COL_US_TOTAL: -0.26, _COL_US_SCV: -0.20, _COL_INTL: -0.25,
     _COL_BONDS: -0.05, _COL_GOLD: +0.67, _COL_CASH: -0.03},   # year 1
    {_COL_US_TOTAL: -0.14, _COL_US_SCV: -0.10, _COL_INTL: -0.18,
     _COL_BONDS: -0.08, _COL_GOLD: +0.66, _COL_CASH: -0.04},   # year 2
    {_COL_US_TOTAL: +0.31, _COL_US_SCV: +0.55, _COL_INTL: +0.35,
     _COL_BONDS: +0.02, _COL_GOLD: -0.25, _COL_CASH: -0.02},   # year 3
    {_COL_US_TOTAL: +0.19, _COL_US_SCV: +0.40, _COL_INTL: +0.03,
     _COL_BONDS: -0.02, _COL_GOLD: -0.10, _COL_CASH: -0.01},   # year 4
    {_COL_US_TOTAL: -0.12, _COL_US_SCV: -0.05, _COL_INTL: +0.15,
     _COL_BONDS: -0.05, _COL_GOLD: +0.30, _COL_CASH: -0.02},   # year 5
]
# Years 6-10: framework cell is "varied" with no numbers → linear-interpolate each
# class from its year-5 explicit value to its 11+ normal-row value (5 gap years).
_STAGFLATION_1970S = _StressTable(
    rows=_STAGFLATION_1970S_EXPLICIT
    + _interp_gap(_STAGFLATION_1970S_EXPLICIT[-1], _STAGFLATION_1970S_NORMAL, 5),
    normal_row=_STAGFLATION_1970S_NORMAL,
)


# Historical Depression — actual 1929–1939 (framework appendix §9): violent crash
# WITH the historical V-recovery. All 10 years explicit; year 11+ is the normal
# row. (Note the +120% SCV 1933 rebound — verbatim from the appendix.)
_HISTORICAL_DEPRESSION_1929 = _StressTable(
    rows=[
        #          US Total  US SCV   Int'l    Bonds    Gold     Cash
        {_COL_US_TOTAL: -0.08, _COL_US_SCV: -0.15, _COL_INTL: -0.10,
         _COL_BONDS: +0.03, _COL_GOLD: 0.0, _COL_CASH: +0.02},     # year 1 (1929)
        {_COL_US_TOTAL: -0.25, _COL_US_SCV: -0.35, _COL_INTL: -0.30,
         _COL_BONDS: +0.05, _COL_GOLD: 0.0, _COL_CASH: +0.02},     # year 2 (1930)
        {_COL_US_TOTAL: -0.43, _COL_US_SCV: -0.50, _COL_INTL: -0.45,
         _COL_BONDS: -0.05, _COL_GOLD: 0.0, _COL_CASH: +0.01},     # year 3 (1931)
        {_COL_US_TOTAL: -0.08, _COL_US_SCV: -0.05, _COL_INTL: -0.10,
         _COL_BONDS: +0.10, _COL_GOLD: 0.0, _COL_CASH: +0.005},    # year 4 (1932)
        {_COL_US_TOTAL: +0.54, _COL_US_SCV: +1.20, _COL_INTL: +0.50,
         _COL_BONDS: 0.0, _COL_GOLD: +0.50, _COL_CASH: 0.0},       # year 5 (1933)
        {_COL_US_TOTAL: -0.01, _COL_US_SCV: +0.25, _COL_INTL: -0.05,
         _COL_BONDS: +0.08, _COL_GOLD: 0.0, _COL_CASH: 0.0},       # year 6 (1934)
        {_COL_US_TOTAL: +0.47, _COL_US_SCV: +0.50, _COL_INTL: +0.40,
         _COL_BONDS: +0.05, _COL_GOLD: 0.0, _COL_CASH: 0.0},       # year 7 (1935)
        {_COL_US_TOTAL: +0.34, _COL_US_SCV: +0.60, _COL_INTL: +0.30,
         _COL_BONDS: +0.07, _COL_GOLD: 0.0, _COL_CASH: 0.0},       # year 8 (1936)
        {_COL_US_TOTAL: -0.35, _COL_US_SCV: -0.50, _COL_INTL: -0.35,
         _COL_BONDS: 0.0, _COL_GOLD: 0.0, _COL_CASH: 0.0},         # year 9 (1937)
        {_COL_US_TOTAL: +0.31, _COL_US_SCV: +0.25, _COL_INTL: +0.25,
         _COL_BONDS: +0.05, _COL_GOLD: 0.0, _COL_CASH: 0.0},       # year 10 (1938)
    ],
    normal_row={
        _COL_US_TOTAL: +0.10, _COL_US_SCV: +0.12, _COL_INTL: +0.07,
        _COL_BONDS: +0.03, _COL_GOLD: +0.02, _COL_CASH: +0.02,
    },
)


# 2000–2002 Slow Grind — dot-com bust (framework appendix §6): prolonged 3-year
# decline, no quick bounce. Years 1-5 explicit; the framework's "6–7 varied" cells
# carry RANGES on two columns (US Total +5–16% → midpoint +0.105; Gold +18–31% →
# midpoint +0.245) and "varied" (no numbers) on the other four → those four
# linear-interpolate from year-5 to the 8+ normal row across the 2 gap years; the
# two range columns are PINNED at their midpoint for both gap years (the framework
# gives one range for the whole 6–7 span, not a per-year glide). Year 8+ normal.
_SLOW_GRIND_2000_NORMAL = {
    _COL_US_TOTAL: +0.08, _COL_US_SCV: +0.10, _COL_INTL: +0.06,
    _COL_BONDS: +0.02, _COL_GOLD: +0.02, _COL_CASH: +0.02,
}
_SLOW_GRIND_2000_EXPLICIT: list[dict[str, float]] = [
    #          US Total  US SCV   Int'l    Bonds    Gold     Cash
    {_COL_US_TOTAL: -0.09, _COL_US_SCV: -0.01, _COL_INTL: -0.14,
     _COL_BONDS: +0.11, _COL_GOLD: -0.05, _COL_CASH: +0.04},   # year 1
    {_COL_US_TOTAL: -0.12, _COL_US_SCV: +0.14, _COL_INTL: -0.21,
     _COL_BONDS: +0.08, _COL_GOLD: +0.02, _COL_CASH: +0.025},  # year 2
    {_COL_US_TOTAL: -0.22, _COL_US_SCV: -0.13, _COL_INTL: -0.16,
     _COL_BONDS: +0.10, _COL_GOLD: +0.24, _COL_CASH: +0.01},   # year 3
    {_COL_US_TOTAL: +0.29, _COL_US_SCV: +0.46, _COL_INTL: +0.39,
     _COL_BONDS: +0.04, _COL_GOLD: +0.20, _COL_CASH: +0.005},  # year 4
    {_COL_US_TOTAL: +0.11, _COL_US_SCV: +0.22, _COL_INTL: +0.21,
     _COL_BONDS: +0.04, _COL_GOLD: +0.04, _COL_CASH: +0.01},   # year 5
]
# Years 6-7: US Total "+5–16%" → midpoint +0.105; Gold "+18–31%" → midpoint
# +0.245 (both pinned for the whole 2-year span — the framework gives one range,
# not a glide). The four "varied" columns (SCV, Int'l, Bonds, Cash) interpolate
# from year-5 to the 8+ normal row across the 2 gap years.
_SLOW_GRIND_2000_RANGE_GAP = {
    _COL_US_TOTAL: +0.105,   # midpoint of framework range +5-16%
    _COL_GOLD: +0.245,       # midpoint of framework range +18-31%
}


def _slow_grind_gap() -> list[dict[str, float]]:
    """The two 6–7 'varied' rows: range-midpoint columns pinned, others interpolated."""
    y5 = _SLOW_GRIND_2000_EXPLICIT[-1]
    interp_cols = (_COL_US_SCV, _COL_INTL, _COL_BONDS, _COL_CASH)
    rows: list[dict[str, float]] = []
    n_gap = 2
    for k in range(1, n_gap + 1):
        row = dict(_SLOW_GRIND_2000_RANGE_GAP)
        for col in interp_cols:
            a = y5[col]
            b = _SLOW_GRIND_2000_NORMAL[col]
            row[col] = a + (b - a) * k / (n_gap + 1)
        rows.append(row)
    return rows


_SLOW_GRIND_2000 = _StressTable(
    rows=_SLOW_GRIND_2000_EXPLICIT + _slow_grind_gap(),
    normal_row=_SLOW_GRIND_2000_NORMAL,
)


# GFC-Style Crash — 2007–2012 (framework appendix §3): sharp −50%-ish drawdown then
# V-recovery. All 6 years explicit; year 7+ is the normal row.
_GFC_2008 = _StressTable(
    rows=[
        #          US Total  US SCV   Int'l    Bonds    Gold     Cash
        {_COL_US_TOTAL: -0.37, _COL_US_SCV: -0.32, _COL_INTL: -0.43,
         _COL_BONDS: +0.05, _COL_GOLD: +0.05, _COL_CASH: +0.02},   # year 1
        {_COL_US_TOTAL: +0.26, _COL_US_SCV: +0.28, _COL_INTL: +0.32,
         _COL_BONDS: +0.06, _COL_GOLD: +0.24, _COL_CASH: 0.0},     # year 2
        {_COL_US_TOTAL: +0.15, _COL_US_SCV: +0.24, _COL_INTL: +0.08,
         _COL_BONDS: +0.06, _COL_GOLD: +0.10, _COL_CASH: 0.0},     # year 3
        {_COL_US_TOTAL: +0.02, _COL_US_SCV: -0.02, _COL_INTL: -0.12,
         _COL_BONDS: +0.04, _COL_GOLD: +0.10, _COL_CASH: 0.0},     # year 4
        {_COL_US_TOTAL: +0.16, _COL_US_SCV: +0.34, _COL_INTL: +0.23,
         _COL_BONDS: -0.02, _COL_GOLD: -0.28, _COL_CASH: 0.0},     # year 5
        {_COL_US_TOTAL: +0.32, _COL_US_SCV: +0.10, _COL_INTL: -0.02,
         _COL_BONDS: +0.06, _COL_GOLD: -0.01, _COL_CASH: 0.0},     # year 6
    ],
    normal_row={
        _COL_US_TOTAL: +0.10, _COL_US_SCV: +0.12, _COL_INTL: +0.07,
        _COL_BONDS: +0.02, _COL_GOLD: +0.02, _COL_CASH: +0.01,
    },
)


# 2025 Valuation Unwind (synthetic, framework appendix §7): high-valuation unwind +
# stock/bond correlation breakdown — the most-relevant-to-retiring-now world. All
# 5 years explicit; year 6+ is the normal row.
_VALUATION_UNWIND_2025 = _StressTable(
    rows=[
        #          US Total  US SCV   Int'l    Bonds    Gold     Cash
        {_COL_US_TOTAL: -0.25, _COL_US_SCV: -0.15, _COL_INTL: -0.20,
         _COL_BONDS: -0.10, _COL_GOLD: +0.30, _COL_CASH: +0.03},   # year 1
        {_COL_US_TOTAL: -0.15, _COL_US_SCV: -0.05, _COL_INTL: -0.10,
         _COL_BONDS: -0.05, _COL_GOLD: +0.20, _COL_CASH: +0.02},   # year 2
        {_COL_US_TOTAL: -0.05, _COL_US_SCV: +0.05, _COL_INTL: 0.0,
         _COL_BONDS: -0.03, _COL_GOLD: +0.10, _COL_CASH: +0.01},   # year 3
        {_COL_US_TOTAL: +0.08, _COL_US_SCV: +0.12, _COL_INTL: +0.10,
         _COL_BONDS: 0.0, _COL_GOLD: -0.05, _COL_CASH: +0.01},     # year 4
        {_COL_US_TOTAL: +0.10, _COL_US_SCV: +0.15, _COL_INTL: +0.12,
         _COL_BONDS: +0.02, _COL_GOLD: -0.03, _COL_CASH: +0.02},   # year 5
    ],
    normal_row={
        _COL_US_TOTAL: +0.07, _COL_US_SCV: +0.09, _COL_INTL: +0.06,
        _COL_BONDS: +0.02, _COL_GOLD: +0.02, _COL_CASH: +0.02,
    },
)


# Depression Extreme (synthetic red-team, framework appendix §8): depression-level
# crash with NO recovery (Japan-style) — the "does anything survive this" sanity
# case. Years 1-5 explicit; the framework's "6–8" cells are all RANGES (midpoints
# below); year 9+ is the normal row.
_DEPRESSION_EXTREME = _StressTable(
    rows=[
        #          US Total  US SCV   Int'l    Bonds    Gold     Cash
        {_COL_US_TOTAL: -0.45, _COL_US_SCV: -0.50, _COL_INTL: -0.50,
         _COL_BONDS: +0.10, _COL_GOLD: +0.20, _COL_CASH: +0.02},   # year 1
        {_COL_US_TOTAL: -0.25, _COL_US_SCV: -0.30, _COL_INTL: -0.30,
         _COL_BONDS: +0.05, _COL_GOLD: +0.15, _COL_CASH: +0.01},   # year 2
        {_COL_US_TOTAL: -0.10, _COL_US_SCV: -0.15, _COL_INTL: -0.15,
         _COL_BONDS: +0.03, _COL_GOLD: +0.05, _COL_CASH: +0.005},  # year 3
        {_COL_US_TOTAL: 0.0, _COL_US_SCV: -0.05, _COL_INTL: -0.05,
         _COL_BONDS: +0.02, _COL_GOLD: 0.0, _COL_CASH: 0.0},       # year 4
        {_COL_US_TOTAL: +0.02, _COL_US_SCV: 0.0, _COL_INTL: 0.0,
         _COL_BONDS: +0.02, _COL_GOLD: -0.02, _COL_CASH: 0.0},     # year 5
        # Years 6-8: framework "6–8" row is all ranges → per-column midpoints:
        #   US Total +3–4% → +0.035; SCV +2–4% → +0.03; Int'l +2–4% → +0.03;
        #   Bonds +2% (point); Gold −3–0% → −0.015; Cash +0.5–1% → +0.0075.
        {_COL_US_TOTAL: +0.035, _COL_US_SCV: +0.03, _COL_INTL: +0.03,
         _COL_BONDS: +0.02, _COL_GOLD: -0.015, _COL_CASH: +0.0075},  # year 6 (midpoints)
        {_COL_US_TOTAL: +0.035, _COL_US_SCV: +0.03, _COL_INTL: +0.03,
         _COL_BONDS: +0.02, _COL_GOLD: -0.015, _COL_CASH: +0.0075},  # year 7 (midpoints)
        {_COL_US_TOTAL: +0.035, _COL_US_SCV: +0.03, _COL_INTL: +0.03,
         _COL_BONDS: +0.02, _COL_GOLD: -0.015, _COL_CASH: +0.0075},  # year 8 (midpoints)
    ],
    normal_row={
        _COL_US_TOTAL: +0.04, _COL_US_SCV: +0.05, _COL_INTL: +0.04,
        _COL_BONDS: +0.02, _COL_GOLD: +0.01, _COL_CASH: +0.01,
    },
)


# The stress registry, keyed by stress id (stress-test design: one VIEW entry
# opens a sub-selector over this registry). Slice 2 ships the six ADVERSE Family-A
# worlds; the three tailwind/false-positive framework scenarios are out of scope.
STRESS_SCENARIOS: dict[str, _StressTable] = {
    "stagflation_1970s": _STAGFLATION_1970S,
    "historical_depression_1929": _HISTORICAL_DEPRESSION_1929,
    "slow_grind_2000": _SLOW_GRIND_2000,
    "gfc_2008": _GFC_2008,
    "valuation_unwind_2025": _VALUATION_UNWIND_2025,
    "depression_extreme": _DEPRESSION_EXTREME,
}


# ---------------------------------------------------------------------------
# Family B — scenario perturbations (stress-test design "The two families").
# ---------------------------------------------------------------------------
# A Family-B stress is NOT an authored return path; it is a TRANSFORM on the
# Scenario itself (cut an income stream, scale spending for an FX hit), then run
# through the ORDINARY deterministic engine (``firebridgelab.simulate.simulate``) under
# the plan's own return assumption. By design, Family B renders as a SINGLE Deterministic line — the same
# ``Trajectory`` output shape as Family A — NOT a fan. One mental model: every
# stress, A or B, shows up as one specified adverse world's trajectory.
#
# Each transform is a pure ``Scenario -> Scenario`` built with
# ``dataclasses.replace`` (so the input scenario is never mutated and the frozen
# dataclass stays immutable). The transform touches ONLY the dimension the stress
# perturbs; everything else (allocation, return assumptions, horizon, the other
# income streams) rides through unchanged — the OVERLAY contract, expressed on the
# Scenario side rather than the return side.


def _ss_haircut_23(scenario: Scenario) -> Scenario:
    """SS trust-fund haircut: scale BOTH Social Security streams by 0.77.

    The ~2034 SSA trustee projection is that, absent legislation, the trust fund
    depletes and benefits become payable at ~77% of scheduled — the framework's
    "SS at 77%" assumption. We model that as a flat ×0.77 on ``person_a_ss_amount`` and
    ``person_b_ss_amount``. Pensions, spending, allocation, and everything else are
    left unchanged — this is an income-side shock on the SS streams ONLY.

    Timing note (why a FLAT 23% cut is correct here, no per-year ramp): both SS
    streams start at age 67 (the default ``*_ss_start_age``), which is AFTER the
    ~2034 depletion (≈ age 60 on the modeler's single age axis). So the streams are
    already in the reduced-benefit world the moment they switch on; a flat 23%
    reduction on the amounts is exact for any claim age ≥ ~60, and no per-year
    timing logic is needed. (Were SS claimed before ~2034 a phased haircut would be
    needed; that is out of scope — the default household claims both at 67.)
    """
    return replace(
        scenario,
        person_a_ss_amount=scenario.person_a_ss_amount * 0.77,
        person_b_ss_amount=scenario.person_b_ss_amount * 0.77,
    )


def _cost_of_living_shock(scenario: Scenario) -> Scenario:
    """Cost-of-living shock: scale spending (and its floor/ceiling rails)
    by 1.20 (+20%).

    A one-time, sustained step-up in what the same lifestyle costs (a relocation,
    a healthcare-cost jump, or an exchange-rate move for someone spending in a
    different currency) — a spending-side shock, not an income one. We scale
    ``annual_spending`` AND the operational ``spending_floor`` / ``spending_ceiling``
    rails by 1.20 so the whole spending band moves together (a dynamic strategy's
    clamps stay proportional to the shocked cost of living, not pinned at the
    un-shocked floor). Guaranteed income is left UNSCALED — it is denominated in
    the plan's own currency and unaffected by the shock.

    The +20% magnitude is a round PLANNING DEFAULT, not a point forecast (major
    exchange rates, for example, have moved 20%+ over multi-year spans). It is a baked preset (no knob), consistent with the
    feature's "stress tests, not fantasy-world creators" stance.
    """
    return replace(
        scenario,
        annual_spending=scenario.annual_spending * 1.20,
        spending_floor=scenario.spending_floor * 1.20,
        spending_ceiling=scenario.spending_ceiling * 1.20,
    )


def _government_austerity(scenario: Scenario) -> Scenario:
    """Combined government-income austerity: SS ×0.77 AND a frozen-COLA pension drift.

    A plan whose guaranteed income is mostly government-sourced (public-sector
    pensions plus Social Security) carries a CONCENTRATION RISK: both government income sources can be squeezed TOGETHER,
    which is what this stress models:

      * SS ×0.77 — the ~2034 trust-fund haircut (the ``ss_haircut_23`` cut), still
        real-constant at the REDUCED level (a flat 23% benefit cut, no per-year
        ramp, applied from each claim age).
      * ``pension_real_drift = 0.025`` — the pension COLA goes from full to FROZEN,
        so the pensions erode 2.5%/yr in REAL terms (roughly the full-COLA-to-zero
        gap at ~2.5% inflation). Applied via ``gi_at_age`` to the two PENSION
        streams only.

    So the two checks are cut together: SS via a level haircut, pensions via a
    per-year real erosion. Spending, allocation, portfolio, horizon all ride
    through unchanged — an income-side shock on the whole government-GI stack.
    """
    return replace(
        scenario,
        person_a_ss_amount=scenario.person_a_ss_amount * 0.77,
        person_b_ss_amount=scenario.person_b_ss_amount * 0.77,
        pension_real_drift=0.025,
    )


def _sustained_cost_drift(scenario: Scenario) -> Scenario:
    """Sustained cost-of-living drift: spending drifts up 1.5%/yr in real terms.

    Distinct from the one-time ``cost_of_living_shock`` (a single +20% step): this is
    a SUSTAINED per-year real drift: a cost of living that outruns the inflation
    index (or, for someone spending in another currency, a steady real
    appreciation of that currency) DRIFTS UP at that rate. Modeled as ``spending_real_drift = 0.015`` — a post-strategy
    cost-of-living multiplier applied in the sim loops (composes with any spending
    strategy).

    Documented as an ADVERSE tail, not a forecast. The rails are NOT scaled — the
    spending line drifts up while the floor/ceiling stay at the input levels.
    """
    return replace(scenario, spending_real_drift=0.015)


# The Family-B registry, keyed by stress id (a SEPARATE registry from the Family-A
# ``STRESS_SCENARIOS`` tables — the two families are never collapsed). Slices 3+4
# ship four perturbations: the SS haircut + cost-of-living shock (slice 3,
# no COLA machinery), and the two COLA-bearing concentration-risk stresses (slice 4
# — government-income austerity and sustained cost-of-living drift).
STRESS_PERTURBATIONS: dict[str, Callable[[Scenario], Scenario]] = {
    "ss_haircut_23": _ss_haircut_23,
    "cost_of_living_shock": _cost_of_living_shock,
    "government_austerity": _government_austerity,
    "sustained_cost_drift": _sustained_cost_drift,
}


def _class_return_array(
    ac: AssetClass, table: _StressTable, n_steps: int
) -> np.ndarray:
    """One asset class's per-year real-return array over the horizon.

    For each of the ``n_steps`` age-steps: years within the authored explicit rows
    (years 1..len(table.rows)) take the class's framework-column value that year;
    years BEYOND the explicit rows take the framework NORMAL row's value for that
    column — and if the class's column is absent from the normal row, fall back to
    the class's OWN deterministic default ``real_return`` (its normal geometric-mean
    assumption), keeping the tail principled rather than invented.

    The class→column map is ``_CLASS_TO_COLUMN`` (every CANONICAL modeler class
    maps). A class id NOT in the map (an arbitrary id the deterministic path would
    accept by reading its supplied ``real_return`` directly) can't be assigned a
    framework column, so it raises the typed ``StressNotAvailable`` — mapped to a
    friendly 422 at the API seam — rather than a bare ``KeyError`` that would escape
    as an uncaught HTTP 500.
    """
    column = _CLASS_TO_COLUMN.get(ac.id)
    if column is None:
        raise StressNotAvailable(
            f"asset class id {ac.id!r} has no stress-table mapping "
            f"(mappable classes: {sorted(_CLASS_TO_COLUMN)}); stress tests run "
            "only on the canonical asset-class menu"
        )
    # The tail value: the normal-row column if present, else the class's own
    # deterministic default real_return (slice-1 tail behavior preserved).
    tail = table.normal_row.get(column, ac.real_return)
    out = np.empty(n_steps, dtype=float)
    for step in range(n_steps):
        if step < len(table.rows):
            out[step] = table.rows[step][column]
        else:
            out[step] = tail
    return out


def simulate_stress(scenario: Scenario, stress_id: str) -> Trajectory:
    """Run one synthetic-cohort stress as a single deterministic trajectory.

    The stress is an OVERLAY: the scenario's allocation / spending / strategy / GI
    are untouched; only the per-year return environment is swapped for the
    authored stress table. Mechanically:

      1. Look up the authored per-column real-return table for ``stress_id``
         (``StressNotAvailable`` if unknown).
      2. For each ``AssetClass`` in ``scenario.allocation``, build its per-year
         real-return array (``_class_return_array``): explicit table years, then
         the framework normal row (or each class's own default) for the tail.
      3. RENORMALIZE the allocation weights over all allocation classes (they all
         map) and blend per year: ``r[step] = Σ weight_i · class_return_i[step]``.
      4. Run the shared per-cohort loop (``_simulate_cohort``) with that blended
         per-year return array → ``(end_portfolios, depleted_age, spends)``.
      5. Wrap into a ``Trajectory`` with one ``YearPoint`` per age, computing
         ``start_portfolio`` and ``growth = start*r`` per year EXACTLY as
         ``firebridgelab.simulate.simulate`` does, so the tooltip stays consistent.

    Scope guard: SINGLE sourcing + CONSTANT allocation only. A glidepath or buckets
    scenario raises ``StressNotAvailable`` (per-phase / per-bucket composition is a
    later slice).

    Tax-drag (design td1, HARD CONSTRAINT "stress mode"): when a tax-location
    split is supplied, step 4 routes through the DRAGGED decomposition
    (``simulate_path_decomposition``) instead of the plain ``_simulate_cohort``
    — "stress is a return-shock lens and the balances underneath it must stay
    honest (a stressed split trajectory pays its taxes like any other)". The
    trajectory's numbers change (a stressed split world now pays its bill), but
    the READOUT stays hidden — ``simulate_stress`` returns the SAME bare
    ``Trajectory`` shape either way, no tax_location/federal_tax block (the tb1
    gap persists here by design, documented in the explainer). td-c: FAMILY B
    is wired the SAME way (it used to dispatch a split straight through the
    plain ``simulate()``, the same pre-tax falsehood Family A fixed in td-b) —
    both families share the dragged-wrapping helper (``_wrap_trajectory``)
    below, so they can't drift from each other. Family B's dragged branch uses
    the PERTURBED scenario's own deterministic blended-return sequence
    (``deterministic_blended_returns`` — Family B never swaps the return
    environment, only the scenario), never the authored stress table.

    Two FAMILIES dispatch off ``stress_id`` (stress-test design "The two families"):

      * FAMILY A (``STRESS_SCENARIOS``) — an authored synthetic-cohort return table,
        the path documented above.
      * FAMILY B (``STRESS_PERTURBATIONS``) — a pure ``Scenario -> Scenario``
        transform (cut SS, scale spending for an FX hit), then run through the
        ORDINARY deterministic ``firebridgelab.simulate.simulate``. By design this renders as a SINGLE Deterministic line — the same
        ``Trajectory`` shape as Family A, NOT a fan. The transform does the work; the
        return environment is the plan's own assumption (no return swap).

    Family B is dispatched FIRST (before the Family-A scope guards), because it runs
    through ``simulate`` — which already handles every sourcing / allocation mode — so
    the synthetic-cohort guards (single + constant only) do not apply to it.

    An unknown ``stress_id`` (in NEITHER registry) raises ``StressNotAvailable``.
    """
    # --- Family B: scenario perturbation, run through the ordinary engine --------
    perturbation = STRESS_PERTURBATIONS.get(stress_id)
    if perturbation is not None:
        perturbed = perturbation(scenario)
        if has_tax_split(perturbed):
            # td-c fix: Family B must pay its taxes too (the td1 HARD
            # CONSTRAINT — "a stressed split trajectory pays its taxes like
            # any other" — applies to a scenario-transform stress exactly as
            # much as an authored-return-table one). Uses the PERTURBED
            # scenario's own deterministic blended returns (Family B never
            # swaps the return environment) and a FRESH strategy reading the
            # perturbed, dragged, taxed balance.
            returns = deterministic_blended_returns(perturbed)
            strategy = make_strategy(perturbed)
            tax_path = simulate_path_decomposition(perturbed, returns, strategy)
            n_steps = len(returns)
            end_portfolios = [
                tax_path.taxable[i] + tax_path.tax_deferred[i] + tax_path.roth[i]
                for i in range(n_steps)
            ]
            return _wrap_trajectory(
                perturbed,
                returns,
                end_portfolios,
                tax_path.dragged_depleted_age,
                list(tax_path.realized_spend),
            )
        return simulate(perturbed)

    if scenario.sourcing_mode != SOURCING_MODE_SINGLE:
        raise StressNotAvailable(
            "stress tests support single sourcing only in this slice "
            f"(got sourcing_mode={scenario.sourcing_mode!r}); bucket composition "
            "is a later slice"
        )
    if scenario.allocation_mode != ALLOCATION_MODE_CONSTANT:
        raise StressNotAvailable(
            "stress tests support constant allocation only in this slice "
            f"(got allocation_mode={scenario.allocation_mode!r}); glidepath "
            "composition is a later slice"
        )

    table = STRESS_SCENARIOS.get(stress_id)
    if table is None:
        raise StressNotAvailable(
            f"unknown stress id {stress_id!r} "
            f"(have {sorted(STRESS_SCENARIOS) + sorted(STRESS_PERTURBATIONS)})"
        )

    n_steps = scenario.end_age - scenario.start_age + 1

    # Renormalize the allocation weights over all allocation classes (every class
    # maps to a column, so none is dropped). A zero / negative total is a broken
    # allocation — surface it rather than divide by zero.
    total_weight = sum(ac.weight for ac in scenario.allocation)
    if total_weight <= 0:
        raise ValueError("allocation weights sum to zero or less")

    # Per-class per-year real-return arrays, then the weight-blended per-year array.
    blended = np.zeros(n_steps, dtype=float)
    for ac in scenario.allocation:
        norm_weight = ac.weight / total_weight
        blended += norm_weight * _class_return_array(ac, table, n_steps)

    if has_tax_split(scenario):
        # td1 HARD CONSTRAINT: drag applies under stress. Sourcing is already
        # guaranteed single by the scope guard above, so has_tax_split alone
        # gates the same way it does elsewhere in the engine. The recorded
        # spend is the REALIZED (fundable-capped) consumption — the same
        # discipline _simulate_cohort's else-branch below applies via its own
        # realized_spend cap (see TaxLocationPath.realized_spend).
        strategy = make_strategy(scenario)
        tax_path = simulate_path_decomposition(scenario, blended, strategy)
        end_portfolios = [
            tax_path.taxable[i] + tax_path.tax_deferred[i] + tax_path.roth[i]
            for i in range(n_steps)
        ]
        depleted_age = tax_path.dragged_depleted_age
        spends = list(tax_path.realized_spend)
    else:
        end_portfolios, depleted_age, spends = _simulate_cohort(scenario, blended)

    return _wrap_trajectory(scenario, blended, end_portfolios, depleted_age, spends)


def _wrap_trajectory(
    scenario: Scenario,
    returns,
    end_portfolios: list[float],
    depleted_age: int | None,
    spends: list[float],
) -> Trajectory:
    """Wrap a per-year ``(end_portfolio, spend)`` series into the single-
    trajectory ``Trajectory`` shape, recomputing ``start_portfolio`` and
    ``growth = start_portfolio * r`` per year EXACTLY as
    ``firebridgelab.simulate.simulate`` does (module docstring step 5) — shared by
    FAMILY A's tail above (both its split and no-split branches already
    converge to this one wrapping step) and FAMILY B's dragged branch (td-c),
    so the two families can never wrap a trajectory differently.

    For a NO-SPLIT run the YearPoint tooltip identity holds exactly (end ==
    start + growth + (gi − spend)·(1+r/2)). For a SPLIT run it deliberately does
    NOT: ``end_portfolio`` IS the dragged total, and the gap between the
    identity's prediction and the recorded end is the year's (hidden) tax bill
    — YearPoint carries no tax field, and the stress readout keeps today's
    visibility (the tb1 gap, documented in the explainer). Do not "fix" the
    residual by re-deriving end from the identity — the dragged total is the
    honest balance.
    """
    points: list[YearPoint] = []
    start = scenario.initial_portfolio
    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        r = net_return(float(returns[step]), scenario.annual_fee)
        growth = start * r
        points.append(
            YearPoint(
                age=age,
                start_portfolio=start,
                growth=growth,
                spending=spends[step],
                guaranteed_income=gi_at_age(scenario, age),
                end_portfolio=end_portfolios[step],
            )
        )
        start = end_portfolios[step]

    return Trajectory(
        points=points,
        depleted_age=depleted_age,
        final_portfolio=end_portfolios[-1] if end_portfolios else scenario.initial_portfolio,
    )


def effective_spending_rails(scenario: Scenario, stress_id: str) -> tuple[float, float]:
    """The EFFECTIVE spending floor/ceiling the run clamps against under a stress.

    A spending-chart guide line must label the floor/ceiling for *the world being
    shown*, not the live UI knob. Family A (and the SS-haircut Family-B
    perturbation) leave the rails untouched, so the effective rails equal the
    input scenario's. The ``cost_of_living_shock`` perturbation scales BOTH rails ×1.20 (it
    scales ``annual_spending`` and the operational rails together so a dynamic
    strategy's clamps stay proportional to the shocked cost of living), so a
    guardrails / FR run there actually floors at the ×1.20 value — and the guide
    line must say so.

    This is a SEPARATE accessor from ``simulate_stress`` deliberately: that
    function's ``Trajectory`` return shape is pinned by the snapshot tests and
    consumed bare across the suite, so we don't widen it. The seam re-derives the
    rails by applying the SAME Family-B transform (when present) and reading the
    post-transform rails off the result — no Family-A perturbation exists, so an
    unknown-but-Family-A id (or any id) returns the input rails unchanged. An id in
    NEITHER registry never reaches here (the API only calls this after a successful
    ``simulate_stress``), but we still return the input rails as the safe default.

    Returns ``(effective_spending_floor, effective_spending_ceiling)``.
    """
    perturbation = STRESS_PERTURBATIONS.get(stress_id)
    effective = perturbation(scenario) if perturbation is not None else scenario
    return (effective.spending_floor, effective.spending_ceiling)
