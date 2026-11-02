"""Scenario construction invariants: ``validate_scenario`` and its error type.

Checks a ``Scenario``'s modes, magnitudes, ages, allocation, rails, strategy
configuration and tax split, raising ``ScenarioValidationError`` (a
``ValueError``) on the first violation with a message naming the field. Also
holds ``validate_glidepath_class_consistency``.
"""

from __future__ import annotations

from firebridgelab.engine.allocation import ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH
from firebridgelab.engine.scenario import (
    MC_DISTRIBUTIONS,
    SOURCING_MODE_BUCKETS,
    SOURCING_MODE_SINGLE,
    Scenario,
)
from firebridgelab.income.household import (
    COLA_FIXED,
    COLA_FULL,
    EVENT_TAX_TREATMENTS,
    KIND_SOCIAL_SECURITY,
    OWNER_HOUSEHOLD,
    PERSON_IDS,
    STREAM_COLAS,
    STREAM_KINDS,
    STREAM_TAX_TREATMENTS,
    TAX_FREE,
    TAX_SOCIAL_SECURITY,
    household_people,
    plan_end_age,
    stream_tax_treatment,
)
from firebridgelab.income.pension import PENSION_COLAS
from firebridgelab.income.social_security import MAX_CLAIM_AGE, MIN_CLAIM_AGE
from firebridgelab.income.survivor import SURVIVOR_DECEDENTS
from firebridgelab.spending.registry import registered_modes


# Recognized allocation / sourcing modes. Spending modes are whatever the
# spending registry has (``registered_modes()``).
_VALID_ALLOCATION_MODES = frozenset(
    {ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH}
)
_VALID_SOURCING_MODES = frozenset({SOURCING_MODE_SINGLE, SOURCING_MODE_BUCKETS})

# Allocation-weight sum tolerance for validate_scenario. Matches the API's
# _parse_allocation 100%-sum check (1e-6); deliberately NOT stricter than the
# default scenario / existing fixtures satisfy (all sum to 1.0 exactly). The
# Historical / Monte Carlo engines RENORMALIZE weights, so this is a sanity guard
# against a clearly-broken allocation, not a hard engine precondition.
_ALLOCATION_SUM_TOLERANCE = 1e-6

# Permissive age sanity bounds for validate_scenario. Intentionally WIDER than the
# API's editorial 18..110 window (api.main._AGE_MIN/_AGE_MAX) — this is the
# low-layer "is this age a plausible integer, not garbage" guard, not the product
# range. Anything the API admits already sits inside this; tests that construct
# extreme horizons (e.g. start_age=0/end_age=199) drive the engine DIRECTLY and
# never flow through validate_scenario, so they are unaffected.
_VALIDATE_AGE_MIN = 0
_VALIDATE_AGE_MAX = 150


class ScenarioValidationError(ValueError):
    """A Scenario violates a construction invariant.

    Raised by ``validate_scenario`` on the FIRST violated invariant, with a
    specific, named-field message. Subclasses ``ValueError`` so existing
    engine-error → 422 translation paths (which catch ``ValueError``) keep
    working if a caller routes it through them.
    """


