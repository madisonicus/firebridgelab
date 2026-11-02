"""Households: people, income streams, one-off cash events and fixed spending.

This module is the ONE place that turns a ``Scenario`` into per-age household
flows. Every engine, the spending rules, the bucket targets, the NPV gap tables
and the tax fixed point read their income through it (``gi_at_age`` and friends
in ``income.guaranteed``, ``income.pension``, ``income.social_security`` and
``tax.location`` all delegate here), so there is a single source of truth.

People and the age axis
-----------------------
A household is one or two people (ids ``"a"`` and ``"b"``), each with an age at
plan start and a planning end age. The engine's year axis is person A's age:
``Scenario.start_age`` is A's age at plan start and ``Scenario.end_age`` is the
A-age of the last plan year, which is the later of the two people's end ages
(B's end age converted to A's axis). Person B's age in a year is A's age plus
the fixed offset ``B.start_age - A.start_age``. Everything a person owns (an
income stream, a cash event, a spending adjustment, a death) is entered in that
person's own age and converted to the A axis here.

A planning end age is a horizon, not a death: a person's streams keep paying
until the plan ends unless the survivor case (below) or the stream's own
``end_age`` stops them.

When ``Scenario.people`` is ``None`` the household is the legacy fictional
couple: two people of the same age, ``start_age`` → ``end_age``.

Income streams
--------------
``Scenario.income_streams`` is a tuple of ``IncomeStream``. When it is ``None``
the four legacy fields (``person_a_pension_*``, ``person_b_pension_*``,
``person_a_ss_*``, ``person_b_ss_*``) are converted into the same internal
streams (``_legacy_streams``), so old scenarios and the explicit form run
through identical arithmetic. In a one-person household only person A's legacy
fields are used.

A stream pays in owner-ages ``[start_age, end_age)`` (``end_age`` exclusive;
``None`` = for life). Its real value at A-age ``t`` is::

    amount * drift * nominal * cola

  - ``drift`` = ``(1 - pension_real_drift) ** years_paid`` for pension streams
    only (the stress-test COLA freeze); 1 otherwise.
  - ``nominal`` = ``1 / (1 + assumed_inflation) ** (start - plan_start)`` when
    ``nominal_until_start`` (the amount is the nominal check at commencement).
  - ``cola`` per year paid: ``full`` keeps real value constant; ``none`` erodes
    at ``assumed_inflation`` (``1 / (1 + i)``); ``fixed`` applies a stated
    nominal COLA ``c`` (``(1 + c) / (1 + i)``); ``fers_diet`` applies the FERS
    rule (``income.pension.fers_diet_cola``) for each year paid from the
    owner's 62 on, and holds the check fixed in nominal terms (as ``none``)
    for the years paid before 62 (5 U.S.C. 8462(c)(3)). A stream with
    ``fers_cola_before_62`` gets the COLA from its start instead: the
    annuitants 8462(c)(3)(B) exempts (disability retirees; special-provision
    law enforcement officers, firefighters and air traffic controllers) and
    survivor annuities, which 8462(c)(3) does not cover.

Social Security streams take the FRA amount (the PIA) as ``amount`` and the
claim age as ``start_age``; the effective benefit is
``effective_ss_amount(amount, start_age)`` (SSA claim factor, 62..70). They
are constant real.

Survivor case
-------------
``survivor_age`` / ``survivor_decedent``: person ``survivor_decedent`` dies at
their own age ``survivor_age`` (for the legacy same-age couple this is the old
"household age" meaning). From that year on:

  - the survivor gets the larger of their own Social Security benefit and the
    widow(er)'s benefit on the decedent's record (``Household._widow_benefit``:
    the decedent's benefit with delayed credits, at least 82.5% of the PIA if
    they claimed early, or the PIA plus credits to the death if they had not
    claimed), unreduced for the survivor's own age;
  - each of the decedent's other streams continues at its ``survivor_fraction``
    (pension 0, annuity 0, work 0, other 0, rental 1.0 by default; the legacy
    pensions use ``Scenario.survivor_pension_fraction``); household-owned
    streams scale by their ``survivor_fraction`` (default 1.0);
  - spending scales by ``survivor_spending_fraction`` and federal tax uses
    single-filer brackets (``spending.lifecycle``, ``tax.location``).

A one-person household has no survivor case and files single throughout.

Cash events and spending adjustments
------------------------------------
``CashEvent`` is a one-off flow at an owner-age: positive is an inflow (a home
sale, an inheritance), negative an outflow (a car, a wedding). It enters the
year's portfolio step as a mid-year flow, like guaranteed income, but the
spending rules never see it. Under a tax-location split an inflow nets against
that year's spending need and any surplus lands in the taxable account; its tax
treatment adds ordinary income (``ordinary``) or long-term gain
``amount * (1 - basis_fraction)`` (``capital_gain``) to that year's return.
The pre-tax engine adds the full amount (it taxes nothing).

``SpendingAdjustment`` is extra real spending per year over owner-ages
``[start_age, end_age)``: healthcare before Medicare, a mortgage until payoff,
college years. It is REQUIRED spending, added on top of whatever the spending
rule chooses. The rules see it the same way they see guaranteed income, with
the sign flipped: every rule is given ``net_income_at_age`` = guaranteed income
minus required spending. So the guardrail withdrawal rate, the funded-ratio and
amortization NPV gaps and the bucket gap all count it as money the portfolio
must supply, and no rule can cut it; only the discretionary part moves. The
spending floor and ceiling bound the discretionary part. Recorded spending
(the trajectory, the spending fan) is the total, discretionary plus required.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from firebridgelab.income.pension import (
    PENSION_COLA_FERS_DIET,
    PENSION_COLA_FULL,
    fers_diet_cola,
)
from firebridgelab.income.social_security import (
    DEFAULT_FRA_AGE,
    MAX_CLAIM_AGE,
    WIDOW_LIMIT_FRACTION,
    effective_ss_amount,
    ss_factor,
)

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario


PERSON_IDS = ("a", "b")
OWNER_HOUSEHOLD = "household"
OWNERS = PERSON_IDS + (OWNER_HOUSEHOLD,)

KIND_PENSION = "pension"
KIND_SOCIAL_SECURITY = "social_security"
KIND_ANNUITY = "annuity"
KIND_WORK = "work"
KIND_RENTAL = "rental"
KIND_OTHER = "other"
STREAM_KINDS = (
    KIND_PENSION, KIND_SOCIAL_SECURITY, KIND_ANNUITY, KIND_WORK, KIND_RENTAL, KIND_OTHER,
)

COLA_FULL = PENSION_COLA_FULL          # constant real
COLA_NONE = "none"                     # fixed nominal, erodes at assumed_inflation
COLA_FIXED = "fixed"                   # a stated nominal COLA (cola_rate)
COLA_FERS_DIET = PENSION_COLA_FERS_DIET
STREAM_COLAS = (COLA_FULL, COLA_NONE, COLA_FIXED, COLA_FERS_DIET)

TAX_ORDINARY = "ordinary"
TAX_SOCIAL_SECURITY = "social_security"   # 85% included as ordinary income
TAX_FREE = "tax_free"
TAX_CAPITAL_GAIN = "capital_gain"
STREAM_TAX_TREATMENTS = (TAX_ORDINARY, TAX_SOCIAL_SECURITY, TAX_FREE)
EVENT_TAX_TREATMENTS = (TAX_FREE, TAX_ORDINARY, TAX_CAPITAL_GAIN)


# The 65+ additional standard deduction is per person.
_SENIOR_AGE = 65


@dataclass(frozen=True)
class Person:
    """One member of the household. ``id`` is ``"a"`` (the age axis) or ``"b"``."""

    id: str
    start_age: int   # age at plan start
    end_age: int     # planning end age (a horizon, not a death)
    name: str = ""


@dataclass(frozen=True)
class IncomeStream:
    """One income stream. Ages are in the OWNER's age (household: person A's).

    ``amount`` is annual real dollars at plan start, or the nominal check at
    commencement when ``nominal_until_start``. For ``social_security`` it is
    the FRA amount and ``start_age`` is the claim age (62..70).
    ``survivor_fraction`` / ``tax_treatment`` left ``None`` take the kind's
    default (``stream_survivor_fraction`` / ``stream_tax_treatment``).
    """

    id: str
    kind: str
    amount: float
    start_age: int
    owner: str = "a"
    end_age: Optional[int] = None     # exclusive; None = for life
    cola: str = COLA_FULL
    cola_rate: float = 0.0            # nominal COLA for cola == "fixed"
    nominal_until_start: bool = False
    survivor_fraction: Optional[float] = None
    tax_treatment: Optional[str] = None
    label: str = ""
    # ``fers_diet`` streams only: COLAs from the start age even before the
    # owner's 62 (disability, special-provision and survivor annuities).
    # Default: no COLA before 62 (5 U.S.C. 8462(c)(3)).
    fers_cola_before_62: bool = False
    # ``work`` streams only: the income is self-employment income (Schedule SE)
    # rather than wages. Read by the payroll tax (``Scenario.payroll_tax``).
    self_employed: bool = False


@dataclass(frozen=True)
class CashEvent:
    """A one-off cash flow at the owner's ``age`` (+ inflow, - outflow)."""

    age: int
    amount: float
    tax_treatment: str = TAX_FREE
    basis_fraction: float = 0.0   # capital_gain only: share of the amount that is basis
    owner: str = "a"
    id: str = ""
    label: str = ""


