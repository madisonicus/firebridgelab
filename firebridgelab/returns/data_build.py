"""Builder: raw free-source files → staged annual REAL return series.

Run by ``firebridgelab-fetch-data`` after the download, or directly with
``python -m firebridgelab.data_build``.

Slice 1 of the asset-class substrate. Mirrors the offline-cache discipline of
``firebridgelab/shiller.py``: the raw vendor files live in the data directory
(``firebridgelab.paths.data_dir()``) and are
processed ONCE, here, into a single canonical CSV
(``asset_class_series.csv``) that the runtime loader
(``firebridgelab/class_series.py``) reads offline. There is deliberately NO network
download at runtime — re-run this script by hand to refresh the staged data.

Every series is an **annual REAL** total return (nominal sources deflated by
the same annual CPI, FRED CPIAUCNS, taken from Damodaran's "Inflation Rate"
sheet — the engine has no separate annual CPI series to reuse, so this one
deflator is used consistently across all new series).

Series produced (class id == series id for all of these — see D5):

Slice 1 (long-history free sources):
  - ``st_treasury``    Damodaran 3-month T.Bill (nominal), 1928+        [cash class]
  - ``lt_treasury``    Damodaran 10-yr T.Bond total return, 1928+
  - ``us_small_value`` Ken French 6 Size×B/M, SMALL HiBM, value-weighted
                       annual, 1927+
  - ``it_treasury``    FRED GS5 5-yr constant-maturity yield, duration-
                       constructed total return (same method shiller.py uses
                       for the 10-yr bond), 1953+
  - ``us_reit``        FTSE Nareit All-Equity REITs total-return index,
                       Dec-to-Dec, 1972+

Slice 4a (the remaining five classes):
  - ``gold``           Damodaran ``histretSP`` gold column (col 7), 1972+
                       (pre-1971 returns are a $35-peg artifact — excluded).
                       Same already-cached raw file slice 1 reads.
  - ``intl_developed`` MSCI EAFE Gross-TR proxy (testfol.io ``EFASIM`` sim),
                       1970+ — annual total returns, dividends reinvested.
  - ``emerging_markets`` FTSE Emerging-Markets index proxy (testfol.io
                       ``VWOSIM`` sim, Vanguard FTSE EM), first FULL year 1995.
  - ``commodities``    S&P GSCI total-return proxy (testfol.io ``GSGSIM`` sim),
                       first FULL year 1980 (the longer-history GSCI choice;
                       BCOM has no free annual splice — see design FYI #3).
  - ``tips``           Bloomberg US-TIPS proxy (testfol.io ``TIPSIM`` sim,
                       iShares TIP), first FULL year 2001 (the 1997 Bloomberg
                       index has no free annual feed — see design FYI #1).

The four testfol.io series are fetched ONCE via the public backtest API and
their raw JSON responses cached as ``testfol_raw_<TICKER>.json`` in the data directory;
this builder reads those cached files offline (no network at build time — same
discipline as ``shiller.py`` / the slice-1 sources). testfol "sim" tickers are
dividend-reinvested total-return splices that extend an ETF/fund past its
inception; the design treats them as a cross-check oracle and explicitly permits
a fund/proxy for these hard-to-source classes (D5). Each was spot-checked
against published index returns (e.g. EAFE/EM/GSCI/TIP 2021–2023) before
staging — see the validation notes in ``tests/test_class_series.py``. To
re-fetch, see ``_TESTFOL_FETCH_CMD`` below.

Output CSV is long-format: ``series_id,year,real_return`` (one row per series
per year), sorted by series then year. ``real_return`` is a fraction
(0.07 = 7%).
"""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
import numpy as np
import xlrd
from xlrd import xldate

from firebridgelab.returns.paths import data_dir


# Set by main(); the builders read their raw files from here.
CACHE = data_dir()
OUT = CACHE / "asset_class_series.csv"

# Same duration convention shiller.py uses to turn a constant-maturity yield
# into a price return. The intermediate (5-yr) Treasury gets ~4.5 yrs; the
# 10-yr uses 7.0 in shiller.py. ~4.5 is the conventional 5-yr CMT duration.
_IT_TREASURY_DURATION = 4.5


