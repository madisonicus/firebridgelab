"""``firebridgelab report``: one self-contained HTML file for a plan.

Charts are drawn with matplotlib and inlined as SVG, styles are inline, and
the page loads nothing from the network, so the file opens offline and can be
mailed or committed beside the plan.
"""

from __future__ import annotations

import datetime as _dt
import html
import io

from firebridgelab.cli import outcomes as oc
from firebridgelab.plan import Plan, dump

# Reference categorical palette (light), in fixed order; a sequential blue for fans.
_SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
_FAN = "#2a78d6"
_INK = "#0b0b0b"
_INK2 = "#52514e"
_GRID = "#e4e3df"

LIMITATIONS = [
    "All dollars are real (plan-start purchasing power). Each year the balance grows by "
    "that year's real return net of the fee; spending and income are mid-year flows.",
    "Historical mode replays every start year in the data (US stocks and the 10-year "
    "Treasury from Shiller's monthly file since 1871; other classes from Damodaran, "
    "Fama-French and FRED). Cohorts overlap heavily, so the success share is a count of "
    "histories, not a probability.",
    "Monte Carlo draws independent yearly returns (Normal or Student-t) from each class's "
    "real return and volatility with a fixed seed: no autocorrelation, no valuation or "
    "regime effects, no parameter uncertainty.",
    "Deterministic mode uses one constant return per class: it shows the plan's mechanics, "
    "not its risk.",
    "Federal tax (with a tax-location split) uses 2026 brackets held constant in real "
    "terms, 85% of Social Security taxable, a flat taxable-gains fraction instead of "
    "tracked basis, RMDs and the pre-59½ penalty. No state tax, IRMAA, ACA subsidies, "
    "payroll tax on work income, or tax on taxable-account dividends and interest.",
    "Social Security uses the SSA claim-age factors with no spousal benefit or earnings "
    "test; the survivor keeps the larger benefit. A planning end age is a horizon, not a "
    "death; deaths come only from the survivor case.",
]


def _svg(fig) -> str:
    import matplotlib.pyplot as plt

    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    plt.close(fig)
    text = buf.getvalue()
    text = text[text.index("<svg"):]
    # Drop matplotlib's RDF metadata block: it is not needed to draw the chart.
    start, end = text.find("<metadata>"), text.find("</metadata>")
    if start != -1 and end != -1:
        text = text[:start] + text[end + len("</metadata>"):]
    return text


def _style():
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams.update({
        "svg.fonttype": "none",
        "svg.hashsalt": "firebridgelab",
        "font.family": "sans-serif",
        "font.size": 10,
        "axes.edgecolor": _GRID,
        "axes.labelcolor": _INK2,
        "axes.titlesize": 11,
        "axes.titlecolor": _INK,
        "axes.grid": True,
        "grid.color": _GRID,
        "grid.linewidth": 0.8,
        "xtick.color": _INK2,
        "ytick.color": _INK2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
    })


def _money_axis(ax):
    from matplotlib.ticker import FuncFormatter

    def f(v, _pos):
        if abs(v) >= 1e6:
            return f"${v / 1e6:.1f}M"
        if abs(v) >= 1e3:
            return f"${v / 1e3:.0f}k"
        return f"${v:.0f}"

    ax.yaxis.set_major_formatter(FuncFormatter(f))
    ax.set_xlabel("age (person A)")


def _fan(ax, ages, lo, q1, mid, q3, hi, label):
    ax.fill_between(ages, lo, hi, color=_FAN, alpha=0.15, linewidth=0, label="p10–p90")
    ax.fill_between(ages, q1, q3, color=_FAN, alpha=0.30, linewidth=0, label="p25–p75")
    ax.plot(ages, mid, color=_FAN, linewidth=2, label=label)


def portfolio_chart(det, stochastic) -> str:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(stochastic), figsize=(5.2 * len(stochastic), 3.6),
                             sharey=True, squeeze=False)
    for ax, o in zip(axes[0], stochastic):
        r = o.result
        _fan(ax, r.ages, r.p10, r.p25, r.median, r.p75, r.p90, "median")
        if det is not None:
            ax.plot([p.age for p in det.result.points], [p.end_portfolio for p in det.result.points],
                    color=_SERIES[1], linewidth=2, linestyle="--", label="deterministic")
        ax.set_title(f"Portfolio, {o.title} ({o.paths} paths)")
        _money_axis(ax)
        ax.set_ylim(bottom=0)
    axes[0][0].legend(loc="upper left")
    return _svg(fig)


