"""Plan files: a whole plan in a small TOML file.

``load(path)`` reads a plan file and returns the ``Scenario`` it describes;
``read(path)`` returns a ``Plan``, which also carries the run settings that
are not part of a ``Scenario`` (Monte Carlo trials and seed, stress ids, a
name). ``dump(scenario_or_plan)`` writes a plan file back out, and
``load(dump(x))`` reproduces ``x`` exactly for any scenario a plan file can
express. The format, with every key, its type, default and meaning, is
documented in docs/plan-files.md.

Errors are ``PlanError`` (a ``ValueError``) and name the offending key:
unknown keys are rejected with the closest known key as a suggestion, wrong
types and out-of-range choices say what was expected, and the finished
scenario goes through ``validate_scenario``.

``apply_override(doc, "income_streams.a_ss.start_age", 70)`` edits a parsed
plan document by dotted key before it is built; the CLI's ``--set`` and
``sweep`` use it. Items in a list of tables are addressed by their ``id`` (or
their position, counting from 0); people may also be addressed as ``a`` and
``b``.
"""

from __future__ import annotations

import difflib
import json
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from firebridgelab.engine.allocation import (
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    CLASS_MENU,
    DEFAULT_CORRELATION,
    AssetClass,
    _legacy_default_volatility,
)
from firebridgelab.engine.monte_carlo import DEFAULT_N_TRIALS, DEFAULT_SEED
from firebridgelab.engine.scenario import (
    MC_DISTRIBUTIONS,
    SOURCING_MODE_BUCKETS,
    SOURCING_MODE_SINGLE,
    Bucket,
    BucketConfig,
    Scenario,
    default_bucket_config,
)
from firebridgelab.engine.validation import ScenarioValidationError, validate_scenario
from firebridgelab.income.household import (
    COLA_FULL,
    EVENT_TAX_TREATMENTS,
    OWNERS,
    PERSON_IDS,
    STREAM_COLAS,
    STREAM_KINDS,
    STREAM_TAX_TREATMENTS,
    TAX_FREE,
    CashEvent,
    IncomeStream,
    Person,
    SpendingAdjustment,
    household_people,
    legacy_income_streams,
    with_household,
)
from firebridgelab.returns.series import SERIES_FOR_CLASS

__all__ = [
    "Plan",
    "PlanError",
    "SCENARIO_FIELD_KEYS",
    "apply_override",
    "dump",
    "load",
    "loads",
    "parse_value",
    "read",
    "read_document",
    "from_document",
]


class PlanError(ValueError):
    """A plan file that cannot be read: the message names the key at fault."""


@dataclass(frozen=True)
class Plan:
    """A scenario plus the run settings a plan file carries."""

    scenario: Scenario
    name: str = ""
    description: str = ""
    trials: int = DEFAULT_N_TRIALS
    seed: int = DEFAULT_SEED
    stress: tuple[str, ...] = ()
    source: str = field(default="", compare=False)


# The two classes of the default 60/40 allocation are not in CLASS_MENU; a plan
# file gives them the same parameters default_scenario() uses.
_LEGACY_CLASSES = {
    "us_total_market": ("US Total Stock Market", 0.07),
    "us_agg_bonds": ("US Bonds (10-yr Treasury proxy)", 0.02),
}
_DEFAULT_WEIGHTS = {"us_total_market": 0.60, "us_agg_bonds": 0.40}

# Defaults for the spending rails, as a multiple of spending.annual.
FLOOR_FRACTION = 0.80
CEILING_FRACTION = 1.50