@dataclass(frozen=True)
class SpendingAdjustment:
    """Extra required real spending per year over owner-ages ``[start_age, end_age)``."""

    amount: float
    start_age: int
    end_age: int
    owner: str = "a"
    survivor_fraction: float = 1.0   # share that continues after the owner's death
    id: str = ""
    label: str = ""


def stream_survivor_fraction(stream: IncomeStream) -> float:
    """The survivor fraction a stream uses: its own, else the kind/owner default."""
    if stream.survivor_fraction is not None:
        return stream.survivor_fraction
    if stream.owner == OWNER_HOUSEHOLD or stream.kind == KIND_RENTAL:
        return 1.0
    return 0.0


def stream_tax_treatment(stream: IncomeStream) -> str:
    """The tax treatment a stream uses: its own, else the kind default."""
    if stream.tax_treatment is not None:
        return stream.tax_treatment
    if stream.kind == KIND_SOCIAL_SECURITY:
        return TAX_SOCIAL_SECURITY
    return TAX_ORDINARY


# --------------------------------------------------------------------------
# People and the age axis
# --------------------------------------------------------------------------


def household_people(scenario: Scenario) -> tuple[Person, ...]:
    """The household's people: ``Scenario.people``, or the legacy same-age couple."""
    if scenario.people is not None:
        return tuple(scenario.people)
    return (
        Person("a", scenario.start_age, scenario.end_age),
        Person("b", scenario.start_age, scenario.end_age),
    )


