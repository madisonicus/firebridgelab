"""Tax-location decomposition with access-aware sourcing and tax drag.

When a tax-location split is supplied, the portfolio is three accounts
(taxable / tax-deferred / Roth) and the year's federal tax bill is drawn from
them like any other outflow. This module holds the per-year step
(``step_buckets_dragged``); ``tax.location_path`` runs it over a path. With no
split there are no accounts and no defensible tax: the pre-tax engine runs
unchanged, because this machinery is reached ONLY through
``simulate_path_decomposition``, which callers invoke ONLY when ``has_tax_split``
gates it.

THE OPERAND ORDER, per year (read this before touching the loop):

  1. Grow each account by ``r`` (``b + b*r`` — ``step_year``'s exact operand
     order).
  2. Source the flat net spending need ``net = spending*(1+r/2) - gi``,
     access-aware (``_source_net``). SPENDING IS SENIOR TO THE TAX BILL: the
     spending draw happens here, unconditionally, BEFORE the fixed point ever
     computes a bill — so in a crossing year (accounts can cover spending or
     tax but not both) spending takes its dollars first and the bill is
     JUNIOR, taking what remains. This is the operand order steps 2 and 4
     encode — named explicitly because the REALIZED-spend recording (see
     ``TaxLocationPath.realized_spend``) leans on it: the year the accounts run
     dry, the recorded consumption caps at what the DRAGGED balance could fund
     (``max_fundable_spend``), and the unpaid remainder of that year's bill
     simply dies with the portfolio (the post-depletion zero-tax convention
     below).
  3+4. THE FIXED POINT (the module's most-likely-wrong spot — read twice).
     Steps 3 (internal transfers: the voluntary bracket-fill lever and the
     RMD-excess movement) and 4 (compute the tax bill and source it as an
     ADDITIONAL draw) are COUPLED, not sequential: the lever fill and the RMD
     excess both add ordinary income, which changes the bill, which changes
     how much tax-funding draw is needed, which — post-59½ — changes how much
     tax-deferred balance remains for the lever to claim. So all of {T (the
     bill), the tax-funding draws, the lever fill, the RMD excess} resolve
     TOGETHER via one scalar fixed point in ``T``:

         T_(k+1) = tax(income(T_k)) [+ the 10% walled-year penalty]

     seeded at ``T_0 = 0`` (the first pass sources zero tax-funding draw) and
     iterated until ``|T_(k+1) - T_k| < $0.01``, capped at 100 iterations
     (``FIXED_POINT_MAX_ITERATIONS`` — generous; the worst case converges in
     well under 40). CONVERGENCE ARGUMENT (why this is safe to assert, never
     silently cap): every marginal dollar of ``T`` sources at most one marginal
     dollar of additional tax-funding draw, which adds at most
     ``max_marginal_rate`` (< 1; worst case ~0.47 = 37% top ordinary bracket +
     10% walled-year penalty) of NEW tax next round. The map
     ``T -> tax(income(T))`` is therefore a contraction with Lipschitz constant
     <= 0.47 < 1, so ``|T_(k+1) - T_k| <= 0.47^k * |T_1 - T_0|`` shrinks
     geometrically. The M4 extensions keep the map a contraction: under the
     provisional Social Security rule a marginal dollar can make 85 more cents
     of benefits taxable, but only while income is in the low brackets (at
     most 1.85 x 24%), and a flat state rate adds at most its own rate times
     1.85, so the constant stays well under 1 (~0.7 at worst). A cap hit means the contraction bound was violated, i.e. a
     BUG (a broken bracket table, a sourcing regression), not a legitimate
     slow-converging case. Deterministic inputs ⇒ a deterministic iterate count
     ⇒ reproducible outputs.

     TRANSFER SETTLEMENT ORDER (load-bearing — this is what the "scarce-TD"
     validation anchor pins): within one year, spending draws happen first
     (step 2, unconditionally); tax-funding draws are sourced from the
     POST-SPEND balances at each fixed-point iteration; the lever/RMD
     TRANSFERS (moving GROSS dollars tax_deferred -> taxable) settle LAST, only
     once the fixed point has converged. This order is not arbitrary: if the
     transfer settled BEFORE tax-funding sourcing, a tax-funding draw in the
     SAME year could pull the freshly-transferred, basis-less dollars back out
     of taxable and tax them AGAIN as knob-rate LTCG — settling the transfer
     last means this year's tax-funding draw can only ever see the OLD taxable
     balance. PRIORITY under scarce tax-deferred (also load-bearing): the
     lever is capped at the RESIDUAL tax-deferred balance AFTER the (candidate)
     tax-funding draw has taken its share, every iteration — the bill is
     non-optional, the lever is elective, so tax-funding always wins the
     scarce dollar first.

  5. The year's dragged canonical total: ``dragged_end = grown_start + gi -
     spending*(1+r/2) - T``, floored at 0 with the SAME absorbing-depletion
     override the scalar engines use. This is the value the three reported
     buckets telescope to exactly (see ``simulate_path_decomposition``'s
     exact-sum reconstruction). The untaxed scalar keeps running in parallel
     as ``pretax_total`` (see "PAIRED PRE-TAX TOTAL" below). Depletion under
     drag: the year's total NEED is spending-need + bill; the cascade takes
     what exists; if the accounts run dry the path is depleted that year
     (same absorbing-floor semantics). POST-DEPLETION CONVENTION: once
     ``dragged_end`` has hit 0 in a PRIOR year, every SUBSEQUENT year records
     zero tax (and zero of every tax-derived field) even though GI itself
     would be real-world taxable — "the model simply stops charging a
     portfolio that no longer exists" (documented simplification, not a
     truth). The year depletion FIRST occurs still records its real,
     attempted bill (matching how a depleting year's spending is recorded
     elsewhere in the engine) — only years where depletion had ALREADY
     happened before this year's step get the zeroed treatment (see the
     ``was_already_depleted`` branch).

ATTRIBUTION (the three-way split, telescoping a SINGLE income vector, never a
second fixed point): once the year's fixed point has converged, let ``base`` =
the final ordinary income minus the lever extra minus the RMD excess (the LTCG
side is untouched by either transfer — both are ordinary-income events, an IRS
distribution being an IRS distribution whether "actual" or "deemed"):

    spending_tax = tax(base)              + penalty   (penalty is always
                                                         spending-attributed:
                                                         it is pre-59½-only,
                                                         the lever/RMD windows
                                                         are 60+, so they never
                                                         co-occur with it)
    voluntary_tax = tax(base + lever)      - tax(base)
    forced_tax     = tax(base + lever + rmd) - tax(base + lever)

By construction ``spending_tax + voluntary_tax + forced_tax`` telescopes to
exactly ``tax(base + lever + rmd) + penalty`` = the SAME income vector's bill —
so the identity holds to the ULP, not just "to the penny", for any income
vector the fixed point converges to (three extra ``compute_federal_tax`` calls,
done ONCE per year after convergence — cheap, closed-form). The penalty itself
covers ALL walled-year tax-deferred draws, including ones the TAX BILL forces
(the bill itself can force a pre-59½ tax-deferred draw that wouldn't otherwise
have happened).

PAIRED PRE-TAX TOTAL: alongside the dragged total, this module keeps computing
the untaxed scalar — same formula, same per-year spend/gi/r, just never touched
by ``T`` — as ``TaxLocationPath.pretax_total``. It is its OWN parallel running
balance with its OWN absorbing-depletion flag (a tax-free world can
out-survive, or under-survive under a dynamic strategy, the dragged one).
SIMPLIFICATION, documented: both totals are driven by the SAME per-year
``spending`` value (computed once, off the DRAGGED begin-of-year balance —
spending strategies read the dragged balance). For constant/lifecycle spending
this is a no-op (spend doesn't depend on the balance). For a DYNAMIC strategy
(guardrails etc.) this means ``pretax_total`` answers "what would this exact,
already-tax-reactive spending plan have cost with no tax at all" rather than
"what would an INDEPENDENTLY-optimal tax-free plan have spent" — the latter
needs a second, fully independent strategy instance reacting to its own
(bigger, untaxed) balance, a heavier double-run not done here. The BINDS
drag-direction test is scoped to STATIC spend, where the two are identical by
construction.

The 59½ boundary with integer ages: age <= 59 is walled, age >= 60 is
penalty-free (``TAX_LOCATION_ACCESS_AGE`` / ``PRE_595_MAX_AGE``; IRC 72(t)).
The voluntary lever's window is ages 60-74 inclusive (``LEVER_MIN_AGE`` /
``LEVER_MAX_AGE``) — the penalty door through the last pre-RMD year; RMDs
(``tax.rmd.RMD_START_AGE`` = 75) take over immediately after, so the lever and
the RMD excess never co-occur.
"""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.tax.core import (
    EARLY_WITHDRAWAL_PENALTY_RATE,
    federal_tax_total,
    standard_deduction,
)
from firebridgelab.tax.rmd import RMD_START_AGE, rmd_factor
from firebridgelab.tax.ss_taxation import fill_amount, taxable_social_security
from firebridgelab.tax.state import state_tax as _state_tax