def validate_scenario(scenario: Scenario) -> None:
    """Validate a Scenario's construction invariants; raise on the first failure.

    Makes the preconditions the engine relies on explicit, so a malformed
    scenario fails here with a ``ScenarioValidationError`` naming the offending
    field instead of crashing deep and opaquely (e.g. an ``AttributeError`` on
    ``scenario.buckets.n1`` when buckets is ``None`` in buckets mode).

    Pure (no I/O). Uses a function-local import of the tax-split sum rule
    (``tax.location.resolve_split``) to reuse its single source of truth. It
    does NOT import higher engine layers.

    ``validate_scenario(default_scenario())`` passes. At the API boundary it runs
    AFTER the API's own ``_validate_*`` checks, so inputs the API already rejects
    keep their API message.
    """
    # --- Recognized mode constants -------------------------------------------
    if scenario.spending_mode not in registered_modes():
        raise ScenarioValidationError(
            f"spending_mode '{scenario.spending_mode}' is not a recognized "
            f"spending mode"
        )
    if scenario.allocation_mode not in _VALID_ALLOCATION_MODES:
        raise ScenarioValidationError(
            f"allocation_mode '{scenario.allocation_mode}' is not a recognized "
            f"allocation mode"
        )
    if scenario.sourcing_mode not in _VALID_SOURCING_MODES:
        raise ScenarioValidationError(
            f"sourcing_mode '{scenario.sourcing_mode}' is not a recognized "
            f"sourcing mode"
        )

    # --- Portfolio / spending magnitudes -------------------------------------
    if scenario.initial_portfolio <= 0:
        raise ScenarioValidationError(
            f"initial_portfolio must be > 0 (got {scenario.initial_portfolio})"
        )
    if scenario.annual_spending < 0:
        raise ScenarioValidationError(
            f"annual_spending must be >= 0 (got {scenario.annual_spending})"
        )

    # --- Horizon / ages ------------------------------------------------------
    age_fields = (
        "start_age",
        "end_age",
        "person_a_pension_start_age",
        "person_b_pension_start_age",
        "person_a_ss_start_age",
        "person_b_ss_start_age",
    )
    for name in age_fields:
        value = getattr(scenario, name)
        if not (_VALIDATE_AGE_MIN <= value <= _VALIDATE_AGE_MAX):
            raise ScenarioValidationError(
                f"{name} must be between {_VALIDATE_AGE_MIN} and "
                f"{_VALIDATE_AGE_MAX} (got {value})"
            )
    if scenario.start_age >= scenario.end_age:
        raise ScenarioValidationError(
            f"start_age ({scenario.start_age}) must be less than end_age "
            f"({scenario.end_age})"
        )

    # --- Allocation ----------------------------------------------------------
    if not scenario.allocation:
        raise ScenarioValidationError(
            "allocation must contain at least one asset class"
        )
    weight_sum = sum(ac.weight for ac in scenario.allocation)
    if abs(weight_sum - 1.0) > _ALLOCATION_SUM_TOLERANCE:
        raise ScenarioValidationError(
            f"allocation weights must sum to 1.0 within "
            f"{_ALLOCATION_SUM_TOLERANCE:g} (got {weight_sum})"
        )

    # --- Spending rails ------------------------------------------------------
    # The operational floor must not exceed the satiation ceiling, or the
    # guardrail / funded-ratio clamps have an inverted band. Checked
    # unconditionally (cheap, and a supplied-but-broken pair shouldn't pass just
    # because the active spending mode ignores the rails).
    if scenario.spending_floor > scenario.spending_ceiling:
        raise ScenarioValidationError(
            f"spending_floor ({scenario.spending_floor}) must be <= "
            f"spending_ceiling ({scenario.spending_ceiling})"
        )

    # --- Real-drift knobs ---------------------------------------------------
    # Sanity bounds on the per-year real-drift rates: a plausible annual real
    # erosion/growth rate, not garbage. ±50% bounds the COLA-freeze / cost-of-
    # living drift well outside any real stress (the shipped stresses use 0.025
    # and 0.015). Default 0.0 always passes.
    for name in ("pension_real_drift", "spending_real_drift"):
        value = getattr(scenario, name)
        if not (-0.5 <= value <= 0.5):
            raise ScenarioValidationError(
                f"{name} must be between -0.5 and 0.5 (got {value})"
            )

    # --- Amortization-frontier planning return -------------------------------
    # The frontier discounts the gap stream at this real return. A value <= -1.0
    # makes the discount factor (1 + r) non-positive (division blows up); the upper
    # bound mirrors the drift knobs' plausible-real-return window. The slider only
    # ever sends [0, allocation-return]; bound it permissively here so garbage 422s
    # cleanly. Default 0.01 always passes.
    if not (-0.5 <= scenario.amortization_real_return <= 0.5):
        raise ScenarioValidationError(
            f"amortization_real_return must be between -0.5 and 0.5 (got "
            f"{scenario.amortization_real_return})"
        )

    # --- Bucket sourcing -----------------------------------------------------
    # buckets mode reads scenario.buckets.{n1,n2,...} unconditionally; a None
    # config would otherwise be an opaque deep AttributeError.
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        if scenario.buckets is None:
            raise ScenarioValidationError(
                "sourcing_mode is 'buckets' but no bucket config was provided "
                "(buckets is None)"
            )
        if scenario.buckets.n1 < 0 or scenario.buckets.n2 < 0:
            raise ScenarioValidationError(
                f"bucket sizing years must be non-negative (got n1="
                f"{scenario.buckets.n1}, n2={scenario.buckets.n2})"
            )

    # --- Glidepath allocation ------------------------------------------------
    # glidepath mode maps each DERIVED phase to phase_allocations by index. The
    # engine CLAMPS a SHORT list to the last supplied phase rather than crashing
    # (see allocation_at_age), so the phase COUNT is intentionally NOT validated
    # here — a short list is a legal run. Only two things are enforced: the list
    # must be PRESENT + NON-EMPTY (a MISSING / EMPTY phase_allocations under
    # glidepath mode would silently degrade to constant), and no
    # individual phase may be empty (an empty phase tuple crashes the engine with
    # "no asset class maps to a known historical series").
    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        if not scenario.phase_allocations:
            raise ScenarioValidationError(
                "allocation_mode is 'glidepath' but no phase_allocations were "
                "provided"
            )
        for i, phase in enumerate(scenario.phase_allocations):
            if not phase:
                raise ScenarioValidationError(
                    f"glidepath phase_allocations[{i}] is empty; each phase must "
                    f"contain at least one asset class"
                )

    # --- Tax-location split --------------------------------------------------
    # When ANY of the three tax-location balances is supplied, all three are
    # treated as supplied (missing → 0) and must sum to initial_portfolio within
    # tolerance. Reuse the canonical sum rule from tax.location via a
    # function-local import; this keeps the $1 tolerance single-sourced.
    from firebridgelab.tax.location import has_tax_split, resolve_split

    if has_tax_split(scenario):
        try:
            resolve_split(scenario)
        except ValueError as exc:
            raise ScenarioValidationError(str(exc)) from exc

    # --- Taxable-gains fraction ---------------------------------------------
    if not (0.0 <= scenario.taxable_gains_fraction <= 1.0):
        raise ScenarioValidationError(
            "taxable_gains_fraction must be between 0 and 1 (got "
            f"{scenario.taxable_gains_fraction})"
        )

    # --- Bracket-fill lever target ------------------------------------------
    # None = lever off, always valid. When set, bounded 0..5,000,000: a dollar
    # income target (not a rate), so the bound is a plausible dollar-income
    # sanity check, not a percentage.
    if scenario.bracket_fill_target is not None and not (
        0.0 <= scenario.bracket_fill_target <= 5_000_000.0
    ):
        raise ScenarioValidationError(
            "bracket_fill_target must be between 0 and 5,000,000 (got "
            f"{scenario.bracket_fill_target})"
        )
    # --- Fee, MC distribution, pension inflation, survivor ------------------
    if not (0.0 <= scenario.annual_fee <= 0.03):
        raise ScenarioValidationError(
            f"annual_fee must be between 0 and 0.03 (got {scenario.annual_fee})"
        )
    if scenario.mc_distribution not in MC_DISTRIBUTIONS:
        raise ScenarioValidationError(
            f"mc_distribution must be one of {MC_DISTRIBUTIONS} "
            f"(got {scenario.mc_distribution!r})"
        )
    if not (2.5 <= scenario.mc_t_df <= 100.0):
        raise ScenarioValidationError(
            f"mc_t_df must be between 2.5 and 100 (got {scenario.mc_t_df})"
        )
    if scenario.pension_cola not in PENSION_COLAS:
        raise ScenarioValidationError(
            f"pension_cola must be one of {PENSION_COLAS} (got {scenario.pension_cola!r})"
        )
    if not (0.0 <= scenario.assumed_inflation <= 0.10):
        raise ScenarioValidationError(
            f"assumed_inflation must be between 0 and 0.10 (got {scenario.assumed_inflation})"
        )
    if scenario.survivor_decedent not in SURVIVOR_DECEDENTS:
        raise ScenarioValidationError(
            f"survivor_decedent must be one of {SURVIVOR_DECEDENTS} "
            f"(got {scenario.survivor_decedent!r})"
        )
    _validate_household(scenario)
    if scenario.people is None and scenario.survivor_age is not None and not (
        scenario.start_age < scenario.survivor_age <= scenario.end_age
    ):
        raise ScenarioValidationError(
            f"survivor_age must be after start_age and at most end_age "
            f"(got {scenario.survivor_age})"
        )
    for name in ("survivor_pension_fraction", "survivor_spending_fraction"):
        value = getattr(scenario, name)
        if not (0.0 <= value <= 1.0):
            raise ScenarioValidationError(f"{name} must be between 0 and 1 (got {value})")