def plan_end_age(people: tuple[Person, ...] | list[Person]) -> int:
    """The last plan year on person A's axis: the latest end age across people."""
    a_start = people[0].start_age
    return max(p.end_age - (p.start_age - a_start) for p in people)


def with_household(
    scenario: Scenario,
    *,
    people,
    income_streams=None,
    cash_events=None,
    spending_adjustments=None,
) -> Scenario:
    """A copy of ``scenario`` with an explicit household.

    Sets ``people`` and derives ``start_age`` (person A's age at plan start)
    and ``end_age`` (``plan_end_age``) from it, so the horizon always agrees
    with the people. ``income_streams=None`` keeps the scenario's streams (or
    the legacy fields); ``cash_events`` / ``spending_adjustments`` ``None``
    keep the scenario's.
    """
    people = tuple(people)
    changes: dict = {
        "people": people,
        "start_age": people[0].start_age,
        "end_age": plan_end_age(people),
    }
    if income_streams is not None:
        changes["income_streams"] = tuple(income_streams)
    if cash_events is not None:
        changes["cash_events"] = tuple(cash_events)
    if spending_adjustments is not None:
        changes["spending_adjustments"] = tuple(spending_adjustments)
    return replace(scenario, **changes)


def legacy_income_streams(scenario: Scenario) -> tuple[IncomeStream, ...]:
    """The legacy ``person_*`` fields expressed as public ``IncomeStream`` objects.

    For display and for converting an old scenario by hand. Social Security
    comes back as an FRA amount (the effective amount divided by the claim
    factor, rounded to the cent). The engine itself does not round-trip
    through this: it converts the legacy fields directly (``_legacy_streams``).
    """
    couple = len(household_people(scenario)) == 2
    out = [
        IncomeStream(
            id="person_a_pension", kind=KIND_PENSION, owner="a",
            amount=scenario.person_a_pension_amount,
            start_age=scenario.person_a_pension_start_age,
            cola=scenario.pension_cola,
            nominal_until_start=scenario.pension_nominal_until_start,
            survivor_fraction=scenario.survivor_pension_fraction,
        ),
    ]
    if couple:
        out.append(
            IncomeStream(
                id="person_b_pension", kind=KIND_PENSION, owner="b",
                amount=scenario.person_b_pension_amount,
                start_age=scenario.person_b_pension_start_age,
                cola=scenario.pension_cola,
                nominal_until_start=scenario.pension_nominal_until_start,
                survivor_fraction=scenario.survivor_pension_fraction,
            )
        )
    for who in (("a", "b") if couple else ("a",)):
        eff = getattr(scenario, f"person_{who}_ss_amount")
        claim = getattr(scenario, f"person_{who}_ss_start_age")
        try:
            fra = round(eff / ss_factor(claim), 2)
        except (ValueError, ZeroDivisionError):
            fra = eff
        out.append(
            IncomeStream(
                id=f"person_{who}_ss", kind=KIND_SOCIAL_SECURITY, owner=who,
                amount=fra, start_age=claim,
            )
        )
    return tuple(out)


