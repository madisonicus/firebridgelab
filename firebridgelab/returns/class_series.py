"""Offline loader for the staged asset-class annual REAL return series.

The two Shiller series (``us_equity`` / ``us_bonds``) come from
``returns.shiller``; the other classes' series are pre-processed by
``returns.data_build`` from free sources (Damodaran, Ken French, FRED, FTSE
Nareit, testfol.io) into a single cached CSV (``asset_class_series.csv`` in the
data directory; see ``returns.paths``) of annual REAL total returns. This
module reads that CSV offline — same discipline as ``returns.shiller`` (NO
network download at runtime; re-run the builder to refresh).

Each row is ``series_id,year,real_return`` (real_return a fraction). The series
are ANNUAL (matching the engine's cohort granularity) and already REAL
(CPI-deflated by the builder), so they drop straight into the real-terms
engine.

Public surface:
  - ``ClassSeries`` — one series' parallel year / real-return arrays plus its
    geometric-mean / stdev moments (the Deterministic and MC defaults).
  - ``load_class_series()`` — parse the cached CSV into ``{series_id:
    ClassSeries}`` (memoized).
  - ``NEW_CLASS_SERIES`` — the staged series ids carried in the CSV (the
    subset of ``ALL_NEW_CLASS_SERIES`` present; optional classes may be
    absent), used by the class menu in ``engine.allocation`` and the series
    registries in ``returns.series``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import numpy as np

from firebridgelab.returns.paths import data_dir, missing_data_message


# Cached staged data file. Read offline; produced once by the builder.
CACHE_FILE = data_dir() / "asset_class_series.csv"

# The series ids the staged CSV carries. class id == series id for all of these.
# Display labels live with the class menu, not here. This single tuple is the
# registration point that fans out through the engine: it builds
# SERIES_REGISTRY_NEW / SERIES_FOR_CLASS / MC_SERIES_REGISTRY /
# HIST_SERIES_REGISTRY (returns.series) and CLASS_MENU (engine.allocation), and
# every id here is loaded by real_return_series_by_series_id (returns.correlation)
# straight from the staged CSV. Adding a class = staging its CSV rows + one entry
# here (+ its display label in engine.allocation._NEW_CLASS_LABELS).
#
# With the two Shiller series these make the 12-class menu.
ALL_NEW_CLASS_SERIES: tuple[str, ...] = (
    # long-history free sources
    "us_small_value",
    "lt_treasury",
    "it_treasury",
    "st_treasury",
    "us_reit",
    # shorter histories: Damodaran gold, testfol.io simulations
    "gold",
    "intl_developed",
    "emerging_markets",
    "commodities",
    "tips",
)

# Classes whose raw source data is NOT freely redistributable (FTSE Nareit for
# ``us_reit``; testfol.io simulations for the other four). They are OPTIONAL: when
# the builder ran without their raw files, the staged CSV simply lacks their rows
# and the engine runs with the remaining classes (they drop out of the class
# menu, the presets that need them, and the historical / MC registries). See
# DATA-SOURCES.md.
OPTIONAL_CLASS_SERIES: frozenset[str] = frozenset(
    {"us_reit", "intl_developed", "emerging_markets", "commodities", "tips"}
)


@dataclass(frozen=True)
class ClassSeries:
    """One asset class's annual REAL return series + its summary moments.

    ``years[i]`` is the calendar year whose real return is ``real_return[i]``
    (the return earned over that year). Arrays are parallel and ascending.

    The three moments back the three engine modes: ``geometric_mean`` is
    the Deterministic default (the constant rate reproducing realized
    cumulative growth — NOT the arithmetic average, which would overstate
    compounding); ``stdev`` is the annual real-return standard deviation used
    by Monte Carlo; ``arithmetic_mean`` is the MC drift before the per-class
    vol²/2 reconciliation (carried for convenience / cross-checking).
    """

    series_id: str
    years: np.ndarray        # int array, ascending
    real_return: np.ndarray  # annual real return per year, fraction

    @property
    def first_year(self) -> int:
        return int(self.years[0])

    @property
    def last_year(self) -> int:
        return int(self.years[-1])

    @property
    def n(self) -> int:
        return int(len(self.years))

    @property
    def geometric_mean(self) -> float:
        """Constant rate g with (1+g)^N == prod(1+r_t) over the full window.

        The Deterministic-mode default. By construction this reproduces the
        realized cumulative growth of the series (a closed-form self-check
        the tests assert).
        """
        growth = float(np.prod(1.0 + self.real_return))
        return growth ** (1.0 / self.n) - 1.0

    @property
    def arithmetic_mean(self) -> float:
        return float(np.mean(self.real_return))

    @property
    def stdev(self) -> float:
        """Sample standard deviation (ddof=1) of the annual real returns."""
        return float(np.std(self.real_return, ddof=1))

    @property
    def cumulative_growth(self) -> float:
        """Realized total growth multiple over the window, prod(1+r_t)."""
        return float(np.prod(1.0 + self.real_return))


def parse_class_series(filepath: Path) -> dict[str, ClassSeries]:
    """Parse the staged ``series_id,year,real_return`` CSV into ClassSeries.

    Groups rows by ``series_id``, sorts each by year, and packs parallel
    arrays. Raises ``ValueError`` if the file is missing the expected header.
    """
    import csv

    by_series: dict[str, list[tuple[int, float]]] = {}
    with open(filepath, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["series_id", "year", "real_return"]:
            raise ValueError(
                f"unexpected header in {filepath}: {reader.fieldnames}"
            )
        for row in reader:
            sid = row["series_id"]
            by_series.setdefault(sid, []).append(
                (int(row["year"]), float(row["real_return"]))
            )

    out: dict[str, ClassSeries] = {}
    for sid, pairs in by_series.items():
        pairs.sort()
        years = np.array([y for y, _ in pairs], dtype=int)
        returns = np.array([r for _, r in pairs], dtype=float)
        out[sid] = ClassSeries(series_id=sid, years=years, real_return=returns)
    return out


@lru_cache(maxsize=1)
def load_class_series(filepath: str | None = None) -> dict[str, ClassSeries]:
    """Parse the cached staged CSV (memoized for the process lifetime).

    ``filepath`` defaults to the shipped cache. Tests can pass an explicit path.
    """
    path = Path(filepath) if filepath else CACHE_FILE
    if not path.exists():
        raise FileNotFoundError(missing_data_message(path))
    return parse_class_series(path)


def _available_new_class_series() -> tuple[str, ...]:
    """``ALL_NEW_CLASS_SERIES`` filtered to the series the staged CSV carries.

    A REQUIRED (freely redistributable) series missing from the CSV is an error;
    an OPTIONAL one is silently dropped. If the CSV itself is missing, return
    the full tuple so the failure surfaces at first use with the loader's own
    FileNotFoundError (same behavior as before this filter existed).
    """
    try:
        loaded = load_class_series()
    except FileNotFoundError:
        return ALL_NEW_CLASS_SERIES
    missing_required = [
        sid for sid in ALL_NEW_CLASS_SERIES
        if sid not in loaded and sid not in OPTIONAL_CLASS_SERIES
    ]
    if missing_required:
        raise ValueError(
            f"staged class-series CSV is missing required series {missing_required}; "
            "re-run firebridgelab-fetch-data"
        )
    return tuple(sid for sid in ALL_NEW_CLASS_SERIES if sid in loaded)


# The series ids actually available in this install (see OPTIONAL_CLASS_SERIES).
NEW_CLASS_SERIES: tuple[str, ...] = _available_new_class_series()