def _check_age(name: str, value) -> None:
    if not isinstance(value, int) or not (_VALIDATE_AGE_MIN <= value <= _VALIDATE_AGE_MAX):
        raise ScenarioValidationError(
            f"{name} must be an integer age between {_VALIDATE_AGE_MIN} and "
            f"{_VALIDATE_AGE_MAX} (got {value!r})"
        )


def _check_fraction(name: str, value) -> None:
    if not (0.0 <= value <= 1.0):
        raise ScenarioValidationError(f"{name} must be between 0 and 1 (got {value})")


def _validate_household(scenario: Scenario) -> None:
    """The people, income streams, cash events and spending adjustments
    (``income.household``). Messages name the offending item by index and id."""
    people = scenario.people
    if people is not None:
        if not 1 <= len(people) <= 2:
            raise ScenarioValidationError(
                f"people must list one or two people (got {len(people)})"
            )
        for i, person in enumerate(people):
            if person.id != PERSON_IDS[i]:
                raise ScenarioValidationError(
                    f"people[{i}].id must be {PERSON_IDS[i]!r} (got {person.id!r})"
                )
            _check_age(f"people[{i}].start_age", person.start_age)
            _check_age(f"people[{i}].end_age", person.end_age)
            if person.end_age <= person.start_age:
                raise ScenarioValidationError(
                    f"people[{i}].end_age ({person.end_age}) must be greater than "
                    f"its start_age ({person.start_age})"
                )
        expected_end = plan_end_age(people)
        if scenario.start_age != people[0].start_age or scenario.end_age != expected_end:
            raise ScenarioValidationError(
                f"start_age/end_age ({scenario.start_age}/{scenario.end_age}) must "
                f"match the people: person a's start age {people[0].start_age} and "
                f"the latest end age on a's axis {expected_end} (build the scenario "
                f"with income.household.with_household)"
            )
    person_ids = tuple(p.id for p in household_people(scenario))
    couple = len(person_ids) == 2
    owners = person_ids + (OWNER_HOUSEHOLD,)
    offsets = {p.id: p.start_age - scenario.start_age for p in household_people(scenario)}
    offsets[OWNER_HOUSEHOLD] = 0

    if scenario.survivor_age is not None and people is not None:
        if not couple:
            raise ScenarioValidationError(
                "survivor_age needs a two-person household (this household has one person)"
            )
        death = scenario.survivor_age - offsets[scenario.survivor_decedent]
        if not (scenario.start_age < death <= scenario.end_age):
            raise ScenarioValidationError(
                f"survivor_age {scenario.survivor_age} (person "
                f"{scenario.survivor_decedent!r}'s own age) must fall after the first "
                f"plan year and no later than the last"
            )

    if scenario.income_streams is not None:
        seen: set[str] = set()
        for i, st in enumerate(scenario.income_streams):
            where = f"income_streams[{i}] ({st.id!r})"
            if not st.id:
                raise ScenarioValidationError(f"income_streams[{i}].id is required")
            if st.id in seen:
                raise ScenarioValidationError(f"{where}: duplicate stream id")
            seen.add(st.id)
            if st.kind not in STREAM_KINDS:
                raise ScenarioValidationError(
                    f"{where}: kind must be one of {STREAM_KINDS} (got {st.kind!r})"
                )
            if st.owner not in owners:
                raise ScenarioValidationError(
                    f"{where}: owner must be one of {owners} (got {st.owner!r})"
                )
            if st.amount < 0:
                raise ScenarioValidationError(f"{where}: amount must be >= 0 (got {st.amount})")
            _check_age(f"{where} start_age", st.start_age)
            if st.end_age is not None:
                _check_age(f"{where} end_age", st.end_age)
                if st.end_age <= st.start_age:
                    raise ScenarioValidationError(
                        f"{where}: end_age ({st.end_age}) must be greater than "
                        f"start_age ({st.start_age})"
                    )
            if st.cola not in STREAM_COLAS:
                raise ScenarioValidationError(
                    f"{where}: cola must be one of {STREAM_COLAS} (got {st.cola!r})"
                )
            if st.cola == COLA_FIXED and not (-0.05 <= st.cola_rate <= 0.10):
                raise ScenarioValidationError(
                    f"{where}: cola_rate must be between -0.05 and 0.10 (got {st.cola_rate})"
                )
            if st.survivor_fraction is not None:
                _check_fraction(f"{where} survivor_fraction", st.survivor_fraction)
            tax = stream_tax_treatment(st)
            if tax not in STREAM_TAX_TREATMENTS:
                raise ScenarioValidationError(
                    f"{where}: tax_treatment must be one of {STREAM_TAX_TREATMENTS} "
                    f"(got {tax!r})"
                )
            if (tax == TAX_SOCIAL_SECURITY) != (st.kind == KIND_SOCIAL_SECURITY):
                raise ScenarioValidationError(
                    f"{where}: the social_security tax treatment applies to, and only "
                    f"to, kind social_security"
                )
            if st.kind == KIND_SOCIAL_SECURITY:
                if st.owner == OWNER_HOUSEHOLD:
                    raise ScenarioValidationError(
                        f"{where}: a Social Security stream belongs to a person, not the household"
                    )
                if st.cola != COLA_FULL or st.nominal_until_start or st.end_age is not None:
                    raise ScenarioValidationError(
                        f"{where}: Social Security is paid for life with a full COLA "
                        f"(cola 'full', no nominal_until_start, no end_age)"
                    )
                if not (MIN_CLAIM_AGE <= st.start_age <= MAX_CLAIM_AGE):
                    raise ScenarioValidationError(
                        f"{where}: Social Security claim age (start_age) must be "
                        f"between {MIN_CLAIM_AGE} and {MAX_CLAIM_AGE} (got {st.start_age})"
                    )

    for i, ev in enumerate(scenario.cash_events):
        where = f"cash_events[{i}]" + (f" ({ev.id!r})" if ev.id else "")
        if ev.owner not in owners:
            raise ScenarioValidationError(
                f"{where}: owner must be one of {owners} (got {ev.owner!r})"
            )
        _check_age(f"{where} age", ev.age)
        a_age = ev.age - offsets[ev.owner]
        if not (scenario.start_age <= a_age <= scenario.end_age):
            raise ScenarioValidationError(
                f"{where}: age {ev.age} falls outside the plan"
            )
        if ev.tax_treatment not in EVENT_TAX_TREATMENTS:
            raise ScenarioValidationError(
                f"{where}: tax_treatment must be one of {EVENT_TAX_TREATMENTS} "
                f"(got {ev.tax_treatment!r})"
            )
        if ev.amount < 0 and ev.tax_treatment != TAX_FREE:
            raise ScenarioValidationError(
                f"{where}: an outflow (negative amount) must use tax_treatment 'tax_free'"
            )
        _check_fraction(f"{where} basis_fraction", ev.basis_fraction)

    for i, adj in enumerate(scenario.spending_adjustments):
        where = f"spending_adjustments[{i}]" + (f" ({adj.id!r})" if adj.id else "")
        if adj.owner not in owners:
            raise ScenarioValidationError(
                f"{where}: owner must be one of {owners} (got {adj.owner!r})"
            )
        if adj.amount < 0:
            raise ScenarioValidationError(f"{where}: amount must be >= 0 (got {adj.amount})")
        _check_age(f"{where} start_age", adj.start_age)
        _check_age(f"{where} end_age", adj.end_age)
        if adj.end_age <= adj.start_age:
            raise ScenarioValidationError(
                f"{where}: end_age ({adj.end_age}) must be greater than start_age "
                f"({adj.start_age})"
            )
        _check_fraction(f"{where} survivor_fraction", adj.survivor_fraction)