def scale_social_security(scenario: Scenario, factor: float) -> Scenario:
    """Scale every Social Security benefit by ``factor`` (legacy fields and
    explicit streams alike) — the trust-fund haircut stresses use this."""
    changes: dict = {
        "person_a_ss_amount": scenario.person_a_ss_amount * factor,
        "person_b_ss_amount": scenario.person_b_ss_amount * factor,
    }
    if scenario.income_streams is not None:
        changes["income_streams"] = tuple(
            replace(s, amount=s.amount * factor) if s.kind == KIND_SOCIAL_SECURITY else s
            for s in scenario.income_streams
        )
    return replace(scenario, **changes)


# --------------------------------------------------------------------------
# Resolution: the canonical internal form, cached on the scenario
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stream:
    """A stream resolved onto person A's axis (the canonical internal form)."""

    owner: str
    kind: str
    amount: float          # effective real amount (SS: after the claim factor)
    start: int             # first paying A-age
    end: Optional[int]     # first non-paying A-age, or None
    cola: str
    cola_rate: float
    nominal_until_start: bool
    survivor_fraction: float
    tax: str
    self_employed: bool = False
    # ``fers_diet`` only: the first A-age from which COLAs are paid (the
    # owner's 62, or the start with ``fers_cola_before_62``).
    cola_from: int = 0


@dataclass(frozen=True)
class HouseholdYear:
    """Every household flow for one plan year (A-age), real dollars.

    ``gi`` is the guaranteed-income total the engines report; ``ordinary``,
    ``social_security`` (gross) and the tax-free remainder partition it for tax.
    ``pension`` is the pension-kind share. ``required_spending`` is the
    spending-adjustment total and ``cash_event`` the signed one-off flow;
    ``event_ordinary`` / ``event_ltcg`` are the income a cash event adds to the
    year's tax return.
    """

    gi: float
    pension: float
    ordinary: float
    social_security: float
    required_spending: float
    cash_event: float
    event_ordinary: float
    event_ltcg: float
    survivor: bool
    n_65_plus: int
    single_filer: bool
    # Splits of ``ordinary`` the tax extensions read: pension and annuity
    # income (state retirement rules), and each person's wages and
    # self-employment income from ``work`` streams (payroll tax), as
    # ``(person_id, amount)`` pairs. Household-owned work counts as person A's.
    retirement: float = 0.0
    wages: tuple = ()
    self_employment: tuple = ()


