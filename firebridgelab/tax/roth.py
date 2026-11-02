"""Roth conversions: the policy's yearly target and the with/without report.

``Scenario.roth_conversion_target`` turns on a conversion policy: in every
plan year inside the window ``[roth_conversion_start_age,
roth_conversion_end_age)`` (person A's ages; the default window is the whole
plan), the fixed point in ``tax.location`` moves money from tax-deferred to
Roth until ordinary TAXABLE income reaches the target. The conversion is
ordinary income that year (IRC 408A(d)(3)(A)(i)), so its tax is part of that
year's bill and is paid from the accounts like any other tax. It is not
subject to the 10% early-withdrawal penalty (IRC 72(t) applies to the
distribution only if it is not rolled into the Roth), though the tax-funding
draws it causes are, when they come from tax-deferred before 59 1/2.

The target is either the top of a named bracket (``"12%"``, ``"22%"``,
``"24%"``, ...; the bracket of the year's filing status) or a dollar amount of
ordinary taxable income. Under the provisional Social Security rule the
conversion also makes more of the benefits taxable, and the fill accounts
for that (``tax.ss_taxation.fill_amount``).

This is distinct from the bracket-fill lever (``bracket_fill_target``), which
moves tax-deferred money to the TAXABLE account at ages 60-74. When both are
on in the same year the conversion fills first and the lever fills what is
left of its own target. Neither may be used to satisfy an RMD: in RMD years
the RMD is taken first and the conversion comes on top.

``compare_roth_conversion`` runs the deterministic after-tax line with and
without the policy and reports lifetime tax and the terminal after-tax value:
taxable + Roth + tax-deferred x (1 - ``roth_heir_tax_rate``). The taxable
account is counted in full because inherited assets get a stepped-up basis
(IRC 1014); a tax-deferred balance is income in respect of a decedent (IRC
691) taxed at the heir's rate, which is an assumption the user sets (default
22%), not a sourced value.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Union

from firebridgelab.tax import core

BRACKET_TARGETS = ("10%", "12%", "22%", "24%", "32%", "35%")


def target_taxable_income(target: Union[str, float], single: bool) -> float:
    """The conversion target in dollars of ordinary taxable income: the top of
    the named bracket for the filing status, or the dollar amount itself."""
    if isinstance(target, str):
        text = target.strip()
        if text not in BRACKET_TARGETS:
            raise ValueError(
                f"roth_conversion_target {target!r} must be one of "
                f"{', '.join(BRACKET_TARGETS)} or a dollar amount"
            )
        rate = float(text[:-1]) / 100.0
        brackets = core._ORDINARY_BRACKETS_SINGLE if single else core._ORDINARY_BRACKETS_MFJ
        for upper, r in brackets:
            if abs(r - rate) < 1e-9:
                return upper
        raise ValueError(f"no {target} bracket")  # pragma: no cover - table invariant
    return float(target)


def conversion_window(scenario) -> tuple[int, int]:
    """``(first, stop)`` A-ages of the conversion window (``stop`` exclusive)."""
    first = scenario.roth_conversion_start_age
    stop = scenario.roth_conversion_end_age
    return (
        scenario.start_age if first is None else first,
        scenario.end_age + 1 if stop is None else stop,
    )


def conversion_target_at_age(scenario, age: int, single: bool) -> Optional[float]:
    """The year's target in dollars of taxable income, or None outside the window."""
    if scenario.roth_conversion_target is None:
        return None
    first, stop = conversion_window(scenario)
    if not first <= age < stop:
        return None
    return target_taxable_income(scenario.roth_conversion_target, single)


@dataclass(frozen=True)
class RothConversionComparison:
    """The deterministic after-tax line with and without the conversion policy."""

    total_converted: float
    lifetime_tax_with: float
    lifetime_tax_without: float
    terminal_after_tax_with: float
    terminal_after_tax_without: float
    heir_tax_rate: float

    @property
    def tax_change(self) -> float:
        return self.lifetime_tax_with - self.lifetime_tax_without

    @property
    def terminal_change(self) -> float:
        return self.terminal_after_tax_with - self.terminal_after_tax_without

    def to_dict(self) -> dict:
        return {
            "total_converted": self.total_converted,
            "lifetime_tax_with": self.lifetime_tax_with,
            "lifetime_tax_without": self.lifetime_tax_without,
            "terminal_after_tax_with": self.terminal_after_tax_with,
            "terminal_after_tax_without": self.terminal_after_tax_without,
            "heir_tax_rate": self.heir_tax_rate,
            "tax_change": self.tax_change,
            "terminal_change": self.terminal_change,
        }


def terminal_after_tax(path, heir_tax_rate: float) -> float:
    """Taxable + Roth + tax-deferred x (1 - heir rate) at the path's last age."""
    if not path.ages:
        return 0.0
    return path.taxable[-1] + path.roth[-1] + path.tax_deferred[-1] * (1.0 - heir_tax_rate)


def compare_roth_conversion(scenario, heir_tax_rate: Optional[float] = None):
    """Run the deterministic after-tax line with the scenario's conversion
    policy and with it removed. Needs a tax-location split and a conversion
    target; returns a ``RothConversionComparison``."""
    from firebridgelab.tax.location import has_tax_split
    from firebridgelab.tax.location_path import simulate_tax_location_deterministic

    if not has_tax_split(scenario):
        raise ValueError("a Roth conversion comparison needs a tax-location split")
    if scenario.roth_conversion_target is None:
        raise ValueError("the scenario has no roth_conversion_target")
    rate = scenario.roth_heir_tax_rate if heir_tax_rate is None else heir_tax_rate
    with_path = simulate_tax_location_deterministic(scenario)
    without = replace(scenario, roth_conversion_target=None)
    without_path = simulate_tax_location_deterministic(without)
    return RothConversionComparison(
        total_converted=sum(with_path.roth_conversion),
        lifetime_tax_with=sum(with_path.tax),
        lifetime_tax_without=sum(without_path.tax),
        terminal_after_tax_with=terminal_after_tax(with_path, rate),
        terminal_after_tax_without=terminal_after_tax(without_path, rate),
        heir_tax_rate=rate,
    )
