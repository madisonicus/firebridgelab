"""FastAPI app for FIRE Bridge Lab.

Thin layer: serves the static web bundle and exposes one endpoint that
runs the engine. The /api/trajectory endpoint accepts optional query
parameters mirroring the Scenario fields; any missing param falls back
to the slice-1 default. No business logic here — just parse, validate,
construct a Scenario, hand to simulate(), serialize the result.
"""

import functools
import logging
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from firebridgelab import (
    AssetClass,
    ScenarioValidationError,
    blended_real_return,
    default_scenario,
    effective_equity_glidepath,
    effective_spending_rails,
    simulate,
    simulate_historical,
    simulate_monte_carlo,
    simulate_stress,
    solve_max_spend,
    validate_scenario,
)
from firebridgelab.returns.sampling import CORRELATION_LIMIT
from firebridgelab.engine.allocation import (
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    CLASS_MENU,
    DEFAULT_CORRELATION,
    _DEFAULT_EQUITY_VOLATILITY,
    _legacy_default_volatility,
)
from firebridgelab.engine.scenario import (
    SOURCING_MODE_BUCKETS,
    SOURCING_MODE_SINGLE,
    Bucket,
    BucketConfig,
    _GUARDRAIL_CEILING,
    _GUARDRAIL_FLOOR,
    default_bucket_config,
)
from firebridgelab.spending.modes import SPENDING_MODE_CONSTANT
from firebridgelab.income.household import (
    CashEvent,
    IncomeStream,
    Person,
    SpendingAdjustment,
    household,
    plan_end_age,
    with_household,
)
from firebridgelab.income.social_security import effective_ss_amount, ss_factor
import firebridgelab
from api import plan_io, scenario_store
from api.plan_io import PLAN_EXTRA_FIELDS, TAX_OPTION_FIELDS
from api.error_translation import engine_error_to_detail
from api.spending_modes import (
    SAVE_VIEW_SPENDING_MODE_IDS,
    SPENDING_MODE_IDS,
    _SPENDING_MODE_BY_ID,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="FIRE Bridge Lab", version=firebridgelab.__version__)


# Validation bounds. These are sanity bounds (catch typos / clearly broken
# inputs), not editorial limits. The form's HTML5 min/max should match.
_AGE_MIN = 18
_AGE_MAX = 110
# Per-class real-return sanity bounds (catch typos, not editorial limits). The
# floor is slightly NEGATIVE: some canonical classes have a small-negative or
# near-zero geometric-mean real return over their full history (broad commodities
# ≈ -0.2%/yr, short Treasuries ≈ +0.3%/yr), so a 0.0 floor would 422 an honest
# data-derived menu default. -0.05 still catches a fat-finger while admitting the
# real near-zero classes. The engine itself is sign-agnostic about returns.
_RETURN_MIN = -0.05
_RETURN_MAX = 0.15


# Valid values for the `mode` param. Deterministic is the default and reproduces
# today's behavior byte-for-byte; historical runs the all-cohorts engine;
# monte_carlo samples correlated per-class Normal returns and returns the SAME
# fan payload shape as historical (so the front-end fan renderer is reused).
_MODE_DETERMINISTIC = "deterministic"
_MODE_HISTORICAL = "historical"
_MODE_MONTE_CARLO = "monte_carlo"
# `stress` (stress-test design slice 1) runs a Family-A synthetic-cohort stress as
# an OVERLAY on the current plan — the authored adverse return path through the
# existing per-cohort loop — and returns the SAME single-trajectory payload as
# Deterministic (so the front-end single-line renderer is reused). It is NOT a
# fourth return mode in the engine sense (Family A rides the Historical cohort
# loop); at the API it's a request flavor carrying a `stress_id`.
_MODE_STRESS = "stress"
_MODES = (_MODE_DETERMINISTIC, _MODE_HISTORICAL, _MODE_MONTE_CARLO, _MODE_STRESS)

# Valid values for the `spending_mode` param. "constant" (default) keeps today's
# flat-real spending byte-for-byte; "lifecycle" activates the age-anchored go-go
# / slow-go / no-go phase curve; "guardrails" activates the two-sided Guyton-
# Klinger dynamic-spending strategy (variable-spending design). Orthogonal to
# `mode` — it flows into the Scenario and runs in all three engines; the spending
# fan it produces is only rendered in Historical mode in slice 1 (MC fan is slice 3).
# Valid mode IDs / save-view eligibility / bucket composability live in the
# spending-mode registry (api/spending_modes.py); see ADR 0007.

# Valid values for the `allocation_mode` param (glidepath design). "constant"
# (default) holds ONE allocation across the whole horizon — byte-for-byte today's
# behavior; "glidepath" carries one allocation per derived phase (a bond tent).
# Orthogonal to `mode` and `spending_mode`. The per-phase allocations ride in the
# POST body's `phase_allocations` (a list of allocation-lists); glidepath is a
# POST-only feature (it needs N allocation matrices), so the GET path validates
# the param but only the constant value is meaningful there.
_ALLOCATION_MODES = (ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH)

# Valid values for the `sourcing_mode` param (bucket-strategy design). "single"
# (default) keeps today's single-blended-balance path byte-for-byte; "buckets"
# routes to the 3-bucket cash/income/growth time-segmentation sourcing. Orthogonal
# to `mode`, `spending_mode`, `allocation_mode`. Guardrails spending + buckets is
# FENCED (422 — composing them is slice 2). Glidepath + buckets is out of scope:
# in buckets mode the bucket allocations supersede the single/glidepath allocation
# (allocation_mode is ignored, not an error).
_SOURCING_MODES = (SOURCING_MODE_SINGLE, SOURCING_MODE_BUCKETS)


# Legacy GET name → class-id alias (asset-class design, round-2 fix B4/D contract).
# GET has no `id` slot, so it is RESTRICTED to the two legacy classes, bound by a
# hard-coded name→id alias (case-insensitive on the name). This quarantines the
# name-as-key hazard to exactly two aliases; any other name on GET is a 422.
# Multi-class / preset requests MUST use POST (which carries explicit ids). The
# canonical display label for each legacy id rides along so the GET echo is the
# same {id, label, ...} shape POST returns.
_LEGACY_NAME_TO_ID = {
    "equity": "us_total_market",
    "bonds": "us_agg_bonds",
}
_CLASS_LABELS = {
    "us_total_market": "US Total Stock Market",
    "us_agg_bonds": "US Bonds (10-yr Treasury proxy)",
}

# Canonical 12-class menu ORDER + the two legacy classes' labels (asset-class
# design "The 12 classes" table). The 10 NEW classes live in firebridgelab.engine.allocation.CLASS_MENU
# (keyed by class id, carrying their geometric-mean real_return + volatility from
# the staged series); the two LEGACY classes (us_total_market / us_agg_bonds) are
# NOT in CLASS_MENU (their default allocation stays length-2 in the engine), so
# their menu metadata is assembled here from the same loaded real-return series
# their Historical/MC path binds to (us_equity / us_bonds). This single ordered
# tuple is the canonical menu order the /api/classes response (and the matrix UI)
# uses; it matches the design's table order (core growth → factor → intl → EM →
# REIT → core bonds → the Treasury/cash ladder → TIPS → gold → commodities).
_CLASS_MENU_ORDER = (
    "us_total_market",
    "us_small_value",
    "intl_developed",
    "emerging_markets",
    "us_reit",
    "us_agg_bonds",
    "lt_treasury",
    "it_treasury",
    "st_treasury",
    "tips",
    "gold",
    "commodities",
)

def _available_class_ids() -> tuple[str, ...]:
    """``_CLASS_MENU_ORDER`` minus optional classes whose data isn't installed
    (the two legacy classes are always present; see firebridgelab.returns.class_series)."""
    return tuple(c for c in _CLASS_MENU_ORDER if c in _CLASS_LABELS or c in CLASS_MENU)


# Named portfolio presets (asset-class design D7). A preset is stored as
# {class_id: integer-percent}, weights summing to 100 over the stable class ids.
# `faithful` flags whether the preset reconstructs its source exactly with the
# 12-class menu (True) or collapses a factor / EM split the menu doesn't carry
# into the mainstream + small-value scope (False — the agreed approximation).
# Selecting a preset populates the editable allocation matrix; the user can then
# edit + run. Order is presentation order in the picker.
_PRESETS = (
    {
        "name": "Classic 60/40",
        "faithful": True,
        "weights": {"us_total_market": 60, "us_agg_bonds": 40},
    },
    {
        "name": "Bogleheads 3-Fund",
        "faithful": False,  # collapses the international developed/EM split
        "weights": {"us_total_market": 34, "intl_developed": 33, "us_agg_bonds": 33},
    },
    {
        "name": "Core Four",
        "faithful": True,
        "weights": {
            "us_total_market": 48,
            "intl_developed": 24,
            "us_reit": 8,
            "us_agg_bonds": 20,
        },
    },
    {
        "name": "Golden Butterfly",
        "faithful": True,
        "weights": {
            "us_total_market": 20,
            "us_small_value": 20,
            "lt_treasury": 20,
            "st_treasury": 20,
            "gold": 20,
        },
    },
    {
        "name": "Permanent Portfolio",
        "faithful": True,
        "weights": {
            "us_total_market": 25,
            "lt_treasury": 25,
            "gold": 25,
            "st_treasury": 25,
        },
    },
    {
        "name": "All-Weather",
        "faithful": True,
        "weights": {
            "us_total_market": 30,
            "lt_treasury": 40,
            "it_treasury": 15,
            "gold": 8,
            "commodities": 7,
        },
    },
    {
        "name": "Coffeehouse",
        "faithful": False,  # collapses the factor/value sub-splits
        "weights": {
            "us_total_market": 30,
            "us_small_value": 10,
            "us_reit": 10,
            "intl_developed": 10,
            "us_agg_bonds": 40,
        },
    },
)


class AllocationEntry(BaseModel):
    """One asset class in a POST allocation body.

    Carries the stable class ``id`` (the allocation/menu/data-binding key),
    ``weight`` (fraction 0..1), ``real_return`` (annual real return), and an
    optional ``volatility`` (MC only). An optional ``label`` (display) may ride
    along; when omitted the canonical label for the id is filled (or the id
    itself for an unknown class). The id — never a display string — is what binds
    to a series in the engine.
    """

    id: str
    weight: float
    real_return: float
    volatility: Optional[float] = None
    label: Optional[str] = None


class BucketBody(BaseModel):
    """One bucket in a POST buckets config (bucket-strategy design).

    Carries the stable bucket ``id`` (``cash`` / ``income`` / ``growth``), an
    optional display ``name``, and the bucket's own ``allocation`` as the SAME
    ``[{id, weight, real_return, volatility}]`` list a single matrix uses. Parsed
    + validated (100%-sum, per-class bounds) exactly like the single allocation.
    """

    id: str
    name: Optional[str] = None
    allocation: List[AllocationEntry]


class BucketsBody(BaseModel):
    """POST buckets configuration (bucket-strategy design "Locked design").

    The three buckets + the forward-gap sizing knobs ``n1`` (cash years) / ``n2``
    (income years). Omitted bucket allocations fall back to ``default_bucket_config``
    on a per-field basis; ``n1`` / ``n2`` default to the research defaults (2 / 7).
    """

    cash: Optional[BucketBody] = None
    income: Optional[BucketBody] = None
    growth: Optional[BucketBody] = None
    n1: Optional[int] = None
    n2: Optional[int] = None


class PersonBody(BaseModel):
    """One household member (``income.household.Person``)."""

    id: str
    start_age: int
    end_age: int
    name: str = ""


class IncomeStreamBody(BaseModel):
    """One income stream (``income.household.IncomeStream``). Ages are in the
    owner's age; Social Security takes the FRA amount and the claim age."""

    id: str
    kind: str
    amount: float
    start_age: int
    owner: str = "a"
    end_age: Optional[int] = None
    cola: str = "full"
    cola_rate: float = 0.0
    nominal_until_start: bool = False
    survivor_fraction: Optional[float] = None
    tax_treatment: Optional[str] = None
    label: str = ""
    self_employed: bool = False


class CashEventBody(BaseModel):
    """A one-off cash flow (``income.household.CashEvent``)."""

    age: int
    amount: float
    tax_treatment: str = "tax_free"
    basis_fraction: float = 0.0
    owner: str = "a"
    id: str = ""
    label: str = ""


class SpendingAdjustmentBody(BaseModel):
    """Required extra spending over an age range
    (``income.household.SpendingAdjustment``)."""

    amount: float
    start_age: int
    end_age: int
    owner: str = "a"
    survivor_fraction: float = 1.0
    id: str = ""
    label: str = ""