# The penalty-free access boundary on the INTEGER age axis. 59½ is the IRS rule;
# on an integer annual axis we model the first FULL penalty-free year as age 60,
# so age <= 59 is treated as pre-59½ / walled (conservative — 59½ lands mid the
# year the retiree turns 59). Same convention as ``engine.allocation``'s
# ALLOCATION_401K_ACCESS_AGE = 60; kept as its own named constant here so the
# tax-location semantics are self-documenting.
TAX_LOCATION_ACCESS_AGE = 60

# The "is this age pre-59½ (walled)" cutoff used by the depletion metric: a
# depletion is counted "pre-59½" iff it happens at an age <= this value (i.e.
# strictly before the penalty-free door at age 60).
PRE_595_MAX_AGE = TAX_LOCATION_ACCESS_AGE - 1  # 59

# The voluntary post-59½ bracket-fill lever's window: ages 60-74 inclusive —
# the penalty door through the last pre-RMD year. Fixed, not user-configurable.
# RMDs (tax.rmd.RMD_START_AGE = 75) take over the following year, so this
# window and the RMD window never overlap.
LEVER_MIN_AGE = TAX_LOCATION_ACCESS_AGE  # 60
LEVER_MAX_AGE = RMD_START_AGE - 1        # 74

# The per-year tax fixed point: converge when the bill moves
# less than a cent between iterations, capped at 100 iterations (generous — the
# worst case converges in well under 40; a cap hit is asserted as a bug, never
# silently absorbed). See the module docstring's convergence argument.
FIXED_POINT_TOLERANCE = 0.01
FIXED_POINT_MAX_ITERATIONS = 100

