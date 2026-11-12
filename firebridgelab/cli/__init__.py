"""The ``firebridgelab`` command: run plan files from the shell.

Subcommands: ``init``, ``run``, ``compare``, ``solve``, ``sweep``, ``report``
and ``ui``. ``firebridgelab <command> --help`` describes each. Results go to
stdout as plain text, JSON or CSV; messages and errors go to stderr. Exit codes:
0 success, 1 the run failed (an engine error, a missing optional dependency,
or an invalid cell in a sweep), 2 bad input (a plan file or argument).
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import sys
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any, Optional, Sequence

from firebridgelab.cli import outcomes as oc
from firebridgelab.plan import Plan, PlanError, apply_override, from_document, parse_value

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

NOTICE = "Educational software, not financial advice (see DISCLAIMER.md). Dollars are real (plan-start); ages are person A's."

_EPILOG = (
    "Plan files are TOML; docs/plan-files.md lists every key. "
    "Exit codes: 0 ok, 1 run failed, 2 bad plan or arguments. " + NOTICE
)


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Examples and the starter plan
# ---------------------------------------------------------------------------


def _examples_dir():
    return resources.files("firebridgelab") / "examples"


def example_names() -> list[str]:
    return sorted(
        p.name[:-5] for p in _examples_dir().iterdir() if p.name.endswith(".toml")
    )


def example_text(name: str) -> str:
    path = _examples_dir() / f"{name}.toml"
    if not path.is_file():
        raise CliError(
            f"no example named {name!r}; examples: {', '.join(example_names())}", EXIT_USAGE
        )
    return path.read_text(encoding="utf-8")


def starter_text() -> str:
    return (resources.files("firebridgelab") / "plan_starter.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Loading plans with --set / --trials / --seed
# ---------------------------------------------------------------------------


def _parse_set(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise CliError(f"--set needs KEY=VALUE (got {item!r})", EXIT_USAGE)
    key, raw = item.split("=", 1)
    key = key.strip()
    if not key:
        raise CliError(f"--set needs KEY=VALUE (got {item!r})", EXIT_USAGE)
    return key, raw


def _read_doc(path: str) -> dict:
    from firebridgelab.plan import read_document

    return read_document(path)


def _build(doc: dict, source: str, overrides: Sequence[tuple[str, Any]], args) -> Plan:
    import copy

    doc = copy.deepcopy(doc)
    for key, value in overrides:
        apply_override(doc, key, value)
    plan = from_document(doc, source)
    if getattr(args, "trials", None) is not None:
        plan = replace(plan, trials=args.trials)
    if getattr(args, "seed", None) is not None:
        plan = replace(plan, seed=args.seed)
    return plan


def _load(path: str, args) -> Plan:
    sets = [(k, parse_value(v)) for k, v in map(_parse_set, getattr(args, "set", None) or [])]
    return _build(_read_doc(path), path, sets, args)


def _plan_name(plan: Plan, path: str) -> str:
    return plan.name or Path(path).stem


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _csv(rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2) + "\n"


def _flags(outcomes: list[oc.Outcome]) -> dict:
    return dict(
        with_tax=any(o.lifetime_tax is not None for o in outcomes),
        with_floor=any(o.ever_at_floor is not None for o in outcomes),
    )


def _headline_table(outcomes: list[oc.Outcome]) -> str:
    flags = _flags(outcomes)
    per = [oc.metric_rows(o, **flags) for o in outcomes]
    rows = []
    for i, (key, label, _v, kind) in enumerate(per[0]):
        rows.append([label] + [oc.fmt(p[i][2], kind) for p in per])
    return oc.render_table(
        ["metric"] + [o.title for o in outcomes], rows, right=set(range(1, len(outcomes) + 1))
    )


def _years_table(plan: Plan, fmt: str) -> str:
    traj, path = oc.deterministic_trajectory(plan.scenario)
    headers, rows = oc.year_rows(plan.scenario, traj, path)
    if fmt == "json":
        return _json([dict(zip(headers, r)) for r in rows])
    if fmt == "csv":
        return _csv([headers] + rows)
    text = [[str(c) if isinstance(c, int) else oc.money(c) for c in r] for r in rows]
    return oc.render_table(headers, text, right=set(range(len(headers)))) + "\n"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    if args.list:
        for name in example_names():
            print(name)
        return EXIT_OK
    text = example_text(args.example) if args.example else starter_text()
    if args.path == "-":
        sys.stdout.write(text)
        return EXIT_OK
    path = Path(args.path)
    if path.exists() and not args.force:
        raise CliError(f"{path} already exists; pass --force to overwrite it", EXIT_USAGE)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}. Edit it, then: firebridgelab run {path}", file=sys.stderr)
    return EXIT_OK


def cmd_run(args) -> int:
    plan = _load(args.plan, args)
    if args.years:
        sys.stdout.write(_years_table(plan, args.format))
        return EXIT_OK
    modes = oc.modes_for(plan, args.mode)
    if not modes:
        raise CliError("--mode stress needs simulation.stress ids in the plan", EXIT_USAGE)
    outcomes = [oc.run_outcome(plan, m) for m in modes]
    name = _plan_name(plan, args.plan)

    if args.format == "json":
        obj = {"plan": name, "source": args.plan, "outcomes": [o.to_dict() for o in outcomes]}
        if oc.DETERMINISTIC in modes:
            traj, path = oc.deterministic_trajectory(plan.scenario)
            headers, rows = oc.year_rows(plan.scenario, traj, path)
            obj["years"] = [dict(zip(headers, r)) for r in rows]
        roth = _roth_comparison(plan)
        if roth is not None:
            obj["roth_conversion"] = roth.to_dict()
        sys.stdout.write(_json(obj))
        return EXIT_OK
    if args.format == "csv":
        flags = _flags(outcomes)
        flat = [oc.flatten(o, **flags) for o in outcomes]
        rows = [["metric"] + [o.mode for o in outcomes]]
        for i, (key, _v) in enumerate(flat[0]):
            rows.append([key] + [f[i][1] for f in flat])
        sys.stdout.write(_csv(rows))
        return EXIT_OK

    print(f"{name}  ({args.plan})")
    print(NOTICE)
    for o in outcomes:
        if o.note:
            print(f"  {o.title}: {o.note}")
    if plan.scenario.allocation_mode == "glidepath":
        from firebridgelab.engine.allocation import glidepath_boundaries

        starts = glidepath_boundaries(plan.scenario)
        print(f"  glidepath phases start at ages {', '.join(map(str, starts))} "
              f"({len(starts)} phases; the plan lists {len(plan.scenario.phase_allocations)})")
    print()
    print(_headline_table(outcomes))
    roth = _roth_comparison(plan)
    if roth is not None:
        print()
        print(_roth_text(roth))
    if oc.DETERMINISTIC in modes:
        print()
        print("Deterministic, year by year:")
        sys.stdout.write(_years_table(plan, "table"))
    return EXIT_OK


def _roth_comparison(plan: Plan):
    """The deterministic with/without Roth conversion comparison, or None."""
    s = plan.scenario
    if s.roth_conversion_target is None or not oc.tax_split_active(s):
        return None
    from firebridgelab.tax.roth import compare_roth_conversion

    return compare_roth_conversion(s)


def _roth_text(c) -> str:
    money = lambda v: f"${v:,.0f}"  # noqa: E731
    return "\n".join([
        "Roth conversions (deterministic):",
        f"  converted in total      {money(c.total_converted)}",
        f"  lifetime tax            {money(c.lifetime_tax_with)} with, "
        f"{money(c.lifetime_tax_without)} without ({money(c.tax_change)})",
        f"  terminal after-tax      {money(c.terminal_after_tax_with)} with, "
        f"{money(c.terminal_after_tax_without)} without ({money(c.terminal_change)}; "
        f"tax-deferred valued at {1 - c.heir_tax_rate:.0%})",
    ])


def cmd_compare(args) -> int:
    if len(args.plans) < 2:
        raise CliError("compare needs at least two plan files", EXIT_USAGE)
    plans = [_load(p, args) for p in args.plans]
    names = [_plan_name(pl, p) for pl, p in zip(plans, args.plans)]
    if len(set(names)) < len(names):
        names = list(args.plans)
    modes = oc.modes_for(plans[0], args.mode) if args.mode != "all" else list(oc.MODES)
    results = {m: [oc.run_outcome(pl, m) for pl in plans] for m in modes}

    if args.format == "json":
        sys.stdout.write(_json({
            "plans": names,
            "sources": list(args.plans),
            "outcomes": {m: [o.to_dict() for o in outs] for m, outs in results.items()},
        }))
        return EXIT_OK

    csv_rows = [["mode", "metric"] + names + [f"diff {n} - {names[0]}" for n in names[1:]]]
    blocks = [NOTICE]
    for m, outs in results.items():
        flags = _flags(outs)
        # Plans can start at different ages, so their spending rows differ:
        # line the rows up by metric key and leave gaps blank.
        per = [{k: (label, v, kind) for k, label, v, kind in oc.metric_rows(o, **flags)}
               for o in outs]
        keys = list(dict.fromkeys(k for p in per for k in p))
        spend = sorted((k for k in keys if k.startswith("spending_at_")),
                       key=lambda k: int(k.rsplit("_", 1)[1]))
        rest = [k for k in keys if not k.startswith("spending_at_")]
        at = rest.index("terminal")
        keys = rest[:at] + spend + rest[at:]
        rows = []
        for key in keys:
            label, _v, kind = next(p[key] for p in per if key in p)
            values = [p[key][1] if key in p else None for p in per]
            deltas = [oc.fmt_delta(values[0], v, kind) for v in values[1:]]
            if args.format == "csv":
                csv_rows.append([m, key] + [
                    "|".join(f"{x:.6g}" for x in v) if kind == "money3" and v else v
                    for v in values
                ] + [oc.raw_delta(values[0], v, kind) for v in values[1:]])
            rows.append([label] + [oc.fmt(v, kind) for v in values] + deltas)
        headers = ["metric"] + names + [f"vs {names[0]}" if len(names) == 2 else f"{n} vs {names[0]}"
                                        for n in names[1:]]
        notes = "".join(f"\n  {n}: {o.note}" for n, o in zip(names, outs) if o.note)
        blocks.append(f"\n{outs[0].title}{notes}\n"
                      + oc.render_table(headers, rows, right=set(range(1, len(headers)))))
    if args.format == "csv":
        sys.stdout.write(_csv(csv_rows))
    else:
        print("\n".join(blocks))
    return EXIT_OK


def cmd_solve(args) -> int:
    from firebridgelab.analysis.solver import solve_max_spend
    from firebridgelab.spending.base import SpendingStrategyNotSolvable

    plan = _load(args.plan, args)
    mode = oc.cli_mode(args.mode)
    if not 0 < args.confidence <= 1:
        raise CliError("--confidence must be between 0 and 1 (e.g. 0.9)", EXIT_USAGE)
    threshold = 1.0 if mode == oc.DETERMINISTIC else args.confidence
    try:
        r = solve_max_spend(plan.scenario, threshold, mode)
    except SpendingStrategyNotSolvable as exc:
        raise CliError(f"{exc}; try --set spending.rule=constant") from None
    obj = {
        "plan": _plan_name(plan, args.plan),
        "mode": mode,
        "confidence_target": r.confidence_target,
        "solved_spend": r.solved_spend,
        "achieved_fraction": r.achieved_fraction,
        "paths": r.cohort_count,
        "hit_ceiling": r.hit_ceiling,
        "current_spend": plan.scenario.annual_spending,
    }
    if args.format == "json":
        sys.stdout.write(_json(obj))
    elif args.format == "csv":
        sys.stdout.write(_csv([list(obj), list(obj.values())]))
    else:
        paths = "" if r.cohort_count is None else f" of {r.cohort_count} paths"
        print(f"{obj['plan']}: max sustainable spending.annual, {mode.replace('_', ' ')}")
        print(f"  {oc.money(r.solved_spend)} a year (rounded down to $1,000)"
              + ("; hit the search ceiling: guaranteed income covers any spend" if r.hit_ceiling else ""))
        print(f"  survives {100 * r.achieved_fraction:.1f}%{paths} "
              f"(target {100 * r.confidence_target:.0f}%)")
        gap = r.solved_spend - plan.scenario.annual_spending
        print(f"  the plan spends {oc.money(plan.scenario.annual_spending)} now "
              f"({oc.money(gap)} headroom)" if gap >= 0 else
              f"  the plan spends {oc.money(plan.scenario.annual_spending)} now "
              f"({oc.money(-gap)} more than the target allows)")
        print(NOTICE)
    return EXIT_OK


def _split_values(raw: str) -> list[str]:
    """Split on commas outside brackets, braces and quotes."""
    out, depth, quote, cur = [], 0, None, ""
    for ch in raw:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return [v.strip() for v in out if v.strip()]


def sweep_values(raw: str) -> list[Any]:
    """``v1,v2,v3`` or a numeric range ``start:stop:step`` (stop included)."""
    parts = raw.split(":")
    if len(parts) == 3:
        nums = [parse_value(p) for p in parts]
        if all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in nums):
            start, stop, step = nums
            if step <= 0 or stop < start:
                raise CliError(f"bad range {raw!r}: need start <= stop and step > 0", EXIT_USAGE)
            count = int(round((stop - start) / step + 1e-9)) + 1
            if count > 1000:
                raise CliError(f"range {raw!r} has {count} values; keep it under 1000", EXIT_USAGE)
            vals = [start + i * step for i in range(count)]
            if all(isinstance(n, int) for n in nums):
                return [int(v) for v in vals]
            return [round(v, 10) for v in vals]
    return [parse_value(v) for v in _split_values(raw)]


def sweep_grid(sets: list[str]) -> tuple[list[str], list[tuple]]:
    keys, axes = [], []
    for item in sets:
        key, raw = _parse_set(item)
        values = sweep_values(raw)
        if not values:
            raise CliError(f"--set {key}= lists no values", EXIT_USAGE)
        keys.append(key)
        axes.append(values)
    return keys, list(itertools.product(*axes))


def cmd_sweep(args) -> int:
    if not args.set:
        raise CliError("sweep needs at least one --set KEY=v1,v2,...", EXIT_USAGE)
    keys, grid = sweep_grid(args.set)
    base = _read_doc(args.plan)
    fixed = [(k, parse_value(v)) for k, v in map(_parse_set, args.fix or [])]
    mode = oc.cli_mode(args.mode)
    cells = []
    failed = 0
    for combo in grid:
        try:
            plan = _build(base, args.plan, fixed + list(zip(keys, combo)), args)
            cells.append((combo, oc.run_outcome(plan, mode), None))
        except (PlanError, ValueError) as exc:
            # A plan error names the file; the row already names the cell.
            msg = str(exc).split(": ", 1)[1] if str(exc).startswith(args.plan) else str(exc)
            cells.append((combo, None, msg))
            failed += 1
    outs = [o for _c, o, _e in cells if o is not None]
    with_floor = any(o.ever_at_floor is not None for o in outs)
    with_tax = any(o.lifetime_tax is not None for o in outs)
    cols = [("success_rate", "success", "pct"), ("depleted", "depleted", "int"),
            ("earliest_depletion_age", "earliest depletion", "age")]
    if with_floor:
        cols.append(("ever_at_floor", "ever at floor", "pct"))
    cols += [("terminal_p10", "ending p10", "money"), ("terminal_p50", "ending p50", "money")]
    if with_tax:
        cols.append(("lifetime_tax", "lifetime tax", "money"))

    def value(o, key):
        if key == "terminal_p10":
            return o.terminal[0]
        if key == "terminal_p50":
            return o.terminal[1]
        return getattr(o, key)

    if args.format == "json":
        sys.stdout.write(_json({
            "plan": args.plan, "mode": mode, "keys": keys,
            "cells": [{"values": dict(zip(keys, c)),
                       "outcome": o.to_dict() if o else None, "error": e}
                      for c, o, e in cells],
        }))
    elif args.format == "csv":
        rows = [keys + [c[0] for c in cols] + ["error"]]
        for combo, o, e in cells:
            rows.append(list(combo) + ([value(o, c[0]) for c in cols] if o else [None] * len(cols))
                        + [e])
        sys.stdout.write(_csv(rows))
    else:
        rows = []
        for combo, o, e in cells:
            vals = [json.dumps(v) if isinstance(v, (str, dict, list)) else str(v) for v in combo]
            if o is None:
                rows.append(vals + [f"invalid: {e}"] + [""] * (len(cols) - 1))
            else:
                rows.append(vals + [oc.fmt(value(o, c[0]), c[2]) for c in cols])
        name = base.get("name") if isinstance(base.get("name"), str) else ""
        print(f"{name or Path(args.plan).stem}: sweep, "
              f"{mode.replace('_', ' ')}, {len(grid)} cells")
        print(NOTICE)
        print()
        print(oc.render_table(keys + [c[1] for c in cols], rows,
                              right=set(range(len(keys), len(keys) + len(cols)))))
    if failed:
        print(f"{failed} of {len(grid)} cells were invalid", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def cmd_report(args) -> int:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        raise CliError(
            "report needs matplotlib: pip install 'firebridgelab[report]'", EXIT_FAILED
        ) from None
    from firebridgelab.cli.report import build_report

    plan = _load(args.plan, args)
    html = build_report(plan, _plan_name(plan, args.plan), args.plan)
    if args.output == "-":
        sys.stdout.write(html)
    else:
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    return EXIT_OK


def _repo_root() -> Optional[Path]:
    import firebridgelab

    root = Path(firebridgelab.__file__).resolve().parent.parent
    return root if (root / "api" / "main.py").is_file() and (root / "web").is_dir() else None


def cmd_ui(args) -> int:
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        raise CliError(
            "the web UI needs FastAPI and uvicorn: pip install 'firebridgelab[web]'", EXIT_FAILED
        ) from None
    root = _repo_root()
    if root is None:
        raise CliError(
            "the web UI (api/ and web/) ships in the source repository, not the package; "
            "clone https://github.com/madisonicus/firebridgelab and run this from the checkout",
            EXIT_FAILED,
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    print(f"FIRE Bridge Lab UI at http://{args.host}:{args.port}/ (Ctrl-C to stop)",
          file=sys.stderr)
    uvicorn.run("api.main:app", host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_plan_opts(p, *, trials=True):
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override a plan key before running, e.g. --set spending.annual=60000 "
                        "or --set income_streams.a_ss.start_age=70 (repeatable)")
    if trials:
        p.add_argument("--trials", type=int, help="Monte Carlo trials (overrides simulation.trials)")
        p.add_argument("--seed", type=int, help="Monte Carlo seed (overrides simulation.seed)")


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="firebridgelab",
        description="Run FIRE retirement plans written as TOML plan files: projections, "
                    "historical backtests, Monte Carlo, max-spend solves, sweeps and reports.",
        epilog=_EPILOG,
        formatter_class=fmt,
    )
    from firebridgelab import __version__

    parser.add_argument("--version", action="version", version=f"firebridgelab {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("init", help="write a commented starter plan (or an example)",
                       description="Write a commented starter plan file, or one of the "
                                   "bundled example plans, for you to edit.",
                       epilog="Examples: firebridgelab init my-plan.toml; "
                              "firebridgelab init --example leanfire-single lean.toml; "
                              "firebridgelab init --list", formatter_class=fmt)
    p.add_argument("path", nargs="?", default="plan.toml",
                   help="where to write it (default plan.toml; '-' for stdout)")
    p.add_argument("--example", metavar="NAME", help="copy a bundled example instead (see --list)")
    p.add_argument("--list", action="store_true", help="list the bundled examples and exit")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="run a plan and print its headline numbers",
                       description="Run a plan in one or more modes and print the headline "
                                   "numbers: success and depletion, the earliest depletion age, "
                                   "how often spending is forced to the floor (dynamic rules), "
                                   "spending percentiles at key ages, ending-portfolio "
                                   "percentiles and lifetime tax (with a tax split). "
                                   "Deterministic runs also print a year-by-year table.",
                       epilog="Examples: firebridgelab run plan.toml; "
                              "firebridgelab run plan.toml --mode historical --format json; "
                              "firebridgelab run plan.toml --years --format csv > years.csv",
                       formatter_class=fmt)
    p.add_argument("plan", help="plan file (TOML)")
    p.add_argument("--mode", default="all",
                   choices=["deterministic", "historical", "monte-carlo", "stress", "all"],
                   help="which engine(s) to run; 'all' (default) runs the three and every "
                        "stress id in simulation.stress")
    p.add_argument("--format", default="table", choices=["table", "json", "csv"])
    p.add_argument("--years", action="store_true",
                   help="print only the deterministic year-by-year table")
    _add_plan_opts(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="headline numbers of several plans side by side",
                       description="Run two or more plans and show their headline numbers side "
                                   "by side, with each plan's difference from the first "
                                   "(percentile triples compare their medians).",
                       epilog="Example: firebridgelab compare plan.toml plan-retire-55.toml "
                              "--mode historical", formatter_class=fmt)
    p.add_argument("plans", nargs="+", help="two or more plan files")
    p.add_argument("--mode", default="all",
                   choices=["deterministic", "historical", "monte-carlo", "all"])
    p.add_argument("--format", default="table", choices=["table", "json", "csv"])
    _add_plan_opts(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("solve", help="find the maximum sustainable annual spending",
                       description="Find the largest spending.annual that survives the "
                                   "confidence target (bisection, rounded down to $1,000). "
                                   "Path-dependent rules (guardrails) and bucket sourcing "
                                   "cannot be solved; Monte Carlo solves use 10,000 trials "
                                   "and are not available with a tax split.",
                       epilog="Example: firebridgelab solve plan.toml --confidence 0.95",
                       formatter_class=fmt)
    p.add_argument("plan")
    p.add_argument("--confidence", type=float, default=0.9,
                   help="share of paths that must never deplete (default 0.9)")
    p.add_argument("--mode", default="historical",
                   choices=["historical", "monte-carlo", "deterministic"])
    p.add_argument("--format", default="table", choices=["table", "json", "csv"])
    _add_plan_opts(p, trials=False)
    p.set_defaults(func=cmd_solve)

    p = sub.add_parser("sweep", help="run a grid over one or two plan keys",
                       description="Run the plan once for every combination of the values "
                                   "given with --set and print one row per combination. Keys "
                                   "are dotted plan keys; list items are named by id. Values "
                                   "are comma-separated TOML values, or start:stop:step "
                                   "(stop included).",
                       epilog="Examples:\n"
                              "  firebridgelab sweep plan.toml --set spending.annual=50000:80000:5000\n"
                              "  firebridgelab sweep plan.toml --set income_streams.a_ss.start_age=62,67,70 \\\n"
                              "      --set people.a.start_age=50,55\n"
                              "  firebridgelab sweep plan.toml --set spending.rule=constant,guardrails",
                       formatter_class=fmt)
    p.add_argument("plan")
    p.add_argument("--set", action="append", metavar="KEY=VALUES", required=True,
                   help="a key and its values, e.g. spending.annual=50000,60000,70000 (repeat "
                        "for a second axis)")
    p.add_argument("--fix", action="append", metavar="KEY=VALUE",
                   help="override a key in every cell (like run --set)")
    p.add_argument("--mode", default="historical",
                   choices=["deterministic", "historical", "monte-carlo"])
    p.add_argument("--format", default="table", choices=["table", "json", "csv"])
    p.add_argument("--trials", type=int, help="Monte Carlo trials (overrides simulation.trials)")
    p.add_argument("--seed", type=int, help="Monte Carlo seed (overrides simulation.seed)")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("report", help="write a self-contained HTML report",
                       description="Write one HTML file with charts (portfolio and spending "
                                   "fans, guaranteed income, tax), depletion and floor stats, "
                                   "the plan's assumptions and the model's limitations. It "
                                   "makes no network requests. Needs the 'report' extra "
                                   "(matplotlib).",
                       epilog="Example: firebridgelab report plan.toml -o report.html",
                       formatter_class=fmt)
    p.add_argument("plan")
    p.add_argument("-o", "--output", default="report.html",
                   help="output file (default report.html; '-' for stdout)")
    _add_plan_opts(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ui", help="launch the web UI and API locally",
                       description="Serve the browser UI and JSON API on this machine. Needs "
                                   "the 'web' extra and a source checkout (api/ and web/).",
                       formatter_class=fmt)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"firebridgelab: error: {exc}", file=sys.stderr)
        return exc.code
    except PlanError as exc:
        print(f"firebridgelab: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"firebridgelab: error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except BrokenPipeError:
        # `firebridgelab run ... | head`: stop quietly (and keep Python from
        # complaining when it flushes stdout at exit).
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