def spending_chart(plan: Plan, det, stochastic) -> str:
    import matplotlib.pyplot as plt

    s = plan.scenario
    dynamic = any(o.result.spend_median is not None for o in stochastic)
    fig, ax = plt.subplots(figsize=(10.4, 3.4))
    if dynamic:
        o = next(o for o in stochastic if o.result.spend_median is not None)
        r = o.result
        _fan(ax, r.ages, r.spend_p10, r.spend_p25, r.spend_median, r.spend_p75, r.spend_p90,
             f"median, {o.title}")
        ax.axhline(s.spending_floor, color=_SERIES[7], linewidth=1.2, linestyle=":",
                   label="floor (rule part)")
        ax.axhline(s.spending_ceiling, color=_INK2, linewidth=1.2, linestyle=":",
                   label="ceiling (rule part)")
        title = "Spending across paths (the rule adjusts it each year)"
    else:
        ages = list(range(s.start_age, s.end_age + 1))
        ax.plot(ages, [oc.planned_spending(s, a) for a in ages], color=_FAN, linewidth=2,
                label="planned spending (every path, while money lasts)")
        title = "Spending by age"
    if det is not None:
        ax.plot([p.age for p in det.result.points], [p.spending for p in det.result.points],
                color=_SERIES[1], linewidth=1.5, linestyle="--", label="deterministic")
    ax.set_title(title)
    _money_axis(ax)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=8)
    return _svg(fig)


def income_chart(plan: Plan) -> str:
    """Guaranteed income stacked by stream, with planned spending on top.

    Each stream's band is the change in total income when it is added to the
    ones before it, so the stack sums to the household total exactly (the
    survivor's Social Security rule makes per-stream amounts order-dependent).
    """
    import matplotlib.pyplot as plt
    from dataclasses import replace

    from firebridgelab.income.guaranteed import gi_at_age
    from firebridgelab.income.household import required_spending_at_age

    s = plan.scenario
    ages = list(range(s.start_age, s.end_age + 1))
    streams = list(s.income_streams or ())
    fig, ax = plt.subplots(figsize=(10.4, 3.4))
    prev = [0.0] * len(ages)
    bands = []
    for k in range(1, len(streams) + 1):
        partial = replace(s, income_streams=tuple(streams[:k]))
        cur = [gi_at_age(partial, a) for a in ages]
        bands.append([c - p for c, p in zip(cur, prev)])
        prev = cur
    if bands:
        colors = [_SERIES[i % len(_SERIES)] for i in range(len(bands))]
        labels = [st.label or st.id for st in streams]
        ax.stackplot(ages, bands, colors=colors, labels=labels, alpha=0.85,
                     edgecolor="white", linewidth=0.5)
    if s.spending_mode in ("constant", "lifecycle"):
        spend, label = [oc.planned_spending(s, a) for a in ages], "planned spending"
    else:
        # A dynamic rule moves spending path by path; show its starting level.
        spend = [s.annual_spending + required_spending_at_age(s, a) for a in ages]
        label = "starting spending level"
    ax.plot(ages, spend, color=_INK, linewidth=1.5, linestyle="--", label=label)
    ax.set_title("Guaranteed income by stream (the gap above it comes from the portfolio)")
    _money_axis(ax)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=4, fontsize=8)
    return _svg(fig)


def tax_chart(plan: Plan, det) -> str:
    import matplotlib.pyplot as plt

    path = det.tax_path
    fig, ax = plt.subplots(figsize=(10.4, 3.0))
    ax.bar(path.ages, path.tax, color=_FAN, width=0.8, label="federal tax")
    ax.bar(path.ages, path.penalty, color=_SERIES[7], width=0.8, label="of which early-withdrawal penalty")
    ax.set_title("Federal tax by year, deterministic (paid from the portfolio)")
    _money_axis(ax)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2)
    return _svg(fig)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _e(x) -> str:
    return html.escape(str(x))