class ScenarioBody(BaseModel):
    """POST JSON body for /api/trajectory and /api/solve (the multi-class path).

    Mirrors the GET query params but carries ``allocation`` as a list of
    ``{id, weight, real_return, volatility}`` entries and an optional ordered
    ``correlations`` N x N matrix (aligned to allocation order). The scalar
    scenario fields are the same optional overrides the GET path accepts; omitted
    fields fall back to ``default_scenario()``. ``mode`` / ``spending_mode`` /
    ``confidence`` carry their endpoint defaults.
    """

    mode: str = _MODE_DETERMINISTIC
    # Stress selector (stress-test design slice 1). Read ONLY when mode=stress; the
    # Family-A synthetic-cohort stress to overlay (e.g. "stagflation_1970s").
    # Omitted (None) on every non-stress request — additive, so existing bodies are
    # unchanged.
    stress_id: Optional[str] = None
    spending_mode: str = SPENDING_MODE_CONSTANT
    # Allocation shape (glidepath design). "constant" (default) holds the single
    # `allocation` across all ages; "glidepath" carries one allocation per derived
    # phase in `phase_allocations`. When glidepath is on, `phase_allocations` is a
    # list of allocation-lists (one `[{id,weight,real_return,volatility}]` per
    # phase, in phase order). Omitted → constant; the single `allocation` carries.
    allocation_mode: str = ALLOCATION_MODE_CONSTANT
    phase_allocations: Optional[List[List[AllocationEntry]]] = None
    # Withdrawal sourcing shape (bucket-strategy design). "single" (default) keeps
    # the single-balance path; "buckets" routes to the 3-bucket sourcing. The
    # bucket config (3 per-bucket allocations + n1/n2) rides in `buckets`. Omitted
    # → single; today's behavior untouched.
    sourcing_mode: str = SOURCING_MODE_SINGLE
    buckets: Optional[BucketsBody] = None
    # Tax-location split (tax-buckets design tb1). Three optional account balances
    # partitioning `initial_portfolio` by TAX LOCATION (distinct from the
    # cash/income/growth `buckets` axis). Omitted (all None) → feature OFF (single
    # undifferentiated pool, canary preserved). If ANY is supplied, all three are
    # treated as supplied (missing → 0) and they must sum to `initial_portfolio`
    # within $1 (validated by `_validate_tax_location`). Threaded onto the Scenario
    # as `taxable_balance` / `tax_deferred_balance` / `roth_balance`.
    taxable_balance: Optional[float] = None
    tax_deferred_balance: Optional[float] = None
    roth_balance: Optional[float] = None
    # Taxable-gains fraction knob (federal-tax overlay design ft1, DECISION 4).
    # The flat LTCG-taxable share of each taxable-account withdrawal, editable at
    # run time. Omitted -> the scenario default (0.5). Validated 0..1 the same
    # way as the tax-location balances (``_validate_taxable_gains_fraction``).
    taxable_gains_fraction: Optional[float] = None
    # Voluntary post-59½ bracket-fill drawdown lever (tax-drag design td1,
    # DECISION 5, td-c). POST-body-only — same convention as the tax-location
    # split balances above (the lever needs those accounts to move money
    # between, so it rides the same POST-only transport). Omitted (None, the
    # default) -> the lever is off (``Scenario.bracket_fill_target`` default).
    # Bound-validated 0..5,000,000 by the engine's ``validate_scenario`` (the
    # SAME check the Scenario field's own docstring describes) via
    # ``_validate_scenario_invariants`` — no separate API-side bound check, so
    # the 422 detail text can never drift from the engine's. In buckets
    # sourcing mode the lever is a logged no-op (``_build_scenario_from_body``)
    # — tax drag is single-mode only, same as the split balances.
    bracket_fill_target: Optional[float] = None
    # Comparison-window alignment (option 3). An optional Historical cohort
    # START-YEAR FLOOR: when set, the Historical run scores only cohorts starting
    # at or after this year, so an A/B comparison can measure both scenarios on the
    # SAME (common, latest) testable window. Historical-only (ignored in
    # Deterministic / Monte Carlo); omitted (None) → each scenario's native window.
    window_start_year: Optional[int] = None
    confidence: float = 0.80
    # Guyton-Klinger guardrail knobs (variable-spending design slice 3). Guardrails-
    # only, optional; omitted → the research defaults on default_scenario(). Same
    # validation as the GET path (_validate_guardrails): 0<=guard_band<1,
    # 0<=guard_adjust<=1, spending_floor>=0, spending_ceiling>spending_floor.
    guard_band: Optional[float] = None
    guard_adjust: Optional[float] = None
    spending_floor: Optional[float] = None
    spending_ceiling: Optional[float] = None
    # Amortization-frontier planning real return (amortization-frontier design
    # decision 4). Read ONLY by the `amortization` spending strategy; omitted → the
    # scenario default (0.01 = 1% real). Additive, so non-amortization bodies are
    # unchanged. The UI exposes it as an adjustable slider so the prototype can
    # settle the fixed-vs-tracking return question empirically.
    amortization_real_return: Optional[float] = None
    initial_portfolio: Optional[float] = None
    annual_spending: Optional[float] = None
    allocation: Optional[List[AllocationEntry]] = None
    correlation: Optional[float] = None
    correlations: Optional[List[List[float]]] = None
    start_age: Optional[int] = None
    end_age: Optional[int] = None
    person_a_pension_amount: Optional[float] = None
    person_a_pension_start_age: Optional[int] = None
    person_b_pension_amount: Optional[float] = None
    person_b_pension_start_age: Optional[int] = None
    # Social Security per person as FRA amount + claim age (NOT effective amount
    # + start age). The API derives the engine's effective ``*_ss_amount`` =
    # ``effective_ss_amount(fra_amount, claim_age)`` and sets ``*_ss_start_age`` =
    # ``claim_age`` before building the Scenario, so the engine stays amount-driven.
    # FRA is the internal DEFAULT_FRA_AGE (67), never a request field. An
    # out-of-range claim age (62..70) is a 422 via the ss_factor ValueError.
    person_a_ss_fra_amount: Optional[float] = None
    person_a_ss_claim_age: Optional[int] = None
    person_b_ss_fra_amount: Optional[float] = None
    person_b_ss_claim_age: Optional[int] = None
    # Review 2026-10-11 knobs (firebridgelab.scenario documents each; None = default).
    annual_fee: Optional[float] = None
    mc_distribution: Optional[str] = None
    mc_t_df: Optional[float] = None
    pension_nominal_until_start: Optional[bool] = None
    pension_cola: Optional[str] = None
    assumed_inflation: Optional[float] = None
    survivor_age: Optional[int] = None
    survivor_decedent: Optional[str] = None
    survivor_pension_fraction: Optional[float] = None
    survivor_spending_fraction: Optional[float] = None
    guard_clamp_base: Optional[bool] = None
    # Household (income.household). All optional; omitted → the legacy
    # person_* fields above describe a same-age couple. ``people`` (one or two)
    # sets start_age / end_age from the people's ages; ``income_streams``
    # (when given, even empty) replaces the four person_* streams.
    people: Optional[List[PersonBody]] = None
    income_streams: Optional[List[IncomeStreamBody]] = None
    cash_events: Optional[List[CashEventBody]] = None
    spending_adjustments: Optional[List[SpendingAdjustmentBody]] = None
    # Tax accuracy (milestone M4; the ``Scenario`` fields of the same names,
    # docs/methodology.md). All optional; omitted -> the scenario defaults
    # (provisional Social Security taxation, every other option off). The
    # opt-ins need a tax-location split; without one validate_scenario 422s.
    ss_taxation: Optional[str] = None
    payroll_tax: Optional[bool] = None
    irmaa: Optional[bool] = None
    irmaa_prior_magi: Optional[float] = None
    roth_conversion_target: Optional[Union[str, float]] = None
    roth_conversion_start_age: Optional[int] = None
    roth_conversion_end_age: Optional[int] = None
    roth_heir_tax_rate: Optional[float] = None
    state: Optional[str] = None
    state_move_age: Optional[int] = None
    state_after_move: Optional[str] = None
    state_custom_rate: Optional[float] = None
    state_custom_exempt_ss: Optional[bool] = None
    state_custom_exempt_pensions: Optional[bool] = None
    state_custom_exempt_ira: Optional[bool] = None
    # Plan-file fields the web UI has no control for (``api.plan_io``). The UI
    # carries them from an imported plan to an exported one; omitted -> the
    # scenario defaults.
    slow_go_start_age: Optional[int] = None
    no_go_start_age: Optional[int] = None
    slow_go_multiplier: Optional[float] = None
    no_go_multiplier: Optional[float] = None
    spending_real_drift: Optional[float] = None
    pension_real_drift: Optional[float] = None


_TAX_OPTION_FIELDS = TAX_OPTION_FIELDS


# ---------- Saved Scenarios (sv1) ----------
#
# Additive feature: persist a named, reloadable scenario as one JSON blob per
# save. The store mechanics (slugify, path confinement, atomic write, caps,
# conflict-file filter, provenance stamp) live in scenario_store; these models
# validate the blob SHAPE before it's persisted, and the four routes below
# translate store exceptions to HTTP codes. The math engine is untouched.

_SAVE_VIEW_SIM_MODES = {_MODE_DETERMINISTIC, _MODE_HISTORICAL, _MODE_MONTE_CARLO}
_SAVE_VIEW_ALLOCATION_MODES = {ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH}
_SAVE_VIEW_SOURCING_MODES = {SOURCING_MODE_SINGLE, SOURCING_MODE_BUCKETS}


class ScenarioView(BaseModel):
    """How to reproduce the chart: the four global modes. Validated against the
    same vocabularies the firebridgelab/UI use so a malformed save 422s."""

    sim_mode: str
    spending_mode: str
    allocation_mode: str
    sourcing_mode: str


class SavedScenarioBody(BaseModel):
    """The PUT body for a saved scenario (server-stamped fields excluded).

    ``inputs`` is intentionally permissive (``dict``) — it carries the same
    flat input-set ``readScenarios()`` produces plus the conditional
    glidepath/buckets/guardrails/MC blocks; the engine-facing validation already
    happens on the trajectory/solve path when the loaded scenario is recomputed.
    Here we pin the OUTER shape: schema_version, name, and the four view modes.
    """

    schema_version: int
    name: str
    view: ScenarioView
    inputs: dict
    # Tolerate (and ignore) server-stamped fields if a client echoes them back;
    # write_scenario re-stamps id/created_at/updated_at/app_revision regardless.
    id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    app_revision: Optional[str] = None


def _validate_saved_scenario(body: "SavedScenarioBody") -> None:
    """Validate the parts Pydantic's type system can't: schema_version == 1, a
    non-empty name, and the four view modes against their vocabularies. Raises
    HTTPException(422) with a friendly detail on failure."""
    if body.schema_version != 1:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported schema_version {body.schema_version!r} (expected 1)",
        )
    if not isinstance(body.name, str) or not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    v = body.view
    for field, value, allowed in (
        ("sim_mode", v.sim_mode, _SAVE_VIEW_SIM_MODES),
        ("spending_mode", v.spending_mode, SAVE_VIEW_SPENDING_MODE_IDS),
        ("allocation_mode", v.allocation_mode, _SAVE_VIEW_ALLOCATION_MODES),
        ("sourcing_mode", v.sourcing_mode, _SAVE_VIEW_SOURCING_MODES),
    ):
        if value not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"view.{field} {value!r} not one of {sorted(allowed)}",
            )


@app.get("/api/scenarios")
def list_scenarios_route() -> list[dict]:
    """List saved scenarios as metadata ``[{id, name, created_at, updated_at}]``,
    sorted updated-desc. Cheap (no full blobs). Conflict copies are filtered."""
    return scenario_store.list_scenarios()


@app.get("/api/scenarios/{scenario_id}")
def get_scenario_route(scenario_id: str) -> dict:
    """Return the full blob for one save. 400 on a malformed/escaping id, 404 if
    absent."""
    try:
        return scenario_store.read_scenario(scenario_id)
    except scenario_store.InvalidScenarioId as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except scenario_store.ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"no scenario {scenario_id!r}")


@app.put("/api/scenarios/{scenario_id}")
def put_scenario_route(scenario_id: str, body: SavedScenarioBody) -> dict:
    """Upsert a scenario by slug derived from ``body.name``. The ``{id}`` path
    param is informational (the slug is authoritative); both are validated.

    422 on a malformed blob / schema mismatch, 400 on a bad slug/confinement,
    413 on an oversize blob, 400 when the store is at its count cap.
    """
    # The path id, when present, must itself be a valid slug shape (defense in
    # depth — a traversal id is rejected before any store work).
    try:
        scenario_store._validate_id(scenario_id)
    except scenario_store.InvalidScenarioId as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _validate_saved_scenario(body)
    blob = body.model_dump(exclude_none=True)
    # Server owns these — drop any client-echoed values so write_scenario stamps
    # them fresh.
    for k in ("id", "created_at", "updated_at", "app_revision"):
        blob.pop(k, None)
    try:
        return scenario_store.write_scenario(blob)
    except scenario_store.ScenarioTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except scenario_store.ScenarioStoreFull as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except scenario_store.InvalidScenarioId as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario_route(scenario_id: str) -> dict:
    """Delete one save. 400 on a malformed/escaping id, 404 if absent."""
    try:
        scenario_store.delete_scenario(scenario_id)
    except scenario_store.InvalidScenarioId as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except scenario_store.ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"no scenario {scenario_id!r}")
    return {"deleted": scenario_id}


@app.get("/api/trajectory")
def get_trajectory(
    mode: str = Query(default=_MODE_DETERMINISTIC),
    stress_id: Optional[str] = Query(default=None),
    spending_mode: str = Query(default=SPENDING_MODE_CONSTANT),
    allocation_mode: str = Query(default=ALLOCATION_MODE_CONSTANT),
    sourcing_mode: str = Query(default=SOURCING_MODE_SINGLE),
    bucket_n1: Optional[int] = Query(default=None),
    bucket_n2: Optional[int] = Query(default=None),
    guard_band: Optional[float] = Query(default=None),
    guard_adjust: Optional[float] = Query(default=None),
    spending_floor: Optional[float] = Query(default=None),
    spending_ceiling: Optional[float] = Query(default=None),
    amortization_real_return: Optional[float] = Query(default=None),
    taxable_gains_fraction: Optional[float] = Query(default=None),
    initial_portfolio: Optional[float] = Query(default=None),
    annual_spending: Optional[float] = Query(default=None),
    allocation: Optional[str] = Query(default=None),
    correlation: Optional[float] = Query(default=None),
    start_age: Optional[int] = Query(default=None),
    end_age: Optional[int] = Query(default=None),
    person_a_pension_amount: Optional[float] = Query(default=None),
    person_a_pension_start_age: Optional[int] = Query(default=None),
    person_b_pension_amount: Optional[float] = Query(default=None),
    person_b_pension_start_age: Optional[int] = Query(default=None),
    person_a_ss_fra_amount: Optional[float] = Query(default=None),
    person_a_ss_claim_age: Optional[int] = Query(default=None),
    person_b_ss_fra_amount: Optional[float] = Query(default=None),
    person_b_ss_claim_age: Optional[int] = Query(default=None),
) -> dict:
    """Run a projection in the requested mode.

    `mode=deterministic` (default) runs the constant-real-return projection and
    returns today's shape, unchanged. `mode=historical` runs the plan over every
    historical cohort and returns the median trajectory + depletion stats.
    `mode=monte_carlo` samples correlated per-class Normal returns and returns the
    SAME fan payload shape as historical (median + per-age bands + ending range +
    depletion), so the front-end fan renderer is reused verbatim.

    Each scenario query parameter is optional; omitted parameters use the default
    values from default_scenario(). The Monte Carlo `volatility` per class rides
    inside the `allocation` string (an optional 4th field per triple); the
    `correlation` scalar is its own param. Inputs are sanity-checked; invalid
    combinations (an unknown mode, vol < 0, |correlation| > 0.99, allocation not
    summing to 100) return HTTP 422.
    """
    if mode not in _MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {', '.join(_MODES)} (got '{mode}')",
        )
    if spending_mode not in SPENDING_MODE_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"spending_mode must be one of {', '.join(SPENDING_MODE_IDS)} "
                f"(got '{spending_mode}')"
            ),
        )
    if allocation_mode not in _ALLOCATION_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"allocation_mode must be one of {', '.join(_ALLOCATION_MODES)} "
                f"(got '{allocation_mode}')"
            ),
        )
    _validate_sourcing(sourcing_mode, spending_mode, bucket_n1, bucket_n2)
    _validate_correlation(correlation)
    _validate_guardrails(
        spending_mode, guard_band, guard_adjust, spending_floor, spending_ceiling
    )
    _validate_taxable_gains_fraction(taxable_gains_fraction)

    scenario = _build_scenario(
        spending_mode=spending_mode,
        allocation_mode=allocation_mode,
        sourcing_mode=sourcing_mode,
        bucket_n1=bucket_n1,
        bucket_n2=bucket_n2,
        guard_band=guard_band,
        guard_adjust=guard_adjust,
        spending_floor=spending_floor,
        spending_ceiling=spending_ceiling,
        amortization_real_return=amortization_real_return,
        taxable_gains_fraction=taxable_gains_fraction,
        initial_portfolio=initial_portfolio,
        annual_spending=annual_spending,
        allocation=allocation,
        correlation=correlation,
        start_age=start_age,
        end_age=end_age,
        person_a_pension_amount=person_a_pension_amount,
        person_a_pension_start_age=person_a_pension_start_age,
        person_b_pension_amount=person_b_pension_amount,
        person_b_pension_start_age=person_b_pension_start_age,
        person_a_ss_fra_amount=person_a_ss_fra_amount,
        person_a_ss_claim_age=person_a_ss_claim_age,
        person_b_ss_fra_amount=person_b_ss_fra_amount,
        person_b_ss_claim_age=person_b_ss_claim_age,
    )

    _validate(scenario)
    _validate_scenario_invariants(scenario)
    return _run_trajectory(scenario, mode, stress_id=stress_id)


