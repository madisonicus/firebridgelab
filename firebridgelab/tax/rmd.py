"""Required minimum distributions: start age and the Uniform Lifetime Table.

Values come from ``data/federal_<year>.toml`` (see ``tax.params``).
"""

from __future__ import annotations

from firebridgelab.tax import params


_RMD = params.load()["rmd"]

# First RMD age (SECURE 2.0, born 1960 or later).
RMD_START_AGE: int = _RMD["start_age"]
# IRS Uniform Lifetime Table: age -> distribution period (years).
_RMD_UNIFORM_LIFETIME_TABLE: dict[int, float] = {
    int(age): float(period) for age, period in _RMD["uniform_lifetime_table"].items()
}


def rmd_factor(age: int) -> float:
    """Uniform Lifetime Table factor at ``age``.

    ``age`` below ``RMD_START_AGE`` never reaches an RMD year (callers gate on
    that before calling this); ages at/above the table's last entry (120) reuse
    the 120 factor (2.0) — the IRS table itself is "120 and over" -> 2.0, so
    this is not an approximation beyond the table's own definition.
    """
    if age >= 120:
        return _RMD_UNIFORM_LIFETIME_TABLE[120]
    if age <= RMD_START_AGE:
        return _RMD_UNIFORM_LIFETIME_TABLE[RMD_START_AGE]
    return _RMD_UNIFORM_LIFETIME_TABLE[age]