# Within-dollar tolerance for the "three balances sum to initial_portfolio" check
# Real-dollar inputs, so $1 is a generous rounding cushion.
SPLIT_SUM_TOLERANCE = 1.0


def has_tax_split(scenario) -> bool:
    """True iff a tax-location split is active on the scenario.

    The feature is OFF when NO split field is supplied (all three ``None``) —
    the pre-tax path. If ANY of the three is supplied, the split is treated as
    active (missing ones resolve to 0 in ``resolve_split``).
    """
    return any(
        getattr(scenario, name) is not None
        for name in ("taxable_balance", "tax_deferred_balance", "roth_balance")
    )


def resolve_split(scenario) -> tuple[float, float, float]:
    """Resolve the three starting balances, validating the sum.

    Returns ``(taxable, tax_deferred, roth)``. A missing (``None``) balance
    resolves to 0.0 (if any is provided, all three are treated as provided,
    missing → 0). Raises ``ValueError`` if the three do not sum to
    ``initial_portfolio`` within ``SPLIT_SUM_TOLERANCE`` ($1).

    Caller is responsible for only invoking this when ``has_tax_split`` is True.
    """
    taxable = scenario.taxable_balance or 0.0
    tax_deferred = scenario.tax_deferred_balance or 0.0
    roth = scenario.roth_balance or 0.0
    total = taxable + tax_deferred + roth
    if abs(total - scenario.initial_portfolio) > SPLIT_SUM_TOLERANCE:
        raise ValueError(
            "tax-location balances (taxable "
            f"{taxable:,.2f} + tax_deferred {tax_deferred:,.2f} + roth "
            f"{roth:,.2f} = {total:,.2f}) must sum to initial_portfolio "
            f"({scenario.initial_portfolio:,.2f}) within "
            f"${SPLIT_SUM_TOLERANCE:g}"
        )
    return float(taxable), float(tax_deferred), float(roth)


def _pension_income_at_age(scenario, age: int) -> float:
    """The ordinary-income share of ``gi_at_age``: every income stream taxed as
    ordinary income (pensions, annuities, work, rental unless marked tax-free).
    Named for the pensions it held when the streams were fixed; read from
    ``income.household`` so the tax split can never drift from the GI sum."""
    from firebridgelab.income.household import household

    return household(scenario).year(age).ordinary