def _table(headers, rows, cls="") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _pct(v):
    return f"{100 * v:.2f}%"


def _assumptions(plan: Plan) -> str:
    from firebridgelab.engine.allocation import glidepath_boundaries
    from firebridgelab.income.household import household_people

    s = plan.scenario
    parts = []
    people = household_people(s)
    parts.append("<h3>People</h3>" + _table(
        ["id", "name", "age at start", "plans to"],
        [[p.id, p.name, p.start_age, p.end_age] for p in people]))
    rows = [
        ["portfolio", oc.money(s.initial_portfolio)],
        ["annual spending", f"{oc.money(s.annual_spending)} ({s.spending_mode})"],
        ["spending floor / ceiling", f"{oc.money(s.spending_floor)} / {oc.money(s.spending_ceiling)}"],
        ["annual fee", _pct(s.annual_fee)],
        ["assumed inflation", _pct(s.assumed_inflation)],
        ["allocation", s.allocation_mode + (", bucket sourcing" if s.sourcing_mode == "buckets" else "")],
        ["Monte Carlo", f"{plan.trials} trials, seed {plan.seed}, {s.mc_distribution}"],
    ]
    if s.taxable_balance is not None or s.tax_deferred_balance is not None or s.roth_balance is not None:
        rows.append(["tax split", " / ".join(oc.money(v or 0) for v in
                     (s.taxable_balance, s.tax_deferred_balance, s.roth_balance))
                     + " (taxable / tax-deferred / Roth)"])
    if s.survivor_age is not None:
        rows.append(["survivor case", f"person {s.survivor_decedent} dies at {s.survivor_age}; "
                     f"survivor spends {_pct(s.survivor_spending_fraction)}"])
    if plan.stress:
        rows.append(["stress paths", ", ".join(plan.stress)])
    parts.append("<h3>Plan</h3>" + _table(["", ""], rows, "kv"))

    groups = [("allocation", s.allocation)]
    if s.phase_allocations:
        starts = glidepath_boundaries(s)
        groups = [(f"phase from {starts[i] if i < len(starts) else '?'}", ph)
                  for i, ph in enumerate(s.phase_allocations)]
    arows = []
    for name, group in groups:
        for ac in group:
            arows.append([name, ac.label or ac.id, _pct(ac.weight), _pct(ac.real_return),
                          _pct(ac.volatility)])
    parts.append("<h3>Asset classes</h3>" + _table(
        ["", "class", "weight", "real return", "volatility"], arows))
    if s.income_streams:
        parts.append("<h3>Income streams</h3>" + _table(
            ["id", "kind", "owner", "amount", "from", "until", "COLA", "survivor share"],
            [[st.id, st.kind, st.owner, oc.money(st.amount)
              + (" (nominal at start)" if st.nominal_until_start else "")
              + (" at FRA" if st.kind == "social_security" else ""),
              st.start_age, "life" if st.end_age is None else st.end_age - 1,
              st.cola + (f" {_pct(st.cola_rate)}" if st.cola == "fixed" else ""),
              "default" if st.survivor_fraction is None else _pct(st.survivor_fraction)]
             for st in s.income_streams]))
    if s.spending_adjustments:
        parts.append("<h3>Required spending</h3>" + _table(
            ["id", "owner", "amount", "from", "until"],
            [[a.id, a.owner, oc.money(a.amount), a.start_age, a.end_age - 1]
             for a in s.spending_adjustments]))
    if s.cash_events:
        parts.append("<h3>Cash events</h3>" + _table(
            ["id", "owner", "age", "amount", "tax"],
            [[e.id, e.owner, e.age, oc.money(e.amount), e.tax_treatment] for e in s.cash_events]))
    parts.append("<details><summary>The plan file, as run</summary><pre>"
                 + _e(dump(plan)) + "</pre></details>")
    return "\n".join(parts)