def _run_trajectory(
    scenario,
    mode: str,
    window_start_year: Optional[int] = None,
    stress_id: Optional[str] = None,
) -> dict:
    """Run a (validated) scenario in the given mode and build the response.

    Mode-dispatch + serialization shared by the GET and POST trajectory paths, so
    both transports produce a byte-identical response shape for the same resolved
    scenario. Caller is responsible for the mode/spending_mode/correlation/scenario
    validation before calling.

    ``window_start_year`` (comparison-window alignment, option 3) is an optional
    Historical cohort START-YEAR FLOOR: when set, the Historical run restricts its
    cohorts to starts at or after it, so an A/B comparison can score both scenarios
    on the SAME (common, latest) testable window. Historical-only — it's ignored in
    Deterministic mode (no cohort window) and Monte Carlo (trials aren't
    cohort-windowed the same way). ``None`` (single, non-comparison runs) keeps the
    native window, so single-run behavior is unchanged.

    Response assembly is delegated to the per-mode Payload namespaces
    (``DeterministicPayload`` / ``HistoricalPayload`` / ``MonteCarloPayload``).
    They own the mode-conditional fields (the stochastic ones own
    ``spending_bands`` / ``adjustment_stats`` / ``worst_cohort``; all three own
    the buckets-only / tax-location fields) so each gate condition lives in one
    place per mode, and all three modes dispatch through a single
    ``Payload.from_result`` call.
    """
    if mode == _MODE_HISTORICAL:
        # Route the engine's no-mapped-class / no-replayable-common-window
        # ValueError to a 422 (same discipline as the Monte Carlo branch below
        # and /api/solve). A multi-class scenario whose selected classes share no
        # full-horizon overlap window (e.g. a short-history class like us_reit
        # over a horizon longer than its data) raises ValueError; without this
        # it surfaces as a 500. Shared by the GET and POST trajectory paths.
        try:
            result = simulate_historical(scenario, window_start_year=window_start_year)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=engine_error_to_detail(exc))
        return HistoricalPayload.from_result(scenario, result)

    if mode == _MODE_MONTE_CARLO:
        # Monte Carlo returns the IDENTICAL fan payload shape as historical, so
        # the front-end fan renderer is reused unchanged. Route the engine's
        # no-mapped-class / missing-matrix ValueError to a 422 (same discipline
        # as /api/solve).
        try:
            mc = simulate_monte_carlo(scenario)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=engine_error_to_detail(exc))
        return MonteCarloPayload.from_result(scenario, mc)

    if mode == _MODE_STRESS:
        # Stress (stress-test design slice 1): a Family-A synthetic-cohort stress
        # OVERLAID on the current plan. Reuses the Deterministic single-trajectory
        # payload (it produces ONE trajectory, like Deterministic), so the
        # front-end single-line renderer is reused. An unknown stress id or the
        # slice-1 scope guard (glidepath / buckets) raises a typed
        # StressNotAvailable (a ValueError) → friendly 422, same discipline as the
        # Historical / Monte Carlo branches above.
        if not stress_id:
            raise HTTPException(
                status_code=422,
                detail="mode=stress requires a stress_id",
            )
        try:
            result = simulate_stress(scenario, stress_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=engine_error_to_detail(exc))
        # Effective spending rails for the spending-chart guide line. A stress can
        # scale the floor/ceiling (cost_of_living_shock ×1.20), so a dynamic strategy clamps
        # against the PERTURBED rails, not the live UI knob. Expose the post-
        # transform values as EXTRA payload fields (echoed `scenario` stays the
        # un-perturbed input, so selecting a stress never repaints the input knobs
        # — the slice-1 overlay invariant). For Family A and ss_haircut these equal
        # the input rails; for cost_of_living_shock they're ×1.20.
        eff_floor, eff_ceiling = effective_spending_rails(scenario, stress_id)
        return DeterministicPayload.from_result(
            scenario,
            result,
            effective_spending_floor=eff_floor,
            effective_spending_ceiling=eff_ceiling,
        )

    from firebridgelab.tax.location import has_tax_split

    # Tax-drag design td1 DECISION 1 / td-c scope 2: for a split-active,
    # single-sourcing scenario the plain deterministic MAIN LINE becomes the
    # DRAGGED per-age total — killing the ft2-era mixed-semantics interim (a
    # dragged ``tax_location`` balance chart sitting next to a still-pre-tax
    # ``trajectory`` main line). Buckets sourcing stays pre-tax (the split is
    # no-op'd there, same gate ``_tax_location_payload`` / ``_federal_tax_payload``
    # use) — ``simulate()`` itself already handles the buckets-mode trajectory.
    # No-split scenarios are BYTE-STABLE: ``has_tax_split`` is False, so this
    # branch is unreachable and the canary (``simulate(default_scenario())``)
    # is untouched.
    if has_tax_split(scenario) and scenario.sourcing_mode != SOURCING_MODE_BUCKETS:
        # det-request memoization: compute the dragged decomposition ONCE
        # here and hand it to both this trajectory build AND
        # DeterministicPayload.from_result (which threads it into
        # _tax_location_payload / _federal_tax_payload /
        # _deterministic_estate_cost below) — collapsing what used to be 4
        # independent simulate_tax_location_deterministic re-runs per request
        # into 1. Per-request only (a fresh `path` every call); never a
        # cross-request cache.
        from firebridgelab.tax.location_path import simulate_tax_location_deterministic

        path = simulate_tax_location_deterministic(scenario)
        result = _dragged_deterministic_trajectory(scenario, path=path)
        return DeterministicPayload.from_result(
            scenario, result, tax_location_path=path
        )
    result = simulate(scenario)
    return DeterministicPayload.from_result(scenario, result)


def _deterministic_bucket_balances(scenario) -> Optional[dict]:
    """The deterministic 3-bucket per-year cash/income/growth balances, or None.

    bucket-strategy design "Bucket-balance visualization": Deterministic → the exact
    3-line chart (cash/income/growth over age). Re-runs the deterministic bucket sim
    (``simulate_buckets_deterministic``) — the SAME run the buckets-mode deterministic
    projection draws — so the summed series matches THAT buckets-mode deterministic
    trajectory's ``end_portfolio`` exactly (no drift). This reconciles to the
    buckets-mode line, NOT the single-allocation projection. Returns one parallel list
    per bucket keyed to ``ages``. None under single sourcing, so the deterministic
    payload simply omits ``bucket_balances`` (back-compatible). In Deterministic
    mode every year is an up year, so this shows the buckets refilling each year and
    never riding a downturn — the caption points the user to Historical / MC to see
    the buffer actually tested.
    """
    if scenario.sourcing_mode != SOURCING_MODE_BUCKETS:
        return None
    from firebridgelab.engine.buckets import simulate_buckets_deterministic

    bt = simulate_buckets_deterministic(scenario)
    return {
        "ages": [y.age for y in bt.years],
        "cash": [y.cash for y in bt.years],
        "income": [y.income for y in bt.years],
        "growth": [y.growth for y in bt.years],
    }


def _effective_equity_payload(scenario) -> Optional[dict]:
    """The effective-equity glidepath the bucket setup imposes, or None.

    SURFACES the allocation gap-based sizing silently imposes (the effective-equity
    glidepath readout): at a high spend the low-equity safe buckets eat most of the
    portfolio, so the effective equity share starts FAR below the growth bucket's
    nominal weight and RISES toward it as guaranteed income lands (the safe targets
    shrink). Computed off the DETERMINISTIC buckets run — the age-anchored reference
    — so it is a property of the SETUP, the same in all three sim modes; attached to
    every mode's payload the same conditional way ``bucket_balances`` /
    ``buffer_survival`` are. None under single sourcing (the key is omitted —
    back-compatible).

    Shape: ``{"ages", "shares"}`` parallel lists over the full horizon (the per-age
    series, so the UI can read any anchor) plus ``{"start_age", "full_gi_age",
    "end_age"}`` convenience anchors — the readout's three landmark ages. The
    full-GI age is the latest age at which a guaranteed-income stream starts (the
    age the GI ramp finishes landing), clamped into the horizon.
    """
    if scenario.sourcing_mode != SOURCING_MODE_BUCKETS:
        return None
    gp = effective_equity_glidepath(scenario)
    ages = [p["age"] for p in gp]
    shares = [p["equity_share"] for p in gp]
    # The full-GI age = the latest GI start age (when the ramp finishes), clamped
    # to the horizon so a stream starting past end_age (or before start_age)
    # resolves to a real in-horizon anchor.
    gi_starts = [st.start for st in household(scenario).streams] or [scenario.start_age]
    full_gi_age = max(scenario.start_age, min(scenario.end_age, max(gi_starts)))
    return {
        "ages": ages,
        "shares": shares,
        "start_age": scenario.start_age,
        "full_gi_age": full_gi_age,
        "end_age": scenario.end_age,
    }


@app.get("/api/solve")
def get_solve(
    mode: str = Query(default=_MODE_DETERMINISTIC),
    spending_mode: str = Query(default=SPENDING_MODE_CONSTANT),
    confidence: float = Query(default=0.80),
    guard_band: Optional[float] = Query(default=None),
    guard_adjust: Optional[float] = Query(default=None),
    spending_floor: Optional[float] = Query(default=None),
    spending_ceiling: Optional[float] = Query(default=None),
    taxable_gains_fraction: Optional[float] = Query(default=None),
    initial_portfolio: Optional[float] = Query(default=None),
    annual_spending: Optional[float] = Query(default=None),
    allocation: Optional[str] = Query(default=None),
    correlation: Optional[float] = Query(default=None),
    start_age: Optional[int] = Query(default=None),
    end_age: Optional[int] = Query(default=None),
    person_a_pension_amount: Optional[float] = Query(default=None),
    person_a_pension_start_age: Optional[int] = Query(default=None),
    person_b_pension_amount: Optional[float] = Query(default=None),
    person_b_pension_start_age: Optional[int] = Query(default=None),
    person_a_ss_fra_amount: Optional[float] = Query(default=None),
    person_a_ss_claim_age: Optional[int] = Query(default=None),
    person_b_ss_fra_amount: Optional[float] = Query(default=None),
    person_b_ss_claim_age: Optional[int] = Query(default=None),
) -> dict:
    """Solve for the max sustainable spend at a chosen confidence.

    Mirrors `/api/trajectory`'s scenario params + `mode`, plus a `confidence`
    dial. The solver binary-searches `annual_spending` for the highest level
    whose survival fraction clears the threshold, then writes it back into the
    scenario. The response echoes the resolved scenario AT the solved spend, so
    the front-end can render the normal projection at that level with no
    re-derivation.

    - `mode=historical` → threshold = `confidence` (must be in (0, 1]); the
      response reports it as `confidence_target`.
    - `mode=monte_carlo` → threshold = `confidence` too (the 10k sampled trials
      make the dial land cleanly); the per-class `volatility` rides inside the
      `allocation` string and the `correlation` scalar is its own param, exactly
      as on `/api/trajectory`. The response reports `confidence_target`.
    - `mode=deterministic` → a single path, so the dial is meaningless: threshold
      is 1.0 ("the one path must survive") and `confidence_target` is `null`.

    Validation matches `/api/trajectory` (422 on unknown mode / bad scenario);
    additionally, out-of-range confidence is a 422, |correlation| > 0.99 is a 422,
    and an engine `ValueError` (e.g. no allocation class maps to a known series,
    or no full-window cohort exists) is routed to a 422 rather than a 500.
    """
    if mode not in _MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {', '.join(_MODES)} (got '{mode}')",
        )
    if spending_mode not in SPENDING_MODE_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"spending_mode must be one of {', '.join(SPENDING_MODE_IDS)} "
                f"(got '{spending_mode}')"
            ),
        )
    # Confidence is the dial for both stochastic modes (historical + monte_carlo);
    # we range-check it whenever it's a dial value in (0, 1]; deterministic ignores
    # it (fixed 1.0).
    if not (0.0 < confidence <= 1.0):
        raise HTTPException(
            status_code=422,
            detail=f"confidence must be in (0, 1] (got {confidence})",
        )
    _validate_correlation(correlation)
    _validate_guardrails(
        spending_mode, guard_band, guard_adjust, spending_floor, spending_ceiling
    )
    _validate_taxable_gains_fraction(taxable_gains_fraction)

    scenario = _build_scenario(
        spending_mode=spending_mode,
        guard_band=guard_band,
        guard_adjust=guard_adjust,
        spending_floor=spending_floor,
        spending_ceiling=spending_ceiling,
        taxable_gains_fraction=taxable_gains_fraction,
        initial_portfolio=initial_portfolio,
        annual_spending=annual_spending,
        allocation=allocation,
        correlation=correlation,
        start_age=start_age,
        end_age=end_age,
        person_a_pension_amount=person_a_pension_amount,
        person_a_pension_start_age=person_a_pension_start_age,
        person_b_pension_amount=person_b_pension_amount,
        person_b_pension_start_age=person_b_pension_start_age,
        person_a_ss_fra_amount=person_a_ss_fra_amount,
        person_a_ss_claim_age=person_a_ss_claim_age,
        person_b_ss_fra_amount=person_b_ss_fra_amount,
        person_b_ss_claim_age=person_b_ss_claim_age,
    )

    _validate(scenario)
    _validate_scenario_invariants(scenario)
    return _run_solve(scenario, mode, spending_mode, confidence)


def _dragged_deterministic_trajectory(scenario, *, path=None):
    """The deterministic main line with tax paid from the portfolio (tax-drag
    design td1 DECISION 1). ``_run_trajectory`` calls it only for a split-active,
    single-sourcing scenario, so the ``trajectory`` block and the dragged
    ``tax_location`` chart describe the same world. The builder lives in the
    library (``tax.location_path.dragged_deterministic_trajectory``) so the CLI
    reports the same line; ``path`` is the request's precomputed decomposition.
    """
    from firebridgelab.tax.location_path import dragged_deterministic_trajectory

    return dragged_deterministic_trajectory(scenario, path=path)


def _run_solve(scenario, mode: str, spending_mode: str, confidence: float) -> dict:
    """Solve a (validated) scenario for max spend and build the response.

    Solver dispatch + serialization shared by the GET and POST solve paths.
    Derives the per-mode threshold from ``confidence`` and routes engine
    ValueErrors to 422. Caller validates mode/spending_mode/confidence/
    correlation/scenario first.
    """
    # Both stochastic modes (historical + monte_carlo) solve against the
    # confidence dial; deterministic solves against 1.0 (the single path must
    # survive). Route engine ValueErrors to 422, matching the _validate
    # discipline (don't let renormalized_weights / empty-cohort / no-mapped-class
    # / missing-matrix errors surface as a 500).
    threshold = confidence if mode != _MODE_DETERMINISTIC else 1.0
    try:
        result = solve_max_spend(scenario, threshold, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=engine_error_to_detail(exc))

    solved_scenario = replace(scenario, annual_spending=result.solved_spend)
    return {
        "mode": mode,
        "spending_mode": spending_mode,
        # The dial drives both stochastic modes; deterministic reports null (no
        # dial — its threshold is the implicit "the one path must survive").
        "confidence_target": result.confidence_target if mode != _MODE_DETERMINISTIC else None,
        "solved_spend": result.solved_spend,
        "achieved_fraction": result.achieved_fraction,
        "hit_ceiling": result.hit_ceiling,
        "cohort_count": result.cohort_count,
        "scenario": asdict(solved_scenario),
    }


def _validate_modes(
    mode: str, spending_mode: str, allocation_mode: str = ALLOCATION_MODE_CONSTANT
) -> None:
    """422 if mode / spending_mode / allocation_mode aren't valid (GET + POST shared)."""
    if mode not in _MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {', '.join(_MODES)} (got '{mode}')",
        )
    if spending_mode not in SPENDING_MODE_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"spending_mode must be one of {', '.join(SPENDING_MODE_IDS)} "
                f"(got '{spending_mode}')"
            ),
        )
    if allocation_mode not in _ALLOCATION_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"allocation_mode must be one of {', '.join(_ALLOCATION_MODES)} "
                f"(got '{allocation_mode}')"
            ),
        )


@app.post("/api/trajectory")
def post_trajectory(body: ScenarioBody = Body(...)) -> dict:
    """Run a projection from a POST JSON body (the multi-class transport).

    The POST analogue of GET ``/api/trajectory`` (asset-class design B4): the body
    carries ``allocation`` as ``[{id, weight, real_return, volatility}]`` plus an
    optional ordered ``correlations`` matrix and the scalar scenario fields. Same
    validation + mode dispatch + response shape as GET (``_run_trajectory``), so
    the JSON echo returns the same ``{id, label, ...}`` allocation shape. Use POST
    for any multi-class or preset allocation; GET stays restricted to the two
    legacy classes.
    """
    _validate_modes(body.mode, body.spending_mode, body.allocation_mode)
    _validate_sourcing(
        body.sourcing_mode, body.spending_mode,
        body.buckets.n1 if body.buckets else None,
        body.buckets.n2 if body.buckets else None,
    )
    _validate_correlation(body.correlation)
    _validate_guardrails(
        body.spending_mode, body.guard_band, body.guard_adjust,
        body.spending_floor, body.spending_ceiling,
    )
    _validate_taxable_gains_fraction(body.taxable_gains_fraction)
    scenario = _build_scenario_from_body(body)
    _validate(scenario)
    _validate_tax_location(scenario)
    _validate_scenario_invariants(scenario)
    return _run_trajectory(
        scenario,
        body.mode,
        window_start_year=body.window_start_year,
        stress_id=body.stress_id,
    )


