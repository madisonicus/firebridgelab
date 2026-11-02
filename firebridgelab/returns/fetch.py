"""Download the freely available raw data sources into the data directory and
build the staged asset-class series.

Usage:

    firebridgelab-fetch-data            # download what's missing, then build
    firebridgelab-fetch-data --force    # re-download everything
    firebridgelab-fetch-data --no-build # download only
    firebridgelab-fetch-data --dir PATH # use PATH instead of the default

The default directory is ``firebridgelab.paths.data_dir()``: the
FIREBRIDGELAB_DATA environment variable, else ``data/cache/`` in a source
checkout, else ``~/.cache/firebridgelab``.

What it fetches (see DATA-SOURCES.md for terms and citations):

    ie_data.xls               Robert Shiller, "Irrational Exuberance" monthly data
    histretSP.xls             Aswath Damodaran, historical returns (NYU Stern)
    6_Portfolios_2x3_CSV.zip  Kenneth R. French Data Library, 6 Size x B/M portfolios
    fred_gs5_gs10.csv         FRED (St. Louis Fed), GS5 + GS10 monthly yields

What it does NOT fetch: the FTSE Nareit REIT index file and the four testfol.io
simulated series. Their terms do not allow redistribution, so obtain them
yourself (instructions are printed at the end). Without them the engine runs
with the remaining asset classes; the REIT / international / emerging-markets /
commodities / TIPS classes simply do not appear.
"""

from __future__ import annotations

import argparse
import datetime
import html
import re
import sys
import urllib.request
from pathlib import Path

from firebridgelab.returns import data_build
from firebridgelab.returns.paths import data_dir


USER_AGENT = "Mozilla/5.0 (firebridgelab data fetch)"

# filename -> candidate URLs, tried in order. Hosts move files occasionally; if
# every candidate fails, download the file by hand from the source's page
# (DATA-SOURCES.md) and drop it into the data directory under this name.
SOURCES: dict[str, list[str]] = {
    # Shiller's current file is published on shillerdata.com (the link is read
    # from that page at fetch time; see _shiller_urls). The Yale copy stopped
    # updating in 2023 and is kept only as a last resort.
    "ie_data.xls": [
        "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
    ],
    "histretSP.xls": [
        "https://pages.stern.nyu.edu/~adamodar/pc/datasets/histretSP.xls",
    ],
    "6_Portfolios_2x3_CSV.zip": [
        "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/6_Portfolios_2x3_CSV.zip",
    ],
    # One CSV with columns observation_date,GS5,GS10 (monthly, percent).
    "fred_gs5_gs10.csv": [
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GS5,GS10",
    ],
}

TESTFOL_TICKERS = ("EFASIM", "VWOSIM", "GSGSIM", "TIPSIM")

OPTIONAL_INSTRUCTIONS = """\
Optional data (not redistributable; fetch it yourself if you want these classes)
-------------------------------------------------------------------------------
* US REITs (class us_reit): download the FTSE Nareit monthly index-history
  workbook (.xls) from Nareit's website (reit.com, Data & Research, REIT
  index data) and save it as nareit_monthly.xls in the data directory. The builder reads sheet
  "Index Data", column 23 (All Equity REITs total-return index).

* International developed, emerging markets, commodities, TIPS
  (classes intl_developed, emerging_markets, commodities, tips): simulated
  total-return series from testfol.io (tickers EFASIM, VWOSIM, GSGSIM, TIPSIM).
  Review testfol.io's terms first. The exact request the builder expects is
  _TESTFOL_FETCH_CMD in firebridgelab/data_build.py; save each response
  as testfol_raw_<TICKER>.json in the data directory.

Then re-run: firebridgelab-fetch-data
"""


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data:
        raise OSError("empty response")
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


SHILLER_PAGE = "https://shillerdata.com/"
_SHILLER_LINK = re.compile(r"""(?:https:)?//img1\.wsimg\.com/[^"'\s]*?ie_data\.xls[^"'\s]*""")


def _shiller_urls() -> list[str]:
    """Current ie_data.xls links found on shillerdata.com (may be empty)."""
    try:
        req = urllib.request.Request(SHILLER_PAGE, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — fall back to the static list
        print(f"  failed    shillerdata.com page ({type(exc).__name__}: {exc})")
        return []
    links = []
    for m in _SHILLER_LINK.findall(page):
        url = html.unescape(m)
        url = "https:" + url if url.startswith("//") else url
        if url not in links:
            links.append(url)
    return links


def _check_shiller(dest: Path) -> None:
    """Warn when the Shiller file looks stale (its last January is old)."""
    from firebridgelab.returns.shiller import parse_shiller

    try:
        last = parse_shiller(dest).last_year
    except Exception as exc:  # noqa: BLE001
        print(f"  warning   could not parse {dest.name}: {exc}")
        return
    if last < datetime.date.today().year - 2:
        print(
            f"  warning   {dest.name} ends with the {last} Jan-to-Jan return; it looks "
            f"stale. Download the current file from {SHILLER_PAGE} into {dest.parent}."
        )


def fetch(cache: Path, force: bool) -> list[str]:
    """Download each public source; return the names that could not be fetched."""
    cache.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for name, urls in SOURCES.items():
        dest = cache / name
        if dest.exists() and not force:
            print(f"  have      {name}")
            continue
        if name == "ie_data.xls":
            urls = _shiller_urls() + urls
        for url in urls:
            try:
                _download(url, dest)
                print(f"  fetched   {name}  <- {url}")
                break
            except Exception as exc:  # noqa: BLE001 — report and try the next URL
                print(f"  failed    {name}  <- {url} ({type(exc).__name__}: {exc})")
        else:
            failed.append(name)
    if (cache / "ie_data.xls").exists():
        _check_shiller(cache / "ie_data.xls")
    return failed


def _optional_present(cache: Path) -> dict[str, bool]:
    names = ["nareit_monthly.xls"] + [f"testfol_raw_{t}.json" for t in TESTFOL_TICKERS]
    return {n: (cache / n).exists() for n in names}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="re-download files already present")
    ap.add_argument("--no-build", action="store_true", help="skip building asset_class_series.csv")
    ap.add_argument("--dir", type=Path, default=None, help="data directory (default: FIREBRIDGELAB_DATA, a checkout's data/cache, or ~/.cache/firebridgelab)")
    args = ap.parse_args(argv)
    cache = (args.dir or data_dir()).expanduser()

    print(f"Fetching public sources into {cache}/")
    failed = fetch(cache, args.force)
    if failed:
        print(f"\nCould not download: {', '.join(failed)}. See DATA-SOURCES.md for the source pages.")
        return 1

    optional = _optional_present(cache)
    missing_optional = [n for n, ok in optional.items() if not ok]
    if missing_optional:
        print()
        print(OPTIONAL_INSTRUCTIONS)
        print(f"(currently missing: {', '.join(missing_optional)})\n")

    if not args.no_build:
        print(f"Building {cache / 'asset_class_series.csv'}")
        data_build.main(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
