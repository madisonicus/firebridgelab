"""Medicare IRMAA: the income-related Part B and Part D surcharges.

Read only when ``Scenario.irmaa`` is on (and a tax split gives the model an
income path). Every living person aged 65 or more pays the Part B and Part D
monthly adjustment for the tier their household's MAGI falls in, using MAGI
from two years earlier (20 CFR 418.1135(a)) and that year's filing status. The
model's MAGI is its own federal AGI for that year (ordinary income including
taxable Social Security, plus long-term gains). For the first two plan years
there is no modelled income, so ``Scenario.irmaa_prior_magi`` stands in (none:
no surcharge in those years).

The surcharge is required spending: it is added on top of the spending rule's
output in the year it is paid and the rule cannot cut it. The standard Part B
premium itself is not modelled here (it belongs in ordinary spending).

Tiers and amounts are CMS's 2026 figures (``tax/data/medicare_2026.toml``).
The ranges are CPI-indexed (20 CFR 418.1115(e)) and held constant in real
terms, except the $500,000 / $750,000 top bounds, frozen through 2027, which
lose one year of inflation. The dollar adjustments are held constant in real
terms (in practice they grow with Part B costs).
"""

from __future__ import annotations

from firebridgelab.tax import params

_P = params.load_medicare()["irmaa"]

LOOKBACK_YEARS: int = int(_P["lookback_years"])
MEDICARE_AGE: int = int(_P["medicare_age"])
_FROZEN_YEARS: int = int(_P["frozen_bounds_years"])


def _tiers(rows):
    out = []
    for row in rows:
        if "magi_up_to" in row:
            bound, inclusive = float(row["magi_up_to"]), True
        elif "magi_below" in row:
            bound, inclusive = float(row["magi_below"]), False
        else:
            bound, inclusive = float("inf"), False
        out.append((bound, inclusive, bool(row.get("frozen", False)),
                    float(row["part_b"]), float(row["part_d"])))
    return tuple(out)


TIERS_SINGLE = _tiers(_P["single"])
TIERS_MFJ = _tiers(_P["mfj"])


def monthly_adjustment(
    magi: float, single: bool, years_since_start: int = 0, inflation: float = 0.0
) -> tuple[float, float]:
    """``(part_b, part_d)`` monthly adjustment per enrollee for ``magi``."""
    frozen_scale = (1.0 + inflation) ** -min(max(years_since_start, 0), _FROZEN_YEARS)
    for bound, inclusive, frozen, b, d in TIERS_SINGLE if single else TIERS_MFJ:
        if frozen:
            bound = bound * frozen_scale
        if magi < bound or (inclusive and magi == bound):
            return b, d
    return TIERS_SINGLE[-1][3:] if single else TIERS_MFJ[-1][3:]


def annual_surcharge(
    magi: float,
    single: bool,
    enrollees: int,
    years_since_start: int = 0,
    inflation: float = 0.0,
) -> float:
    """The household's yearly IRMAA: 12 x (Part B + Part D) x enrollees."""
    if enrollees <= 0:
        return 0.0
    b, d = monthly_adjustment(magi, single, years_since_start, inflation)
    return 12.0 * (b + d) * enrollees