@app.post("/api/solve")
def post_solve(body: ScenarioBody = Body(...)) -> dict:
    """Solve for max sustainable spend from a POST JSON body (multi-class).

    The POST analogue of GET ``/api/solve``: same body shape as
    ``post_trajectory`` plus the ``confidence`` dial. Same validation, solver
    dispatch, and response shape as the GET path (``_run_solve``).
    """
    _validate_modes(body.mode, body.spending_mode, body.allocation_mode)
    if not (0.0 < body.confidence <= 1.0):
        raise HTTPException(
            status_code=422,
            detail=f"confidence must be in (0, 1] (got {body.confidence})",
        )
    _validate_sourcing(
        body.sourcing_mode, body.spending_mode,
        body.buckets.n1 if body.buckets else None,
        body.buckets.n2 if body.buckets else None,
    )
    _validate_correlation(body.correlation)
    _validate_guardrails(
        body.spending_mode, body.guard_band, body.guard_adjust,
        body.spending_floor, body.spending_ceiling,
    )
    _validate_taxable_gains_fraction(body.taxable_gains_fraction)
    scenario = _build_scenario_from_body(body)
    _validate(scenario)
    _validate_tax_location(scenario)
    _validate_scenario_invariants(scenario)
    return _run_solve(scenario, body.mode, body.spending_mode, body.confidence)


# ---------- Plan files (M5) ----------
#
# The web UI loads and downloads the same TOML plan files the command line
# runs (docs/plan-files.md). Parsing and writing are firebridgelab.plan's;
# api.plan_io converts a plan's Scenario into the body the UI posts.


class PlanImportBody(BaseModel):
    """``POST /api/plan/import``: the text of a plan file."""

    toml: str
    filename: str = "plan.toml"


class PlanExportBody(BaseModel):
    """``POST /api/plan/export``: a scenario body plus the plan's run settings."""

    scenario: ScenarioBody
    name: str = ""
    description: str = ""
    trials: Optional[int] = None
    seed: Optional[int] = None
    stress: Optional[List[str]] = None


def _plan_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@app.post("/api/plan/import")
def post_plan_import(body: PlanImportBody = Body(...)) -> dict:
    """Parse a plan file and return its scenario as an API body.

    The response is ``{name, description, trials, seed, stress, body}``; ``body``
    is what ``POST /api/trajectory`` accepts, with the household explicit. A
    plan file that cannot be read is a 422 whose detail names the key at fault.
    """
    try:
        plan = plan_io.parse_plan_text(body.toml, body.filename or "plan.toml")
    except plan_io.PlanError as exc:
        raise _plan_error(exc)
    return plan_io.plan_to_payload(plan)


@app.post("/api/plan/export")
def post_plan_export(body: PlanExportBody = Body(...)) -> dict:
    """Write a scenario body as plan-file TOML: ``{"toml": "..."}``.

    The scenario is built and validated exactly as ``POST /api/trajectory``
    builds it, then written by ``firebridgelab.plan.dump``. The text is read
    back before it is returned, so a file this route writes always loads.
    """
    from firebridgelab.engine.monte_carlo import DEFAULT_N_TRIALS, DEFAULT_SEED
    from firebridgelab.plan import Plan, dump

    sb = body.scenario
    _validate_modes(_MODE_DETERMINISTIC, sb.spending_mode, sb.allocation_mode)
    _validate_sourcing(
        sb.sourcing_mode, sb.spending_mode,
        sb.buckets.n1 if sb.buckets else None,
        sb.buckets.n2 if sb.buckets else None,
    )
    _validate_correlation(sb.correlation)
    _validate_taxable_gains_fraction(sb.taxable_gains_fraction)
    scenario = _build_scenario_from_body(sb)
    _validate(scenario)
    _validate_tax_location(scenario)
    _validate_scenario_invariants(scenario)
    plan = Plan(
        scenario,
        name=body.name,
        description=body.description,
        trials=body.trials if body.trials is not None else DEFAULT_N_TRIALS,
        seed=body.seed if body.seed is not None else DEFAULT_SEED,
        stress=tuple(body.stress or ()),
    )
    text = dump(plan)
    try:
        plan_io.parse_plan_text(text)
    except plan_io.PlanError as exc:
        raise HTTPException(
            status_code=422, detail=f"this scenario cannot be written as a plan file: {exc}"
        )
    return {"toml": text}


@app.get("/api/examples")
def get_examples() -> list[dict]:
    """The bundled example plans as ``[{id, name, description}]``."""
    out = []
    for name in plan_io.example_names():
        plan = plan_io.parse_plan_text(plan_io.example_text(name), f"{name}.toml")
        out.append({"id": name, "name": plan.name or name, "description": plan.description})
    return out


