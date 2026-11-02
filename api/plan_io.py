"""Plan files in the web UI: convert between plan-file TOML and API bodies.

The web UI and the command line share one file format. ``scenario_to_body``
turns the ``Scenario`` a plan file describes into the JSON body that
``POST /api/trajectory`` accepts (every field explicit, the household spelled
out), so the UI can load it into its form. The reverse direction needs no
code of its own: the API builds a ``Scenario`` from a body exactly as it does
for a projection, and ``firebridgelab.plan.dump`` writes it out.

The routes that use these live in ``api.main``: ``POST /api/plan/import``,
``POST /api/plan/export``, ``GET /api/examples`` and ``GET /api/examples/{name}``.
"""

from __future__ import annotations

from dataclasses import asdict
from importlib import resources
from typing import Any, Optional

from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS
from firebridgelab.income.household import household_people, legacy_income_streams
from firebridgelab.plan import Plan, PlanError, loads

# Scenario fields the API body carries but the web UI has no control for. The
# UI keeps them as an opaque block (``plan_extras``) from import to export, so
# a plan file loaded into the UI and downloaded again keeps them.
PLAN_EXTRA_FIELDS = (
    "slow_go_start_age",
    "no_go_start_age",
    "slow_go_multiplier",
    "no_go_multiplier",
    "spending_real_drift",
    "pension_real_drift",
    "mc_t_df",
)

# The M4 tax options, as API body fields.
TAX_OPTION_FIELDS = (
    "ss_taxation", "payroll_tax", "irmaa", "irmaa_prior_magi",
    "roth_conversion_target", "roth_conversion_start_age", "roth_conversion_end_age",
    "roth_heir_tax_rate", "state", "state_move_age", "state_after_move",
    "state_custom_rate", "state_custom_exempt_ss", "state_custom_exempt_pensions",
    "state_custom_exempt_ira",
)


def _allocation(classes) -> list[dict]:
    return [
        {"id": ac.id, "weight": ac.weight, "real_return": ac.real_return,
         "volatility": ac.volatility}
        for ac in classes
    ]


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def scenario_to_body(s) -> dict[str, Any]:
    """The API body that reproduces scenario ``s``.

    The household is always explicit (``people`` and ``income_streams``), even
    for a scenario on the legacy ``person_*`` fields, so the body never relies
    on the API's defaults for who is in the plan.
    """
    streams = s.income_streams if s.income_streams is not None else legacy_income_streams(s)
    body: dict[str, Any] = {
        "initial_portfolio": s.initial_portfolio,
        "annual_spending": s.annual_spending,
        "spending_mode": s.spending_mode,
        "allocation_mode": s.allocation_mode,
        "sourcing_mode": s.sourcing_mode,
        "allocation": _allocation(s.allocation),
        "correlation": s.correlation,
        "correlations": [list(r) for r in s.correlations] if s.correlations else None,
        "phase_allocations": (
            [_allocation(ph) for ph in s.phase_allocations] if s.phase_allocations else None
        ),
        "taxable_balance": s.taxable_balance,
        "tax_deferred_balance": s.tax_deferred_balance,
        "roth_balance": s.roth_balance,
        "taxable_gains_fraction": s.taxable_gains_fraction,
        "bracket_fill_target": s.bracket_fill_target,
        "guard_band": s.guard_band,
        "guard_adjust": s.guard_adjust,
        "spending_floor": s.spending_floor,
        "spending_ceiling": s.spending_ceiling,
        "amortization_real_return": s.amortization_real_return,
        "annual_fee": s.annual_fee,
        "assumed_inflation": s.assumed_inflation,
        "mc_distribution": s.mc_distribution,
        "guard_clamp_base": s.guard_clamp_base,
        "survivor_age": s.survivor_age,
        "survivor_decedent": s.survivor_decedent,
        "survivor_spending_fraction": s.survivor_spending_fraction,
        "people": [asdict(p) for p in household_people(s)],
        "income_streams": [asdict(st) for st in streams],
        "cash_events": [asdict(ev) for ev in s.cash_events],
        "spending_adjustments": [asdict(adj) for adj in s.spending_adjustments],
    }
    if s.sourcing_mode == SOURCING_MODE_BUCKETS and s.buckets is not None:
        b = s.buckets
        body["buckets"] = {
            "n1": b.n1,
            "n2": b.n2,
            **{
                bucket.id: {"id": bucket.id, "allocation": _allocation(bucket.allocation)}
                for bucket in (b.cash, b.income, b.growth)
            },
        }
    for name in TAX_OPTION_FIELDS + PLAN_EXTRA_FIELDS:
        body[name] = getattr(s, name)
    return _drop_none(body)


def plan_to_payload(plan: Plan, toml_text: Optional[str] = None) -> dict[str, Any]:
    """The JSON the import and example routes return for one plan."""
    out = {
        "name": plan.name,
        "description": plan.description,
        "trials": plan.trials,
        "seed": plan.seed,
        "stress": list(plan.stress),
        "body": scenario_to_body(plan.scenario),
    }
    if toml_text is not None:
        out["toml"] = toml_text
    return out


def parse_plan_text(text: str, source: str = "plan.toml") -> Plan:
    """Parse plan-file text; ``PlanError`` names the key at fault."""
    return loads(text, source)


def _examples_dir():
    return resources.files("firebridgelab") / "examples"


def example_names() -> list[str]:
    return sorted(
        p.name[:-5] for p in _examples_dir().iterdir() if p.name.endswith(".toml")
    )


def example_text(name: str) -> str:
    if name not in example_names():
        raise KeyError(name)
    return (_examples_dir() / f"{name}.toml").read_text(encoding="utf-8")


__all__ = [
    "PLAN_EXTRA_FIELDS",
    "PlanError",
    "example_names",
    "example_text",
    "parse_plan_text",
    "plan_to_payload",
    "scenario_to_body",
]
