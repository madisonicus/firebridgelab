"""Load the federal tax parameters from ``data/federal_<year>.toml``.

The file is read once per process. ``core`` and ``rmd`` take their constants
from it, so refreshing the tax year is an edit of the data file.
"""

from __future__ import annotations

import functools
import tomllib
from importlib import resources
from typing import Any

TAX_YEAR = 2026

Brackets = tuple[tuple[float, float], ...]


@functools.cache
def load(year: int = TAX_YEAR) -> dict[str, Any]:
    """The parsed parameter file for ``year``."""
    text = resources.files(__package__).joinpath(f"data/federal_{year}.toml").read_text(
        encoding="utf-8"
    )
    return tomllib.loads(text)


def brackets(rows: list[list[float]]) -> Brackets:
    """``[[upper, rate], ...]`` from the data file as a tuple of float pairs."""
    return tuple((float(upper), float(rate)) for upper, rate in rows)