@app.get("/api/examples/{name}")
def get_example(name: str) -> dict:
    """One bundled example: the import payload plus its TOML text."""
    try:
        text = plan_io.example_text(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no example named {name!r}")
    plan = plan_io.parse_plan_text(text, f"{name}.toml")
    return {"id": name, **plan_io.plan_to_payload(plan, text)}


def _class_menu_entries() -> list[dict]:
    """The canonical 12-class menu as ``{id, label, real_return, volatility,
    data_start_year, is_equity}`` dicts in design order.

    ``is_equity`` is read directly from ``firebridgelab.engine.buckets.EQUITY_CLASS_IDS`` — the
    SINGLE SOURCE of the equity/non-equity classification. The browser consumes it
    (glide-preset mapping) instead of hand-keeping a JS replica of that set.

    Sources, by class (asset-class design "The 12 classes"):
      - The 10 NEW classes come from ``firebridgelab.engine.allocation.CLASS_MENU`` (their geometric-mean
        ``real_return`` + ``volatility`` already seeded from the staged series).
      - The 2 LEGACY classes (``us_total_market`` / ``us_agg_bonds``) are not in
        CLASS_MENU, so their menu defaults are derived here from the SAME loaded
        real-return series their engine path binds to (``us_equity`` /
        ``us_bonds``): the geometric mean (Deterministic default) + the sample
        stdev (MC volatility). These are MENU defaults a user gets when ADDING the
        class; the default *scenario* keeps the canary 7%/2% on the 60/40 rows
        (seeded separately on the frontend from the echoed default scenario).

    ``data_start_year`` is each class's earliest year of real data — the "testable
    since YYYY" hint for the matrix row. Built from the same year-keyed accessor
    Historical/MC read, so the hint can't drift from the data.
    """
    import numpy as np

    from firebridgelab.engine.buckets import EQUITY_CLASS_IDS
    from firebridgelab.returns.correlation import real_return_series_by_series_id
    from firebridgelab.returns.series import SERIES_FOR_CLASS

    series = real_return_series_by_series_id()
    entries: list[dict] = []
    for class_id in _available_class_ids():
        series_id = SERIES_FOR_CLASS.get(class_id)
        data_start_year = None
        if series_id is not None and series_id in series:
            data_start_year = min(series[series_id])
        if class_id in CLASS_MENU:
            ac = CLASS_MENU[class_id]
            real_return = ac.real_return
            volatility = ac.volatility
            label = ac.label
        else:
            # Legacy class: derive the menu default from its loaded series.
            label = _CLASS_LABELS.get(class_id, class_id)
            returns = np.array(list(series[series_id].values()), dtype=float)
            growth = float(np.prod(1.0 + returns))
            real_return = growth ** (1.0 / len(returns)) - 1.0
            # Volatility: the SAME computation as default_scenario() /
            # default_bucket_config() (single-sourced so this
            # endpoint and the default-scenario MC run can never drift apart).
            volatility = _legacy_default_volatility(class_id)
        entries.append(
            {
                "id": class_id,
                "label": label,
                "real_return": real_return,
                "volatility": volatility,
                "data_start_year": data_start_year,
                # SINGLE SOURCE OF TRUTH for the equity/non-equity split: read
                # directly from firebridgelab.engine.buckets.EQUITY_CLASS_IDS (the same set the
                # effective-equity glidepath readout uses). Additive field — the
                # browser consumes it for glide-preset classification instead of
                # hand-keeping a JS replica. The engine constant stays canonical;
                # this only publishes it.
                "is_equity": class_id in EQUITY_CLASS_IDS,
            }
        )
    return entries


@functools.lru_cache(maxsize=1)
def _canonical_volatility_by_id() -> dict:
    """{class_id -> canonical volatility}, sourced from the SAME registry
    /api/classes serves, so an omitted per-class vol resolves to that class's
    own default rather than the equity dataclass default.
    """
    return {e["id"]: e["volatility"] for e in _class_menu_entries()}


@app.get("/api/classes")
def get_classes() -> dict:
    """Return the canonical 12-class asset menu (asset-class design deliverable 1).

    Each class carries
    ``{id, label, real_return, volatility, data_start_year, is_equity}``: the
    stable class id (allocation/menu key), the display label, the geometric-mean
    real return (the Deterministic default + the matrix's pre-filled return), the
    annual real-return volatility (the MC default), the earliest year of real data
    (the "testable since YYYY" hint), and ``is_equity`` — the equity/non-equity
    flag read from ``firebridgelab.engine.buckets.EQUITY_CLASS_IDS`` (single source of truth; the
    glide-preset client reads it from here). The frontend builds the
    allocation-matrix rows + per-class CMA defaults from this.
    """
    return {"classes": _class_menu_entries()}


@app.get("/api/presets")
def get_presets() -> dict:
    """Return the named portfolio presets (asset-class design deliverable 2 / D7).

    Each preset is ``{name, faithful, weights: {class_id: integer-percent}}`` with
    weights summing to 100 over the stable class ids. Selecting a preset populates
    the editable allocation matrix; the user can then edit + run. ``faithful``
    flags whether the 12-class menu reconstructs the source exactly (True) or
    collapses a factor/EM split into the mainstream + small-value scope (False).
    """
    available = set(_available_class_ids())
    return {
        "presets": [
            dict(p) for p in _PRESETS if set(p["weights"]) <= available
        ]
    }


def _fan_bands(result) -> list[dict]:
    """One ``{age, p10, p25, median, p75, p90}`` per age — the shared fan shape.

    Both stochastic engines (``HistoricalResult``, ``MonteCarloResult``) carry
    the same portfolio percentile-fan fields, so this helper builds the
    ``bands`` list identically for both. Used by ``HistoricalPayload`` and
    ``MonteCarloPayload``.
    """
    return [
        {
            "age": age,
            "p10": p10,
            "p25": p25,
            "median": median,
            "p75": p75,
            "p90": p90,
        }
        for age, p10, p25, median, p75, p90 in zip(
            result.ages,
            result.p10,
            result.p25,
            result.median,
            result.p75,
            result.p90,
        )
    ]


def _deterministic_flows(scenario) -> list[dict]:
    """One ``{age, spending, guaranteed_income}`` per age — the deterministic flows.

    (flow-overlay slice 2): the Historical / Monte-Carlo portfolio fans
    overlay a GI-floor line (always) + a spending line (constant/lifecycle only).
    Both are DETERMINISTIC functions of age — the GI streams turn on at fixed ages
    and constant/lifecycle spend is a pure function of age — so the single source of
    truth is the same ``simulate(scenario)`` trajectory the Deterministic mode runs.
    Read each ``YearPoint``'s ``.age`` / ``.spending`` / ``.guaranteed_income``;
    BOTH fields are always included (the front-end gates which line it draws, not
    which field it receives). Used by ``HistoricalPayload`` and ``MonteCarloPayload``.
    """
    return [
        {
            "age": p.age,
            "spending": p.spending,
            "guaranteed_income": p.guaranteed_income,
        }
        for p in simulate(scenario).points
    ]


def _spending_bands(result) -> Optional[list]:
    """The per-age spending percentile fan (guardrails-only), or None.

    Variable-spending design slice 1: under ``guardrails`` spending varies across
    cohorts / trials, so the result carries ``spend_median``/``spend_p10``/.../
    ``spend_p90`` (a fan mirroring the portfolio fan). Serialize them as one
    ``{age, p10, p25, median, p75, p90}`` per age — the SAME shape ``bands`` uses,
    so the front-end reuses the percentile-fan renderer verbatim. Returns None
    when the spend fan is absent (constant/lifecycle, or a result without the
    fields), so the payload simply omits ``spending_bands``.
    """
    spend_median = getattr(result, "spend_median", None)
    if spend_median is None:
        return None
    return [
        {
            "age": age,
            "p10": p10,
            "p25": p25,
            "median": median,
            "p75": p75,
            "p90": p90,
        }
        for age, p10, p25, median, p75, p90 in zip(
            result.ages,
            result.spend_p10,
            result.spend_p25,
            result.spend_median,
            result.spend_p75,
            result.spend_p90,
        )
    ]


def _adjustment_stats_payload(result) -> Optional[dict]:
    """The aggregate cost-of-flexibility stats (guardrails-only), or None.

    Variable-spending design slice 2: under ``guardrails`` the result carries an
    ``AdjustmentStats`` (how often a cut/raise fires, median first-cut age, time
    at the operational floor, the deepest cut, % ever forced down to the floor).
    Serialize it as a flat dict for the readout panel. Returns None when absent
    (constant/lifecycle), so the payload simply omits ``adjustment_stats``.
    """
    stats = getattr(result, "adjustment_stats", None)
    if stats is None:
        return None
    return {
        "cohort_count": stats.cohort_count,
        "pct_ever_cut": stats.pct_ever_cut,
        "pct_ever_raised": stats.pct_ever_raised,
        "median_first_cut_age": stats.median_first_cut_age,
        "median_years_at_floor": stats.median_years_at_floor,
        "deepest_cut": stats.deepest_cut,
        "lowest_single_year_spend": stats.lowest_single_year_spend,
        "pct_ever_forced_to_floor": stats.pct_ever_forced_to_floor,
    }


def _buffer_survival_payload(result) -> Optional[dict]:
    """The buffer-survival readout (buckets-only), or None.

    bucket-strategy design "Output": under ``sourcing_mode=buckets`` the result
    carries a ``BufferSurvivalStats`` (% of histories whose buffer was breached and
    a growth sale forced, the median age of the first forced sale, the worst /
    earliest forced-sale age). Serialize it as a flat dict for the readout. Returns
    None when absent (single sourcing), so the payload omits ``buffer_survival`` —
    exactly how the guardrails ``adjustment_stats`` field is attached.
    """
    stats = getattr(result, "buffer_survival", None)
    if stats is None:
        return None
    return {
        "count": stats.count,
        "pct_cohorts_forced_to_sell_growth": stats.pct_cohorts_forced_to_sell_growth,
        "median_years_buffer_lasted": stats.median_years_buffer_lasted,
        "worst_forced_sale_age": stats.worst_forced_sale_age,
    }


def _tax_location_payload(scenario, *, path=None) -> Optional[dict]:
    """The deterministic tax-location decomposition block, or None (tax-buckets tb1).

    Present only when a tax-location split is supplied AND sourcing is single
    (tb1 is single-mode only — under buckets sourcing the split is no-op'd, so
    this returns None and the payload omits the block, back-compatible). Runs the
    deterministic shadow decomposition (``simulate_tax_location_deterministic``)
    — the SAME per-age balances that sum to the deterministic scalar portfolio
    every year — and serializes the three per-age balance lines plus the two
    single-path depletion facts.

    ``path`` (det-request memoization) is the OPTIONAL precomputed
    ``TaxLocationPath`` — ``DeterministicPayload.from_result`` resolves it once
    per request and passes it to this + the two sibling builders below so the
    decomposition runs once, not per-block; ``None`` recomputes inline
    (byte-identical to pre-memoization behavior).

    Shape (the web agent builds against this verbatim):
        {
          "ages": [int, ...],                 # start_age .. end_age
          "taxable": [float, ...],            # per-age end-of-year balance
          "tax_deferred": [float, ...],
          "roth": [float, ...],
          "taxable_depletion_age": int | null,    # earliest age <=59 taxable hit 0
          "reachable_depletion_age": int | null,  # earliest age <=59 taxable+roth hit 0
        }
    """
    from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS as _BUCKETS
    from firebridgelab.tax.location import has_tax_split
    from firebridgelab.tax.location_path import simulate_tax_location_deterministic

    if not has_tax_split(scenario) or scenario.sourcing_mode == _BUCKETS:
        return None
    if path is None:
        path = simulate_tax_location_deterministic(scenario)
    return {
        "ages": path.ages,
        "taxable": path.taxable,
        "tax_deferred": path.tax_deferred,
        "roth": path.roth,
        "taxable_depletion_age": path.taxable_depletion_age,
        "reachable_depletion_age": path.reachable_depletion_age,
    }


def _tax_location_risk_payload(result) -> Optional[dict]:
    """The stochastic tax-location risk block, or None (tax-buckets tb1).

    Historical / Monte Carlo only. Present only when a split was supplied AND
    sourcing was single (the shadow decomposition ran across cohorts/trials);
    None otherwise, so the payload omits the block (back-compatible) — exactly how
    ``buffer_survival`` is attached. Serializes the aggregated headline metric.

    Shape (the web agent builds against this verbatim):
        {
          "count": int,                            # cohorts/trials covered
          "pct_taxable_depleted_pre_595": float,   # 0..100
          "pct_reachable_depleted_pre_595": float, # 0..100
          "taxable_depletion": {"median": int|null, "p10": int|null, "earliest": int|null},
          "reachable_depletion": {"median": int|null, "p10": int|null, "earliest": int|null},
          "ages": [int, ...] | null,               # shared age axis for all series
          "taxable_p10": [float, ...] | null,
          "taxable_median": [float, ...] | null,
          "taxable_p90": [float, ...] | null,
          # Per-account median series + the TOTAL-investments fan (txlb hotfix).
          # Median is not additive: the three account medians do NOT stack to
          # total_median. The total_* fan equals the portfolio fan to float tol.
          "tax_deferred_median": [float, ...] | null,
          "roth_median": [float, ...] | null,
          "total_p10": [float, ...] | null,
          "total_median": [float, ...] | null,
          "total_p90": [float, ...] | null,
        }
    """
    risk = getattr(result, "tax_location_risk", None)
    if risk is None:
        return None

    def _dist(d) -> dict:
        return {"median": d.median, "p10": d.p10, "earliest": d.earliest}

    return {
        "count": risk.count,
        "pct_taxable_depleted_pre_595": risk.pct_taxable_depleted_pre_595,
        "pct_reachable_depleted_pre_595": risk.pct_reachable_depleted_pre_595,
        "taxable_depletion": _dist(risk.taxable_depletion),
        "reachable_depletion": _dist(risk.reachable_depletion),
        "ages": risk.ages,
        "taxable_p10": risk.taxable_p10,
        "taxable_median": risk.taxable_median,
        "taxable_p90": risk.taxable_p90,
        "tax_deferred_median": risk.tax_deferred_median,
        "roth_median": risk.roth_median,
        "total_p10": risk.total_p10,
        "total_median": risk.total_median,
        "total_p90": risk.total_p90,
    }


def _federal_tax_payload(scenario, *, path=None) -> Optional[dict]:
    """The deterministic federal-tax overlay block, or None (federal-tax overlay
    design ft1/ft2; reworked td-c). Present only when a tax-location split is
    supplied AND sourcing is single — the SAME gate as ``_tax_location_payload``
    (the overlay rides the dragged decomposition; no split, no defensible tax
    number — DECISION 3). Runs the deterministic decomposition + the pure tax
    overlay (``firebridgelab.tax.overlay.compute_tax_overlay``) and serializes the
    per-age series plus the path scalars.

    ``path`` (det-request memoization) — see ``_tax_location_payload``'s
    docstring; same optional precomputed-``TaxLocationPath`` contract.

    Shape (the web agent builds against this verbatim):
        {
          "ages": [int, ...],
          "tax": [float, ...],             # total tax INCLUDING the penalty
          "spending_tax": [float, ...],    # spending-attributed bill (+penalty)
          "voluntary_tax": [float, ...],   # td-c: marginal tax on the lever's extra draw
          "forced_tax": [float, ...],      # marginal tax on the forced RMD excess
          "gross_outflow": [float, ...],   # td-c (DECISION 7): realized_spend + tax —
                                            # "what the portfolio actually funds"
          "realized_spend": [float, ...],  # td-c: the REALIZED (fundable-capped)
                                            # consumption series (TaxLocationPath.realized_spend)
          "ordinary_income": [float, ...],
          "ltcg_income": [float, ...],
          "penalty": [float, ...],
          "total_tax": float,
          "avg_annual_tax": float,
          "effective_rate": float,
          "worst_tax": float,
          "worst_tax_age": int,
          "total_forced_tax": float,       # "of which forced" share of total_tax
          "total_voluntary_tax": float,    # td-c: "of which voluntary" share of total_tax
        }
    Identity (DECISION 6, td-c fix — this used to be documented as a stale
    TWO-way ft2 split, false now that the lever can be active):
        spending_tax[i] + voluntary_tax[i] + forced_tax[i] == tax[i]
    at every age, exact ``==`` (``tax`` is ASSEMBLED as the sum of the three,
    so bit-equality holds by construction). A SEPARATE, float-APPROXIMATE
    relationship (DECISION 7):
        gross_outflow[i] - realized_spend[i] ≈ tax[i]
    — ``gross_outflow`` is recorded as ``realized_spend + tax``, and IEEE-754
    does NOT guarantee ``(a + b) - a == b``, so consumers must compare with a
    tolerance, never a bare ``==`` (the crossing-year caveat is semantic on
    top of that — see ``TaxLocationPath.gross_outflow``'s docstring).
    """
    from firebridgelab.tax.overlay import compute_tax_overlay
    from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS as _BUCKETS
    from firebridgelab.tax.location import has_tax_split
    from firebridgelab.tax.location_path import simulate_tax_location_deterministic

    if not has_tax_split(scenario) or scenario.sourcing_mode == _BUCKETS:
        return None
    if path is None:
        path = simulate_tax_location_deterministic(scenario)
    overlay = compute_tax_overlay(scenario, path)
    return {
        "ages": overlay.ages,
        "tax": overlay.tax,
        "spending_tax": overlay.spending_tax,
        "voluntary_tax": overlay.voluntary_tax,
        "forced_tax": overlay.forced_tax,
        "gross_outflow": overlay.gross_outflow,
        "realized_spend": path.realized_spend,
        "ordinary_income": overlay.ordinary_income,
        "ltcg_income": overlay.ltcg_income,
        "penalty": overlay.penalty,
        "total_tax": overlay.total_tax,
        "avg_annual_tax": overlay.avg_annual_tax,
        "effective_rate": overlay.effective_rate,
        "worst_tax": overlay.worst_tax,
        "worst_tax_age": overlay.worst_tax_age,
        "total_forced_tax": overlay.total_forced_tax,
        "total_voluntary_tax": overlay.total_voluntary_tax,
        # M4: the per-age shares of ``tax`` from state and payroll tax, the
        # taxable Social Security inside ordinary_income, the Roth conversion
        # transfer, the IRMAA surcharge (required spending, inside
        # realized_spend) and the year's MAGI.
        "state_tax": path.state_tax,
        "payroll_tax": path.payroll_tax,
        "taxable_ss": path.taxable_ss,
        "roth_conversion": path.roth_conversion,
        "irmaa": path.irmaa,
        "magi": path.magi,
        "roth_conversion_comparison": _roth_comparison_payload(scenario),
    }


def _roth_comparison_payload(scenario) -> Optional[dict]:
    """Lifetime tax and terminal after-tax value with and without the Roth
    conversion policy (``tax.roth.compare_roth_conversion``), or None when no
    conversion target is set."""
    if scenario.roth_conversion_target is None:
        return None
    from firebridgelab.tax.roth import compare_roth_conversion

    return compare_roth_conversion(scenario).to_dict()


def _federal_tax_aggregate_payload(result) -> Optional[dict]:
    """The stochastic federal-tax overlay aggregate, or None (federal-tax
    overlay design ft1/ft2; reworked td-c). Historical / Monte Carlo only —
    mirrors ``_tax_location_risk_payload``'s gating (present only when a split
    was supplied AND sourcing was single, so the engine populated
    ``result.federal_tax``).

    Shape (the web agent builds against this verbatim):
        {
          "ages": [int, ...],
          "tax_median": [float, ...],
          "forced_tax_median": [float, ...],
          "voluntary_tax_median": [float, ...],    # td-c
          "gross_outflow_median": [float, ...],    # td-c
          "median_avg_annual_tax": float,
          "median_effective_rate": float,
          "median_worst_tax": float,
          "median_worst_tax_age": int,
          "median_total_forced_tax": float,
          "median_total_voluntary_tax": float,     # td-c
        }
    NOTE: unlike the deterministic block, there is no ``realized_spend`` series
    here — the engine's ``FederalTaxOverlayAggregate`` (Historical/MC) carries
    no per-age realized-consumption median outside the guardrails-gated spend
    fan (``spending_bands``, a separate top-level key); td-d reads that key
    when present (for guardrails/funded-ratio spending) or reconstructs the
    shaped target client-side (constant/lifecycle) rather than expecting a
    realized_spend median in this block — see web/app.js
    portfolioFanTooltipRows / renderSpendingFanChart.
    """
    ft = getattr(result, "federal_tax", None)
    if ft is None:
        return None
    return {
        "ages": ft.ages,
        "tax_median": ft.tax_median,
        "forced_tax_median": ft.forced_tax_median,
        "voluntary_tax_median": ft.voluntary_tax_median,
        "gross_outflow_median": ft.gross_outflow_median,
        "median_avg_annual_tax": ft.median_avg_annual_tax,
        "median_effective_rate": ft.median_effective_rate,
        "median_worst_tax": ft.median_worst_tax,
        "median_worst_tax_age": ft.median_worst_tax_age,
        "median_total_forced_tax": ft.median_total_forced_tax,
        "median_total_voluntary_tax": ft.median_total_voluntary_tax,
    }


def _estate_cost_payload(estate_cost) -> Optional[dict]:
    """Serialize an ``EstateCostOfTaxes`` (or None) into the DECISION 9 readout
    block — one serializer for all three modes, since the deterministic single
    pair and the Historical/MC median-of-per-path-differences share the SAME
    ``firebridgelab.tax.location_risk.EstateCostOfTaxes`` dataclass. ``None`` (no split, or
    the buckets no-op) omits the block.

    Shape (the web agent builds against this verbatim):
        {
          "count": int,                     # paths covered (1 for deterministic)
          "median_estate_cost": float,      # pretax_terminal - dragged_terminal,
                                             # median across paths ("estate cost of
                                             # taxes" — DECISION 9). >= 0 for a
                                             # static-spend fixture (drag only ever
                                             # shrinks the estate there); a dynamic
                                             # strategy's two independently-spending
                                             # runs can in principle invert it locally
                                             # — the copy discloses that.
          "pretax_success_rate": float,     # 0..1: fraction of paths that never
                                             # depleted in the INDEPENDENT pre-tax world
          "dragged_success_rate": float,    # 0..1: same, in the dragged world
          "success_rate_delta": float,      # dragged_success_rate - pretax_success_rate
                                             # (typically <= 0) — paired with the cost
                                             # above so a $0 median cost when BOTH worlds
                                             # deplete can't misread as "taxes are free"
        }
    """
    if estate_cost is None:
        return None
    return {
        "count": estate_cost.count,
        "median_estate_cost": estate_cost.median_estate_cost,
        "pretax_success_rate": estate_cost.pretax_success_rate,
        "dragged_success_rate": estate_cost.dragged_success_rate,
        "success_rate_delta": estate_cost.success_rate_delta,
    }


def _deterministic_estate_cost(scenario, *, path=None):
    """The deterministic single-pair ``EstateCostOfTaxes`` (DECISION 9), or None.

    None (feature-off gate: no split, or buckets sourcing) — the SAME gate
    ``_tax_location_payload`` / ``_federal_tax_payload`` use. Per DECISION 9 the
    pre-tax counterfactual is an INDEPENDENT run (its OWN fresh spending
    strategy reacting to the untaxed balance), never the same-spend
    ``TaxLocationPath.pretax_total`` (that series is paired against the
    DRAGGED run's own spend decisions — kept for the drag-direction BINDS
    invariant, not this readout). ``simulate(scenario)`` already IS that
    independent pre-tax run over the identical deterministic blended-return
    sequence (``deterministic_blended_returns`` factors out the SAME sequence
    ``simulate`` builds internally via ``blended_real_return_at_age``) — the
    same discipline ``firebridgelab.engine.cohort_aggregate._split_cohort_step`` uses for
    Historical/MC (a separate ``_simulate_cohort`` pass, never the paired
    series). ``count == 1`` — ``aggregate_estate_cost`` degenerates correctly
    to the single-pair case.

    ``path`` (det-request memoization) — see ``_tax_location_payload``'s
    docstring; same optional precomputed-``TaxLocationPath`` contract. Does
    NOT memoize the ``simulate(scenario)`` pre-tax counterfactual below — that
    is a different, unrelated computation (not the decomposition the card
    targets) and every call site here needs its own independent pre-tax run.
    """
    from firebridgelab.engine.scenario import SOURCING_MODE_BUCKETS as _BUCKETS
    from firebridgelab.tax.location_risk import aggregate_estate_cost
    from firebridgelab.tax.location import has_tax_split
    from firebridgelab.tax.location_path import simulate_tax_location_deterministic

    if not has_tax_split(scenario) or scenario.sourcing_mode == _BUCKETS:
        return None
    if path is None:
        path = simulate_tax_location_deterministic(scenario)
    dragged_terminal = (
        path.taxable[-1] + path.tax_deferred[-1] + path.roth[-1]
        if path.ages
        else scenario.initial_portfolio
    )
    pretax_result = simulate(scenario)
    return aggregate_estate_cost(
        [pretax_result.final_portfolio],
        [dragged_terminal],
        [pretax_result.depleted_age is None],
        [path.dragged_depleted_age is None],
    )


def _worst_bucket_cohort_payload(result) -> Optional[dict]:
    """The worst-cohort 3-bucket balance series (buckets + Historical only), or None.

    bucket-strategy design "Bucket-balance visualization": Historical renders the
    WORST cohort's bucket dynamics (cash drains through the crash while growth
    rides). The engine selects + slices it (``WorstBucketCohort``, earliest forced
    growth sale); this serializes its three per-age balance lines + the label/
    annotations the UI titles the chart with (start year, first forced sale age,
    depletion age). Returns None when absent (single sourcing, or MC — synthetic
    trials have no calendar cohort to drill, consistent with the guardrails
    worst-cohort being Historical-only), so the payload omits ``worst_bucket_cohort``.
    """
    wc = getattr(result, "worst_bucket_cohort", None)
    if wc is None:
        return None
    return {
        "start_year": wc.start_year,
        "ages": wc.ages,
        "cash": wc.cash,
        "income": wc.income,
        "growth": wc.growth,
        "first_forced_sale_age": wc.first_forced_sale_age,
        "depleted_age": wc.depleted_age,
    }


def _worst_cohort_payload(result) -> Optional[dict]:
    """The worst-cohort drill-down (guardrails + historical only), or None.

    Variable-spending design slice 2: the most-squeezed cohort (lowest total real
    spend) with its year-by-year annotated path. Each path year carries the
    dollars the user thinks in (spend, begin_portfolio, gi) plus the display action
    + label (GI-driven raises distinguished from market-driven ones). Returns
    None when absent (constant/lifecycle, or MC — synthetic trials have no
    calendar year), so the payload omits ``worst_cohort``.
    """
    wc = getattr(result, "worst_cohort", None)
    if wc is None:
        return None
    return {
        "start_year": wc.start_year,
        "total_spend": wc.total_spend,
        "min_spend": wc.min_spend,
        "depleted_age": wc.depleted_age,
        "path": [
            {
                "age": y.age,
                "action": y.action,
                "gi_driven": y.gi_driven,
                "spend": y.spend,
                "begin_portfolio": y.begin_portfolio,
                "gi": y.gi,
                "label": y.label,
            }
            for y in wc.path
        ],
    }


class HistoricalPayload:
    """The Historical-mode JSON response — assembly is grouped here.

    ``from_result(scenario, result)`` returns the response dict ready to
    JSON-serialize. The class is a NAMESPACE (no instance state); the
    guardrails-conditional fields (``spending_bands`` / ``adjustment_stats`` /
    ``worst_cohort``) are gated on whether the result carries the corresponding
    fields — present iff ``scenario.spending_mode == "guardrails"`` AND the
    Historical engine populated them. The key insertion order is FIXED — the JSON
    response shape is the contract ``web/app.js`` parses, so any drift here moves
    the UI.

    Key order (the front-end doesn't depend on it, but byte-stability anchors
    against silent drift): ``scenario`` → ``bands`` → ``ending_range`` →
    ``depletion`` → ``mode`` → ``cohort_count`` → ``start_year`` → ``flows`` →
    [``spending_bands`` → ``adjustment_stats`` → ``worst_cohort`` →
    ``buffer_survival`` → ``worst_bucket_cohort`` → ``effective_equity`` →
    ``tax_location_risk`` → ``federal_tax`` → ``estate_cost_of_taxes``] →
    ``testable_since``.
    """

    @classmethod
    def from_result(cls, scenario, result) -> dict:
        ending_low, ending_high = result.ending_range
        # Build 2: per-age percentile fan + ending range + depletion + the
        # Historical-only mode/cohort/start_year identifiers. Insertion order
        # matches the pre-refactor _fan_payload + _run_trajectory order exactly
        # (byte-stable; the snapshot tests pin this).
        payload: dict = {
            "scenario": asdict(scenario),
            "bands": _fan_bands(result),
            "ending_range": {"p10": ending_low, "p90": ending_high},
            "depletion": {
                "count": result.depletion_count,
                "earliest_age": result.earliest_depletion_age,
            },
            "mode": _MODE_HISTORICAL,
            "cohort_count": result.cohort_count,
            "start_year": result.start_year,
            # Deterministic flows for the fan's flow-overlay: one
            # {age, spending, guaranteed_income} per age, from simulate(scenario)
            # (single source of truth). Always present; the front-end gates which
            # line it draws (GI always; spending in constant/lifecycle only).
            "flows": _deterministic_flows(scenario),
        }
        # Guardrails-conditional fan + readouts (variable-spending design slices
        # 1–2). Each helper returns None for constant/lifecycle and the key is
        # omitted — back-compatible with the existing front-end gates.
        spending_bands = _spending_bands(result)
        if spending_bands is not None:
            payload["spending_bands"] = spending_bands
        adjustment_stats = _adjustment_stats_payload(result)
        if adjustment_stats is not None:
            payload["adjustment_stats"] = adjustment_stats
        worst_cohort = _worst_cohort_payload(result)
        if worst_cohort is not None:
            payload["worst_cohort"] = worst_cohort
        # Buffer-survival readout (bucket-strategy design) — buckets-only, attached
        # the same conditional way as the guardrails block above. None (omitted)
        # under single sourcing, so back-compatible with the existing front-end.
        buffer_survival = _buffer_survival_payload(result)
        if buffer_survival is not None:
            payload["buffer_survival"] = buffer_survival
        # Worst-cohort 3-bucket balance series (bucket-strategy design) — buckets-only,
        # the data the bucket-balance chart draws in Historical mode. Attached the
        # same conditional way; omitted under single sourcing (back-compatible).
        worst_bucket_cohort = _worst_bucket_cohort_payload(result)
        if worst_bucket_cohort is not None:
            payload["worst_bucket_cohort"] = worst_bucket_cohort
        # Effective-equity glidepath (buckets-only) — the allocation the gap-based
        # sizing imposes. Computed off the deterministic reference (a property of the
        # setup, the same across all three modes); omitted under single sourcing.
        effective_equity = _effective_equity_payload(scenario)
        if effective_equity is not None:
            payload["effective_equity"] = effective_equity
        # Tax-location risk (tax-buckets design tb1) — single-mode-only headline
        # metric. Attached the same conditional way; omitted (back-compatible)
        # when no split is supplied. Sits before testable_since to keep that label
        # last (byte-stability of the existing tail).
        tax_location_risk = _tax_location_risk_payload(result)
        if tax_location_risk is not None:
            payload["tax_location_risk"] = tax_location_risk
        # Federal-tax overlay (federal-tax overlay design ft1) — the aggregate
        # readout (median tax series + median-of-path-scalars). Same gate as
        # tax_location_risk (omitted, back-compatible, when no split is active).
        federal_tax = _federal_tax_aggregate_payload(result)
        if federal_tax is not None:
            payload["federal_tax"] = federal_tax
        # Estate cost of taxes (tax-drag design td1, DECISION 9, td-c) — the
        # median-of-per-path-differences readout paired with the success-rate
        # delta. Same gate as tax_location_risk / federal_tax (omitted,
        # back-compatible, when no split is active).
        estate_cost = _estate_cost_payload(getattr(result, "estate_cost", None))
        if estate_cost is not None:
            payload["estate_cost_of_taxes"] = estate_cost
        # The D3 window-policy label rides at the END of the Historical payload
        # (after the optional guardrails block) — same insertion order as the
        # pre-refactor code, kept for byte-stability of the JSON response.
        payload["testable_since"] = result.testable_since
        return payload


class MonteCarloPayload:
    """The Monte-Carlo-mode JSON response — assembly is grouped here.

    ``from_result(scenario, result)`` returns the response dict ready to
    JSON-serialize. Same shape as ``HistoricalPayload`` for the fan (so the
    front-end fan renderer is reused unchanged), with two deliberate
    differences:

    - ``trial_count`` instead of ``cohort_count`` / ``start_year`` (returns are
      sampled, not replayed).
    - NO ``worst_cohort``. MC trials are synthetic (no calendar year), so the
      Historical "1969 cohort" drill-down narrative does not translate — the
      design pins MC to fan + aggregate stats and omits the worst-trial
      drill-down.

    Key order: ``scenario`` → ``bands`` → ``ending_range`` → ``depletion`` →
    ``mode`` → ``trial_count`` → ``flows`` → [``spending_bands`` →
    ``adjustment_stats`` → ``buffer_survival`` → ``effective_equity`` →
    ``tax_location_risk`` → ``federal_tax`` → ``estate_cost_of_taxes``].
    """

    @classmethod
    def from_result(cls, scenario, result) -> dict:
        ending_low, ending_high = result.ending_range
        payload: dict = {
            "scenario": asdict(scenario),
            "bands": _fan_bands(result),
            "ending_range": {"p10": ending_low, "p90": ending_high},
            "depletion": {
                "count": result.depletion_count,
                "earliest_age": result.earliest_depletion_age,
            },
            "mode": _MODE_MONTE_CARLO,
            "trial_count": result.trial_count,
            # Deterministic flows for the fan's flow-overlay: one
            # {age, spending, guaranteed_income} per age, from simulate(scenario)
            # (single source of truth, same helper Historical uses). Always
            # present; the front-end gates which line it draws (GI always;
            # spending in constant/lifecycle only).
            "flows": _deterministic_flows(scenario),
        }
        # Guardrail support in Monte Carlo (variable-spending design slice 3).
        # Same spend fan + aggregate stats as Historical (spending varies across
        # synthetic trials); back-compatible — these keys are omitted under
        # constant/lifecycle. NOTE: no worst_cohort — see class docstring.
        spending_bands = _spending_bands(result)
        if spending_bands is not None:
            payload["spending_bands"] = spending_bands
        adjustment_stats = _adjustment_stats_payload(result)
        if adjustment_stats is not None:
            payload["adjustment_stats"] = adjustment_stats
        # Buffer-survival readout (bucket-strategy design) — buckets-only, the MC
        # twin of the Historical buffer-survival block. Omitted under single
        # sourcing, so back-compatible with the existing front-end.
        buffer_survival = _buffer_survival_payload(result)
        if buffer_survival is not None:
            payload["buffer_survival"] = buffer_survival
        # Effective-equity glidepath (buckets-only) — the allocation the gap-based
        # sizing imposes, off the deterministic reference (a property of the setup).
        # Omitted under single sourcing (back-compatible).
        effective_equity = _effective_equity_payload(scenario)
        if effective_equity is not None:
            payload["effective_equity"] = effective_equity
        # Tax-location risk (tax-buckets design tb1) — the MC twin of the Historical
        # tax_location_risk block. Omitted (back-compatible) when no split.
        tax_location_risk = _tax_location_risk_payload(result)
        if tax_location_risk is not None:
            payload["tax_location_risk"] = tax_location_risk
        # Federal-tax overlay (federal-tax overlay design ft1) — the MC twin of
        # the Historical aggregate block above. Omitted (back-compatible) when
        # no split is active.
        federal_tax = _federal_tax_aggregate_payload(result)
        if federal_tax is not None:
            payload["federal_tax"] = federal_tax
        # Estate cost of taxes (tax-drag design td1, DECISION 9, td-c) — the MC
        # twin of the Historical block above. Same gate; omitted when no split
        # is active.
        estate_cost = _estate_cost_payload(getattr(result, "estate_cost", None))
        if estate_cost is not None:
            payload["estate_cost_of_taxes"] = estate_cost
        return payload


class DeterministicPayload:
    """The Deterministic-mode JSON response — assembly is grouped here.

    ``from_result(scenario, result)`` returns the response dict ready to
    JSON-serialize, where ``result`` is a ``Trajectory`` — ``simulate(scenario)``
    for a no-split (or buckets-sourcing) scenario, byte-stable and untouched;
    for a split-active, single-sourcing scenario (tax-drag design td1 DECISION 1,
    td-c scope 2) the CALLER (``_run_trajectory``, via
    ``_dragged_deterministic_trajectory``) already swapped in the DRAGGED
    per-age total / depletion age / terminal BEFORE calling this method — this
    class never checks the split gate itself for the trajectory, it just
    serializes whatever ``Trajectory`` it's handed. Like ``HistoricalPayload`` /
    ``MonteCarloPayload`` this is a NAMESPACE (no instance state); it gives the
    deterministic branch the same serializer seam the two stochastic modes
    already have, so all three modes dispatch through a single
    ``Payload.from_result`` call.

    The shape is unrelated to the fan family (no bands / depletion fan), but the
    buckets-only / tax-location conditional fields are gated the same way the
    stochastic payloads gate ``buffer_survival`` — present only when the relevant
    mode is active, omitted (back-compatible) otherwise.

    Key order (byte-stable — the snapshot tests pin this): ``mode`` →
    ``scenario`` → ``trajectory`` → [``bucket_balances`` → ``effective_equity`` →
    ``tax_location`` → ``federal_tax`` → ``estate_cost_of_taxes``] →
    [``effective_spending_floor`` → ``effective_spending_ceiling``].

    ``effective_spending_floor`` / ``effective_spending_ceiling`` are STRESS-only
    extras (the API passes them only on the stress branch): the post-transform
    spending rails the run clamps against, so the spending-chart guide line can
    label the floor for the world being shown rather than the live UI knob. They
    are APPENDED last and only when supplied, so the ordinary deterministic
    payload (and its byte-stable canary) is unchanged.
    """

    @classmethod
    def from_result(
        cls,
        scenario,
        result,
        effective_spending_floor=None,
        effective_spending_ceiling=None,
        tax_location_path=None,
    ) -> dict:
        # det-request memoization: ``tax_location_path`` is the OPTIONAL
        # precomputed ``TaxLocationPath`` the caller (``_run_trajectory``'s
        # dragged-trajectory branch) already resolved for this request. Any
        # OTHER caller (the stress branch above, or a future one) that hasn't
        # precomputed it gets it resolved HERE, once, under the same
        # has_tax_split + non-buckets gate every block below already checks —
        # so the three block builders share ONE decomposition instead of each
        # independently re-running it (was up to 4x per request).
        if tax_location_path is None and scenario.sourcing_mode != SOURCING_MODE_BUCKETS:
            from firebridgelab.tax.location import has_tax_split
            from firebridgelab.tax.location_path import simulate_tax_location_deterministic

            if has_tax_split(scenario):
                tax_location_path = simulate_tax_location_deterministic(scenario)
        payload: dict = {
            "mode": _MODE_DETERMINISTIC,
            "scenario": asdict(scenario),
            "trajectory": {
                "points": [asdict(p) for p in result.points],
                "depleted_age": result.depleted_age,
                "final_portfolio": result.final_portfolio,
            },
        }
        # Bucket-balance series (bucket-strategy design "Output") — buckets-only, the
        # deterministic 3-line chart's data. Attached the same conditional way the
        # stochastic payloads attach buffer_survival: present only when
        # sourcing_mode=buckets, omitted (back-compatible) under single sourcing.
        bucket_balances = _deterministic_bucket_balances(scenario)
        if bucket_balances is not None:
            payload["bucket_balances"] = bucket_balances
        # Effective-equity glidepath (buckets-only) — the allocation the gap-based
        # sizing imposes, computed off the deterministic reference. Present in all three
        # modes (it's a property of the setup); attached the same conditional way,
        # omitted under single sourcing (back-compatible).
        effective_equity = _effective_equity_payload(scenario)
        if effective_equity is not None:
            payload["effective_equity"] = effective_equity
        # Tax-location decomposition (tax-buckets design tb1) — single-mode-only, the
        # deterministic 3-balance series + depletion facts. Attached the same
        # conditional way; omitted (back-compatible) when no split is supplied.
        tax_location = _tax_location_payload(scenario, path=tax_location_path)
        if tax_location is not None:
            payload["tax_location"] = tax_location
        # Federal tax (federal-tax overlay design ft1; dragged under TAX_DRAG
        # td1) — single-mode-only, the deterministic per-age tax /
        # gross-outflow series + path scalars. Attached the same conditional
        # way; omitted (back-compatible) when no split is supplied.
        federal_tax = _federal_tax_payload(scenario, path=tax_location_path)
        if federal_tax is not None:
            payload["federal_tax"] = federal_tax
        # Estate cost of taxes (tax-drag design td1, DECISION 9, td-c) — the
        # deterministic single pair (pretax vs dragged terminal, paired success
        # flags). Same gate as tax_location / federal_tax above.
        estate_cost = _estate_cost_payload(
            _deterministic_estate_cost(scenario, path=tax_location_path)
        )
        if estate_cost is not None:
            payload["estate_cost_of_taxes"] = estate_cost
        # Stress-only effective spending rails (appended LAST, only when supplied —
        # the non-stress deterministic payload and its byte-stable canary are
        # untouched). The guide line reads these under stress instead of the live
        # floor knob, so it labels the floor for the perturbed world (cost_of_living_shock ×1.20).
        if effective_spending_floor is not None:
            payload["effective_spending_floor"] = effective_spending_floor
        if effective_spending_ceiling is not None:
            payload["effective_spending_ceiling"] = effective_spending_ceiling
        return payload


def _validate_correlation(correlation: Optional[float]) -> None:
    """422 if an explicit correlation is out of the valid [-0.99, +0.99] range.

    Exactly +/-1 is rank-deficient (np.linalg.cholesky raises), so an in-range
    `correlation = 1.0` would 500 the sampler — clamp the guard to
    |correlation| > CORRELATION_LIMIT (0.99). `None` (param absent) is fine; the
    scenario default applies. Only the Monte Carlo engine reads correlation, but
    we validate it whenever it's supplied (cheap, and keeps the 422 discipline
    uniform across the modes that ignore it).
    """
    if correlation is None:
        return
    if abs(correlation) > CORRELATION_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"correlation must be between -{CORRELATION_LIMIT} and "
                f"{CORRELATION_LIMIT} (got {correlation})"
            ),
        )