class Household:
    """The resolved household for one scenario, with a per-age cache."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        people = household_people(scenario)
        self.people = people
        a_start = people[0].start_age
        self.offsets = {p.id: p.start_age - a_start for p in people}
        self.offsets[OWNER_HOUSEHOLD] = 0
        self.couple = len(people) == 2
        if scenario.income_streams is None:
            self.streams = _legacy_streams(scenario, self.couple)
        else:
            self.streams = tuple(
                _resolve_stream(s, self.offsets) for s in scenario.income_streams
            )
        self.decedent: Optional[str] = None
        self.death_age: Optional[int] = None
        if self.couple and scenario.survivor_age is not None:
            self.decedent = scenario.survivor_decedent
            self.death_age = scenario.survivor_age - self.offsets.get(self.decedent, 0)
        self.adjustments = tuple(
            (
                adj.start_age - self.offsets.get(adj.owner, 0),
                adj.end_age - self.offsets.get(adj.owner, 0),
                adj,
            )
            for adj in scenario.spending_adjustments
        )
        self.events = tuple(
            (ev.age - self.offsets.get(ev.owner, 0), ev) for ev in scenario.cash_events
        )
        self._years: dict[int, HouseholdYear] = {}

    def age_of(self, person_id: str, age: int) -> int:
        """Person ``person_id``'s own age in the plan year whose A-age is ``age``."""
        return age + self.offsets[person_id]

    def stream_ages(self) -> list[int]:
        """Every A-age at which some stream starts or stops paying."""
        ages = [s.start for s in self.streams]
        ages += [s.end for s in self.streams if s.end is not None]
        return ages

    def living(self, age: int) -> tuple[Person, ...]:
        """The people alive in the plan year whose A-age is ``age``."""
        if self.is_survivor_year(age):
            return tuple(p for p in self.people if p.id != self.decedent)
        return self.people

    def is_survivor_year(self, age: int) -> bool:
        return self.death_age is not None and age >= self.death_age

    def _dies_with(self, owner: str) -> bool:
        """Whether a flow owned by ``owner`` is affected by the death."""
        return owner == self.decedent or owner == OWNER_HOUSEHOLD

    def _widow_benefit(self, st: _Stream) -> float:
        """The widow(er)'s benefit on the decedent's Social Security stream
        ``st``, unreduced for the survivor's age (20 CFR 404.338).

        - The decedent had claimed by the death year: their benefit in
          payment, including delayed credits (404.338(b)); if they claimed
          early, the larger of that reduced benefit and 82.5% of their PIA
          (404.338(c)).
        - The decedent died before claiming: the PIA (404.338(a)), plus the
          delayed credits earned up to the death if they died after full
          retirement age (404.313(e)(1)), at whole-year resolution.
        """
        if st.amount == 0.0:
            return 0.0
        off = self.offsets[st.owner]
        claim_age = st.start + off
        death_age = self.death_age + off
        try:
            factor = ss_factor(claim_age)
        except ValueError:
            # A legacy claim age outside 62-70 (validation rejects it for a
            # nonzero benefit): the amount in payment, as before.
            return st.amount if claim_age <= death_age else 0.0
        pia = st.amount / factor
        if claim_age <= death_age:
            if factor < 1.0:
                return max(st.amount, WIDOW_LIMIT_FRACTION * pia)
            return st.amount
        credited_age = min(max(death_age, DEFAULT_FRA_AGE), MAX_CLAIM_AGE)
        return pia * ss_factor(credited_age)

    def year(self, age: int) -> HouseholdYear:
        hy = self._years.get(age)
        if hy is None:
            hy = self._compute_year(age)
            self._years[age] = hy
        return hy

    def _compute_year(self, age: int) -> HouseholdYear:
        s = self.scenario
        survivor = self.is_survivor_year(age)
        infl = s.assumed_inflation

        # Non-Social-Security streams, summed in list order (the order the
        # legacy pension sum used, so legacy totals are bit-identical).
        nonss = 0.0
        pension = 0.0
        ordinary = 0.0
        ss_by_person: dict[str, float] = {}
        ss_total = 0.0
        retirement = 0.0
        wages: dict[str, float] = {}
        self_emp: dict[str, float] = {}
        widow = 0.0
        for st in self.streams:
            if survivor and st.kind == KIND_SOCIAL_SECURITY and st.owner == self.decedent:
                # The decedent's benefit becomes a widow(er)'s benefit from the
                # death year, whether or not the decedent had claimed.
                widow += self._widow_benefit(st)
                continue
            if age < st.start or (st.end is not None and age >= st.end):
                continue
            if st.kind == KIND_SOCIAL_SECURITY:
                ss_by_person[st.owner] = ss_by_person.get(st.owner, 0.0) + st.amount
                ss_total += st.amount
                continue
            years_paid = age - st.start
            if st.kind == KIND_PENSION:
                mult = (1.0 - s.pension_real_drift) ** years_paid
            else:
                mult = 1.0
            if st.nominal_until_start:
                mult /= (1.0 + infl) ** max(0, st.start - s.start_age)
            if st.cola == COLA_FERS_DIET:
                # No COLA before the owner's 62 (5 U.S.C. 8462(c)(3)): the
                # nominal check is fixed until then, then gets the 8462(b)
                # COLA once a year.
                cola_years = max(0, age - max(st.start, st.cola_from))
                mult *= ((1.0 + fers_diet_cola(infl)) / (1.0 + infl)) ** cola_years
                mult /= (1.0 + infl) ** (years_paid - cola_years)
            elif st.cola == COLA_NONE:
                mult /= (1.0 + infl) ** years_paid
            elif st.cola == COLA_FIXED:
                mult *= ((1.0 + st.cola_rate) / (1.0 + infl)) ** years_paid
            value = st.amount * mult
            if survivor and self._dies_with(st.owner):
                value *= st.survivor_fraction
            nonss += value
            if st.kind == KIND_PENSION:
                pension += value
            if st.tax == TAX_ORDINARY:
                ordinary += value
                if st.kind in (KIND_PENSION, KIND_ANNUITY):
                    retirement += value
                elif st.kind == KIND_WORK:
                    who = "a" if st.owner == OWNER_HOUSEHOLD else st.owner
                    bucket = self_emp if st.self_employed else wages
                    bucket[who] = bucket.get(who, 0.0) + value

        # Social Security: after the death the survivor gets the larger of
        # their own benefit in payment and the widow(er)'s benefit on the
        # decedent's record (20 CFR 404.407, 404.338; ``_widow_benefit``). The
        # widow(er)'s benefit is not reduced for the survivor's own age (20
        # CFR 404.410(c) would reduce it before full retirement age), and it
        # is paid at any survivor age (the regulation starts it at 60).
        if survivor:
            ss = max(max(ss_by_person.values(), default=0.0), widow)
        else:
            ss = ss_total

        required = 0.0
        for start, end, adj in self.adjustments:
            if start <= age < end:
                amount = adj.amount
                if survivor and self._dies_with(adj.owner):
                    amount *= adj.survivor_fraction
                required += amount

        cash = 0.0
        event_ordinary = 0.0
        event_ltcg = 0.0
        for ev_age, ev in self.events:
            if ev_age != age:
                continue
            cash += ev.amount
            if ev.amount > 0:
                if ev.tax_treatment == TAX_ORDINARY:
                    event_ordinary += ev.amount
                elif ev.tax_treatment == TAX_CAPITAL_GAIN:
                    event_ltcg += ev.amount * (1.0 - ev.basis_fraction)

        # Filing profile: joint for a couple until the death, single after it
        # and throughout for a one-person household. One 65+ addition per
        # living person aged 65 or more.
        if not self.couple:
            single = True
            n65 = 1 if age >= _SENIOR_AGE else 0
        elif survivor:
            single = True
            survivor_id = "a" if self.decedent == "b" else "b"
            n65 = 1 if self.age_of(survivor_id, age) >= _SENIOR_AGE else 0
        else:
            single = False
            n65 = sum(1 for p in self.people if self.age_of(p.id, age) >= _SENIOR_AGE)

        return HouseholdYear(
            gi=nonss + ss,
            pension=pension,
            ordinary=ordinary,
            social_security=ss,
            required_spending=required,
            cash_event=cash,
            event_ordinary=event_ordinary,
            event_ltcg=event_ltcg,
            survivor=survivor,
            n_65_plus=n65,
            single_filer=single,
            retirement=retirement,
            wages=tuple(sorted(wages.items())),
            self_employment=tuple(sorted(self_emp.items())),
        )


