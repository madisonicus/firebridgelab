"""Owl cross-check: this library's side, re-derived for three named cohorts.

Notebook 03 (notebooks/03-owl-cross-check.ipynb) runs the default fictional
household through Owl (https://github.com/mdlacasse/Owl, owlplanner
2026.10.28) and this library for every start year 1928-1980, and compares the
highest flat after-tax spend that lasts to 95 with nothing left. That notebook
is the evidence; it needs Owl and is not run in CI. This test re-derives this
library's side for three cohorts with the notebook's own method (bisection on
the tax-dragged path, ``simulate_dragged_depletion_only``) and pins it, so an
engine change that moves the comparison fails here.

Pins were taken on 2026-11-02 against the reference Shiller file
(tests/_canary.py). Tolerance: on that file, $20 (the bisection stops within
$10); on any other revision of Shiller's data, 0.5%. Shiller revises recent
months; these cohorts read 46 years of data, so a revision moves them little,
but by more than the canary's 1e-4 when the revised months fall inside the
window.

Owl's side, from the same notebook run, is recorded beside each pin; the
per-cohort gap must stay inside the range the notebook documents for all 53
cohorts (-3.7% to +6.3%).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import firebridgelab as rm
from firebridgelab.engine.cohort_windows import cohort_return_sequences
from firebridgelab.spending import make_strategy
from firebridgelab.tax.location_path import (
    simulate_dragged_depletion_only,
    year_tax_context_table,
)
from tests._canary import on_reference_data

# start year: (this library, Owl), max flat after-tax spend, real dollars.
PINS = {
    1929: (109_130.86, 104_502.0),   # the 1929 crash
    1966: (93_658.45, 93_237.0),     # the classic worst 30-year start
    1973: (90_991.21, 91_845.0),     # the worst start for both tools here
}
GAP_RANGE = (-0.037, 0.063)


def _plan():
    return replace(
        rm.default_scenario(),
        taxable_balance=450_000.0, tax_deferred_balance=1_080_000.0, roth_balance=270_000.0,
        taxable_gains_fraction=0.5,
    )


def _max_flat_spend(plan, returns, context, lo=0.0, hi=400_000.0, tol=10.0) -> float:
    while hi - lo > tol:
        mid = (lo + hi) / 2
        probe = replace(plan, annual_spending=mid)
        alive = simulate_dragged_depletion_only(
            probe, returns, make_strategy(probe), year_context=context
        ) is None
        lo, hi = (mid, hi) if alive else (lo, mid)
    return lo


@pytest.fixture(scope="module")
def ours() -> dict[int, float]:
    plan = _plan()
    assert plan.initial_portfolio == 1_800_000.0
    context = year_tax_context_table(plan)
    years, returns = cohort_return_sequences(plan)
    by_year = dict(zip(years, returns))
    return {y: _max_flat_spend(plan, by_year[y], context) for y in PINS}


@pytest.mark.parametrize("year", sorted(PINS))
def test_owl_cohort_pins(ours, year):
    """This library's max flat spend for the cohort, as notebook 03 computes
    it, against the pin; and the gap to Owl inside the notebook's range."""
    pinned, owl = PINS[year]
    if on_reference_data():
        assert ours[year] == pytest.approx(pinned, abs=20.0)
    else:
        assert ours[year] == pytest.approx(pinned, rel=0.005)
    gap = ours[year] / owl - 1.0
    assert GAP_RANGE[0] <= gap <= GAP_RANGE[1], gap


def test_owl_worst_cohort_is_1973(ours):
    """Both tools name 1973 the worst of the three (and of all 53 cohorts in
    the notebook)."""
    assert min(ours, key=ours.get) == 1973
    assert min(PINS, key=lambda y: PINS[y][1]) == 1973