def _validate_taxable_gains_fraction(taxable_gains_fraction: Optional[float]) -> None:
    """422 if an explicit taxable_gains_fraction is out of [0, 1] (federal-tax
    overlay design ft1, DECISION 4). ``None`` (param absent) is fine; the
    scenario default (0.5) applies. Same idiom as ``_validate_correlation``.
    """
    if taxable_gains_fraction is None:
        return
    if not (0.0 <= taxable_gains_fraction <= 1.0):
        raise HTTPException(
            status_code=422,
            detail=(
                f"taxable_gains_fraction must be between 0 and 1 "
                f"(got {taxable_gains_fraction})"
            ),
        )


def _validate_sourcing(
    sourcing_mode: str,
    spending_mode: str,
    n1: Optional[int],
    n2: Optional[int],
) -> None:
    """422 on an invalid sourcing_mode, a buckets+guardrails combination, or a
    bad N1/N2 (bucket-strategy design "Slice 1a" validation).

    Rules:
      - ``sourcing_mode`` ∈ {single, buckets}.
      - ``n1`` / ``n2`` (when supplied) are non-negative integers (FastAPI already
        coerces the GET param to int; this catches a negative). They are only
        meaningful in buckets mode, but a supplied-but-broken value 422s regardless
        (cheap, uniform with the guardrail-knob discipline).
      - **Guardrails fence**: ``sourcing_mode=buckets`` + ``spending_mode=guardrails``
        is fenced — composing bucket sourcing with the Guyton-Klinger guardrail is
        slice 2, not this slice. The engine also raises a typed
        ``BucketsGuardrailsNotComposable`` (defense-in-depth); this is the up-front
        422 with the user-facing detail.
    """
    if sourcing_mode not in _SOURCING_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"sourcing_mode must be one of {', '.join(_SOURCING_MODES)} "
                f"(got '{sourcing_mode}')"
            ),
        )
    errors = []
    if n1 is not None and n1 < 0:
        errors.append(f"bucket N1 (cash years) must be a non-negative integer (got {n1})")
    if n2 is not None and n2 < 0:
        errors.append(f"bucket N2 (income years) must be a non-negative integer (got {n2})")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    desc = _SPENDING_MODE_BY_ID.get(spending_mode)
    if (
        sourcing_mode == SOURCING_MODE_BUCKETS
        and desc is not None
        and not desc.composable_with_buckets
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Dynamic spending (guardrails / funded-ratio) and bucket sourcing "
                "don't compose yet — that's a later slice. Use constant or "
                "lifecycle spending with buckets, or single sourcing with the "
                "dynamic spending strategy."
            ),
        )


def _validate_guardrails(
    spending_mode: str,
    guard_band: Optional[float],
    guard_adjust: Optional[float],
    spending_floor: Optional[float],
    spending_ceiling: Optional[float],
) -> None:
    """422 on an out-of-range / inconsistent guardrail knob (variable-spending
    design slice 3 §"Editable guardrail knobs").

    The four Guyton-Klinger knobs are guardrails-only and user-editable from the
    Advanced section. We range-check each whenever it's supplied (omitted → the
    research default carries) and cross-check the floor/ceiling ordering. The
    knobs are read ONLY by the ``guardrails`` strategy, so they are harmless under
    constant/lifecycle — but we validate the supplied values regardless (cheap,
    keeps the 422 discipline uniform, and a supplied-but-broken value should not
    silently pass just because the active mode ignores it).

    Rules (all inclusive unless noted):
      - ``guard_band``     ∈ [0, 1)   — the symmetric band half-width. 0 disables
        the trip (no band); 1 would push the lower band to 0 and the upper to 2×,
        and ≥1 makes the lower band non-positive (a degenerate "always raise"),
        so the band width must stay strictly below 1.
      - ``guard_adjust``   ∈ [0, 1]   — the per-trip proportional nudge. 0 = no
        nudge (constant-equivalent); 1 = a full jump to the rail. Outside that the
        multiply ``(1 ± adjust)`` is nonsensical (negative spend / >100% cut).
      - ``spending_floor`` ≥ 0        — the operational real-dollar floor (default
        $85k — "we don't dip below this").
      - ``spending_ceiling`` > ``spending_floor`` — the satiation cap must sit
        strictly above the floor (an inverted or equal pair leaves no band for
        spending to move in). When only one of the pair is supplied, the other
        falls back to its default for the comparison, so a supplied ceiling below
        the default $85k floor (or a supplied floor above the default $150k
        ceiling) is still caught.
    """
    errors = []
    if guard_band is not None and not (0.0 <= guard_band < 1.0):
        errors.append(f"guard_band must be in [0, 1) (got {guard_band})")
    if guard_adjust is not None and not (0.0 <= guard_adjust <= 1.0):
        errors.append(f"guard_adjust must be in [0, 1] (got {guard_adjust})")
    if spending_floor is not None and spending_floor < 0:
        errors.append(f"spending_floor must be >= 0 (got {spending_floor})")
    # Cross-check the rail ordering. Resolve each side against its default when not
    # supplied so a one-sided edit that crosses the other rail still 422s.
    eff_floor = spending_floor if spending_floor is not None else _GUARDRAIL_FLOOR
    eff_ceiling = (
        spending_ceiling if spending_ceiling is not None else _GUARDRAIL_CEILING
    )
    if spending_ceiling is not None or spending_floor is not None:
        if eff_ceiling <= eff_floor:
            errors.append(
                f"spending_ceiling must be greater than spending_floor "
                f"(got ceiling {eff_ceiling}, floor {eff_floor})"
            )
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


def _default_ss_fra(effective_amount: float, start_age: int) -> float:
    """The default scenario's FRA (PIA) amount, recovered from its effective
    amount at its claim age (rounded to the cent)."""
    return round(effective_amount / ss_factor(start_age), 2)


def _derive_ss(
    fra_amount: Optional[float],
    claim_age: Optional[int],
    default_fra_amount: float,
    default_claim_age: int,
) -> tuple[float, int]:
    """Derive a person's engine-facing (effective SS amount, start age) from the
    request's FRA amount + claim age.

    The request specifies SS per person as an FRA ("primary insurance amount")
    benefit + a claim age; the engine stays amount-driven, so the API derives the
    EFFECTIVE amount = ``effective_ss_amount(fra_amount, claim_age)`` and the start
    age = ``claim_age`` here. An omitted field falls back to the person's default.
    ``default_fra_amount`` is the default scenario's PIA, recovered from its
    effective amount by ``_default_ss_fra`` so a default claim age other than FRA
    is never discounted twice. An out-of-range claim age (the ``ss_factor``
    ``ValueError``) becomes a 422 with a clear message.
    """
    fra = fra_amount if fra_amount is not None else default_fra_amount
    age = claim_age if claim_age is not None else default_claim_age
    try:
        return effective_ss_amount(fra, age), age
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=engine_error_to_detail(exc))


def _build_scenario(
    *,
    spending_mode: str,
    allocation_mode: str = ALLOCATION_MODE_CONSTANT,
    sourcing_mode: str = SOURCING_MODE_SINGLE,
    bucket_n1: Optional[int] = None,
    bucket_n2: Optional[int] = None,
    guard_band: Optional[float] = None,
    guard_adjust: Optional[float] = None,
    spending_floor: Optional[float] = None,
    spending_ceiling: Optional[float] = None,
    amortization_real_return: Optional[float] = None,
    taxable_gains_fraction: Optional[float] = None,
    initial_portfolio: Optional[float],
    annual_spending: Optional[float],
    allocation: Optional[str],
    correlation: Optional[float] = None,
    start_age: Optional[int],
    end_age: Optional[int],
    person_a_pension_amount: Optional[float],
    person_a_pension_start_age: Optional[int],
    person_b_pension_amount: Optional[float],
    person_b_pension_start_age: Optional[int],
    person_a_ss_fra_amount: Optional[float],
    person_a_ss_claim_age: Optional[int],
    person_b_ss_fra_amount: Optional[float],
    person_b_ss_claim_age: Optional[int],
):
    """Resolve the scenario from optional query params over the defaults.

    Shared by `/api/trajectory` and `/api/solve` so the two endpoints construct
    their Scenario identically (same override discipline: omitted params fall
    back to `default_scenario()`, allocation parsed with the same validation).
    A malformed allocation raises HTTPException 422 via `_parse_allocation`. The
    `correlation` scalar (Monte Carlo only) overrides the scenario default when
    supplied; omitted, the Shiller default carries.
    """
    defaults = default_scenario()
    # Derive each person's engine-facing effective SS amount + start age from the
    # request's FRA amount + claim age (the engine stays amount-driven). Omitting
    # both reproduces the default effective SS.
    person_a_ss_amount, person_a_ss_start_age = _derive_ss(
        person_a_ss_fra_amount, person_a_ss_claim_age,
        _default_ss_fra(defaults.person_a_ss_amount, defaults.person_a_ss_start_age),
        defaults.person_a_ss_start_age,
    )
    person_b_ss_amount, person_b_ss_start_age = _derive_ss(
        person_b_ss_fra_amount, person_b_ss_claim_age,
        _default_ss_fra(defaults.person_b_ss_amount, defaults.person_b_ss_start_age),
        defaults.person_b_ss_start_age,
    )
    parsed_allocation = (
        _parse_allocation(allocation) if allocation is not None else None
    )
    # Glidepath on the GET path (glidepath design): GET has no per-phase allocation
    # slot (it's restricted to the legacy single allocation), so glidepath here
    # seeds EVERY derived phase from the single allocation — i.e. the degenerate
    # glide, a no-op vs constant. Real per-phase editing flows through the POST
    # body's `phase_allocations`. Resolve the boundary count off the scenario the
    # GI-age overrides imply, so the seed has one phase per derived boundary.
    phase_allocations = None
    if allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        from firebridgelab.engine.allocation import glidepath_boundaries

        seed_alloc = parsed_allocation if parsed_allocation is not None else defaults.allocation
        # Build a probe carrying the GI-age overrides so the derived phase count is
        # correct for THIS request (a GET edit to an SS claim age changes it).
        probe = replace(
            defaults,
            **{
                k: v
                for k, v in {
                    "start_age": start_age,
                    "end_age": end_age,
                    "person_a_pension_start_age": person_a_pension_start_age,
                    "person_b_pension_start_age": person_b_pension_start_age,
                    "person_a_ss_start_age": person_a_ss_start_age,
                    "person_b_ss_start_age": person_b_ss_start_age,
                }.items()
                if v is not None
            },
        )
        phase_allocations = tuple(seed_alloc for _ in glidepath_boundaries(probe))
    # Buckets on the GET path (bucket-strategy design): GET has no per-bucket
    # allocation slots (those need the POST body), so a GET buckets run uses the
    # DEFAULT bucket allocations (cash 100% short-Treasuries / income 100% agg-bonds
    # / growth 100% total-market) with optional N1/N2 overrides. Rich per-bucket
    # allocation editing flows through POST. Single mode leaves `buckets` None.
    buckets = None
    if sourcing_mode == SOURCING_MODE_BUCKETS:
        base = default_bucket_config()
        buckets = replace(
            base,
            n1=bucket_n1 if bucket_n1 is not None else base.n1,
            n2=bucket_n2 if bucket_n2 is not None else base.n2,
        )
    overrides = {
        k: v
        for k, v in {
            "spending_mode": spending_mode,
            "allocation_mode": allocation_mode,
            "phase_allocations": phase_allocations,
            "sourcing_mode": sourcing_mode,
            "buckets": buckets,
            "guard_band": guard_band,
            "guard_adjust": guard_adjust,
            "spending_floor": spending_floor,
            "spending_ceiling": spending_ceiling,
            "amortization_real_return": amortization_real_return,
            "taxable_gains_fraction": taxable_gains_fraction,
            "initial_portfolio": initial_portfolio,
            "annual_spending": annual_spending,
            "allocation": parsed_allocation,
            "correlation": correlation,
            "start_age": start_age,
            "end_age": end_age,
            "person_a_pension_amount": person_a_pension_amount,
            "person_a_pension_start_age": person_a_pension_start_age,
            "person_b_pension_amount": person_b_pension_amount,
            "person_b_pension_start_age": person_b_pension_start_age,
            "person_a_ss_amount": person_a_ss_amount,
            "person_a_ss_start_age": person_a_ss_start_age,
            "person_b_ss_amount": person_b_ss_amount,
            "person_b_ss_start_age": person_b_ss_start_age,
        }.items()
        if v is not None
    }
    return replace(defaults, **overrides)


def _parse_allocation(raw: str) -> tuple:
    """Parse a GET allocation string into a tuple of AssetClass.

    Format: ``Name:weight:real_return`` triples separated by ``;`` —
    e.g. ``Equity:0.60:0.07;Bonds:0.40:0.02``. A class may carry an OPTIONAL 4th
    field, the Monte Carlo ``volatility`` (annual real-return stdev) —
    e.g. ``Equity:0.60:0.07:0.174``. When omitted, the class's own canonical
    volatility applies (resolved via ``_canonical_volatility_by_id()``) — Deterministic/Historical strings, which carry no vol and whose
    engines ignore it, are unaffected either way.

    GET is RESTRICTED to the two legacy classes (asset-class design B4): each
    Name is mapped to its stable class id via ``_LEGACY_NAME_TO_ID``
    (``equity``→``us_total_market``, ``bonds``→``us_agg_bonds``,
    case-insensitive). A name that isn't a legacy alias is a 422 — multi-class /
    preset requests must use the POST body (which carries explicit ids). The
    parsed class's display label is the canonical label for its id.

    Validates per-class weight in [0, 1], class real_return in
    [_RETURN_MIN, _RETURN_MAX], volatility >= 0, and that the weights sum to 100%
    within 1e-6. Raises HTTPException 422 on any malformed or out-of-bounds input.
    """
    classes = []
    triples = [chunk.strip() for chunk in raw.split(";") if chunk.strip()]
    if not triples:
        raise HTTPException(
            status_code=422,
            detail="allocation must contain at least one Name:weight:real_return triple",
        )
    for chunk in triples:
        parts = chunk.split(":")
        if len(parts) not in (3, 4):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"malformed allocation entry '{chunk}' "
                    f"(expected Name:weight:real_return[:volatility])"
                ),
            )
        name = parts[0].strip()
        if not name:
            raise HTTPException(
                status_code=422,
                detail=f"malformed allocation entry '{chunk}' (empty asset-class name)",
            )
        class_id = _LEGACY_NAME_TO_ID.get(name.lower())
        if class_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"GET allocation is restricted to the legacy classes "
                    f"{sorted(_LEGACY_NAME_TO_ID)} (got '{name}'); use the POST "
                    f"body with explicit class ids for multi-class allocations"
                ),
            )
        label = _CLASS_LABELS.get(class_id, name)
        try:
            weight = float(parts[1])
            ret = float(parts[2])
            vol = float(parts[3]) if len(parts) == 4 else None
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"malformed allocation entry '{chunk}' "
                    f"(weight, real_return and volatility must be numbers)"
                ),
            )
        if not (0.0 <= weight <= 1.0):
            raise HTTPException(
                status_code=422,
                detail=f"allocation weight for '{name}' must be between 0 and 1 (got {weight})",
            )
        if not (_RETURN_MIN <= ret <= _RETURN_MAX):
            raise HTTPException(
                status_code=422,
                detail=f"allocation real_return for '{name}' must be between {_RETURN_MIN} and {_RETURN_MAX}",
            )
        if vol is not None and vol < 0:
            raise HTTPException(
                status_code=422,
                detail=f"allocation volatility for '{name}' must be >= 0 (got {vol})",
            )
        # Omitted volatility -> that class's own canonical default, resolved via
        # _canonical_volatility_by_id() (previously fell through to the
        # AssetClass dataclass default, which is the EQUITY figure for every
        # class). Construct with the resolved class id + canonical label (GET
        # names are aliased to ids above).
        if vol is None:
            resolved_vol = _canonical_volatility_by_id().get(
                class_id, _DEFAULT_EQUITY_VOLATILITY
            )
            classes.append(AssetClass(class_id, label, weight, ret, resolved_vol))
        else:
            classes.append(AssetClass(class_id, label, weight, ret, vol))

    total = sum(ac.weight for ac in classes)
    if abs(total - 1.0) > 1e-6:
        raise HTTPException(
            status_code=422,
            detail=f"allocation weights must sum to 100% (got {round(total * 100)}%)",
        )
    return tuple(classes)