def _load_cpi() -> dict[int, float]:
    """Annual CPI inflation as a fraction, year → rate, from Damodaran.

    The "Inflation Rate" sheet is FRED CPIAUCNS percent-change-year-ago: col 0
    is the year, col 2 the inflation fraction (0.0273 = 2.73%). Used as the
    single real-deflator for every nominal source here.
    """
    wb = xlrd.open_workbook(CACHE / "histretSP.xls")
    sh = wb.sheet_by_name("Inflation Rate")
    cpi: dict[int, float] = {}
    for r in range(sh.nrows):
        y = sh.cell_value(r, 0)
        infl = sh.cell_value(r, 2)
        if isinstance(y, (int, float)) and 1900 <= y <= 2100 and isinstance(infl, (int, float)):
            cpi[int(y)] = float(infl)
    return cpi


def _deflate(nominal: float, inflation: float) -> float:
    """Nominal annual return → real, given the year's inflation fraction."""
    return (1.0 + nominal) / (1.0 + inflation) - 1.0


def _damodaran_nominal(col: int) -> dict[int, float]:
    """Year → nominal annual return for a column of the 'Returns by year' sheet.

    Col map (0-indexed): 0=Year, 1=S&P500, 2=US small (decile), 3=3-mo T.Bill,
    4=US T.Bond(10yr), 7=Gold. We read only the per-year rows (col 0 a 4-digit
    year), skipping the trailing decade-average summary rows.
    """
    wb = xlrd.open_workbook(CACHE / "histretSP.xls")
    sh = wb.sheet_by_name("Returns by year")
    out: dict[int, float] = {}
    for r in range(sh.nrows):
        y = sh.cell_value(r, 0)
        v = sh.cell_value(r, col)
        if isinstance(y, (int, float)) and 1900 <= y <= 2100 and isinstance(v, (int, float)):
            out[int(y)] = float(v)
    return out


def build_st_treasury(cpi: dict[int, float]) -> dict[int, float]:
    """3-month T.Bill REAL annual return (the cash class), Damodaran col 3."""
    nominal = _damodaran_nominal(3)
    return {y: _deflate(n, cpi[y]) for y, n in nominal.items() if y in cpi}


def build_lt_treasury(cpi: dict[int, float]) -> dict[int, float]:
    """10-yr T.Bond REAL total return, Damodaran col 4 (already a TR series)."""
    nominal = _damodaran_nominal(4)
    return {y: _deflate(n, cpi[y]) for y, n in nominal.items() if y in cpi}


def build_us_small_value(cpi: dict[int, float]) -> dict[int, float]:
    """Ken French SMALL HiBM (small-cap value), value-weighted, annual, REAL.

    The '6_Portfolios_2x3.csv' carries several sections; we parse the one
    headed 'Average Value Weighted Returns -- Annual'. Header columns:
    Date,SMALL LoBM,ME1 BM2,SMALL HiBM,BIG LoBM,ME2 BM2,BIG HiBM — so
    small-value (small size, high book-to-market) is field index 3. Returns
    are percent; -99.99/-999 are French's missing-data sentinels.
    """
    z = zipfile.ZipFile(CACHE / "6_Portfolios_2x3_CSV.zip")
    text = z.read("6_Portfolios_2x3.csv").decode("latin-1")
    lines = text.splitlines()

    # Locate the annual value-weighted block, then read until a blank/non-data row.
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "Average Value Weighted Returns -- Annual":
            start = i + 2  # skip the section title and the column-header row
            break
    if start is None:
        raise ValueError("could not find annual value-weighted section in French CSV")

    out: dict[int, float] = {}
    for line in lines[start:]:
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0].isdigit():
            break  # end of the annual VW block
        year = int(parts[0])
        small_value_pct = float(parts[3])  # SMALL HiBM
        if small_value_pct <= -99.0:  # missing-data sentinel
            continue
        nominal = small_value_pct / 100.0
        if year in cpi:
            out[year] = _deflate(nominal, cpi[year])
    return out