def _ss_income_at_age(scenario, age: int) -> float:
    """Gross (pre-85%) Social Security half of ``gi_at_age``; the caller applies
    the flat 85% inclusion. Delegates to ``income.social_security.ss_income_at_age``."""
    from firebridgelab.income.social_security import ss_income_at_age

    return ss_income_at_age(scenario, age)


def _other_ordinary_and_ltcg(scenario, age: int) -> tuple[float, float]:
    """Income a household adds to the return beyond its streams and account
    draws: the cash events' ordinary income and long-term gain that year."""
    from firebridgelab.income.household import household

    hy = household(scenario).year(age)
    return hy.event_ordinary, hy.event_ltcg


def _filing_profile(scenario, age: int) -> tuple[int, bool]:
    """``(n_65_plus, single)`` for a year: joint filing for a couple with one
    65+ addition per spouse aged 65+ (each on their own age), single filing
    after the first death (``survivor_age``) and for a one-person household.
    For the legacy same-age couple this is ``n_65_plus_at_age`` exactly."""
    from firebridgelab.income.household import household

    hy = household(scenario).year(age)
    return hy.n_65_plus, hy.single_filer


def _source_net(
    taxable: float, tax_deferred: float, roth: float, net: float, penalty_free: bool
) -> tuple[float, float, float, bool, float, float, float]:
    """Draw ``net`` (>= 0) from the three GROWN balances, access-aware.

    Returns the post-draw ``(taxable, tax_deferred, roth, forced_pre_595_draw,
    taxable_take, tax_deferred_take, roth_take)`` where ``forced_pre_595_draw``
    is True iff a pre-59½ draw had to dip into tax_deferred because taxable AND
    roth were both exhausted (the bridge-failure event the feature is meant to
    surface), and the three ``*_take`` values are exactly the natural
    per-account amounts drawn this year — additive recording only, no change to
    the sourcing/cascade arithmetic below.

    ``net == 0`` is a legal, common call (the tax-drag fixed point's seed and
    any year with no tax owed): the loop's ``remaining <= 0`` guard breaks
    immediately, returning the INPUT balances unchanged with all takes 0.0 —
    this is what lets the fixed point call this helper on every iteration
    (including the T=0 seed) without a special case.

    Sourcing order:
      - Pre-59½ (walled): taxable → roth; remainder forced from tax_deferred.
      - At/after 59½: taxable → tax_deferred → roth.

    Floor / cascade: each account floors at 0; any shortfall an account can't
    cover cascades to the NEXT account in the order. Money is never dropped on
    the floor — the total drawn equals ``min(net, taxable+tax_deferred+roth)``.

    perf: the cascade is UNROLLED (plain locals + branches, no per-call
    dict/tuple construction) — this runs once per fixed-point iteration plus
    once per spending draw, ~4M calls in a split-MC 10k run. Per account:
    ``take = avail if avail < remaining else remaining``, ``avail - take``,
    early-out when ``remaining <= 0``, and the forced flag on a positive
    walled-year tax-deferred take. Both orders share the taxable-first leg.
    """
    taxable_take = tax_deferred_take = roth_take = 0.0
    forced = False
    remaining = net

    # Leg 1 (both orders): taxable.
    if remaining > 0:
        take = taxable if taxable < remaining else remaining
        taxable = taxable - take
        taxable_take = take
        remaining -= take

        if penalty_free:
            # At/after 59½: taxable -> tax_deferred -> roth.
            if remaining > 0:
                take = tax_deferred if tax_deferred < remaining else remaining
                tax_deferred = tax_deferred - take
                tax_deferred_take = take
                remaining -= take
                if remaining > 0:
                    take = roth if roth < remaining else remaining
                    roth = roth - take
                    roth_take = take
        else:
            # Pre-59½ (walled): taxable -> roth; remainder forced from
            # tax_deferred.
            if remaining > 0:
                take = roth if roth < remaining else remaining
                roth = roth - take
                roth_take = take
                remaining -= take
                if remaining > 0:
                    take = tax_deferred if tax_deferred < remaining else remaining
                    tax_deferred = tax_deferred - take
                    tax_deferred_take = take
                    if take > 0:
                        forced = True

    return (
        taxable,
        tax_deferred,
        roth,
        forced,
        taxable_take,
        tax_deferred_take,
        roth_take,
    )