def _parse_allocation_entries(entries: List[AllocationEntry]) -> tuple:
    """Parse a POST allocation (list of AllocationEntry) into a tuple of AssetClass.

    The multi-class path (asset-class design B4): each entry carries an explicit
    stable class ``id`` (never aliased — the POST contract is id-native), so this
    is NOT restricted to the two legacy classes. The display ``label`` defaults to
    the canonical label for the id, else the id itself. Same per-class bounds and
    100%-sum check as the GET parser. Raises HTTPException 422 on bad input.
    """
    if not entries:
        raise HTTPException(
            status_code=422,
            detail="allocation must contain at least one class entry",
        )
    classes = []
    for e in entries:
        class_id = e.id.strip()
        if not class_id:
            raise HTTPException(
                status_code=422,
                detail="allocation entry has an empty class id",
            )
        label = e.label if e.label else _CLASS_LABELS.get(class_id, class_id)
        if not (0.0 <= e.weight <= 1.0):
            raise HTTPException(
                status_code=422,
                detail=f"allocation weight for '{class_id}' must be between 0 and 1 (got {e.weight})",
            )
        if not (_RETURN_MIN <= e.real_return <= _RETURN_MAX):
            raise HTTPException(
                status_code=422,
                detail=f"allocation real_return for '{class_id}' must be between {_RETURN_MIN} and {_RETURN_MAX}",
            )
        if e.volatility is not None and e.volatility < 0:
            raise HTTPException(
                status_code=422,
                detail=f"allocation volatility for '{class_id}' must be >= 0 (got {e.volatility})",
            )
        if e.volatility is None:
            # Omitted volatility -> that class's own canonical default.
            # The POST id is not menu-restricted, so an unrecognized id
            # falls back to the equity dataclass default.
            resolved_vol = _canonical_volatility_by_id().get(
                class_id, _DEFAULT_EQUITY_VOLATILITY
            )
            classes.append(
                AssetClass(class_id, label, e.weight, e.real_return, resolved_vol)
            )
        else:
            classes.append(
                AssetClass(class_id, label, e.weight, e.real_return, e.volatility)
            )

    total = sum(ac.weight for ac in classes)
    if abs(total - 1.0) > 1e-6:
        raise HTTPException(
            status_code=422,
            detail=f"allocation weights must sum to 100% (got {round(total * 100)}%)",
        )
    return tuple(classes)


def _build_bucket_config_from_body(buckets: Optional[BucketsBody]) -> BucketConfig:
    """Resolve a ``BucketConfig`` from the POST ``buckets`` body over the defaults.

    bucket-strategy design: the three per-bucket allocations + N1/N2. Each supplied
    bucket allocation is parsed with the SAME ``_parse_allocation_entries`` the
    single matrix uses (id-native, 100%-sum + per-class bounds — so a bad bucket
    422s exactly like a bad single matrix). Any omitted bucket / knob falls back to
    ``default_bucket_config()`` per-field, so a partial body (e.g. only N1) still
    yields a complete, valid config. N1/N2 are already range-checked by
    ``_validate_sourcing``.
    """
    base = default_bucket_config()

    def _resolve(field: Optional[BucketBody], default_bucket: Bucket) -> Bucket:
        if field is None:
            return default_bucket
        allocation = (
            _parse_allocation_entries(field.allocation)
            if field.allocation is not None
            else default_bucket.allocation
        )
        return Bucket(
            id=default_bucket.id,
            name=field.name if field.name else default_bucket.name,
            allocation=allocation,
        )

    return BucketConfig(
        cash=_resolve(buckets.cash if buckets else None, base.cash),
        income=_resolve(buckets.income if buckets else None, base.income),
        growth=_resolve(buckets.growth if buckets else None, base.growth),
        n1=(buckets.n1 if buckets and buckets.n1 is not None else base.n1),
        n2=(buckets.n2 if buckets and buckets.n2 is not None else base.n2),
    )


def _build_scenario_from_body(body: ScenarioBody):
    """Resolve a Scenario from a POST JSON body over the defaults.

    The POST analogue of ``_build_scenario``: omitted fields fall back to
    ``default_scenario()``; ``allocation`` (if present) is parsed by
    ``_parse_allocation_entries`` (id-native, not the legacy GET alias) and
    ``correlations`` (if present) becomes the ordered N x N matrix on the
    Scenario (held as a tuple-of-tuples to keep the frozen dataclass immutable).
    The scalar ``correlation`` overrides the scenario default when supplied.
    """
    defaults = default_scenario()
    # Derive each person's engine-facing effective SS amount + start age from the
    # body's FRA amount + claim age (same derivation as the GET path; the engine
    # stays amount-driven). Omitting both reproduces the default effective SS.
    person_a_ss_amount, person_a_ss_start_age = _derive_ss(
        body.person_a_ss_fra_amount, body.person_a_ss_claim_age,
        _default_ss_fra(defaults.person_a_ss_amount, defaults.person_a_ss_start_age),
        defaults.person_a_ss_start_age,
    )
    person_b_ss_amount, person_b_ss_start_age = _derive_ss(
        body.person_b_ss_fra_amount, body.person_b_ss_claim_age,
        _default_ss_fra(defaults.person_b_ss_amount, defaults.person_b_ss_start_age),
        defaults.person_b_ss_start_age,
    )
    parsed_allocation = (
        _parse_allocation_entries(body.allocation)
        if body.allocation is not None
        else None
    )
    # Glidepath (glidepath design): when allocation_mode is "glidepath", each phase's
    # allocation is parsed with the SAME per-class validation the single matrix uses
    # (100%-sum, per-class bounds), so an invalid phase 422s exactly like a single
    # bad matrix. Under constant mode phase_allocations is ignored (left None).
    parsed_phase_allocations = None
    if body.allocation_mode == ALLOCATION_MODE_GLIDEPATH and body.phase_allocations:
        parsed_phase_allocations = tuple(
            _parse_allocation_entries(phase) for phase in body.phase_allocations
        )
        # Glidepath v1 contract (trust boundary): a glidepath varies WEIGHTS across
        # phases; the per-class real_return / volatility are a property of the class,
        # not the phase. The same id with differing return params across phases makes
        # the deterministic engine (reads each phase's own real_return) and Monte
        # Carlo (dedups the sampled union by id) silently describe different
        # portfolios. Reject it here rather than blend silently.
        seen: dict = {}
        for phase in parsed_phase_allocations:
            for ac in phase:
                params = (ac.real_return, ac.volatility)
                prior = seen.get(ac.id)
                if prior is None:
                    seen[ac.id] = params
                elif prior != params:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"class '{ac.id}' has conflicting real_return/volatility "
                            f"across glidepath phases; per-class return assumptions "
                            f"must be consistent — vary weights, not returns"
                        ),
                    )
    correlations = (
        tuple(tuple(float(x) for x in row) for row in body.correlations)
        if body.correlations is not None
        else None
    )
    # Buckets (bucket-strategy design): when sourcing_mode is "buckets", build the
    # 3-bucket config from the body (per-bucket allocations parsed with the SAME
    # 100%-sum / per-class validation the single matrix uses, defaults filling any
    # omitted bucket/knob). Under single sourcing `buckets` stays None.
    buckets = (
        _build_bucket_config_from_body(body.buckets)
        if body.sourcing_mode == SOURCING_MODE_BUCKETS
        else None
    )
    if body.bracket_fill_target is not None and body.sourcing_mode == SOURCING_MODE_BUCKETS:
        # Tax-drag design td1 (td-c): the lever is a LOGGED no-op under buckets
        # sourcing — same idiom as the whole tax-location split's buckets-mode
        # no-op log in firebridgelab.historical / firebridgelab.monte_carlo (tax drag is
        # single-mode only; never silently ignored).
        logging.getLogger(__name__).info(
            "bracket_fill_target supplied with sourcing_mode=buckets; ignoring "
            "the lever (tax drag is single-mode only — the tax-location x "
            "cash/income/growth product is a later card)"
        )
    overrides = {
        k: v
        for k, v in {
            "spending_mode": body.spending_mode,
            "allocation_mode": body.allocation_mode,
            "phase_allocations": parsed_phase_allocations,
            "sourcing_mode": body.sourcing_mode,
            "buckets": buckets,
            # Tax-location split (tax-buckets design tb1). Passed through verbatim;
            # None drops out of the override (feature OFF). resolve_split treats a
            # missing one of a supplied trio as 0.
            "taxable_balance": body.taxable_balance,
            "tax_deferred_balance": body.tax_deferred_balance,
            "roth_balance": body.roth_balance,
            # Taxable-gains fraction knob (federal-tax overlay design ft1). None
            # drops out of the override (scenario default 0.5 applies).
            "taxable_gains_fraction": body.taxable_gains_fraction,
            # Voluntary bracket-fill lever (tax-drag design td1 DECISION 5, td-c).
            # None drops out of the override (lever off). Threaded through even
            # under buckets sourcing (logged no-op above) — harmless, since the
            # engine only ever reads it from the dragged decomposition, which
            # buckets sourcing never reaches.
            "bracket_fill_target": body.bracket_fill_target,
            "guard_band": body.guard_band,
            "guard_adjust": body.guard_adjust,
            "spending_floor": body.spending_floor,
            "spending_ceiling": body.spending_ceiling,
            "amortization_real_return": body.amortization_real_return,
            "initial_portfolio": body.initial_portfolio,
            "annual_spending": body.annual_spending,
            "allocation": parsed_allocation,
            "correlation": body.correlation,
            "correlations": correlations,
            "start_age": body.start_age,
            "end_age": body.end_age,
            "person_a_pension_amount": body.person_a_pension_amount,
            "person_a_pension_start_age": body.person_a_pension_start_age,
            "person_b_pension_amount": body.person_b_pension_amount,
            "person_b_pension_start_age": body.person_b_pension_start_age,
            "person_a_ss_amount": person_a_ss_amount,
            "person_a_ss_start_age": person_a_ss_start_age,
            "person_b_ss_amount": person_b_ss_amount,
            "person_b_ss_start_age": person_b_ss_start_age,
            "annual_fee": body.annual_fee,
            "mc_distribution": body.mc_distribution,
            "mc_t_df": body.mc_t_df,
            "pension_nominal_until_start": body.pension_nominal_until_start,
            "pension_cola": body.pension_cola,
            "assumed_inflation": body.assumed_inflation,
            "survivor_age": body.survivor_age,
            "survivor_decedent": body.survivor_decedent,
            "survivor_pension_fraction": body.survivor_pension_fraction,
            "survivor_spending_fraction": body.survivor_spending_fraction,
            "guard_clamp_base": body.guard_clamp_base,
            **{name: getattr(body, name) for name in _TAX_OPTION_FIELDS},
            **{name: getattr(body, name) for name in PLAN_EXTRA_FIELDS},
        }.items()
        if v is not None
    }
    return _apply_household_body(body, replace(defaults, **overrides))


def _apply_household_body(body: ScenarioBody, scenario):
    """Thread the body's household (people, income streams, cash events,
    spending adjustments) onto the scenario. With ``people`` the horizon is
    derived from the people (``with_household``); a ``start_age`` / ``end_age``
    that disagrees with them is a 422 rather than silently overridden."""
    streams = (
        tuple(IncomeStream(**st.model_dump()) for st in body.income_streams)
        if body.income_streams is not None
        else None
    )
    events = (
        tuple(CashEvent(**ev.model_dump()) for ev in body.cash_events)
        if body.cash_events is not None
        else None
    )
    adjustments = (
        tuple(SpendingAdjustment(**adj.model_dump()) for adj in body.spending_adjustments)
        if body.spending_adjustments is not None
        else None
    )
    if body.people is not None:
        if not body.people:
            raise HTTPException(status_code=422, detail="people must list one or two people")
        people = tuple(Person(**p.model_dump()) for p in body.people)
        derived_end = plan_end_age(people)
        for field, given, derived in (
            ("start_age", body.start_age, people[0].start_age),
            ("end_age", body.end_age, derived_end),
        ):
            if given is not None and given != derived:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{field} {given} conflicts with people (derived {derived}); "
                        f"omit {field} when people is given"
                    ),
                )
        return with_household(
            scenario,
            people=people,
            income_streams=streams,
            cash_events=events,
            spending_adjustments=adjustments,
        )
    changes = {
        k: v
        for k, v in {
            "income_streams": streams,
            "cash_events": events,
            "spending_adjustments": adjustments,
        }.items()
        if v is not None
    }
    return replace(scenario, **changes) if changes else scenario


def _validate_tax_location(scenario) -> None:
    """422 if a tax-location split is supplied but doesn't sum to initial_portfolio.

    tax-buckets design tb1: the split is OFF unless supplied; when supplied (any of
    the three balances non-None), the three must sum to ``initial_portfolio``
    within $1. Delegates to ``firebridgelab.tax.location.resolve_split`` (the single
    source of the sum rule), translating its ``ValueError`` to a 422 so the same
    rule guards the engine and the API. No-op when the feature is off.
    """
    from firebridgelab.tax.location import has_tax_split, resolve_split

    if not has_tax_split(scenario):
        return
    try:
        resolve_split(scenario)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _validate_scenario_invariants(scenario) -> None:
    """422 on any engine-level Scenario construction invariant violation.

    Runs the engine's ``validate_scenario`` seam AFTER the existing ``_validate_*``
    checks, so inputs the API already rejects keep their existing 422 detail text
    (those checks fire first). This catches only invariants the API did NOT already
    cover — e.g. a glidepath with too few phases, or a buckets-mode scenario with no
    bucket config — converting what used to be a deep 500-crash into a clean 422.
    """
    try:
        validate_scenario(scenario)
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _validate(scenario) -> None:
    """Sanity-check scenario inputs; raise 422 with a clear message on failure."""
    errors = []
    if scenario.initial_portfolio <= 0:
        errors.append("initial_portfolio must be > 0")
    if scenario.annual_spending < 0:
        errors.append("annual_spending must be >= 0")
    if not scenario.allocation:
        errors.append("allocation must contain at least one asset class")
    elif not (_RETURN_MIN <= blended_real_return(scenario) <= _RETURN_MAX):
        errors.append(
            f"blended real return must be between {_RETURN_MIN} and {_RETURN_MAX}"
        )
    age_fields = (
        "start_age",
        "end_age",
        "person_a_pension_start_age",
        "person_b_pension_start_age",
        "person_a_ss_start_age",
        "person_b_ss_start_age",
    )
    for field in age_fields:
        val = getattr(scenario, field)
        if not (_AGE_MIN <= val <= _AGE_MAX):
            errors.append(f"{field} must be between {_AGE_MIN} and {_AGE_MAX}")
    if scenario.end_age <= scenario.start_age:
        errors.append("end_age must be greater than start_age")
    # Each GI amount may be zero (legitimately "not counting this stream").
    amount_fields = (
        "person_a_pension_amount",
        "person_b_pension_amount",
        "person_a_ss_amount",
        "person_b_ss_amount",
    )
    for field in amount_fields:
        if getattr(scenario, field) < 0:
            errors.append(f"{field} must be >= 0")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


# Static files served under /static/*. index.html references its assets by
# relative path, so the app also works mounted under a reverse-proxy prefix.
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
