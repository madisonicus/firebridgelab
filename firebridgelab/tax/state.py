"""State income tax: no-tax states, flat-rate states, and a custom flat rate.

``Scenario.state`` names the state of residence by its two-letter code (the
states listed in ``tax/data/states_2026.toml``, ``STATE_CODES``) or
``"custom"``; ``None`` (the default) charges no state tax. ``state_move_age``
and ``state_after_move`` change the state from that person-A age on (a move).

Read only under a tax-location split: the state bill joins the federal bill
inside the per-year fixed point (``tax.location``), so it is paid from the
portfolio like the federal tax and every account draw it causes is taxed too.

The state base starts from federal AGI (the model's ordinary income including
taxable Social Security, plus long-term gains) or, for a state that starts from
federal taxable income, from that. Then, per the state's data:

  - the federally taxable Social Security is subtracted when the state exempts it;
  - pension and annuity income and tax-deferred account distributions (spending
    draws, RMDs, the bracket-fill lever and Roth conversions) are subtracted
    when exempt for people of that age;
  - a per-person retirement exclusion absorbs the income it covers;
  - the standard deduction and personal exemptions come off, and the flat rate
    applies above any zero-rate band.

Simplifications (docs/methodology.md lists them): accounts are household-wide,
so an age-gated exemption of IRA distributions or pensions applies to the share
of living people who meet the age; state deductions are held constant in real
terms; local and county income taxes, credits, and the special treatment of
particular public pensions are not modelled.

``custom``: ``state_custom_rate`` on federal AGI, with toggles to exempt Social
Security, pensions and annuities, and tax-deferred account distributions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from firebridgelab.tax import params

_DATA = params.load_states()
_STATES: dict = _DATA["states"]

STATE_CUSTOM = "custom"
STATE_CODES: tuple[str, ...] = tuple(sorted(_STATES))
STATE_CHOICES: tuple[str, ...] = STATE_CODES + (STATE_CUSTOM,)

KIND_NONE = "none"
KIND_FLAT = "flat"

EXCLUSION_COVERS = ("pension", "ira", "ltcg", "other")


def state_info(code: str) -> dict:
    """The data-file table for a state code."""
    return _STATES[code]


@dataclass(frozen=True)
class StateYear:
    """One plan year's state-tax inputs that do not depend on the path."""

    code: str
    rate: float
    zero_bracket: float
    from_federal_taxable: bool
    deduction: float
    low_agi_extra: float
    low_agi_limit: float
    agi_limit: float
    exempt_ss: bool
    pension_exempt_frac: float
    ira_exempt_frac: float
    exclusion: float
    covers: frozenset
    pension: float
    other: float


def state_code_at_age(scenario, age: int) -> Optional[str]:
    """The state of residence in the plan year whose person-A age is ``age``."""
    if scenario.state_move_age is not None and age >= scenario.state_move_age:
        return scenario.state_after_move
    return scenario.state


def state_year(scenario, age: int) -> Optional[StateYear]:
    """The year's ``StateYear``, or ``None`` when no state tax applies."""
    code = state_code_at_age(scenario, age)
    if code is None:
        return None
    from firebridgelab.income.household import household

    h = household(scenario)
    hy = h.year(age)
    living_ages = [h.age_of(p.id, age) for p in h.living(age)]
    n_living = len(living_ages) or 1
    wages = sum(a for _, a in hy.wages) + sum(a for _, a in hy.self_employment)
    other = hy.ordinary - hy.retirement - wages + hy.event_ordinary
    if other < 0.0:
        other = 0.0

    if code == STATE_CUSTOM:
        return StateYear(
            code=code,
            rate=scenario.state_custom_rate,
            zero_bracket=0.0,
            from_federal_taxable=False,
            deduction=0.0,
            low_agi_extra=0.0,
            low_agi_limit=0.0,
            agi_limit=float("inf"),
            exempt_ss=scenario.state_custom_exempt_ss,
            pension_exempt_frac=1.0 if scenario.state_custom_exempt_pensions else 0.0,
            ira_exempt_frac=1.0 if scenario.state_custom_exempt_ira else 0.0,
            exclusion=0.0,
            covers=frozenset(),
            pension=hy.retirement,
            other=other,
        )

    d = _STATES[code]
    if d["kind"] == KIND_NONE:
        return None

    def frac(key: str) -> float:
        if key not in d:
            return 0.0
        return sum(1 for a in living_ages if a >= d[key]) / n_living

    n65 = sum(1 for a in living_ages if a >= 65)
    single = hy.single_filer
    deduction = (
        (d.get("deduction_single", 0.0) if single else d.get("deduction_mfj", 0.0))
        + d.get("deduction_per_person", 0.0) * len(living_ages)
        + d.get("deduction_per_65_plus", 0.0) * n65
    )
    exclusion = 0.0
    for a in living_ages:
        amount = 0.0
        for min_age, dollars in d.get("retirement_exclusion", []):
            if a >= min_age:
                amount = float(dollars)
        exclusion += amount
    limit_key = "exemption_agi_limit_single" if single else "exemption_agi_limit_mfj"
    return StateYear(
        code=code,
        rate=float(d["rate"]),
        zero_bracket=float(d.get("zero_bracket", 0.0)),
        from_federal_taxable=d.get("start", "federal_agi") == "federal_taxable_income",
        deduction=deduction,
        low_agi_extra=d.get("extra_65_plus_low_agi", 0.0) * n65,
        low_agi_limit=d.get("low_agi_limit", 0.0),
        agi_limit=float(d.get(limit_key, float("inf"))),
        exempt_ss=bool(d.get("exempt_social_security", False)),
        pension_exempt_frac=frac("pension_exempt_min_age"),
        ira_exempt_frac=frac("ira_exempt_min_age"),
        exclusion=exclusion,
        covers=frozenset(d.get("exclusion_covers", ())),
        pension=hy.retirement,
        other=other,
    )


def state_tax(
    sy: StateYear,
    agi: float,
    federal_taxable: float,
    taxable_ss: float,
    ira: float,
    ltcg: float,
) -> float:
    """The state bill for one year.

    ``agi`` is federal AGI, ``federal_taxable`` federal taxable income (both
    including ``taxable_ss``), ``ira`` the year's tax-deferred account
    distributions and ``ltcg`` its long-term gains.
    """
    base = federal_taxable if sy.from_federal_taxable else agi
    if sy.exempt_ss:
        base -= taxable_ss
    pension_left = sy.pension
    ira_left = ira
    if sy.pension_exempt_frac:
        base -= sy.pension * sy.pension_exempt_frac
        pension_left = sy.pension * (1.0 - sy.pension_exempt_frac)
    if sy.ira_exempt_frac:
        base -= ira * sy.ira_exempt_frac
        ira_left = ira * (1.0 - sy.ira_exempt_frac)
    if sy.exclusion > 0.0:
        covered = 0.0
        if "pension" in sy.covers:
            covered += pension_left
        if "ira" in sy.covers:
            covered += ira_left
        if "ltcg" in sy.covers:
            covered += ltcg
        if "other" in sy.covers:
            covered += sy.other
        base -= sy.exclusion if sy.exclusion < covered else covered
    deduction = 0.0 if agi > sy.agi_limit else sy.deduction
    if sy.low_agi_extra and agi < sy.low_agi_limit:
        deduction += sy.low_agi_extra
    taxable = base - deduction - sy.zero_bracket
    if taxable <= 0.0:
        return 0.0
    return sy.rate * taxable