# Scenario field names (the library's vocabulary) -> plan-file keys. Used to
# point a library user at the plan-file name, and accepted by apply_override.
SCENARIO_FIELD_KEYS = {
    "initial_portfolio": "portfolio.balance",
    "taxable_balance": "portfolio.taxable",
    "tax_deferred_balance": "portfolio.tax_deferred",
    "roth_balance": "portfolio.roth",
    "taxable_gains_fraction": "portfolio.taxable_gains_fraction",
    "bracket_fill_target": "portfolio.bracket_fill_target",
    "annual_fee": "portfolio.annual_fee",
    "annual_spending": "spending.annual",
    "spending_mode": "spending.rule",
    "spending_floor": "spending.floor",
    "spending_ceiling": "spending.ceiling",
    "guard_band": "spending.guard_band",
    "guard_adjust": "spending.guard_adjust",
    "guard_clamp_base": "spending.guard_clamp_base",
    "slow_go_start_age": "spending.slow_go_start_age",
    "no_go_start_age": "spending.no_go_start_age",
    "slow_go_multiplier": "spending.slow_go_multiplier",
    "no_go_multiplier": "spending.no_go_multiplier",
    "amortization_real_return": "spending.amortization_real_return",
    "spending_real_drift": "spending.real_drift",
    "allocation_mode": "allocation.mode",
    "sourcing_mode": "allocation.sourcing",
    "correlation": "allocation.correlation",
    "correlations": "allocation.correlations",
    "phase_allocations": "allocation.phases",
    "assumed_inflation": "assumptions.inflation",
    "pension_real_drift": "assumptions.pension_real_drift",
    "survivor_age": "survivor.age",
    "survivor_decedent": "survivor.decedent",
    "survivor_spending_fraction": "survivor.spending_fraction",
    "mc_distribution": "simulation.distribution",
    "mc_t_df": "simulation.t_df",
    "n_trials": "simulation.trials",
    "trials": "simulation.trials",
    "seed": "simulation.seed",
    "start_age": "people.a.start_age",
    "end_age": "people.a.end_age",
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_REQUIRED = object()

# Every dotted key a parse asked for, with list positions dropped
# ("income_streams.kind"). Filled only when a caller passes a set (the docs test).
_KeyLog = Optional[set]


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a table"
    return type(value).__name__


def _suggest(word: str, candidates: Iterable[str]) -> str:
    match = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.6)
    return f"; did you mean '{match[0]}'?" if match else ""