_CSS = """
:root { --bg:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --line:#e4e3df; --accent:#2a78d6; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 36px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
h3 { font-size: 15px; margin: 18px 0 6px; }
.sub { color: var(--ink2); margin: 0; }
.notice { border-left: 4px solid #eda100; background: #fff7e6; padding: 10px 14px; margin: 16px 0; }
.chart svg { width: 100%; height: auto; display: block; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-size: 13px; margin: 6px 0; }
th, td { padding: 4px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
table.kv td:last-child { text-align: left; white-space: normal; }
thead th { color: var(--ink2); font-weight: 600; }
pre { background: #f3f2ef; padding: 12px; overflow-x: auto; font-size: 12px; }
li { margin: 4px 0; }
"""


def build_report(plan: Plan, name: str, source: str) -> str:
    _style()
    s = plan.scenario
    outcomes = [oc.run_outcome(plan, m) for m in oc.modes_for(plan, "all")]
    det = next(o for o in outcomes if o.mode == oc.DETERMINISTIC)
    stochastic = [o for o in outcomes if o.mode in (oc.HISTORICAL, oc.MONTE_CARLO)]

    with_tax = det.lifetime_tax is not None
    flags = dict(with_tax=with_tax, with_floor=any(o.ever_at_floor is not None for o in outcomes))
    per = [oc.metric_rows(o, **flags) for o in outcomes]
    head_rows = [[label] + [oc.fmt(p[i][2], kind) for p in per]
                 for i, (_k, label, _v, kind) in enumerate(per[0])]
    headline = _table(["metric"] + [o.title for o in outcomes], head_rows)
    notes = "".join(f"<li>{_e(o.title)}: {_e(o.note)}</li>" for o in outcomes if o.note)

    sections = [
        f"<h1>{_e(name)}</h1>",
        f'<p class="sub">{_e(plan.description)}</p>' if plan.description else "",
        f'<p class="sub">Plan file <code>{_e(source)}</code>; generated '
        f'{_dt.date.today().isoformat()} by firebridgelab.</p>',
        '<div class="notice"><strong>Educational software, not financial advice.</strong> '
        "These are model outputs under the assumptions below, not predictions. Consult a "
        "qualified advisor before acting.</div>",
        "<h2>Headline</h2>",
        "<p>Dollars are real (plan-start purchasing power). Ages are person A's. Success means "
        "the portfolio never ran out; with a dynamic spending rule, “ever forced to spending "
        "floor” is the share of paths that hit the floor at least once.</p>",
        f'<div class="scroll">{headline}</div>',
        f"<ul>{notes}</ul>",
        "<h2>Portfolio</h2>",
        f'<div class="chart">{portfolio_chart(det, stochastic)}</div>',
        "<h2>Spending</h2>",
        f'<div class="chart">{spending_chart(plan, det, stochastic)}</div>',
        "<h2>Guaranteed income</h2>",
        f'<div class="chart">{income_chart(plan)}</div>',
    ]
    if with_tax:
        rows = [["deterministic", oc.money(det.lifetime_tax), oc.fmt(det.effective_tax_rate, "pct")]]
        for o in stochastic:
            rows.append([f"{o.title} (median)", oc.money(o.lifetime_tax),
                         oc.fmt(o.effective_tax_rate, "pct")])
        sections += [
            "<h2>Federal tax</h2>",
            _table(["", "lifetime tax", "effective rate"], rows),
            f'<div class="chart">{tax_chart(plan, det)}</div>',
        ]
    headers, yrows = oc.year_rows(s, det.result, det.tax_path)
    ytext = [[str(c) if isinstance(c, int) else oc.money(c) for c in r] for r in yrows]
    sections += [
        "<h2>Year by year (deterministic)</h2>",
        f'<details><summary>Show the table</summary><div class="scroll">'
        f"{_table([h.replace('_', ' ') for h in headers], ytext)}</div></details>",
        "<h2>Assumptions</h2>",
        _assumptions(plan),
        "<h2>Methodology and limitations</h2>",
        "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in LIMITATIONS) + "</ul>",
        '<div class="notice"><strong>Educational software, not financial advice.</strong> '
        "FIRE Bridge Lab is provided as is, without warranty.</div>",
    ]
    body = "\n".join(x for x in sections if x)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(name)} – FIRE Bridge Lab report</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )
