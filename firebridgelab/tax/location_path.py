"""Run the dragged tax-location decomposition over a whole path.

``simulate_path_decomposition`` steps ``step_buckets_dragged`` (``tax.location``,
whose module docstring holds the per-year operand order, fixed point and
conventions) across one return path and records the per-age series in a
``TaxLocationPath``. ``simulate_tax_location_deterministic`` is the single
deterministic line; ``simulate_dragged_depletion_only`` is the solver's lean
depletion-only variant; ``year_tax_context_table`` precomputes the
path-independent per-age inputs once per scenario.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from firebridgelab.engine.step import max_fundable_spend, net_return
from firebridgelab.tax.location import (
    PRE_595_MAX_AGE,
    TAX_LOCATION_ACCESS_AGE,
    _filing_profile,
    _other_ordinary_and_ltcg,
    _pension_income_at_age,
    _ss_income_at_age,
    resolve_split,
    step_buckets_dragged,
)


@dataclass(frozen=True)
class TaxLocationPath:
    """One path's tax-location decomposition (deterministic line, or one cohort/trial).

    ``taxable`` / ``tax_deferred`` / ``roth`` are per-age END-of-year DRAGGED
    balances aligned to ``ages`` (start_age .. end_age), reflecting the
    portfolio paying its own tax bill. The depletion facts are the per-path
    signal the headline metric aggregates (computed off the dragged natural
    balances):

      - ``taxable_depletion_age`` — earliest age (<= 59) at which the taxable
        balance hit 0, or None if it never did pre-59½.
      - ``reachable_depletion_age`` — earliest age (<= 59) at which the REACHABLE
        money (taxable + roth) hit 0, or None — the forced-tax-deferred-draw
        event (the bridge failing).
      - ``dragged_depleted_age`` — the earliest age (FULL horizon,
        not pre-59½-limited) at which the DRAGGED canonical total
        (``taxable+tax_deferred+roth``) hit 0, or None if it never depleted. The
        tax-drag analogue of ``_simulate_cohort``'s bare ``depleted_age`` — for
        split scenarios the Historical/MC portfolio fan IS the dragged total, and
        this is what recomputes ``depletion_count`` / ``earliest_depletion_age``
        off the DRAGGED world instead of the pre-tax ``_simulate_cohort`` pass.
        Distinct from
        ``taxable_depletion_age`` / ``reachable_depletion_age`` above (which are
        pre-59½-only and measure a NARROWER "freely reachable money" signal, not
        whether the whole portfolio ran out).
      - ``forced_pre_595_draw`` — True iff a pre-59½ year had to source from
        tax_deferred, EITHER for spending OR for the tax bill itself (the
        bill can force a walled-year tax-deferred draw that wouldn't
        otherwise have happened).

    Per-age DRAW + spending series:
    ``taxable_draw`` / ``tax_deferred_draw`` / ``roth_draw`` are the SPENDING-
    driven natural per-account amounts (never the tax-funding draws — see
    ``DraggedYearStep``); ``spending`` is the year's INTENDED spend — the
    strategy's raw output, exactly what the fixed point and the dragged-total
    recurrence step with (the reconciliation tests pin the recurrence against
    THIS series, so it must never be capped).

      - ``realized_spend`` — the year's REALIZED spend: ``min(spending,
        max_fundable_spend(dragged_begin_balance, r, gi))``, mirroring
        ``engine.sim_core._simulate_cohort``'s realized-spend discipline (same
        ``engine.step.max_fundable_spend`` formula, same begin-of-year
        operand) applied to the DRAGGED balance. Equal to ``spending`` on
        every non-depleting year; in the depletion year it caps at what the
        dragged balance could actually fund; post-depletion it is the
        affordable level (``min(intent, gi)`` — as low as 0.0 before
        GI starts). This is the CONSUMPTION series — the spend fan,
        worst-cohort drill-down, adjustment stats, stress trajectory points,
        the gross-outflow line, and the overlay's ``effective_rate`` all read
        it (recording the intended spend there would overstate after-tax
        consumption on a $0 portfolio and invert the worst-cohort ranking).
        REPORTING ONLY: the engine still steps with ``spending`` (identical
        dragged totals — the same argument ``max_fundable_spend``'s docstring
        makes for the scalar engines).

    The dragged decomposition's tax series, all per-age and aligned to
    ``ages``:

      - ``pretax_total`` — the PAIRED pre-tax total: the same year's
        spend/gi/r with NO tax ever subtracted, run in parallel with its OWN
        absorbing-depletion floor (see the module docstring's "PAIRED PRE-TAX
        TOTAL"). The "estate cost of taxes" readout pairs this against
        ``taxable+tax_deferred+roth`` per path.
      - ``rmd_excess`` — the amount moved gross, tax_deferred -> taxable, this
        year because the actual tax-deferred draw fell short of the RMD.
        0.0 outside age >= 75.
      - ``lever_transfer`` — the amount moved gross, tax_deferred -> taxable,
        this year by the voluntary bracket-fill lever. 0.0
        outside ages 60-74 or when ``bracket_fill_target`` is None. At most
        one of ``rmd_excess`` / ``lever_transfer`` is ever nonzero in a given
        year (the two windows never overlap).
      - ``ordinary_income`` / ``ltcg_income`` — the year's taxable-income
        components the fixed point converged on (pre-deduction).
      - ``tax`` — the converged fixed-point bill, INCLUDING the 10%
        early-withdrawal penalty. 0.0 once the path has already
        depleted in a PRIOR year (the post-depletion convention).
      - ``spending_tax`` / ``voluntary_tax`` / ``forced_tax`` — the exact
        three-way telescoping attribution:
        ``spending_tax + voluntary_tax + forced_tax == tax`` at every age,
        EXACT ``==`` by construction (never a re-run fixed point).
      - ``penalty`` — the 10% early-withdrawal penalty alone (a SUBSET of
        ``tax``, not additional to it — see ``gross_outflow`` below).
      - ``gross_outflow`` — "what the portfolio actually funds this year" =
        ``realized_spend + tax`` (the REALIZED consumption, so a
        depleted path never shows the portfolio "funding" a six-figure target
        it doesn't hold; post-depletion years contribute zero tax per the
        skip convention above, so the line falls to the GI-affordable level).
        The tax component stays the recorded ATTEMPTED bill, so the identity
        ``gross_outflow − realized_spend == tax`` holds EXACTLY at every age
        (the chart tooltip decomposes on it) — the one cost is that in the
        single crossing year (spending senior, bill junior, accounts run dry
        mid-bill) the gross line slightly overstates the FUNDED outflow by
        the bill's unfundable remainder. NOTE this is NOT ``realized_spend +
        tax + penalty`` (``tax`` already has the penalty folded in via
        ``spending_tax`` — see ``DraggedYearStep``'s docstring and
        ``compute_federal_tax``'s ``FederalTaxYear.total``, which excludes
        the penalty; the ATTRIBUTION-level ``tax`` field is the sum INCLUDING
        it). Double-adding here would overstate the true portfolio draw by
        exactly the penalty each walled year.
    """

    ages: list[int]
    taxable: list[float]
    tax_deferred: list[float]
    roth: list[float]
    taxable_depletion_age: int | None
    reachable_depletion_age: int | None
    forced_pre_595_draw: bool
    # See the class docstring's "dragged_depleted_age" entry.
    # Defaulted (not every construction site needs to pass it — deterministic
    # single-path callers rarely read it) but always populated by
    # ``simulate_path_decomposition``.
    dragged_depleted_age: int | None = None

    taxable_draw: list[float] = field(default_factory=list)
    tax_deferred_draw: list[float] = field(default_factory=list)
    roth_draw: list[float] = field(default_factory=list)
    spending: list[float] = field(default_factory=list)
    # The REALIZED (fundable-capped) consumption series — see the class
    # docstring. spending = intent (the math input); realized_spend = what was
    # actually affordable (the reporting output).
    realized_spend: list[float] = field(default_factory=list)

    # Tax series (see the class docstring above).
    pretax_total: list[float] = field(default_factory=list)
    rmd_excess: list[float] = field(default_factory=list)
    lever_transfer: list[float] = field(default_factory=list)
    ordinary_income: list[float] = field(default_factory=list)
    ltcg_income: list[float] = field(default_factory=list)
    tax: list[float] = field(default_factory=list)
    spending_tax: list[float] = field(default_factory=list)
    voluntary_tax: list[float] = field(default_factory=list)
    forced_tax: list[float] = field(default_factory=list)
    penalty: list[float] = field(default_factory=list)
    gross_outflow: list[float] = field(default_factory=list)

    # M4 tax extensions, per age (all zeros when the options are off):
    # ``roth_conversion`` the tax-deferred -> Roth transfer; ``taxable_ss`` the
    # taxable part of Social Security inside ``ordinary_income``;
    # ``state_tax`` / ``payroll_tax`` the shares of ``tax`` they make up;
    # ``irmaa`` the Medicare surcharge paid that year (required spending, so
    # it is inside ``spending``, not ``tax``); ``magi`` the year's federal AGI
    # (``ordinary_income + ltcg_income``), which IRMAA reads two years later.
    roth_conversion: list[float] = field(default_factory=list)
    taxable_ss: list[float] = field(default_factory=list)
    state_tax: list[float] = field(default_factory=list)
    payroll_tax: list[float] = field(default_factory=list)
    irmaa: list[float] = field(default_factory=list)
    magi: list[float] = field(default_factory=list)


class YearTaxContext(NamedTuple):
    """The path-independent per-age inputs to ``step_buckets_dragged``.

    The first five are the original tax context. ``pension_ss_ordinary`` is
    the year's ordinary income from streams and cash events, less the
    half-self-employment-tax deduction, plus the flat 85% of Social Security
    under ``ss_taxation = "flat85"``; under ``"provisional"`` the benefits ride
    in ``ss_benefits`` and the fixed point taxes them. ``fixed_tax`` is the
    payroll tax; ``state`` the ``tax.state.StateYear`` (or None);
    ``conversion_target`` the Roth conversion target in dollars of taxable
    income when this age is in the conversion window (else None).
    """

    penalty_free: bool
    n_65_plus: int
    pension_ss_ordinary: float
    single: bool
    extra_ltcg: float
    ss_benefits: float = 0.0
    ss_flat_taxable: float = 0.0
    threshold_scale: float = 1.0
    fixed_tax: float = 0.0
    state: object = None
    conversion_target: Optional[float] = None


def _step_kwargs(ctx: YearTaxContext) -> dict:
    """The keyword arguments ``step_buckets_dragged`` takes from a context."""
    return dict(
        pension_ss_ordinary=ctx.pension_ss_ordinary,
        n_65_plus=ctx.n_65_plus,
        single=ctx.single,
        extra_ltcg=ctx.extra_ltcg,
        ss_benefits=ctx.ss_benefits,
        ss_flat_taxable=ctx.ss_flat_taxable,
        threshold_scale=ctx.threshold_scale,
        fixed_tax=ctx.fixed_tax,
        state=ctx.state,
        conversion_target=ctx.conversion_target,
    )


def irmaa_for_year(scenario, age: int, magi_by_age: dict, context) -> float:
    """The Medicare IRMAA surcharge paid in the plan year ``age``, from the
    MAGI of ``age - 2`` (``tax.irmaa``). ``magi_by_age`` holds the path's
    recorded MAGI; ``context`` maps an age to its ``YearTaxContext`` (for the
    lookback year's filing status). Before the plan has two years of its own
    income, ``Scenario.irmaa_prior_magi`` is used (None: no surcharge)."""
    if not scenario.irmaa:
        return 0.0
    from firebridgelab.income.household import household
    from firebridgelab.tax.irmaa import LOOKBACK_YEARS, MEDICARE_AGE, annual_surcharge

    h = household(scenario)
    enrollees = sum(1 for p in h.living(age) if h.age_of(p.id, age) >= MEDICARE_AGE)
    if not enrollees:
        return 0.0
    look = age - LOOKBACK_YEARS
    if look < scenario.start_age:
        magi = scenario.irmaa_prior_magi
        if magi is None:
            return 0.0
        single = context(age).single
    else:
        magi = magi_by_age.get(look, 0.0)
        single = context(look).single
    return annual_surcharge(
        magi, single, enrollees, age - scenario.start_age, scenario.assumed_inflation
    )


def year_tax_context_table(scenario) -> dict[int, YearTaxContext]:
    """Precompute the per-age ``YearTaxContext`` (``_year_tax_context``) ONCE per scenario, keyed by age (a Monte Carlo hot-path
    optimization).

    All three depend ONLY on ``scenario`` + ``age`` — NEVER on the return path,
    the spend, or which cohort/trial is running — so every path in a
    Historical/MC run can share ONE lookup table instead of each path
    recomputing it fresh at every age (otherwise a 10k-trial MC run calls
    ``_pension_income_at_age`` / ``_ss_income_at_age`` 10,000x per age, all
    returning the identical number). ``simulate_path_decomposition``
    / ``simulate_dragged_depletion_only`` accept this table as the optional
    ``year_context`` — passing ``None`` (the default) recomputes the same
    values inline (the deterministic single path, the hand-anchored unit
    tests).
    """
    table: dict[int, YearTaxContext] = {}
    for age in range(scenario.start_age, scenario.end_age + 1):
        table[age] = _year_tax_context(scenario, age)
    return table


def _year_tax_context(scenario, age: int) -> YearTaxContext:
    """One year's ``YearTaxContext``: the account-access flag, the filing
    profile, the stream ordinary income (plus 85% of Social Security under the
    flat rule) plus any cash event taxed as ordinary, a cash event's long-term
    gain (all from ``income.household``), and the M4 inputs."""
    from firebridgelab.income.household import household_year
    from firebridgelab.tax.ss_taxation import FLAT_INCLUSION, SS_TAXATION_PROVISIONAL
    from firebridgelab.tax.thresholds import nominal_scale

    penalty_free = age >= TAX_LOCATION_ACCESS_AGE
    n_65_plus, single = _filing_profile(scenario, age)
    ss = _ss_income_at_age(scenario, age)
    provisional = scenario.ss_taxation == SS_TAXATION_PROVISIONAL
    if provisional:
        pension_ss_ordinary = _pension_income_at_age(scenario, age)
        ss_flat_taxable = 0.0
    else:
        ss_flat_taxable = FLAT_INCLUSION * ss
        pension_ss_ordinary = _pension_income_at_age(scenario, age) + ss_flat_taxable
    event_ordinary, extra_ltcg = _other_ordinary_and_ltcg(scenario, age)
    if event_ordinary:
        pension_ss_ordinary += event_ordinary
    scale = nominal_scale(scenario, age)

    fixed_tax = 0.0
    if scenario.payroll_tax:
        from firebridgelab.tax.payroll import payroll_tax

        hy = household_year(scenario, age)
        pay = payroll_tax(hy.wages, hy.self_employment, single, scale)
        fixed_tax = pay.tax
        pension_ss_ordinary -= pay.se_deduction

    state = None
    if scenario.state is not None or scenario.state_after_move is not None:
        from firebridgelab.tax.state import state_year

        state = state_year(scenario, age)

    conversion_target = None
    if scenario.roth_conversion_target is not None:
        from firebridgelab.tax.roth import conversion_target_at_age

        conversion_target = conversion_target_at_age(scenario, age, single)

    return YearTaxContext(
        penalty_free,
        n_65_plus,
        pension_ss_ordinary,
        single,
        extra_ltcg,
        ss_benefits=ss if provisional else 0.0,
        ss_flat_taxable=ss_flat_taxable,
        threshold_scale=scale,
        fixed_tax=fixed_tax,
        state=state,
        conversion_target=conversion_target,
    )


def simulate_path_decomposition(
    scenario,
    returns,
    strategy,
    *,
    year_context: dict[int, tuple] | None = None,
) -> TaxLocationPath:
    """Run the DRAGGED decomposition for ONE path (deterministic / cohort / trial).

    ``returns`` is the per-age real-return sequence (one entry per age step,
    start_age .. end_age) — the SAME sequence the scalar path for this line
    uses. ``strategy`` is the path's spending strategy (already built), read
    against the DRAGGED begin-of-year balance (for constant/lifecycle this is
    a no-op since spend ignores the balance). ``year_context`` is the OPTIONAL
    precomputed ``year_tax_context_table`` — Historical/MC callers build it
    ONCE per scenario and pass it to every cohort/trial; ``None`` (the
    default) recomputes the same values inline per age.

    Per year: grow -> source spending -> resolve the fixed
    point (transfers + tax, ``step_buckets_dragged`` — see its docstring and
    the module docstring for the full argument) -> compute the dragged
    canonical total (with absorbing depletion) -> reconcile the three buckets
    to it exactly (roth as the ULP balancer) -> advance the PAIRED pre-tax total in parallel.

    POST-DEPLETION SHORT-CIRCUIT: once the dragged total has hit 0 in a PRIOR
    year, this loop skips ``step_buckets_dragged`` entirely for every
    subsequent year (cheap AND correct — see the module docstring's
    "post-depletion convention": a naive call would compute a real, nonzero
    bill on GI alone, which the convention says to suppress).
    """
    from firebridgelab.income.household import household_year
    from firebridgelab.spending.lifecycle import spending_multiplier

    taxable, tax_deferred, roth = resolve_split(scenario)
    gains_fraction = scenario.taxable_gains_fraction
    bracket_fill_target = scenario.bracket_fill_target
    # The very first simulated year's RMD (if any) reads the scenario's
    # INITIAL tax-deferred balance as "last year's end balance" (no prior
    # simulated year exists).
    initial_tax_deferred = tax_deferred

    ages: list[int] = []
    taxable_series: list[float] = []
    tax_deferred_series: list[float] = []
    roth_series: list[float] = []
    taxable_depletion_age: int | None = None
    reachable_depletion_age: int | None = None
    forced_any = False
    taxable_draw_series: list[float] = []
    tax_deferred_draw_series: list[float] = []
    roth_draw_series: list[float] = []
    spending_series: list[float] = []
    realized_spend_series: list[float] = []
    pretax_total_series: list[float] = []
    rmd_excess_series: list[float] = []
    lever_transfer_series: list[float] = []
    ordinary_income_series: list[float] = []
    ltcg_income_series: list[float] = []
    tax_series: list[float] = []
    spending_tax_series: list[float] = []
    voluntary_tax_series: list[float] = []
    forced_tax_series: list[float] = []
    penalty_series: list[float] = []
    gross_outflow_series: list[float] = []

    # The DRAGGED canonical running total (the reconciliation target) and its
    # absorbing-depletion flag (the tax bill can also trigger it).
    dragged_portfolio = scenario.initial_portfolio
    dragged_depleted = False
    # The FIRST age depletion actually happens (full horizon), for the
    # Historical/MC fan re-point's depletion-count aggregation. Distinct from
    # ``taxable_depletion_age``/``reachable_depletion_age`` (pre-59½-only).
    dragged_depleted_age: int | None = None
    # The PAIRED pre-tax total: the SAME per-year spend/gi/r, no tax ever
    # subtracted, its OWN independent absorbing-depletion flag.
    pretax_portfolio = scenario.initial_portfolio
    pretax_depleted = False

    contexts: dict = year_context if year_context is not None else {}

    def ctx_at(a: int) -> YearTaxContext:
        c = contexts.get(a)
        if c is None:
            c = _year_tax_context(scenario, a)
            contexts[a] = c
        return c

    magi_by_age: dict[int, float] = {}
    roth_conversion_series: list[float] = []
    taxable_ss_series: list[float] = []
    state_tax_series: list[float] = []
    payroll_tax_series: list[float] = []
    irmaa_series: list[float] = []
    magi_series: list[float] = []

    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        r = net_return(float(returns[step]), scenario.annual_fee)
        # Household flows (``income.household``): the strategy reads income net
        # of required spending; the required amount is added back after the
        # drift multiplier, and a cash event joins GI as a mid-year flow — the
        # SAME order as engine.sim_core._simulate_cohort.
        hy = household_year(scenario, age)
        # Medicare IRMAA (``tax.irmaa``) is required spending, set by the MAGI
        # this path recorded two years ago.
        irmaa = irmaa_for_year(scenario, age, magi_by_age, ctx_at) if scenario.irmaa else 0.0
        required = hy.required_spending + irmaa
        # The strategy reads the DRAGGED begin-of-year balance.
        spending = strategy.spend_for_year(
            scenario, age, dragged_portfolio, hy.gi - required
        )
        # Cost-of-living real drift — mirrors engine.sim_core._simulate_cohort's
        # post-strategy multiplier EXACTLY; KEEP IN LOCKSTEP (same guard: none —
        # unconditional for every spending mode, incl. amortization, whose
        # strategy solves drift-aware and EXPECTS this post-multiply — see
        # spending.rules.amortization; same position: immediately after
        # spend_for_year, before the realized cap and the fixed point; same
        # operand order; same start_age-anchored exponent — the escalation
        # anchors at the SIMULATION start, never a re-solve age). A drift here
        # the scalar loops don't share (or vice versa) makes a split run
        # disagree with its own pre-tax counterfactual. A no-op at the default
        # drift of 0.0 ((1+0)**k == 1.0).
        spending *= spending_multiplier(scenario, age)
        spending += required
        gi = hy.gi + hy.cash_event
        # Realized-spend cap (mirrors engine.sim_core._simulate_cohort's
        # discipline on the DRAGGED begin-of-year balance; see the
        # TaxLocationPath docstring): the strategy returns the INTENDED spend;
        # a depleting year can only fund what the dragged balance + GI cover.
        # REPORTING ONLY — the fixed point and the dragged-total recurrence
        # below still step with the INTENDED ``spending`` (identical totals;
        # only the recorded consumption dollars change).
        realized_spend = min(spending, max_fundable_spend(dragged_portfolio, r, gi))

        ctx = ctx_at(age)
        prior_tax_deferred = (
            tax_deferred_series[-1] if tax_deferred_series else initial_tax_deferred
        )

        was_already_depleted = dragged_depleted

        if was_already_depleted:
            # Post-depletion convention (module docstring): no portfolio, no
            # draws, no transfers, no tax — even though GI would be real-world
            # taxable, the model stops charging a portfolio that isn't there.
            taxable = tax_deferred = roth = 0.0
            year_tax = 0.0
            step_result = None
        else:
            step_result = step_buckets_dragged(
                taxable,
                tax_deferred,
                roth,
                r,
                gi,
                spending,
                ctx.penalty_free,
                gains_fraction=gains_fraction,
                age=age,
                bracket_fill_target=bracket_fill_target,
                prior_tax_deferred=prior_tax_deferred,
                **_step_kwargs(ctx),
            )
            taxable = step_result.taxable
            tax_deferred = step_result.tax_deferred
            roth = step_result.roth
            year_tax = step_result.tax
            magi_by_age[age] = step_result.ordinary_income + step_result.ltcg_income

        # The DRAGGED canonical total (step 5): grown start + gi - spend - T,
        # floored at 0 with the same absorbing override the scalar engines use.
        growth = dragged_portfolio * r
        dragged_end = (
            dragged_portfolio + growth + gi * (1 + r / 2) - spending * (1 + r / 2) - year_tax
        )
        if dragged_end < 0:
            dragged_end = 0.0
            if not dragged_depleted:
                dragged_depleted_age = age
            dragged_depleted = True
        elif dragged_depleted:
            dragged_end = 0.0

        # The PAIRED pre-tax total: identical formula, T never subtracted, its
        # own independent absorbing floor.
        pretax_growth = pretax_portfolio * r
        pretax_end = pretax_portfolio + pretax_growth + gi * (1 + r / 2) - spending * (1 + r / 2)
        if pretax_end < 0:
            pretax_end = 0.0
            pretax_depleted = True
        elif pretax_depleted:
            pretax_end = 0.0

        # EXACT-SUM RECONSTRUCTION, telescoping to the DRAGGED total: roth is
        # the ULP
        # balancer, same defensive re-homing to tax_deferred if it goes
        # negative. Depletion facts below read the NATURAL (pre-nudge)
        # balances, so a ~1-ULP nudge can never create or erase a depletion
        # event.
        nat_taxable, nat_tax_deferred, nat_roth = taxable, tax_deferred, roth
        if dragged_end == 0.0:
            taxable = tax_deferred = roth = 0.0
        else:
            taxable, tax_deferred = nat_taxable, nat_tax_deferred
            roth = dragged_end - (nat_taxable + nat_tax_deferred)
            if roth < 0:
                roth = 0.0
                tax_deferred = dragged_end - taxable
                if tax_deferred < 0:
                    tax_deferred = 0.0
                    taxable = dragged_end

        dragged_portfolio = dragged_end
        pretax_portfolio = pretax_end

        ages.append(age)
        taxable_series.append(taxable)
        tax_deferred_series.append(tax_deferred)
        roth_series.append(roth)
        taxable_draw_series.append(step_result.taxable_draw if step_result else 0.0)
        tax_deferred_draw_series.append(
            step_result.tax_deferred_draw if step_result else 0.0
        )
        roth_draw_series.append(step_result.roth_draw if step_result else 0.0)
        spending_series.append(spending)
        realized_spend_series.append(realized_spend)
        pretax_total_series.append(pretax_end)
        rmd_excess_series.append(step_result.rmd_excess if step_result else 0.0)
        lever_transfer_series.append(
            step_result.lever_transfer if step_result else 0.0
        )
        ordinary_income_series.append(
            step_result.ordinary_income if step_result else 0.0
        )
        ltcg_income_series.append(step_result.ltcg_income if step_result else 0.0)
        tax_series.append(year_tax)
        spending_tax_series.append(step_result.spending_tax if step_result else 0.0)
        voluntary_tax_series.append(
            step_result.voluntary_tax if step_result else 0.0
        )
        forced_tax_series.append(step_result.forced_tax if step_result else 0.0)
        penalty_series.append(step_result.penalty if step_result else 0.0)
        # REALIZED consumption + the attempted bill — see the gross_outflow
        # field docstring for the exact identity this preserves
        # (gross_outflow − realized_spend == tax) and the one crossing-year
        # overstatement it accepts.
        gross_outflow_series.append(realized_spend + year_tax)
        roth_conversion_series.append(step_result.roth_conversion if step_result else 0.0)
        taxable_ss_series.append(step_result.taxable_ss if step_result else 0.0)
        state_tax_series.append(step_result.state_tax if step_result else 0.0)
        payroll_tax_series.append(step_result.payroll_tax if step_result else 0.0)
        irmaa_series.append(irmaa)
        magi_series.append(magi_by_age.get(age, 0.0))

        # Depletion facts read from the NATURAL (pre-nudge) balances; only
        # count pre-59½ (age <= 59) for the headline metric.
        pre_595 = age <= PRE_595_MAX_AGE
        if pre_595 and taxable_depletion_age is None and nat_taxable <= 0.0:
            taxable_depletion_age = age
        if (
            pre_595
            and reachable_depletion_age is None
            and (nat_taxable + nat_roth) <= 0.0
        ):
            reachable_depletion_age = age
        if step_result is not None and step_result.forced_pre_595_draw and pre_595:
            forced_any = True

    return TaxLocationPath(
        ages=ages,
        taxable=taxable_series,
        tax_deferred=tax_deferred_series,
        roth=roth_series,
        taxable_depletion_age=taxable_depletion_age,
        reachable_depletion_age=reachable_depletion_age,
        forced_pre_595_draw=forced_any,
        dragged_depleted_age=dragged_depleted_age,
        taxable_draw=taxable_draw_series,
        tax_deferred_draw=tax_deferred_draw_series,
        roth_draw=roth_draw_series,
        spending=spending_series,
        realized_spend=realized_spend_series,
        pretax_total=pretax_total_series,
        rmd_excess=rmd_excess_series,
        lever_transfer=lever_transfer_series,
        ordinary_income=ordinary_income_series,
        ltcg_income=ltcg_income_series,
        tax=tax_series,
        spending_tax=spending_tax_series,
        voluntary_tax=voluntary_tax_series,
        forced_tax=forced_tax_series,
        penalty=penalty_series,
        gross_outflow=gross_outflow_series,
        roth_conversion=roth_conversion_series,
        taxable_ss=taxable_ss_series,
        state_tax=state_tax_series,
        payroll_tax=payroll_tax_series,
        irmaa=irmaa_series,
        magi=magi_series,
    )


def deterministic_blended_returns(scenario) -> list[float]:
    """The per-age CONSTANT-blend return sequence the deterministic engine uses.

    Shared by ``simulate_tax_location_deterministic`` and the solver's dragged
    deterministic probe (``analysis.solver``), so both build the SAME return
    sequence ``engine.deterministic.simulate`` feeds ``step_year`` — never a second,
    possibly-drifting derivation of "what deterministic returns are".
    """
    from firebridgelab.engine.allocation import blended_real_return_at_age

    return [
        blended_real_return_at_age(scenario, age)
        for age in range(scenario.start_age, scenario.end_age + 1)
    ]


def simulate_tax_location_deterministic(scenario) -> TaxLocationPath:
    """Deterministic tax-location decomposition (the single deterministic line).

    Builds the per-age CONSTANT-blend return sequence the deterministic engine
    uses (``deterministic_blended_returns`` — the SAME seam ``simulate`` feeds
    ``step_year``) and a fresh spending strategy (``make_strategy`` — the SAME
    the deterministic engine builds), then runs the dragged decomposition.
    Caller is responsible for only invoking this when ``has_tax_split`` is True
    and the scenario is in single sourcing mode.
    """
    from firebridgelab.spending.registry import make_strategy

    returns = deterministic_blended_returns(scenario)
    strategy = make_strategy(scenario)
    return simulate_path_decomposition(scenario, returns, strategy)


def dragged_deterministic_trajectory(scenario, *, path: TaxLocationPath | None = None):
    """The deterministic projection with federal tax paid from the portfolio.

    For a scenario with a tax-location split in single sourcing (the caller
    checks), this is the deterministic main line: the same ``Trajectory`` shape
    ``engine.deterministic.simulate`` returns, built from the dragged
    decomposition. ``start_portfolio`` and ``growth = start * r`` come from the
    same deterministic return sequence the decomposition used; ``spending`` is
    the realized (fundable-capped) spend; ``end_portfolio`` is the after-tax
    total of the three accounts; ``depleted_age`` is the dragged depletion age.
    Pass ``path`` when ``simulate_tax_location_deterministic`` has already run.
    """
    from firebridgelab.engine.deterministic import Trajectory, YearPoint
    from firebridgelab.income.guaranteed import gi_at_age

    if path is None:
        path = simulate_tax_location_deterministic(scenario)
    returns = deterministic_blended_returns(scenario)
    totals = [
        path.taxable[i] + path.tax_deferred[i] + path.roth[i] for i in range(len(path.ages))
    ]
    points = []
    start = scenario.initial_portfolio
    for i, age in enumerate(path.ages):
        r = float(returns[i])
        points.append(
            YearPoint(
                age=age,
                start_portfolio=start,
                growth=start * r,
                spending=path.realized_spend[i],
                guaranteed_income=gi_at_age(scenario, age),
                end_portfolio=totals[i],
            )
        )
        start = totals[i]
    return Trajectory(
        points=points,
        depleted_age=path.dragged_depleted_age,
        final_portfolio=totals[-1] if totals else scenario.initial_portfolio,
    )


def simulate_dragged_depletion_only(
    scenario,
    returns,
    strategy,
    *,
    year_context: dict[int, tuple] | None = None,
) -> int | None:
    """Lean, depletion-only dragged run — the solver's inner-loop primitive.

    ``year_context`` is the OPTIONAL precomputed ``year_tax_context_table`` —
    since ``annual_spending`` never enters that
    table, the SAME table serves every bisection probe of a solve, built once
    by the caller (mirrors ``simulate_path_decomposition``'s optimization).

    The tax-drag analogue of ``engine.sim_core._simulate_cohort``'s bare
    depletion check: mirrors ``simulate_path_decomposition``'s per-year setup
    EXACTLY (same ``penalty_free`` / ``n_65_plus`` / ``pension_ss_ordinary``
    derivation, same ``prior_tax_deferred`` carry, same dragged-total formula)
    and calls the IDENTICAL ``step_buckets_dragged`` every year — the fixed
    point itself is never re-derived, only two things are skipped because the
    solver needs just one bit ("did this path ever deplete, and when"):

      1. **No per-age SERIES recording** — no ``taxable``/``tax_deferred``/
         ``roth``/``tax``/attribution lists, no ``DraggedYearStep`` fields kept
         beyond the three balances + the bill, no exact-sum reconciliation
         (that nudge is for DISPLAY precision; the solver only needs the
         aggregate total, which is already exact from the running scalar).
      2. **Early exit on depletion** — once the dragged total goes negative the
         remaining years' fixed points are moot (the solver only asks "did it
         ever deplete"), so this returns immediately instead of continuing to
         record zeroed years the way the display path must (it still owes a
         value for every remaining age).

    Both are real savings ``simulate_path_decomposition`` cannot take (it must
    keep a full, display-ready series for every age, including post-depletion
    zeros) — the precompute-once discipline ``analysis.solver``'s module
    docstring describes for the plain engine, carried over to the dragged one.

    Returns the first depleted age, or ``None`` if the path never depletes.
    """
    from firebridgelab.income.household import household_year
    from firebridgelab.spending.lifecycle import spending_multiplier

    taxable, tax_deferred, roth = resolve_split(scenario)
    gains_fraction = scenario.taxable_gains_fraction
    bracket_fill_target = scenario.bracket_fill_target
    # First year's RMD reads the scenario's INITIAL tax-deferred balance as
    # "last year's end balance" — same convention as the full loop.
    prior_tax_deferred = tax_deferred

    dragged_portfolio = scenario.initial_portfolio
    contexts: dict = year_context if year_context is not None else {}

    def ctx_at(a: int) -> YearTaxContext:
        c = contexts.get(a)
        if c is None:
            c = _year_tax_context(scenario, a)
            contexts[a] = c
        return c

    magi_by_age: dict[int, float] = {}

    for age in range(scenario.start_age, scenario.end_age + 1):
        step = age - scenario.start_age
        r = net_return(float(returns[step]), scenario.annual_fee)
        hy = household_year(scenario, age)
        irmaa = irmaa_for_year(scenario, age, magi_by_age, ctx_at) if scenario.irmaa else 0.0
        required = hy.required_spending + irmaa
        spending = strategy.spend_for_year(
            scenario, age, dragged_portfolio, hy.gi - required
        )
        # Cost-of-living real drift — the SAME post-strategy multiplier the
        # full loop (simulate_path_decomposition) and engine.sim_core._simulate_cohort
        # apply; KEEP ALL THREE IN LOCKSTEP (this lean solver loop mirrors the
        # full loop's per-year setup EXACTLY, per this function's docstring —
        # a drift applied there but not here would make the solver bisect a
        # DIFFERENT spend path than the display engine renders). Inert at the
        # default drift of 0.0.
        spending *= spending_multiplier(scenario, age)
        spending += required
        gi = hy.gi + hy.cash_event

        ctx = ctx_at(age)
        step_result = step_buckets_dragged(
            taxable,
            tax_deferred,
            roth,
            r,
            gi,
            spending,
            ctx.penalty_free,
            gains_fraction=gains_fraction,
            age=age,
            bracket_fill_target=bracket_fill_target,
            prior_tax_deferred=prior_tax_deferred,
            **_step_kwargs(ctx),
        )
        magi_by_age[age] = step_result.ordinary_income + step_result.ltcg_income
        taxable = step_result.taxable
        tax_deferred = step_result.tax_deferred
        roth = step_result.roth

        growth = dragged_portfolio * r
        dragged_end = (
            dragged_portfolio + growth + gi * (1 + r / 2) - spending * (1 + r / 2) - step_result.tax
        )
        if dragged_end < 0:
            return age
        dragged_portfolio = dragged_end
        prior_tax_deferred = tax_deferred

    return None
