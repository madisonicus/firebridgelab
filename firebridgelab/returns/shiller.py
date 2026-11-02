"""Offline loader for Robert Shiller's historical dataset (``ie_data.xls``).

Adapted from the archived prior version's vetted parser — only the
data-loading/parsing logic, in the current codebase's clean style. Turns
Shiller's monthly Excel sheet into **annual real total returns** for two
series:

  - ``us_equity`` — S&P 500 real total return (price appreciation + dividends,
    CPI-adjusted).
  - ``us_bonds``  — 10-year Treasury real total return, duration-adjusted from
    the long-term yield (a documented simplification cFIREsim makes too).

Returns are already real (CPI-adjusted), so they drop straight into the
real-terms engine; CPI is not needed for Historical mode.

This reads a **cached** file shipped with the app (``ie_data.xls`` in the data directory; see ``firebridgelab.paths``).
There is deliberately NO network download at runtime — the service reads the
file offline. ``xlrd`` is the .xls engine.

Public surface:
  - ``ShillerData`` — parsed annual series + ``returns_for_period`` /
    ``all_starting_years`` helpers (same shape the archived module exposed).
  - ``load_shiller()`` — parse the cached file (memoized).
  - ``SERIES_LOADERS`` — registry mapping a series id to the array on
    ``ShillerData`` that holds it (see firebridgelab.historical for the registry that
    binds asset classes to these ids).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

from firebridgelab.returns.paths import data_dir, missing_data_message


# Cached data file shipped with the app. Read offline; never downloaded at
# runtime (the archived module's download path is intentionally dropped).
CACHE_FILE = data_dir() / "ie_data.xls"

# Approximate duration of a 10-year Treasury, used to convert a yield change
# into a price return for the bond series. ~7 years is the conventional figure
# and matches the archived parser (and, broadly, cFIREsim's approach).
_TREASURY_DURATION = 7.0


@dataclass(frozen=True)
class ShillerData:
    """Parsed Shiller dataset as annual real returns, indexed by start year.

    ``years[i]`` is the calendar year whose real return is ``stock_real_return[i]``
    / ``bond_real_return[i]`` (the return earned *over* that year). Arrays are
    parallel and sorted ascending by year.
    """

    years: np.ndarray            # int array of years, ascending
    stock_real_return: np.ndarray  # S&P real total return for each year
    bond_real_return: np.ndarray   # 10yr Treasury real total return for each year

    @property
    def first_year(self) -> int:
        return int(self.years[0])

    @property
    def last_year(self) -> int:
        return int(self.years[-1])

    def all_starting_years(self, n_years: int) -> list[int]:
        """Every start year with a complete ``n_years``-long window of data.

        A cohort starting in year Y needs returns for years Y .. Y+n_years-1, so
        the latest valid start is ``last_year - n_years + 1``. Recent starts
        without a full window are excluded (per the cohort model in the brief).
        """
        max_start = self.last_year - n_years + 1
        return [int(y) for y in self.years if y <= max_start]

    def returns_for_period(self, start_year: int, n_years: int) -> dict:
        """Stock and bond real-return arrays for the ``n_years`` window at ``start_year``.

        Returns ``{"us_equity": ndarray, "us_bonds": ndarray}`` each of length
        ``n_years``. Raises ``ValueError`` if the window runs past the data.
        """
        idx = int(np.searchsorted(self.years, start_year))
        if idx >= len(self.years) or int(self.years[idx]) != start_year:
            raise ValueError(f"no data for start year {start_year}")
        if idx + n_years > len(self.years):
            raise ValueError(
                f"not enough data: need {start_year} + {n_years} years, "
                f"data ends at {self.last_year}"
            )
        return {
            "us_equity": self.stock_real_return[idx : idx + n_years],
            "us_bonds": self.bond_real_return[idx : idx + n_years],
        }


def parse_shiller(filepath: Path) -> ShillerData:
    """Parse the Shiller ``ie_data.xls`` 'Data' sheet into annual real returns.

    Layout (0-indexed columns on the 'Data' sheet, header rows skipped):
        col 0  Date as year.month (e.g. 1871.01)
        col 1  S&P 500 price
        col 2  Dividend (annual rate)
        col 4  CPI
        col 6  Long interest rate (10-year yield, %)

    For each January-to-January step we compute the year's inflation, the
    nominal stock total return (price change + dividend), and a
    duration-adjusted nominal bond return, then deflate both to real.
    """
    df = pd.read_excel(
        filepath, sheet_name="Data", header=None, skiprows=7, engine="xlrd"
    )

    # Keep only rows whose date cell is numeric (drops trailing notes/footers).
    df = df.dropna(subset=[0])
    df = df[pd.to_numeric(df[0], errors="coerce").notna()]

    date_col = df[0].astype(float).to_numpy()
    price = pd.to_numeric(df[1], errors="coerce").to_numpy()
    dividend = pd.to_numeric(df[2], errors="coerce").to_numpy()
    cpi = pd.to_numeric(df[4], errors="coerce").to_numpy()
    bond_yield = pd.to_numeric(df[6], errors="coerce").to_numpy()

    years = np.floor(date_col).astype(int)
    months = np.round((date_col - years) * 100).astype(int)

    monthly = pd.DataFrame(
        {
            "year": years,
            "month": months,
            "price": price,
            "dividend": dividend,
            "cpi": cpi,
            "bond_yield": bond_yield,
        }
    ).dropna(subset=["price", "cpi"])

    # Annual snapshots: take each year's January row, sorted ascending.
    jan = (
        monthly[monthly["month"] == 1]
        .sort_values("year")
        .reset_index(drop=True)
    )

    valid_years: list[int] = []
    stock_real: list[float] = []
    bond_real: list[float] = []

    for i in range(len(jan) - 1):
        row = jan.iloc[i]
        nxt = jan.iloc[i + 1]
        p0, p1 = row["price"], nxt["price"]
        cpi0, cpi1 = row["cpi"], nxt["cpi"]
        # Shiller's D is the trailing-12-month dividend at an annual rate, so the
        # dividends PAID during the Jan(t)->Jan(t+1) holding year are D at Jan(t+1).
        # (Using D at Jan(t) paired year t's price change with year t-1's
        # dividends: about -9 bp/yr over 1871-2025; review 2026-10-11.)
        div = nxt["dividend"]
        if pd.isna(div):  # latest month not yet published: fall back to trailing D
            div = row["dividend"]
        y_now, y_next = row["bond_yield"], nxt["bond_yield"]

        if any(pd.isna([p0, p1, cpi0, cpi1])):
            continue
        if p0 <= 0 or cpi0 <= 0 or cpi1 <= 0:
            continue

        inflation = cpi1 / cpi0 - 1.0

        # Nominal stock total return = (price change + dividend) / start price.
        div_amount = 0.0 if pd.isna(div) else float(div)
        nominal_stock = (p1 + div_amount) / p0 - 1.0
        real_stock = (1.0 + nominal_stock) / (1.0 + inflation) - 1.0

        # Bond real return, duration-adjusted from the 10yr yield: coupon plus a
        # price gain/loss as the yield moves (falling yields → capital gain).
        if not pd.isna(y_now) and not pd.isna(y_next):
            r0 = float(y_now) / 100.0
            r1 = float(y_next) / 100.0
            nominal_bond = r0 + _TREASURY_DURATION * (r0 - r1)
            real_bond = (1.0 + nominal_bond) / (1.0 + inflation) - 1.0
        elif not pd.isna(y_now):
            real_bond = float(y_now) / 100.0 - inflation
        else:
            # Last-resort fallback; effectively never hit on Shiller's data.
            real_bond = 0.02

        valid_years.append(int(row["year"]))
        stock_real.append(real_stock)
        bond_real.append(real_bond)

    return ShillerData(
        years=np.array(valid_years, dtype=int),
        stock_real_return=np.array(stock_real, dtype=float),
        bond_real_return=np.array(bond_real, dtype=float),
    )


@lru_cache(maxsize=1)
def load_shiller(filepath: str | None = None) -> ShillerData:
    """Parse the cached Shiller file (memoized for the process lifetime).

    ``filepath`` defaults to the shipped cache. The result is cached so the
    Excel parse runs once per process, not once per request. Tests can pass an
    explicit path to bypass the default cache file.
    """
    path = Path(filepath) if filepath else CACHE_FILE
    if not path.exists():
        raise FileNotFoundError(missing_data_message(path))
    return parse_shiller(path)