def _resolve_stream(stream: IncomeStream, offsets: dict[str, int]) -> _Stream:
    off = offsets.get(stream.owner, 0)
    if stream.kind == KIND_SOCIAL_SECURITY:
        amount = effective_ss_amount(stream.amount, stream.start_age)
    else:
        amount = stream.amount
    return _Stream(
        owner=stream.owner,
        kind=stream.kind,
        amount=amount,
        start=stream.start_age - off,
        end=None if stream.end_age is None else stream.end_age - off,
        cola=stream.cola,
        cola_rate=stream.cola_rate,
        nominal_until_start=stream.nominal_until_start,
        survivor_fraction=stream_survivor_fraction(stream),
        tax=stream_tax_treatment(stream),
        self_employed=stream.self_employed,
        cola_from=(stream.start_age - off) if stream.fers_cola_before_62 else FERS_COLA_AGE - off,
    )


# 5 U.S.C. 8462(c)(3): no FERS COLA for an annuitant under 62.
FERS_COLA_AGE = 62


def _legacy_streams(scenario: Scenario, couple: bool) -> tuple[_Stream, ...]:
    """The four legacy fields as internal streams (two for a one-person
    household). Social Security is already the effective amount on the
    Scenario, so it is used as is. Ages are the owner's own ages."""
    owners = ("a", "b") if couple else ("a",)
    out = []
    for who in owners:
        out.append(
            _Stream(
                owner=who,
                kind=KIND_PENSION,
                amount=getattr(scenario, f"person_{who}_pension_amount"),
                start=getattr(scenario, f"person_{who}_pension_start_age"),
                end=None,
                cola=scenario.pension_cola,
                cola_rate=0.0,
                nominal_until_start=scenario.pension_nominal_until_start,
                survivor_fraction=scenario.survivor_pension_fraction,
                tax=TAX_ORDINARY,
                cola_from=FERS_COLA_AGE,
            )
        )
    for who in owners:
        out.append(
            _Stream(
                owner=who,
                kind=KIND_SOCIAL_SECURITY,
                amount=getattr(scenario, f"person_{who}_ss_amount"),
                start=getattr(scenario, f"person_{who}_ss_start_age"),
                end=None,
                cola=COLA_FULL,
                cola_rate=0.0,
                nominal_until_start=False,
                survivor_fraction=0.0,
                tax=TAX_SOCIAL_SECURITY,
            )
        )
    if couple and scenario.people is not None:
        # Explicit people with legacy streams: person B's legacy ages are B's
        # own ages, so shift them onto A's axis.
        off = scenario.people[1].start_age - scenario.people[0].start_age
        out = [
            replace(st, start=st.start - off, cola_from=st.cola_from - off)
            if st.owner == "b" else st
            for st in out
        ]
    return tuple(out)