@dataclass(frozen=True)
class DraggedYearStep:
    """One year's DRAGGED decomposition step (a dataclass because there are
    more named outputs than a tuple should carry silently).

    ``taxable`` / ``tax_deferred`` / ``roth`` are the NATURAL (pre-exact-sum-
    reconstruction) end-of-year balances, AFTER the spending draw, the
    tax-funding draw, and the lever/RMD transfer have all settled — the caller
    (``simulate_path_decomposition``) reconciles their sum to the dragged
    canonical total exactly. ``tax`` is the converged fixed-point bill (includes the
    penalty); ``spending_tax`` / ``voluntary_tax`` / ``forced_tax`` are its
    exact three-way telescoping split and sum to ``tax`` exactly.
    ``lever_transfer`` / ``rmd_excess`` are the two internal (sum-preserving)
    transfers — at most one is ever nonzero in a given year (the lever window
    is 60-74, RMDs start at 75). ``taxable_draw`` /
    ``tax_deferred_draw`` / ``roth_draw`` are the SPENDING-driven draws only
    (the tax-funding draws are NOT folded in, so
    ``test_engine_draws_non_negative_and_reconcile_with_net_draw``-style
    reconciliation to the spending ``net`` holds exactly).
    ``iterations`` is the fixed point's iterate count (diagnostic / test-only).
    """

    taxable: float
    tax_deferred: float
    roth: float
    forced_pre_595_draw: bool
    taxable_draw: float
    tax_deferred_draw: float
    roth_draw: float
    tf_taxable_draw: float
    tf_tax_deferred_draw: float
    tf_roth_draw: float
    lever_transfer: float
    rmd_excess: float
    ordinary_income: float
    ltcg_income: float
    tax: float
    spending_tax: float
    voluntary_tax: float
    forced_tax: float
    penalty: float
    iterations: int
    # M4 tax extensions (all 0.0 when off). ``roth_conversion`` is the gross
    # tax-deferred -> Roth transfer; ``taxable_ss`` the taxable part of Social
    # Security in ``ordinary_income``; ``state_tax`` and ``payroll_tax`` are
    # the shares of ``tax`` they make up.
    roth_conversion: float = 0.0
    taxable_ss: float = 0.0
    state_tax: float = 0.0
    payroll_tax: float = 0.0


