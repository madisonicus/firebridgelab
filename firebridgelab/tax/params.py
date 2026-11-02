"""Load the tax parameters from ``data/*.toml``: federal, Medicare and state.

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
def load_file(name: str) -> dict[str, Any]:
    """The parsed data file ``data/<name>.toml``."""
    text = resources.files(__package__).joinpath(f"data/{name}.toml").read_text(
        encoding="utf-8"
    )
    return tomllib.loads(text)


def load(year: int = TAX_YEAR) -> dict[str, Any]:
    """The parsed federal parameter file for ``year``."""
    return load_file(f"federal_{year}")


def load_medicare(year: int = TAX_YEAR) -> dict[str, Any]:
    """The parsed Medicare (IRMAA) parameter file for ``year``."""
    return load_file(f"medicare_{year}")


def load_states(year: int = TAX_YEAR) -> dict[str, Any]:
    """The parsed state income tax file for ``year``."""
    return load_file(f"states_{year}")


def data_files() -> list[str]:
    """Names (without ``.toml``) of every data file in ``data/``."""
    return sorted(
        p.name[: -len(".toml")]
        for p in resources.files(__package__).joinpath("data").iterdir()
        if p.name.endswith(".toml")
    )


def brackets(rows: list[list[float]]) -> Brackets:
    """``[[upper, rate], ...]`` from the data file as a tuple of float pairs."""
    return tuple((float(upper), float(rate)) for upper, rate in rows)