class _Table:
    """A TOML table being read: typed getters, then ``done()`` rejects any key
    no getter asked for."""

    def __init__(self, data: Any, path: str, log: _KeyLog, log_path: str | None = None):
        if not isinstance(data, dict):
            raise PlanError(f"'{path}' must be a table (got {_type_name(data)})")
        self.data = data
        self.path = path
        self.log_path = path if log_path is None else log_path
        self.log = log
        self.known: list[str] = []

    def name(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def _raw(self, key: str, default: Any) -> Any:
        self.known.append(key)
        if self.log is not None:
            self.log.add(f"{self.log_path}.{key}" if self.log_path else key)
        if key in self.data:
            return self.data[key]
        if default is _REQUIRED:
            close = difflib.get_close_matches(
                key, [k for k in self.data if k not in self.known], n=1, cutoff=0.6)
            hint = f" (found '{close[0]}': a typo?)" if close else ""
            raise PlanError(f"missing required key '{self.name(key)}'{hint}")
        return default

    def num(self, key, default=_REQUIRED, *, lo=None, hi=None, integer=False):
        value = self._raw(key, default)
        if value is None or (value is default and default is not _REQUIRED):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            want = "an integer" if integer else "a number"
            raise PlanError(
                f"'{self.name(key)}' must be {want} (got {_type_name(value)}: {value!r})"
            )
        if integer:
            if isinstance(value, float):
                if not value.is_integer():
                    raise PlanError(f"'{self.name(key)}' must be a whole number (got {value!r})")
                value = int(value)
        else:
            value = float(value)
            if not math.isfinite(value):
                raise PlanError(f"'{self.name(key)}' must be finite (got {value!r})")
        if lo is not None and value < lo:
            raise PlanError(f"'{self.name(key)}' must be at least {lo} (got {value!r})")
        if hi is not None and value > hi:
            raise PlanError(f"'{self.name(key)}' must be at most {hi} (got {value!r})")
        return value

    def int(self, key, default=_REQUIRED, **kw):
        return self.num(key, default, integer=True, **kw)

    def str(self, key, default=_REQUIRED, *, choices=None):
        value = self._raw(key, default)
        if value is None or (value is default and default is not _REQUIRED):
            return value
        if not isinstance(value, str):
            raise PlanError(
                f"'{self.name(key)}' must be a string (got {_type_name(value)}: {value!r})"
            )
        if choices is not None and value not in choices:
            raise PlanError(
                f"'{self.name(key)}' must be one of {', '.join(sorted(choices))} "
                f"(got {value!r}){_suggest(value, choices)}"
            )
        return value

    def bool(self, key, default=_REQUIRED):
        value = self._raw(key, default)
        if not isinstance(value, bool):
            raise PlanError(
                f"'{self.name(key)}' must be true or false (got {_type_name(value)}: {value!r})"
            )
        return value

    def table(self, key, *, required=False) -> "_Table":
        value = self._raw(key, _REQUIRED if required else {})
        return _Table(value, self.name(key), self.log)

    def list(self, key, default=_REQUIRED):
        value = self._raw(key, default)
        if value is None or (value is default and default is not _REQUIRED):
            return value
        if not isinstance(value, list):
            raise PlanError(
                f"'{self.name(key)}' must be a list (got {_type_name(value)}: {value!r})"
            )
        return value

    def tables(self, key) -> list["_Table"]:
        """An array of tables; each item is named by its id when it has one."""
        items = self.list(key, [])
        out = []
        for i, item in enumerate(items):
            label = item.get("id") if isinstance(item, dict) else None
            where = f"{self.name(key)}[{label!r}]" if label else f"{self.name(key)}[{i}]"
            out.append(_Table(item, where, self.log, self.name(key)))
        return out

    def done(self) -> None:
        unknown = [k for k in self.data if k not in self.known]
        if not unknown:
            return
        key = unknown[0]
        if key in SCENARIO_FIELD_KEYS:
            hint = f"; the plan-file key is '{SCENARIO_FIELD_KEYS[key]}'"
        else:
            hint = _suggest(key, self.known)
        raise PlanError(f"unknown key '{self.name(key)}'{hint}")


def _class_defaults(class_id: str, where: str) -> tuple[str, float, float]:
    """(label, real_return, volatility) a class gets when the plan gives none."""
    if class_id in _LEGACY_CLASSES:
        label, ret = _LEGACY_CLASSES[class_id]
        return label, ret, _legacy_default_volatility(class_id)
    if class_id in CLASS_MENU:
        ac = CLASS_MENU[class_id]
        return ac.label, ac.real_return, ac.volatility
    if class_id in SERIES_FOR_CLASS:
        raise PlanError(
            f"{where}: asset class '{class_id}' needs optional data that is not "
            f"installed (see DATA-SOURCES.md)"
        )
    raise PlanError(
        f"{where}: unknown asset class '{class_id}'"
        f"{_suggest(class_id, SERIES_FOR_CLASS)} "
        f"(classes: {', '.join(SERIES_FOR_CLASS)})"
    )


def _weights(t: _Table, key: str, default=_REQUIRED) -> Optional[dict[str, float]]:
    raw = t._raw(key, default)
    if raw is None:
        return None
    return _weight_table(raw, t.name(key))


def _weight_table(raw: Any, where: str) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise PlanError(
            f"'{where}' must be a table of class id = weight, e.g. "
            f"{{ us_total_market = 0.6, us_agg_bonds = 0.4 }}"
        )
    out = {}
    for class_id, weight in raw.items():
        _class_defaults(class_id, where)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise PlanError(
                f"'{where}.{class_id}' must be a number (got {_type_name(weight)}: {weight!r})"
            )
        if not 0.0 <= weight <= 1.0:
            hint = " (weights are fractions: 0.6, not 60)" if weight > 1 else ""
            raise PlanError(f"'{where}.{class_id}' must be between 0 and 1{hint} (got {weight!r})")
        out[class_id] = float(weight)
    total = sum(out.values())
    if abs(total - 1.0) > 1e-6:
        raise PlanError(f"'{where}' weights must sum to 1 (got {total:.6g})")
    return out


def _class_params(t: _Table, key: str) -> dict[str, float]:
    raw = t._raw(key, {})
    if not isinstance(raw, dict):
        raise PlanError(f"'{t.name(key)}' must be a table of class id = value")
    out = {}
    for class_id, value in raw.items():
        _class_defaults(class_id, t.name(key))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlanError(
                f"'{t.name(key)}.{class_id}' must be a number (got {_type_name(value)}: {value!r})"
            )
        out[class_id] = float(value)
    return out


def _allocation(weights, returns, vols, where) -> tuple[AssetClass, ...]:
    out = []
    for class_id, weight in weights.items():
        label, ret, vol = _class_defaults(class_id, where)
        out.append(
            AssetClass(
                class_id, label, weight, returns.get(class_id, ret), vols.get(class_id, vol)
            )
        )
    return tuple(out)


def from_document(doc: dict, source: str = "<plan>", *, key_log: _KeyLog = None) -> Plan:
    """Build a ``Plan`` from a parsed plan document (the dict ``tomllib`` returns)."""
    from firebridgelab.analysis.stress import STRESS_PERTURBATIONS, STRESS_SCENARIOS
    from firebridgelab.spending.registry import registered_modes

    try:
        return _from_document(doc, source, key_log, registered_modes(),
                              tuple(STRESS_SCENARIOS) + tuple(STRESS_PERTURBATIONS))
    except PlanError as exc:
        raise PlanError(f"{source}: {exc}") from None


def _from_document(doc, source, log, spending_rules, stress_ids) -> Plan:
    top = _Table(doc, "", log)
    name = top.str("name", "")
    description = top.str("description", "")

    # People.
    people_tables = top.tables("people")
    if not people_tables:
        raise PlanError("a plan needs a [[people]] table for at least one person")
    if len(people_tables) > 2:
        raise PlanError(f"'people' lists {len(people_tables)} people; a household has one or two")
    people = []
    for i, pt in enumerate(people_tables):
        pid = pt.str("id", PERSON_IDS[i], choices=PERSON_IDS)
        if pid != PERSON_IDS[i]:
            raise PlanError(f"{pt.path}: the {('first', 'second')[i]} person's id must be "
                            f"'{PERSON_IDS[i]}' (got '{pid}')")
        people.append(
            Person(
                pid,
                pt.int("start_age", lo=18, hi=110),
                pt.int("end_age", lo=18, hi=120),
                pt.str("name", ""),
            )
        )
        pt.done()
    owners = OWNERS if len(people) == 2 else ("a", "household")

    streams = []
    for st in top.tables("income_streams"):
        streams.append(
            IncomeStream(
                id=st.str("id"),
                kind=st.str("kind", choices=STREAM_KINDS),
                amount=st.num("amount", lo=0),
                start_age=st.int("start_age"),
                owner=st.str("owner", "a", choices=owners),
                end_age=st.int("end_age", None),
                cola=st.str("cola", COLA_FULL, choices=STREAM_COLAS),
                cola_rate=st.num("cola_rate", 0.0),
                nominal_until_start=st.bool("nominal_until_start", False),
                survivor_fraction=st.num("survivor_fraction", None, lo=0, hi=1),
                tax_treatment=st.str("tax_treatment", None, choices=STREAM_TAX_TREATMENTS),
                label=st.str("label", ""),
            )
        )
        st.done()

    events = []
    for et in top.tables("cash_events"):
        events.append(
            CashEvent(
                age=et.int("age"),
                amount=et.num("amount"),
                tax_treatment=et.str("tax_treatment", TAX_FREE, choices=EVENT_TAX_TREATMENTS),
                basis_fraction=et.num("basis_fraction", 0.0, lo=0, hi=1),
                owner=et.str("owner", "a", choices=owners),
                id=et.str("id", ""),
                label=et.str("label", ""),
            )
        )
        et.done()

    adjustments = []
    for at in top.tables("spending_adjustments"):
        adjustments.append(
            SpendingAdjustment(
                amount=at.num("amount", lo=0),
                start_age=at.int("start_age"),
                end_age=at.int("end_age"),
                owner=at.str("owner", "a", choices=owners),
                survivor_fraction=at.num("survivor_fraction", 1.0, lo=0, hi=1),
                id=at.str("id", ""),
                label=at.str("label", ""),
            )
        )
        at.done()

    # Portfolio and tax split.
    pt = top.table("portfolio", required=True)
    taxable = pt.num("taxable", None, lo=0)
    tax_deferred = pt.num("tax_deferred", None, lo=0)
    roth = pt.num("roth", None, lo=0)
    split = [v for v in (taxable, tax_deferred, roth) if v is not None]
    balance = pt.num("balance", sum(split) if split else _REQUIRED, lo=0)
    gains_fraction = pt.num("taxable_gains_fraction", 0.5, lo=0, hi=1)
    bracket_fill = pt.num("bracket_fill_target", None, lo=0)
    fee = pt.num("annual_fee", 0.0, lo=0, hi=0.03)
    pt.done()

    # Allocation.
    al = top.table("allocation")
    alloc_mode = al.str("mode", ALLOCATION_MODE_CONSTANT,
                        choices=(ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH))
    sourcing = al.str("sourcing", SOURCING_MODE_SINGLE,
                      choices=(SOURCING_MODE_SINGLE, SOURCING_MODE_BUCKETS))
    weights = _weights(al, "weights", None)
    returns = _class_params(al, "real_return")
    vols = _class_params(al, "volatility")
    for class_id, v in vols.items():
        if v < 0:
            raise PlanError(f"'allocation.volatility.{class_id}' must be >= 0 (got {v!r})")
    raw_phases = al.list("phases", None)
    correlation = al.num("correlation", DEFAULT_CORRELATION, lo=-0.99, hi=0.99)
    raw_corr = al.list("correlations", None)
    al.done()

    phases = None
    if raw_phases is not None:
        if not raw_phases:
            raise PlanError("'allocation.phases' must list at least one phase")
        phases = tuple(
            _allocation(_weight_table(ph, f"allocation.phases[{i}]"), returns, vols,
                        f"allocation.phases[{i}]")
            for i, ph in enumerate(raw_phases)
        )
    if alloc_mode == ALLOCATION_MODE_GLIDEPATH and phases is None:
        raise PlanError("allocation.mode is 'glidepath' but 'allocation.phases' is missing")
    if alloc_mode == ALLOCATION_MODE_CONSTANT and phases is not None:
        raise PlanError("'allocation.phases' is set but allocation.mode is 'constant'; "
                        "set mode = \"glidepath\" or remove phases")
    if weights is None:
        allocation = phases[0] if phases else _allocation(_DEFAULT_WEIGHTS, returns, vols,
                                                          "allocation")
    else:
        allocation = _allocation(weights, returns, vols, "allocation.weights")
    correlations = None
    if raw_corr is not None:
        try:
            correlations = tuple(tuple(float(x) for x in row) for row in raw_corr)
        except (TypeError, ValueError):
            raise PlanError("'allocation.correlations' must be a list of lists of numbers")
        if any(isinstance(x, bool) for row in raw_corr for x in row):
            raise PlanError("'allocation.correlations' must be a list of lists of numbers")

    # Buckets.
    bt = top.table("buckets")
    defaults = default_bucket_config()
    n1 = bt.int("n1", defaults.n1, lo=0)
    n2 = bt.int("n2", defaults.n2, lo=0)
    bucket_weights = {b: _weights(bt, b, None) for b in ("cash", "income", "growth")}
    bt.done()
    buckets = None
    if sourcing == SOURCING_MODE_BUCKETS:
        made = {}
        for b in ("cash", "income", "growth"):
            dflt = getattr(defaults, b)
            if bucket_weights[b] is None:
                made[b] = dflt
            else:
                made[b] = Bucket(dflt.id, dflt.name,
                                 _allocation(bucket_weights[b], returns, vols, f"buckets.{b}"))
        buckets = BucketConfig(made["cash"], made["income"], made["growth"], n1, n2)
    elif bt.data:
        raise PlanError("a [buckets] table is set but allocation.sourcing is 'single'; "
                        "set sourcing = \"buckets\" or remove [buckets]")

    # Spending.
    sp = top.table("spending", required=True)
    annual = sp.num("annual", lo=0)
    rule = sp.str("rule", "constant", choices=spending_rules)
    floor = sp.num("floor", FLOOR_FRACTION * annual, lo=0)
    ceiling = sp.num("ceiling", CEILING_FRACTION * annual, lo=0)
    spending = dict(
        guard_band=sp.num("guard_band", 0.20),
        guard_adjust=sp.num("guard_adjust", 0.10),
        guard_clamp_base=sp.bool("guard_clamp_base", False),
        slow_go_start_age=sp.int("slow_go_start_age", 75),
        no_go_start_age=sp.int("no_go_start_age", 85),
        slow_go_multiplier=sp.num("slow_go_multiplier", 0.85, lo=0),
        no_go_multiplier=sp.num("no_go_multiplier", 0.75, lo=0),
        amortization_real_return=sp.num("amortization_real_return", 0.01),
        spending_real_drift=sp.num("real_drift", 0.0),
    )
    sp.done()

    asm = top.table("assumptions")
    inflation = asm.num("inflation", 0.025)
    pension_drift = asm.num("pension_real_drift", 0.0)
    asm.done()

    sv = top.table("survivor")
    survivor = {}
    if sv.data:
        survivor = dict(
            survivor_decedent=sv.str("decedent", choices=PERSON_IDS),
            survivor_age=sv.int("age"),
            survivor_spending_fraction=sv.num("spending_fraction", 0.75, lo=0, hi=1),
        )
    else:
        sv.str("decedent", None)
        sv.int("age", None)
        sv.num("spending_fraction", None)
    sv.done()

    sim = top.table("simulation")
    trials = sim.int("trials", DEFAULT_N_TRIALS, lo=1)
    seed = sim.int("seed", DEFAULT_SEED, lo=0)
    distribution = sim.str("distribution", "normal", choices=MC_DISTRIBUTIONS)
    t_df = sim.num("t_df", 5.0)
    stress = sim.list("stress", [])
    for sid in stress:
        if sid not in stress_ids:
            raise PlanError(
                f"'simulation.stress' has unknown stress id {sid!r}{_suggest(str(sid), stress_ids)} "
                f"(ids: {', '.join(stress_ids)})"
            )
    sim.done()
    top.done()

    a = people[0]
    scenario = Scenario(
        start_age=a.start_age,
        end_age=a.end_age,
        initial_portfolio=balance,
        allocation=allocation,
        annual_spending=annual,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=a.start_age,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=a.start_age,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=a.start_age,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=a.start_age,
        spending_mode=rule,
        spending_floor=floor,
        spending_ceiling=ceiling,
        correlation=correlation,
        correlations=correlations,
        allocation_mode=alloc_mode,
        phase_allocations=phases,
        sourcing_mode=sourcing,
        buckets=buckets,
        taxable_balance=taxable,
        tax_deferred_balance=tax_deferred,
        roth_balance=roth,
        taxable_gains_fraction=gains_fraction,
        bracket_fill_target=bracket_fill,
        annual_fee=fee,
        mc_distribution=distribution,
        mc_t_df=t_df,
        assumed_inflation=inflation,
        pension_real_drift=pension_drift,
        **spending,
        **survivor,
    )
    scenario = with_household(
        scenario,
        people=people,
        income_streams=streams,
        cash_events=events,
        spending_adjustments=adjustments,
    )
    try:
        validate_scenario(scenario)
    except ScenarioValidationError as exc:
        raise PlanError(f"invalid plan: {exc}") from None
    return Plan(scenario, name, description, trials, seed, tuple(stress), source)


def read_document(path: Union[str, Path]) -> dict:
    """Parse a plan file into a plain dict, without building it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{path}: cannot read plan file ({exc.strerror or exc})") from None
    return _parse_toml(text, str(path))


def _parse_toml(text: str, source: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"{source}: not valid TOML: {exc}") from None


def loads(text: str, source: str = "<string>", overrides: Iterable = ()) -> Plan:
    """Parse plan-file text into a ``Plan``. ``overrides`` are ``(key, value)`` pairs."""
    doc = _parse_toml(text, source)
    for key, value in overrides:
        apply_override(doc, key, value)
    return from_document(doc, source)


def read(path: Union[str, Path], overrides: Iterable = ()) -> Plan:
    """Read a plan file into a ``Plan`` (scenario plus run settings)."""
    doc = read_document(path)
    for key, value in overrides:
        apply_override(doc, key, value)
    return from_document(doc, str(path))


def load(path: Union[str, Path]) -> Scenario:
    """Read a plan file and return its ``Scenario``."""
    return read(path).scenario


# ---------------------------------------------------------------------------
# Overrides (--set and sweep)
# ---------------------------------------------------------------------------


def parse_value(raw: str) -> Any:
    """A command-line value: a TOML literal (``60000``, ``0.5``, ``true``,
    ``"x"``, ``[1, 2]``, ``{ a = 1 }``), else the bare text as a string."""
    raw = raw.strip()
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def apply_override(doc: dict, key: str, value: Any) -> None:
    """Set dotted ``key`` in a parsed plan document, creating tables as needed.

    A segment that meets a list of tables picks the item whose ``id`` matches,
    else the item at that position; ``people.a`` / ``people.b`` also work when
    the people have no explicit ids. A library field name such as
    ``annual_spending`` is translated to its plan key (``spending.annual``).
    """
    key = SCENARIO_FIELD_KEYS.get(key, key)
    parts = key.split(".")
    if not all(parts):
        raise PlanError(f"bad key '{key}': empty segment")
    node: Any = doc
    walked = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(node, list):
            node = _list_item(node, part, ".".join(walked))
            walked.append(part)
            if last:
                raise PlanError(f"'{key}' names a whole item; set one of its keys instead")
            continue
        if not isinstance(node, dict):
            raise PlanError(f"cannot set '{key}': '{'.'.join(walked)}' is not a table")
        if last:
            node[part] = value
            return
        if part not in node:
            node[part] = {}
        node = node[part]
        walked.append(part)


def _list_item(items: list, part: str, where: str) -> dict:
    for item in items:
        if isinstance(item, dict) and item.get("id") == part:
            return item
    if where == "people" and part in PERSON_IDS:
        idx = PERSON_IDS.index(part)
        if idx < len(items):
            return items[idx]
    if part.isdigit() and int(part) < len(items):
        return items[int(part)]
    ids = [str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")]
    raise PlanError(
        f"'{where}' has no item '{part}'{_suggest(part, ids)}"
        + (f" (ids: {', '.join(ids)})" if ids else "")
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Whole-dollar amounts read better as 1_800_000; the loader takes an
        # integer wherever it takes a number.
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):_}" if abs(value) >= 10_000 else str(int(value))
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_fmt(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {value!r} to a plan file")


def _section(lines: list[str], header: str, items: list[tuple[str, Any]]) -> None:
    lines.append("")
    lines.append(header)
    for key, value in items:
        if value is not None:
            lines.append(f"{key} = {_fmt(value)}")


def _alloc_weights(allocation) -> dict[str, float]:
    return {ac.id: ac.weight for ac in allocation}


def dump(obj: Union[Scenario, Plan]) -> str:
    """Write a scenario (or a whole plan) as plan-file TOML, every value explicit.

    A scenario built on the legacy ``person_*`` fields is written with its
    household made explicit (``legacy_income_streams``), which runs the same
    plan; Social Security comes back as an FRA amount rounded to the cent.
    Display labels on asset classes are not written.
    """
    plan = obj if isinstance(obj, Plan) else Plan(obj)
    s = plan.scenario
    lines = ["# firebridgelab plan file (docs/plan-files.md)."]
    if plan.name:
        lines.append(f"name = {_fmt(plan.name)}")
    if plan.description:
        lines.append(f"description = {_fmt(plan.description)}")

    people = household_people(s)
    for p in people:
        _section(lines, "[[people]]", [("id", p.id), ("name", p.name or None),
                                       ("start_age", p.start_age), ("end_age", p.end_age)])

    streams = s.income_streams if s.income_streams is not None else legacy_income_streams(s)
    for st in streams:
        _section(lines, "[[income_streams]]", [
            ("id", st.id), ("kind", st.kind), ("owner", st.owner), ("amount", st.amount),
            ("start_age", st.start_age), ("end_age", st.end_age), ("cola", st.cola),
            ("cola_rate", st.cola_rate if st.cola_rate else None),
            ("nominal_until_start", st.nominal_until_start or None),
            ("survivor_fraction", st.survivor_fraction),
            ("tax_treatment", st.tax_treatment), ("label", st.label or None),
        ])
    for ev in s.cash_events:
        _section(lines, "[[cash_events]]", [
            ("id", ev.id or None), ("owner", ev.owner), ("age", ev.age), ("amount", ev.amount),
            ("tax_treatment", ev.tax_treatment),
            ("basis_fraction", ev.basis_fraction if ev.basis_fraction else None),
            ("label", ev.label or None),
        ])
    for adj in s.spending_adjustments:
        _section(lines, "[[spending_adjustments]]", [
            ("id", adj.id or None), ("owner", adj.owner), ("amount", adj.amount),
            ("start_age", adj.start_age), ("end_age", adj.end_age),
            ("survivor_fraction", adj.survivor_fraction), ("label", adj.label or None),
        ])

    _section(lines, "[portfolio]", [
        ("balance", s.initial_portfolio), ("taxable", s.taxable_balance),
        ("tax_deferred", s.tax_deferred_balance), ("roth", s.roth_balance),
        ("taxable_gains_fraction", s.taxable_gains_fraction),
        ("bracket_fill_target", s.bracket_fill_target), ("annual_fee", s.annual_fee),
    ])

    # Per-class return/volatility overrides: only where they differ from the
    # class default, so the file shows what was chosen.
    classes: dict[str, AssetClass] = {}
    groups = [s.allocation] + list(s.phase_allocations or ())
    if s.sourcing_mode == SOURCING_MODE_BUCKETS and s.buckets is not None:
        groups += [s.buckets.cash.allocation, s.buckets.income.allocation,
                   s.buckets.growth.allocation]
    for group in groups:
        for ac in group:
            classes.setdefault(ac.id, ac)
    returns, vols = {}, {}
    for cid, ac in classes.items():
        _, ret, vol = _class_defaults(cid, "allocation")
        if ac.real_return != ret:
            returns[cid] = ac.real_return
        if ac.volatility != vol:
            vols[cid] = ac.volatility
    _section(lines, "[allocation]", [
        ("mode", s.allocation_mode), ("sourcing", s.sourcing_mode),
        ("weights", _alloc_weights(s.allocation)),
        ("phases", [_alloc_weights(ph) for ph in s.phase_allocations]
         if s.phase_allocations else None),
        ("real_return", returns or None), ("volatility", vols or None),
        ("correlation", s.correlation),
        ("correlations", [list(r) for r in s.correlations] if s.correlations else None),
    ])
    if s.sourcing_mode == SOURCING_MODE_BUCKETS and s.buckets is not None:
        b = s.buckets
        _section(lines, "[buckets]", [
            ("n1", b.n1), ("n2", b.n2), ("cash", _alloc_weights(b.cash.allocation)),
            ("income", _alloc_weights(b.income.allocation)),
            ("growth", _alloc_weights(b.growth.allocation)),
        ])

    _section(lines, "[spending]", [
        ("annual", s.annual_spending), ("rule", s.spending_mode),
        ("floor", s.spending_floor), ("ceiling", s.spending_ceiling),
        ("guard_band", s.guard_band), ("guard_adjust", s.guard_adjust),
        ("guard_clamp_base", s.guard_clamp_base),
        ("slow_go_start_age", s.slow_go_start_age), ("no_go_start_age", s.no_go_start_age),
        ("slow_go_multiplier", s.slow_go_multiplier), ("no_go_multiplier", s.no_go_multiplier),
        ("amortization_real_return", s.amortization_real_return),
        ("real_drift", s.spending_real_drift),
    ])
    _section(lines, "[assumptions]", [
        ("inflation", s.assumed_inflation), ("pension_real_drift", s.pension_real_drift),
    ])
    if s.survivor_age is not None:
        _section(lines, "[survivor]", [
            ("decedent", s.survivor_decedent), ("age", s.survivor_age),
            ("spending_fraction", s.survivor_spending_fraction),
        ])
    _section(lines, "[simulation]", [
        ("trials", plan.trials), ("seed", plan.seed), ("distribution", s.mc_distribution),
        ("t_df", s.mc_t_df), ("stress", list(plan.stress)),
    ])
    return "\n".join(lines) + "\n"