_CACHE_ATTR = "_household_cache"


def household(scenario: Scenario) -> Household:
    """The resolved ``Household`` for ``scenario``, built once and cached on it.

    ``Scenario`` is frozen, so the cache can never go stale: a changed plan is
    a new object (``dataclasses.replace``) with its own cache. The attribute is
    not a dataclass field, so it never enters equality, hashing, ``asdict`` or
    ``replace``.
    """
    h = scenario.__dict__.get(_CACHE_ATTR)
    if h is None:
        h = Household(scenario)
        object.__setattr__(scenario, _CACHE_ATTR, h)
    return h


def household_year(scenario: Scenario, age: int) -> HouseholdYear:
    """All household flows for the plan year whose person-A age is ``age``."""
    return household(scenario).year(age)


def required_spending_at_age(scenario: Scenario, age: int) -> float:
    """Required (fixed-adjustment) real spending at ``age``."""
    return household(scenario).year(age).required_spending


def cash_event_at_age(scenario: Scenario, age: int) -> float:
    """The signed one-off cash flow at ``age`` (0 when there is none)."""
    return household(scenario).year(age).cash_event


def net_income_at_age(scenario: Scenario, age: int) -> float:
    """Guaranteed income minus required spending: what a spending rule may
    count on toward discretionary spending. Every rule reads this, so required
    spending is a draw the rule cannot cut (see the module docstring). Equal
    to ``gi_at_age`` when there are no spending adjustments."""
    hy = household(scenario).year(age)
    if hy.required_spending == 0.0:
        return hy.gi
    return hy.gi - hy.required_spending