def build_it_treasury(cpi: dict[int, float]) -> dict[int, float]:
    """Intermediate (5-yr) Treasury REAL total return, FRED GS5 constructed.

    FRED gives the 5-yr constant-maturity *yield* monthly. We take each year's
    January yield (matching shiller.py's Jan-to-Jan annual convention) and
    build a duration-adjusted nominal total return:
        nominal = y0 + DURATION * (y0 - y1)
    (coupon income y0 plus a price gain/loss as the yield moves), then deflate
    by that year's CPI. Same construction shiller.py uses for the 10-yr bond.
    GS5 begins 1953-04, so the first full Jan-to-Jan year is 1954.
    """
    jan_yield: dict[int, float] = {}
    with open(CACHE / "fred_gs5_gs10.csv", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row["observation_date"]
            gs5 = row.get("GS5", "").strip()
            if not date or not gs5:
                continue
            year_s, month_s, _ = date.split("-")
            if month_s == "01":  # January observation
                jan_yield[int(year_s)] = float(gs5) / 100.0

    out: dict[int, float] = {}
    years = sorted(jan_yield)
    for y in years:
        if (y + 1) not in jan_yield or (y + 1) not in cpi:
            continue
        y0 = jan_yield[y]
        y1 = jan_yield[y + 1]
        nominal = y0 + _IT_TREASURY_DURATION * (y0 - y1)
        out[y] = _deflate(nominal, cpi[y + 1])  # inflation over the same Jan-Jan window
    return out


# The exact (offline-cacheable) command used to fetch each testfol.io series'
# raw JSON. Recorded for reproducibility — re-run this per ticker to refresh the
# cached ``testfol_raw_<TICKER>.json`` files, then re-run this builder. The
# request asks for nominal ("adj_inflation": false) annual TOTAL returns over the
# series' full available history; we deflate to real here with the same CPI.
_TESTFOL_FETCH_CMD = (
    "curl -s https://testfol.io/api/backtest -X POST "
    "-H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' "
    "-d '{\"start_date\":\"1900-01-01\",\"end_date\":\"2025-12-31\","
    "\"start_val\":10000,\"adj_inflation\":false,\"cashflow\":0,"
    "\"cashflow_freq\":\"Yearly\",\"rebalance\":\"None\",\"rolling_window\":60,"
    "\"adjustment\":\"Total\",\"backtests\":[{\"name\":\"x\","
    "\"allocation\":{\"<TICKER>\":100}}]}' > testfol_raw_<TICKER>.json"
)

# class id → (testfol sim ticker, human-readable proxy description). class id ==
# series id for all four (D5). The ticker names the cached raw JSON file.
_TESTFOL_SERIES: dict[str, tuple[str, str]] = {
    "intl_developed": ("EFASIM", "MSCI EAFE gross-TR proxy"),
    "emerging_markets": ("VWOSIM", "Vanguard FTSE Emerging Markets index proxy"),
    "commodities": ("GSGSIM", "S&P GSCI total-return proxy"),
    "tips": ("TIPSIM", "iShares TIP / Bloomberg US TIPS proxy"),
}


def build_gold(cpi: dict[int, float]) -> dict[int, float]:
    """Gold REAL annual total return, Damodaran 'Returns by year' col 7, 1972+.

    Pre-1971 gold returns reflect the fixed $35/oz Bretton-Woods peg (near-zero,
    structurally meaningless — design FYI #2), so the series starts 1972 (the
    first full post-decoupling year). The same already-cached ``histretSP.xls``
    slice 1 reads; col 7 is the 'Gold*' nominal annual return, deflated by the
    year's CPI like the other Damodaran columns.
    """
    nominal = _damodaran_nominal(7)
    return {
        y: _deflate(n, cpi[y])
        for y, n in nominal.items()
        if y >= 1972 and y in cpi
    }


def _testfol_full_year_returns(ticker: str) -> dict[int, float]:
    """Year → nominal annual total return from a cached testfol raw JSON file.

    The API's ``annual_returns`` are ``[year, percent, end_value]``. We drop the
    inception year UNLESS the series started on/before Jan 2 of that year (so a
    mid-year inception's partial stub never enters as if it were a full calendar
    year — e.g. VWOSIM 1994-05-04 → first FULL year 1995; EFASIM 1969-12-31 →
    1970 is a full year, kept). Returns the nominal fractions keyed by year.
    """
    path = CACHE / f"testfol_raw_{ticker}.json"
    with open(path) as f:
        payload = json.load(f)

    start_date = payload["start_date"]  # "YYYY-MM-DD"
    start_year, start_month, start_day = (int(p) for p in start_date.split("-"))
    # A calendar year y is FULL iff the series was live at its start, i.e. it
    # began on/before Jan 2 of year y (Dec 31 of y-1 counts; any later day in y
    # makes y a partial stub to drop).
    first_full_year = start_year if (start_month, start_day) <= (1, 2) else start_year + 1

    out: dict[int, float] = {}
    for year, pct, _end_val in payload["annual_returns"]:
        if int(year) < first_full_year:
            continue
        out[int(year)] = float(pct) / 100.0
    return out


def _build_testfol_real(ticker: str, cpi: dict[int, float]) -> dict[int, float]:
    """A testfol-sourced class's REAL annual total return (nominal → real)."""
    nominal = _testfol_full_year_returns(ticker)
    return {y: _deflate(n, cpi[y]) for y, n in nominal.items() if y in cpi}


def build_us_reit(cpi: dict[int, float]) -> dict[int, float]:
    """FTSE Nareit All-Equity REITs REAL annual total return, Dec-to-Dec.

    The 'Index Data' sheet holds monthly index values; the All-Equity REITs
    group's total-return INDEX is column 23 (group header 'All Equity REITs',
    sub-header 'Total'/'Index'). Dates are Excel serials. We take December
    index levels and compute Dec(y-1)→Dec(y) annual returns, then deflate by
    year y's CPI. Base index 100 at Dec 1971, so the first return year is 1972.
    """
    wb = xlrd.open_workbook(CACHE / "nareit_monthly.xls")
    sh = wb.sheet_by_name("Index Data")
    dec_index: dict[int, float] = {}
    for r in range(sh.nrows):
        d = sh.cell_value(r, 0)
        idx = sh.cell_value(r, 23)
        if not isinstance(d, (int, float)) or d <= 0:
            continue
        if not isinstance(idx, (int, float)) or idx <= 0:
            continue
        dt = xldate.xldate_as_datetime(d, wb.datemode).date()
        if dt.month == 12:
            dec_index[dt.year] = float(idx)

    out: dict[int, float] = {}
    for y in sorted(dec_index):
        if (y - 1) not in dec_index or y not in cpi:
            continue
        nominal = dec_index[y] / dec_index[y - 1] - 1.0
        out[y] = _deflate(nominal, cpi[y])
    return out


def main(cache: Path | None = None) -> None:
    """Build ``asset_class_series.csv`` in ``cache`` (default: ``data_dir()``)."""
    global CACHE, OUT
    CACHE = Path(cache) if cache is not None else data_dir()
    OUT = CACHE / "asset_class_series.csv"
    cpi = _load_cpi()
    builders = {
        # slice 1 — long-history free sources
        "st_treasury": build_st_treasury,
        "lt_treasury": build_lt_treasury,
        "us_small_value": build_us_small_value,
        "it_treasury": build_it_treasury,
        "us_reit": build_us_reit,
        # slice 4a — the remaining five classes
        "gold": build_gold,
        "intl_developed": lambda c: _build_testfol_real("EFASIM", c),
        "emerging_markets": lambda c: _build_testfol_real("VWOSIM", c),
        "commodities": lambda c: _build_testfol_real("GSGSIM", c),
        "tips": lambda c: _build_testfol_real("TIPSIM", c),
    }
    rows: list[tuple[str, int, float]] = []
    for series_id, fn in builders.items():
        try:
            series = fn(cpi)
        except FileNotFoundError as e:
            print(f"{series_id:16s} SKIPPED (raw file missing: {e.filename})")
            continue
        for year in sorted(series):
            rows.append((series_id, year, series[year]))
        arr = np.array([series[y] for y in sorted(series)], dtype=float)
        geo = float(np.prod(1.0 + arr) ** (1.0 / len(arr)) - 1.0)
        print(
            f"{series_id:16s} n={len(arr):3d} "
            f"{min(series)}-{max(series)} "
            f"geo={geo:+.4%} stdev={arr.std(ddof=1):.4%}"
        )

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["series_id", "year", "real_return"])
        for series_id, year, r in rows:
            w.writerow([series_id, year, f"{r:.10f}"])
    print(f"\nwrote {len(rows)} rows → {OUT}")


if __name__ == "__main__":
    main()