def step_buckets_dragged(
    taxable: float,
    tax_deferred: float,
    roth: float,
    r: float,
    gi: float,
    spending: float,
    penalty_free: bool,
    *,
    pension_ss_ordinary: float,
    gains_fraction: float,
    n_65_plus: int,
    age: int,
    single: bool = False,
    bracket_fill_target: float | None,
    prior_tax_deferred: float,
    extra_ltcg: float = 0.0,
    ss_benefits: float = 0.0,
    ss_flat_taxable: float = 0.0,
    threshold_scale: float = 1.0,
    fixed_tax: float = 0.0,
    state=None,
    conversion_target: float | None = None,
) -> DraggedYearStep:
    """One year of the DRAGGED access-aware decomposition.

    Steps 1-2 grow the accounts and source the flat spending need; it then
    resolves the per-year
    fixed point (steps 3-4, coupled: the lever fill, the RMD excess, and the
    tax-funding draw all move together) and settles the internal transfers
    LAST (see the module docstring's "TRANSFER SETTLEMENT ORDER").

    SPENDING IS SENIOR TO THE TAX BILL (module docstring step 2): the
    spending draw (step 2) sources unconditionally BEFORE the fixed
    point computes any bill, so in a crossing year the bill is JUNIOR — the
    tax-funding cascade takes what remains after spending, and whatever part
    of the bill the emptied accounts can't cover dies with the portfolio
    (depletion, the absorbing floor). Named so the realized-spend recording
    can lean on it.

    ``pension_ss_ordinary`` = pensions + 0.85 x SS already summed by the caller
    (``_pension_income_at_age`` + 0.85 x ``_ss_income_at_age``) — passed in
    rather than re-derived here so this function stays a plain numeric step,
    independently unit-testable with hand-picked inputs (the hand-computed
    anchor tests call this directly). ``prior_tax_deferred`` is the
    PRIOR year's recorded (reconciled) tax-deferred balance — the RMD's
    denominator input (future RMDs shrink through the real table: each year's
    excess movement reduces next year's ``prior_tax_deferred``). ``bracket_fill_target`` is ``None``
    when the lever is off (every iteration then skips the lever entirely).
    ``pension_ss_ordinary`` also carries any other non-account ordinary income
    (a cash event taxed as ordinary), and ``extra_ltcg`` is long-term gain that
    did not come from a taxable-account draw (a cash event's capital gain).

    The M4 keywords all default to the original behaviour:
    ``ss_benefits`` > 0 taxes those benefits by the provisional-income rule
    (``pension_ss_ordinary`` must then exclude them); ``ss_flat_taxable`` is
    the 85% share already inside ``pension_ss_ordinary`` under the flat rule
    (reported, and subtracted by states that exempt it);
    ``threshold_scale`` is the real value of the unindexed thresholds
    (``tax.thresholds``); ``fixed_tax`` is the payroll tax; ``state`` a
    ``tax.state.StateYear``; ``conversion_target`` the Roth conversion target
    in dollars of ordinary taxable income (``tax.roth``).
    """
    # 1. Grow each account (step_year's exact operand order: b + b*r).
    taxable = taxable + taxable * r
    tax_deferred = tax_deferred + tax_deferred * r
    roth = roth + roth * r

    # 2. Source the flat net spending need (the access-aware cascade).
    net = (spending - gi) * (1 + r / 2)
    spend_forced = False
    sp_taxable_draw = sp_tax_deferred_draw = sp_roth_draw = 0.0
    if net <= 0:
        # Income covered spending; the surplus is a contribution to taxable.
        taxable = taxable + (-net)
    else:
        (
            taxable,
            tax_deferred,
            roth,
            spend_forced,
            sp_taxable_draw,
            sp_tax_deferred_draw,
            sp_roth_draw,
        ) = _source_net(taxable, tax_deferred, roth, net, penalty_free)
    # Balances AFTER the spending draw, BEFORE any tax-funding draw — the pool
    # every fixed-point iteration sources its CANDIDATE tax-funding draw from
    # (never cumulatively; each iteration re-sources fresh from these same
    # post-spend balances for its own candidate T).
    taxable_after_spend = taxable
    tax_deferred_after_spend = tax_deferred
    roth_after_spend = roth

    # 3+4. THE FIXED POINT — see the module docstring for the full argument.
    lever_window = LEVER_MIN_AGE <= age <= LEVER_MAX_AGE
    rmd_window = age >= RMD_START_AGE
    deduction = standard_deduction(n_65_plus, single)

    # perf, all value-identical: inline conditionals instead of ``max``/``min``
    # builtins, the allocation-free ``federal_tax_total`` instead of
    # ``compute_federal_tax(...).total`` (same ``_tax_components`` arithmetic),
    # the convergence test compares the signed delta against ±tolerance instead
    # of calling ``abs()``, and the hot names are hoisted to locals — this loop
    # body runs ~3.5M times in a split-MC 10k run.
    _source = _source_net
    _tax_total = federal_tax_total
    _tol = FIXED_POINT_TOLERANCE
    lever_on = lever_window and bracket_fill_target is not None
    conversion_on = conversion_target is not None
    # The M4 extensions. With all of them off the bill is exactly the
    # original ``federal_tax_total(ordinary, ltcg, n_65_plus, single)``.
    provisional = ss_benefits > 0.0
    extended = provisional or state is not None or threshold_scale != 1.0

    def _bill(other_ordinary: float, ltcg: float, ira: float) -> tuple[float, float, float]:
        """``(federal + state, taxable_ss, state)`` for one income vector.
        ``other_ordinary`` excludes the Social Security under the provisional
        rule (it already includes the flat 85% otherwise); ``ira`` is the
        year's tax-deferred distributions (for the state's retirement rules)."""
        if provisional:
            tss = taxable_social_security(
                ss_benefits, other_ordinary + ltcg, single, threshold_scale
            )
            ordinary = other_ordinary + tss
        else:
            tss = ss_flat_taxable
            ordinary = other_ordinary
        fed = _tax_total(ordinary, ltcg, n_65_plus, single, threshold_scale)
        st = 0.0
        if state is not None:
            agi = ordinary + ltcg
            fed_taxable = agi - deduction
            if fed_taxable < 0.0:
                fed_taxable = 0.0
            st = _state_tax(state, agi, fed_taxable, tss, ira, ltcg)
        return fed + st, tss, st

    def _fill(other_ordinary: float, ltcg: float, target_taxable: float) -> float:
        """Extra ordinary income that brings ordinary TAXABLE income (after
        the standard deduction) up to ``target_taxable``."""
        target = target_taxable + deduction
        if provisional:
            return fill_amount(
                other_ordinary, ltcg, ss_benefits, target, single, threshold_scale
            )
        fill = target - other_ordinary
        return fill if fill > 0.0 else 0.0

    T = 0.0
    converged = False
    tf_forced = False
    tf_taxable_draw = tf_tax_deferred_draw = tf_roth_draw = 0.0
    lever_extra = rmd_excess = conversion = 0.0
    ordinary_income = ltcg_income = penalty = 0.0
    iterations = 0
    for iterations in range(1, FIXED_POINT_MAX_ITERATIONS + 1):
        (
            _t_after_tf,
            _td_after_tf,
            _r_after_tf,
            tf_forced,
            tf_taxable_draw,
            tf_tax_deferred_draw,
            tf_roth_draw,
        ) = _source(
            taxable_after_spend,
            tax_deferred_after_spend,
            roth_after_spend,
            T,
            penalty_free,
        )
        total_td_draw = sp_tax_deferred_draw + tf_tax_deferred_draw
        total_taxable_draw = sp_taxable_draw + tf_taxable_draw
        # What's left in tax-deferred once the (candidate) tax-funding draw has
        # taken its share — the pool the lever/RMD transfer can claim
        # (the scarce-TD priority: the bill is satisfied FIRST).
        residual_tax_deferred = tax_deferred_after_spend - tf_tax_deferred_draw
        cap = residual_tax_deferred if residual_tax_deferred > 0.0 else 0.0

        ltcg_income = total_taxable_draw * gains_fraction
        if extra_ltcg:
            ltcg_income += extra_ltcg

        rmd_excess = 0.0
        if rmd_window:
            rmd = prior_tax_deferred / rmd_factor(age)
            excess = rmd - total_td_draw
            if excess < 0.0:
                excess = 0.0
            # Defensive cap (never transfer more than exists) — not expected
            # to bind in practice since growth generally keeps the current
            # balance >= the RMD, but guards a pathological crash-year fixture.
            rmd_excess = excess if excess < cap else cap
            cap -= rmd_excess

        # Roth conversion (an RMD cannot be converted, so it comes first).
        conversion = 0.0
        if conversion_on:
            conversion = _fill(
                pension_ss_ordinary + total_td_draw + rmd_excess,
                ltcg_income,
                conversion_target,
            )
            if conversion > cap:
                conversion = cap
            cap -= conversion

        lever_extra = 0.0
        if lever_on:
            if extended or conversion_on:
                lever_extra = _fill(
                    pension_ss_ordinary + total_td_draw + conversion,
                    ltcg_income,
                    bracket_fill_target,
                )
            else:
                lever_extra = bracket_fill_target + deduction - (
                    pension_ss_ordinary + total_td_draw
                )
                if lever_extra < 0.0:
                    lever_extra = 0.0
            if lever_extra > cap:
                lever_extra = cap

        other_ordinary = (
            pension_ss_ordinary + total_td_draw + lever_extra + rmd_excess + conversion
        )
        penalty = EARLY_WITHDRAWAL_PENALTY_RATE * total_td_draw if not penalty_free else 0.0

        if extended:
            bill = _bill(other_ordinary, ltcg_income, other_ordinary - pension_ss_ordinary)[0]
        else:
            bill = _tax_total(other_ordinary, ltcg_income, n_65_plus, single)
        T_next = bill + penalty + fixed_tax

        delta = T_next - T
        if -_tol < delta < _tol:
            T = T_next
            converged = True
            break
        T = T_next

    if not converged:
        raise RuntimeError(
            f"tax-drag fixed point failed to converge in "
            f"{FIXED_POINT_MAX_ITERATIONS} iterations at age {age} (last bill "
            f"${T:,.2f}) — the map is a monotone contraction with Lipschitz "
            f"constant well under 1 and should converge in well under 40 "
            f"iterations, so a cap hit here is a BUG (a broken bracket table "
            f"or a sourcing regression), never a legitimate slow case. See "
            f"the module docstring's convergence argument."
        )

    # Attribution: telescoping three-way split of THIS SAME final
    # income vector — never a re-run of the fixed point with a component
    # dropped (that would resolve DIFFERENT tax-funding draws and the exact
    # `==` identity below would fail by cents).
    #
    # The voluntary share covers the bracket-fill lever AND the Roth
    # conversion (both elective); the forced share is the RMD excess. The
    # payroll tax (``fixed_tax``) does not depend on the account draws and is
    # spending-attributed, like the penalty.
    #
    # perf: ``compute_federal_tax(x + 0.0, ...)`` is EXACTLY
    # ``compute_federal_tax(x, ...)`` — adding a literal 0.0 changes no float
    # bit — so when the elective fill (or ``rmd_excess``) is exactly 0.0,
    # calling again with it added is a wasted call; every skip reuses the
    # PRECISE already-computed value rather than recomputing it.
    elective = lever_extra + conversion
    base_ordinary = other_ordinary - elective - rmd_excess
    if extended:
        base_ira = base_ordinary - pension_ss_ordinary
        tax_base, tss_final, state_final = _bill(base_ordinary, ltcg_income, base_ira)
        if elective != 0.0:
            tax_base_lever, tss_final, state_final = _bill(
                base_ordinary + elective, ltcg_income, base_ira + elective
            )
        else:
            tax_base_lever = tax_base
        if rmd_excess != 0.0:
            tax_base_lever_rmd, tss_final, state_final = _bill(
                base_ordinary + elective + rmd_excess,
                ltcg_income,
                base_ira + elective + rmd_excess,
            )
        else:
            tax_base_lever_rmd = tax_base_lever
    else:
        tss_final = ss_flat_taxable
        state_final = 0.0
        tax_base = _tax_total(base_ordinary, ltcg_income, n_65_plus, single)
        if elective != 0.0:
            tax_base_lever = _tax_total(
                base_ordinary + elective, ltcg_income, n_65_plus, single
            )
        else:
            tax_base_lever = tax_base
        if rmd_excess != 0.0:
            tax_base_lever_rmd = _tax_total(
                base_ordinary + elective + rmd_excess, ltcg_income, n_65_plus, single
            )
        else:
            tax_base_lever_rmd = tax_base_lever
    spending_tax = tax_base + penalty + fixed_tax
    voluntary_tax = tax_base_lever - tax_base
    forced_tax = tax_base_lever_rmd - tax_base_lever
    tax_total = spending_tax + voluntary_tax + forced_tax
    # The year's ordinary income as reported (federal AGI less the gains):
    # under the provisional rule the taxable Social Security is added here.
    ordinary_income = other_ordinary + (tss_final if provisional else 0.0)

    # Apply the tax-funding draws, THEN settle the transfers (load-bearing
    # order — module docstring "TRANSFER SETTLEMENT ORDER"): the lever/RMD
    # transfer moves gross, tax_deferred -> taxable, and the conversion
    # tax_deferred -> roth, AFTER tax-funding has already sourced from the
    # PRE-transfer balances.
    taxable = taxable_after_spend - tf_taxable_draw
    tax_deferred = tax_deferred_after_spend - tf_tax_deferred_draw
    roth = roth_after_spend - tf_roth_draw
    transfer = lever_extra + rmd_excess
    tax_deferred -= transfer + conversion
    taxable += transfer
    roth += conversion

    # Defensive float-noise floor.
    if taxable < 0:
        taxable = 0.0
    if tax_deferred < 0:
        tax_deferred = 0.0
    if roth < 0:
        roth = 0.0

    return DraggedYearStep(
        taxable=taxable,
        tax_deferred=tax_deferred,
        roth=roth,
        forced_pre_595_draw=spend_forced or tf_forced,
        taxable_draw=sp_taxable_draw,
        tax_deferred_draw=sp_tax_deferred_draw,
        roth_draw=sp_roth_draw,
        tf_taxable_draw=tf_taxable_draw,
        tf_tax_deferred_draw=tf_tax_deferred_draw,
        tf_roth_draw=tf_roth_draw,
        lever_transfer=lever_extra,
        rmd_excess=rmd_excess,
        ordinary_income=ordinary_income,
        ltcg_income=ltcg_income,
        tax=tax_total,
        spending_tax=spending_tax,
        voluntary_tax=voluntary_tax,
        forced_tax=forced_tax,
        penalty=penalty,
        iterations=iterations,
        roth_conversion=conversion,
        taxable_ss=tss_final,
        state_tax=state_final,
        payroll_tax=fixed_tax,
    )
