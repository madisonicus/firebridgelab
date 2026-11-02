"""Headline numbers for a plan, shared by ``run``, ``compare``, ``sweep`` and ``report``.

``run_outcome(plan, mode)`` runs one mode and reduces it to an ``Outcome``:
success and depletion counts, the earliest depletion age, the share of paths
ever forced to the spending floor, spending percentiles at key ages, terminal
percentiles and, with a tax-location split, lifetime federal tax. Ages are
person A's (the engine's age axis); dollars are real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS, Scenario
from firebridgelab.plan import Plan

DETERMINISTIC = "deterministic"
HISTORICAL = "historical"
MONTE_CARLO = "monte_carlo"
MODES = (DETERMINISTIC, HISTORICAL, MONTE_CARLO)
STRESS_PREFIX = "stress:"


def cli_mode(name: str) -> str:
    """``monte-carlo`` (the CLI spelling) -> ``monte_carlo``."""
    return name.replace("-", "_")


@dataclass
class Outcome:
    mode: str
    paths: int
    depleted: int
    earliest_depletion_age: Optional[int]
    ever_at_floor: Optional[float]
    spending: dict[int, tuple[float, float, float]]   # age -> (p10, p50, p90)
    terminal: tuple[float, float, float]                # (p10, p50, p90)
    lifetime_tax: Optional[float] = None
    effective_tax_rate: Optional[float] = None
    note: str = ""
    result: Any = field(default=None, repr=False)
    tax_path: Any = field(default=None, repr=False)

    @property
    def success_rate(self) -> float:
        return (self.paths - self.depleted) / self.paths if self.paths else 0.0

    @property
    def title(self) -> str:
        if self.mode.startswith(STRESS_PREFIX):
            return "stress " + self.mode[len(STRESS_PREFIX):]
        return self.mode.replace("_", " ")

    def to_dict(self) -> dict:
        trip = lambda t: {"p10": t[0], "p50": t[1], "p90": t[2]}  # noqa: E731
        return {
            "mode": self.mode,
            "paths": self.paths,
            "success_rate": self.success_rate,
            "depleted": self.depleted,
            "earliest_depletion_age": self.earliest_depletion_age,
            "ever_at_floor_share": self.ever_at_floor,
            "spending": {str(age): trip(v) for age, v in self.spending.items()},
            "terminal": trip(self.terminal),
            "lifetime_tax": self.lifetime_tax,
            "effective_tax_rate": self.effective_tax_rate,
            "note": self.note,
        }


def key_ages(scenario: Scenario) -> list[int]:
    """Plan start, then every round decade up to the plan's last year."""
    start, end = scenario.start_age, scenario.end_age
    return [start] + [a for a in range(10 * (start // 10 + 1), end + 1, 10)]


def tax_split_active(scenario: Scenario) -> bool:
    from firebridgelab.tax.location import has_tax_split

    return has_tax_split(scenario) and scenario.sourcing_mode != SOURCING_MODE_BUCKETS


def planned_spending(scenario: Scenario, age: int) -> float:
    """Spending at ``age`` for a rule that does not depend on the path
    (constant, lifecycle): the same on every path while money lasts."""
    from firebridgelab.income.household import household_year
    from firebridgelab.spending.lifecycle import spending_multiplier
    from firebridgelab.spending.registry import make_strategy

    hy = household_year(scenario, age)
    spend = make_strategy(scenario).spend_for_year(
        scenario, age, scenario.initial_portfolio, hy.gi - hy.required_spending
    )
    return spend * spending_multiplier(scenario, age) + hy.required_spending


def deterministic_trajectory(scenario: Scenario):
    """``(Trajectory, TaxLocationPath | None)``: the after-tax line when a
    tax split is given, else ``simulate``."""
    from firebridgelab.engine.deterministic import simulate

    if tax_split_active(scenario):
        from firebridgelab.tax.location_path import (
            dragged_deterministic_trajectory,
            simulate_tax_location_deterministic,
        )

        path = simulate_tax_location_deterministic(scenario)
        return dragged_deterministic_trajectory(scenario, path=path), path
    return simulate(scenario), None


def _from_trajectory(mode: str, scenario: Scenario, traj, path=None) -> Outcome:
    by_age = {p.age: p for p in traj.points}
    spending = {}
    for age in key_ages(scenario):
        s = by_age[age].spending
        spending[age] = (s, s, s)
    final = traj.final_portfolio
    out = Outcome(
        mode=mode,
        paths=1,
        depleted=0 if traj.depleted_age is None else 1,
        earliest_depletion_age=traj.depleted_age,
        ever_at_floor=None,
        spending=spending,
        terminal=(final, final, final),
        result=traj,
        tax_path=path,
    )
    if path is not None:
        from firebridgelab.tax.overlay import compute_tax_overlay

        overlay = compute_tax_overlay(scenario, path)
        out.lifetime_tax = overlay.total_tax
        out.effective_tax_rate = overlay.effective_rate
    return out


def _from_aggregate(mode: str, scenario: Scenario, result, paths: int) -> Outcome:
    idx = {age: i for i, age in enumerate(result.ages)}
    spending = {}
    for age in key_ages(scenario):
        if result.spend_median is not None:
            i = idx[age]
            spending[age] = (result.spend_p10[i], result.spend_median[i], result.spend_p90[i])
        else:
            s = planned_spending(scenario, age)
            spending[age] = (s, s, s)
    stats = result.adjustment_stats
    out = Outcome(
        mode=mode,
        paths=paths,
        depleted=result.depletion_count,
        earliest_depletion_age=result.earliest_depletion_age,
        ever_at_floor=None if stats is None else stats.pct_ever_forced_to_floor,
        spending=spending,
        terminal=(result.p10[-1], result.median[-1], result.p90[-1]),
        result=result,
    )
    if result.federal_tax is not None:
        # Every path has the same length, so the median of the paths' averages
        # times the length is the median lifetime total.
        out.lifetime_tax = result.federal_tax.median_avg_annual_tax * len(result.ages)
        out.effective_tax_rate = result.federal_tax.median_effective_rate
    return out


def run_outcome(plan: Plan, mode: str) -> Outcome:
    """Run ``plan`` in one mode: ``deterministic``, ``historical``,
    ``monte_carlo`` or ``stress:<id>``."""
    s = plan.scenario
    if mode == DETERMINISTIC:
        traj, path = deterministic_trajectory(s)
        return _from_trajectory(mode, s, traj, path)
    if mode == HISTORICAL:
        from firebridgelab.engine.historical import simulate_historical

        r = simulate_historical(s)
        out = _from_aggregate(mode, s, r, r.cohort_count)
        out.note = f"{r.cohort_count} cohorts starting {r.start_year} or later"
        return out
    if mode == MONTE_CARLO:
        from firebridgelab.engine.monte_carlo import simulate_monte_carlo

        r = simulate_monte_carlo(s, n_trials=plan.trials, seed=plan.seed)
        out = _from_aggregate(mode, s, r, r.trial_count)
        out.note = f"{r.trial_count} trials, seed {plan.seed}, {s.mc_distribution} returns"
        return out
    if mode.startswith(STRESS_PREFIX):
        from firebridgelab.analysis.stress import simulate_stress

        traj = simulate_stress(s, mode[len(STRESS_PREFIX):])
        return _from_trajectory(mode, s, traj)
    raise ValueError(f"unknown mode {mode!r}")


def modes_for(plan: Plan, selection: str) -> list[str]:
    """The modes a ``--mode`` choice runs; ``all`` adds the plan's stress ids."""
    if selection == "all":
        return list(MODES) + [STRESS_PREFIX + sid for sid in plan.stress]
    if selection == "stress":
        return [STRESS_PREFIX + sid for sid in plan.stress]
    return [cli_mode(selection)]


# ---------------------------------------------------------------------------
# Metrics as rows
# ---------------------------------------------------------------------------

# kind: "int", "pct", "money", "age", "money3" (a p10/p50/p90 triple)


def metric_rows(o: Outcome, *, with_tax: bool, with_floor: bool) -> list[tuple[str, str, Any, str]]:
    rows = [
        ("paths", "paths (cohorts or trials)", o.paths, "int"),
        ("success_rate", "success (never depleted)", o.success_rate, "pct"),
        ("depleted", "depleted paths", o.depleted, "int"),
        ("earliest_depletion_age", "earliest depletion age", o.earliest_depletion_age, "age"),
    ]
    if with_floor:
        rows.append(("ever_at_floor_share", "ever forced to spending floor",
                     o.ever_at_floor, "pct"))
    for age, trip in o.spending.items():
        rows.append((f"spending_at_{age}", f"spending at {age} (p10/p50/p90)", trip, "money3"))
    rows.append(("terminal", "ending portfolio (p10/p50/p90)", o.terminal, "money3"))
    if with_tax:
        rows.append(("lifetime_tax", "lifetime federal tax (median)", o.lifetime_tax, "money"))
        rows.append(("effective_tax_rate", "effective tax rate (median)",
                     o.effective_tax_rate, "pct"))
    return rows


def money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def compact_money(v: float) -> str:
    """$950k, $1.25M: for percentile triples, where full dollars crowd the table."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e6:
        return f"{sign}${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}k"
    return f"{sign}${v:.0f}"


def fmt(value: Any, kind: str) -> str:
    if value is None:
        return "never" if kind == "age" else "n/a"
    if kind == "pct":
        return f"{100 * value:.1f}%"
    if kind == "money":
        return money(value)
    if kind == "money3":
        if value[0] == value[1] == value[2]:
            return money(value[1])
        return " / ".join(compact_money(v) for v in value)
    return str(value)


def raw_delta(a: Any, b: Any, kind: str) -> Optional[float]:
    """``b - a`` as a number (medians for triples), or None when either is missing."""
    if a is None or b is None:
        return None
    if kind == "money3":
        a, b = a[1], b[1]
    return b - a


def fmt_delta(a: Any, b: Any, kind: str) -> str:
    """``b - a`` for the compare table; triples compare their medians."""
    if a is None or b is None:
        return ""
    if kind == "money3":
        a, b, kind = a[1], b[1], "money"
    d = b - a
    if d == 0:
        return "0"
    sign = "+" if d > 0 else "-"
    if kind == "pct":
        return f"{sign}{100 * abs(d):.1f} pt"
    if kind == "money":
        return f"{sign}${abs(d):,.0f}"
    return f"{sign}{abs(d)}"


def flatten(o: Outcome, *, with_tax: bool, with_floor: bool) -> list[tuple[str, Any]]:
    """Scalar (key, value) pairs for CSV: triples become .p10/.p50/.p90."""
    out = []
    for key, _label, value, kind in metric_rows(o, with_tax=with_tax, with_floor=with_floor):
        if kind == "money3":
            for name, v in zip(("p10", "p50", "p90"), value):
                out.append((f"{key}.{name}", v))
        else:
            out.append((key, value))
    return out


def render_table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    """A plain aligned text table (no colour, pipe-friendly)."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def year_rows(scenario: Scenario, traj, tax_path=None) -> tuple[list[str], list[list[Any]]]:
    """The deterministic year-by-year table: headers and raw rows."""
    from firebridgelab.income.household import Household

    hh = Household(scenario)
    couple = hh.couple
    years = [hh.year(p.age) for p in traj.points]
    show_required = any(y.required_spending for y in years)
    show_events = any(y.cash_event for y in years)
    headers = ["age_a"] + (["age_b"] if couple else []) + [
        "start_portfolio", "growth", "guaranteed_income", "spending"]
    if show_required:
        headers.append("required_spending")
    if show_events:
        headers.append("cash_event")
    if tax_path is not None:
        headers.append("federal_tax")
    headers.append("end_portfolio")
    rows = []
    for i, (p, y) in enumerate(zip(traj.points, years)):
        row = [p.age] + ([hh.age_of("b", p.age)] if couple else []) + [
            p.start_portfolio, p.growth, p.guaranteed_income, p.spending]
        if show_required:
            row.append(y.required_spending)
        if show_events:
            row.append(y.cash_event)
        if tax_path is not None:
            row.append(tax_path.tax[i])
        row.append(p.end_portfolio)
        rows.append(row)
    return headers, rows