def validate_glidepath_class_consistency(scenario: Scenario) -> None:
    """Enforce the glidepath contract: per-class return ASSUMPTIONS are global.

    A glidepath changes WEIGHTS across phases; the per-class ``real_return`` and
    ``volatility`` are a property of the class, not of the phase. If the SAME class
    id carries DIFFERING ``real_return`` or ``volatility`` across phases, the engines
    silently disagree: the deterministic path reads each phase's own ``real_return``
    (``engine.allocation.blended_real_return_at_age``), while the Monte Carlo sampler
    dedups the union of phase classes by id (first occurrence wins — one vol per sampled
    series), so the two would describe DIFFERENT portfolios with no error. Rather than
    blend silently, reject the inconsistency loudly.

    Raises ``ValueError`` naming the offending class. A no-op under ``constant``
    mode, when ``phase_allocations`` is empty, or when every phase agrees on each
    class's params (the legal case: same id, same params, different WEIGHTS).
    """
    if scenario.allocation_mode != ALLOCATION_MODE_GLIDEPATH:
        return
    phases = scenario.phase_allocations
    if not phases:
        return
    seen: dict[str, tuple[float, float | None]] = {}
    for phase in phases:
        for ac in phase:
            params = (ac.real_return, ac.volatility)
            prior = seen.get(ac.id)
            if prior is None:
                seen[ac.id] = params
            elif prior != params:
                raise ValueError(
                    f"class '{ac.id}' has conflicting real_return/volatility "
                    f"across glidepath phases; per-class return assumptions must be "
                    f"consistent — vary weights, not returns"
                )
