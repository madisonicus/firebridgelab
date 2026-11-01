"""Tax-location decomposition + access-aware sourcing, now DRAGGED (tax-drag
design td1 supersedes the tb1/ft1/ft2 display-only
shadow whenever a tax-location split is supplied).

THE BIG CHANGE FROM tb1/ft1/ft2: this decomposition used to be a "shadow" that
ran ALONGSIDE the scalar engine and never perturbed it (no tax, no penalty
drag). Under td1, when a split is supplied, this module's decomposition BECOMES
the true portfolio math: the year's federal tax bill is drawn from the accounts
like any other outflow (DECISION 1). No split ⇒ no accounts ⇒ no defensible tax
⇒ the legacy pre-tax engine still runs byte-stable — the deterministic canary
(``simulate(default_scenario()).final_portfolio == 8397280.186644783``) and
every no-split test are untouched, because this module's new machinery is
reached ONLY through ``simulate_path_decomposition``, which callers invoke ONLY
when ``has_tax_split`` gates it.

THE OPERAND ORDER, per year (DECISION 3 — read this before touching the loop):

  1. Grow each account by ``r`` (``b + b*r`` — ``step_year``'s exact operand
     order).
  2. Source the flat net spending need ``net = spending*(1+r/2) - gi``,
     access-aware, EXACTLY the tb1 cascade (``_source_net``) — unchanged.
     SPENDING IS SENIOR TO THE TAX BILL (td-b, pinned): the spending draw
     happens here, unconditionally, BEFORE the fixed point ever computes a
     bill — so in a crossing year (accounts can cover spending or tax but not
     both) spending takes its dollars first and the bill is JUNIOR, taking
     what remains. This is not a new rule, it is the operand order steps 2 and
     4 already encode — named explicitly because the REALIZED-spend recording
     (see ``TaxLocationPath.realized_spend``) leans on it: the year the
     accounts run dry, the recorded consumption caps at what the DRAGGED
     balance could fund (``max_fundable_spend``), and the unpaid remainder of
     that year's bill simply dies with the portfolio (the post-depletion
     zero-tax convention below).
  3+4. THE FIXED POINT (the module's most-likely-wrong spot — read twice).
     Steps 3 (internal transfers: the voluntary bracket-fill lever, DECISION 5;
     the RMD-excess movement, DECISION 4) and 4 (compute the tax bill and
     source it as an ADDITIONAL draw) are COUPLED, not sequential: the lever
     fill and the RMD excess both add ordinary income, which changes the bill,
     which changes how much tax-funding draw is needed, which — post-59½ —
     changes how much tax-deferred balance remains for the lever to claim. So
     all of {T (the bill), the tax-funding draws, the lever fill, the RMD
     excess} resolve TOGETHER via one scalar fixed point in ``T``:

         T_(k+1) = tax(income(T_k)) [+ the 10% walled-year penalty]

     seeded at ``T_0 = 0`` (which, on the FIRST pass, sources zero tax-funding
     draw — exactly "the seed is the bill with no tax-funding draws" the design
     specifies) and iterated until ``|T_(k+1) - T_k| < $0.01``, capped at 100
     iterations (``FIXED_POINT_MAX_ITERATIONS`` — generous; the worst case
     converges in well under 40). CONVERGENCE ARGUMENT (why this is safe to
     assert, never silently cap): every marginal dollar of ``T`` sources at
     most one marginal dollar of additional tax-funding draw, which adds at
     most ``max_marginal_rate`` (< 1; worst case ~0.47 = 37% top ordinary
     bracket + 10% walled-year penalty) of NEW tax next round. The map
     ``T -> tax(income(T))`` is therefore a contraction with Lipschitz constant
     <= 0.47 < 1, so ``|T_(k+1) - T_k| <= 0.47^k * |T_1 - T_0|`` shrinks
     geometrically — a cap hit means the contraction bound was violated, i.e. a
     BUG (a broken bracket table, a sourcing regression), not a legitimate
     slow-converging case. Deterministic inputs ⇒ a deterministic iterate count
     ⇒ byte-stable outputs (the no-split canary never reaches this code at
     all, so it stays exactly as stable as before).

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
     override tb1/the scalar engines use. This REPLACES the old
     pre-tax scalar as the value the three reported buckets telescope to
     exactly (see ``simulate_path_decomposition``'s exact-sum reconstruction).
     The OLD pre-tax scalar is NOT discarded — it keeps running in parallel,
     completely untaxed, as ``pretax_total`` (a separate per-age series; see
     "PAIRED PRE-TAX TOTAL" below). Depletion under drag: the year's total NEED
     is spending-need + bill; the cascade takes what exists; if the accounts
     run dry the path is depleted that year (same absorbing-floor semantics).
     POST-DEPLETION CONVENTION: once ``dragged_end`` has hit 0 in a PRIOR year,
     every SUBSEQUENT year records zero tax (and zero of every tax-derived
     field) even though GI itself would be real-world taxable — "the model
     simply stops charging a portfolio that no longer exists" (documented
     simplification, not a truth). The year depletion FIRST occurs still
     records its real, attempted bill (matching how a depleting year's
     spending is recorded elsewhere in the engine) — only years where
     depletion had ALREADY happened before this year's step get the zeroed
     treatment (see the ``was_already_depleted`` branch below).

ATTRIBUTION (DECISION 6 — the three-way split, telescoping a SINGLE income
vector, never a second fixed point): once the year's fixed point has
converged, let ``base`` = the final ordinary income minus the lever extra minus
the RMD excess (the LTCG side is untouched by either transfer — both are
ordinary-income events, an IRS distribution being an IRS distribution whether
"actual" or "deemed"):

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
(new vs ft2 — under drag the bill itself can force a pre-59½ tax-deferred
draw that wouldn't otherwise have happened).

PAIRED PRE-TAX TOTAL: alongside the dragged total, this module keeps computing
the OLD (ft2-era) untaxed scalar — same formula, same per-year spend/gi/r,
just never touched by ``T`` — as ``TaxLocationPath.pretax_total``. It is its
OWN parallel running balance with its OWN absorbing-depletion flag (a tax-free
world can out-survive, or under-survive under a dynamic strategy, the dragged
one). SIMPLIFICATION, documented: both totals are driven by the SAME per-year
``spending`` value (computed once, off the DRAGGED begin-of-year balance, per
DECISION 2 — "spending strategies keep reading the now-dragged balance"). For
constant/lifecycle spending this is a no-op (spend doesn't depend on the
balance). For a DYNAMIC strategy (guardrails etc.) this means ``pretax_total``
answers "what would this exact, already-tax-reactive spending plan have cost
with no tax at all" rather than "what would an INDEPENDENTLY-optimal tax-free
plan have spent" — the latter needs a second, fully independent strategy
instance reacting to its own (bigger, untaxed) balance, which is a heavier
double-run left for the td-b "estate cost of taxes" readout (DECISION 9) to
decide it needs. Not needed for any td-a invariant: the BINDS drag-direction
test is scoped to STATIC spend, where the two are identical by construction.

The 59½ boundary with integer ages: unchanged from tb1 — age <= 59 is walled,
age >= 60 is penalty-free (``TAX_LOCATION_ACCESS_AGE`` / ``PRE_595_MAX_AGE``).
The voluntary lever's window (DECISION 5) is ages 60-74 inclusive
(``LEVER_MIN_AGE`` / ``LEVER_MAX_AGE``) — the penalty door through the last
pre-RMD year; RMDs (``federal_tax_core.RMD_START_AGE`` = 75) take over
immediately after, so the lever and the RMD excess never co-occur (DECISION 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from firebridgelab._step import max_fundable_spend, net_return
from firebridgelab.federal_tax_core import (
    ADDITIONAL_DEDUCTION_65_PLUS,
    EARLY_WITHDRAWAL_PENALTY_RATE,
    RMD_START_AGE,
    STANDARD_DEDUCTION_MFJ,
    federal_tax_total,
    standard_deduction,
    n_65_plus_at_age,
    rmd_factor,
)

# The penalty-free access boundary on the INTEGER age axis. 59½ is the IRS rule;
# on an integer annual axis we model the first FULL penalty-free year as age 60,
# so age <= 59 is treated as pre-59½ / walled (conservative — 59½ lands mid the
# year the retiree turns 59). Same convention as firebridgelab.scenario's
# ALLOCATION_401K_ACCESS_AGE = 60; kept as its own named constant here so the
# tax-location semantics are self-documenting.
TAX_LOCATION_ACCESS_AGE = 60

# The "is this age pre-59½ (walled)" cutoff used by the depletion metric: a
# depletion is counted "pre-59½" iff it happens at an age <= this value (i.e.
# strictly before the penalty-free door at age 60).
PRE_595_MAX_AGE = TAX_LOCATION_ACCESS_AGE - 1  # 59

# The voluntary post-59½ bracket-fill lever's window (td1 DECISION 5): ages
# 60-74 inclusive — the penalty door through the last pre-RMD year. Fixed, not
# user-configurable in td1. RMDs (federal_tax_core.RMD_START_AGE = 75) take
# over the following year, so this window and the RMD window never overlap.
LEVER_MIN_AGE = TAX_LOCATION_ACCESS_AGE  # 60
LEVER_MAX_AGE = RMD_START_AGE - 1        # 74

# The per-year tax fixed point (td1 DECISION 3): converge when the bill moves
# less than a cent between iterations, capped at 100 iterations (generous — the
# worst case converges in well under 40; a cap hit is asserted as a bug, never
# silently absorbed). See the module docstring's convergence argument.
FIXED_POINT_TOLERANCE = 0.01
FIXED_POINT_MAX_ITERATIONS = 100

# Within-dollar tolerance for the "three balances sum to initial_portfolio" check
# (the design's "$1"). Real-dollar inputs, so $1 is a generous rounding cushion.
SPLIT_SUM_TOLERANCE = 1.0


def has_tax_split(scenario) -> bool:
    """True iff a tax-location split is active on the scenario.

    The feature is OFF when NO split field is supplied (all three ``None``) —
    the canary path. If ANY of the three is supplied, the split is treated as
    active (missing ones resolve to 0 in ``resolve_split``).
    """
    return any(
        getattr(scenario, name) is not None
        for name in ("taxable_balance", "tax_deferred_balance", "roth_balance")
    )


def resolve_split(scenario) -> tuple[float, float, float]:
    """Resolve the three starting balances, validating the sum.

    Returns ``(taxable, tax_deferred, roth)``. A missing (``None``) balance
    resolves to 0.0 (per the design: "if any is provided, all three are treated
    as provided, missing → 0"). Raises ``ValueError`` if the three do not sum to
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
    """Pension half of ``firebridgelab.scenario.gi_at_age`` (fully ordinary income).
    Delegates to the canonical ``firebridgelab.scenario.pension_income_at_age`` so the
    tax split can never drift from the GI sum the rest of the engine reads."""
    from firebridgelab.scenario import pension_income_at_age

    return pension_income_at_age(scenario, age)


def _ss_income_at_age(scenario, age: int) -> float:
    """Gross (pre-85%) Social Security half of ``gi_at_age``; the caller applies
    the flat 85% inclusion. Delegates to ``firebridgelab.scenario.ss_income_at_age``."""
    from firebridgelab.scenario import ss_income_at_age

    return ss_income_at_age(scenario, age)


def _filing_profile(scenario, age: int) -> tuple[int, bool]:
    """``(n_65_plus, single)`` for a year: joint filing with both spouses aged
    together, or — after ``survivor_age`` — a single filer with one 65+
    addition."""
    from firebridgelab.scenario import is_survivor_year

    if is_survivor_year(scenario, age):
        return (1 if age >= 65 else 0), True
    return n_65_plus_at_age(age), False


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

    Sourcing order (tb1 DECISION 1, unchanged by drag):
      - Pre-59½ (walled): taxable → roth; remainder forced from tax_deferred.
      - At/after 59½: taxable → tax_deferred → roth.

    Floor / cascade: each account floors at 0; any shortfall an account can't
    cover cascades to the NEXT account in the order. Money is never dropped on
    the floor — the total drawn equals ``min(net, taxable+tax_deferred+roth)``.

    perf: the cascade is UNROLLED (plain locals + branches, no per-call
    dict/tuple construction) — this runs once per fixed-point iteration plus
    once per spending draw, ~4M calls in a split-MC 10k run. The arithmetic
    per account is byte-identical to the old order-tuple loop (same
    ``take = avail if avail < remaining else remaining`` cap, same
    ``avail - take`` update, same early-out when ``remaining <= 0``, same
    forced flag on a positive walled-year tax-deferred take); only the data
    structures changed. Both orders share the taxable-first leg.
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
    """One year's DRAGGED decomposition step — the td1 analogue of the plain
    tuple tb1's ``step_buckets`` returned (a dataclass because drag adds many
    more named outputs than a tuple should carry silently).

    ``taxable`` / ``tax_deferred`` / ``roth`` are the NATURAL (pre-exact-sum-
    reconstruction) end-of-year balances, AFTER the spending draw, the
    tax-funding draw, and the lever/RMD transfer have all settled — the caller
    (``simulate_path_decomposition``) reconciles their sum to the dragged
    canonical total exactly (same discipline tb1 documented for
    ``step_buckets``). ``tax`` is the converged fixed-point bill (includes the
    penalty); ``spending_tax`` / ``voluntary_tax`` / ``forced_tax`` are its
    exact three-way telescoping split (DECISION 6) and sum to ``tax`` exactly.
    ``lever_transfer`` / ``rmd_excess`` are the two internal (sum-preserving)
    transfers — at most one is ever nonzero in a given year (DECISION 6: the
    lever window is 60-74, RMDs start at 75). ``taxable_draw`` /
    ``tax_deferred_draw`` / ``roth_draw`` are the SPENDING-driven draws only
    (unchanged meaning from tb1/ft1 — the tax-funding draws are NOT folded in,
    so ``test_engine_draws_non_negative_and_reconcile_with_net_draw``-style
    reconciliation to the spending ``net`` still holds byte-for-byte).
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
) -> DraggedYearStep:
    """One year of the DRAGGED access-aware decomposition (td1 DECISION 3-6).

    Mirrors tb1's ``step_buckets`` for steps 1-2 (grow, then source the flat
    spending need — byte-identical to before), then resolves the per-year
    fixed point (steps 3-4, coupled: the lever fill, the RMD excess, and the
    tax-funding draw all move together) and settles the internal transfers
    LAST (see the module docstring's "TRANSFER SETTLEMENT ORDER").

    SPENDING IS SENIOR TO THE TAX BILL (td-b, pinned — module docstring step
    2): the spending draw (step 2) sources unconditionally BEFORE the fixed
    point computes any bill, so in a crossing year the bill is JUNIOR — the
    tax-funding cascade takes what remains after spending, and whatever part
    of the bill the emptied accounts can't cover dies with the portfolio
    (depletion, the absorbing floor). This is the operand order the code has
    always had; named so the realized-spend recording can lean on it.

    ``pension_ss_ordinary`` = pensions + 0.85 x SS already summed by the caller
    (``_pension_income_at_age`` + 0.85 x ``_ss_income_at_age``) — passed in
    rather than re-derived here so this function stays a plain numeric step,
    independently unit-testable with hand-picked inputs (the design's
    hand-computed anchors call this directly). ``prior_tax_deferred`` is the
    PRIOR year's recorded (reconciled) tax-deferred balance — the RMD's
    denominator input, per DECISION 4 ("Future RMDs shrink through the real
    table" — this is what makes that true: each year's excess movement reduces
    next year's ``prior_tax_deferred``). ``bracket_fill_target`` is ``None``
    when the lever is off (every iteration then skips DECISION 5 entirely).
    """
    # 1. Grow each account (step_year's exact operand order: b + b*r).
    taxable = taxable + taxable * r
    tax_deferred = tax_deferred + tax_deferred * r
    roth = roth + roth * r

    # 2. Source the flat net spending need (tb1 cascade, byte-identical).
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

    # perf, all byte-identical (see each site): ``max``/``min`` builtins
    # became inline conditionals, ``compute_federal_tax(...).total`` became the
    # allocation-free ``federal_tax_total`` (same ``_tax_components``
    # arithmetic, no ``FederalTaxYear`` construction per iterate), the
    # convergence test compares the signed delta against ±tolerance instead of
    # calling ``abs()``, and the hot names are hoisted to locals — this loop
    # body runs ~3.5M times in a split-MC 10k run.
    _source = _source_net
    _tax_total = federal_tax_total
    _tol = FIXED_POINT_TOLERANCE
    lever_on = lever_window and bracket_fill_target is not None

    T = 0.0
    converged = False
    tf_forced = False
    tf_taxable_draw = tf_tax_deferred_draw = tf_roth_draw = 0.0
    lever_extra = rmd_excess = 0.0
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
        # (DECISION 3's scarce-TD priority: the bill is satisfied FIRST).
        residual_tax_deferred = tax_deferred_after_spend - tf_tax_deferred_draw

        lever_extra = 0.0
        if lever_on:
            target = bracket_fill_target + deduction - (
                pension_ss_ordinary + total_td_draw
            )
            if target < 0.0:
                target = 0.0
            cap = residual_tax_deferred if residual_tax_deferred > 0.0 else 0.0
            lever_extra = target if target < cap else cap

        rmd_excess = 0.0
        if rmd_window:
            rmd = prior_tax_deferred / rmd_factor(age)
            excess = rmd - total_td_draw
            if excess < 0.0:
                excess = 0.0
            # Defensive cap (never transfer more than exists) — not expected
            # to bind in practice since growth generally keeps the current
            # balance >= the RMD, but guards a pathological crash-year fixture.
            cap = residual_tax_deferred if residual_tax_deferred > 0.0 else 0.0
            rmd_excess = excess if excess < cap else cap

        ordinary_income = pension_ss_ordinary + total_td_draw + lever_extra + rmd_excess
        ltcg_income = total_taxable_draw * gains_fraction
        penalty = EARLY_WITHDRAWAL_PENALTY_RATE * total_td_draw if not penalty_free else 0.0

        T_next = _tax_total(ordinary_income, ltcg_income, n_65_plus, single) + penalty

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
            f"constant <= ~0.47 and should converge in well under 40 "
            f"iterations, so a cap hit here is a BUG (a broken bracket table "
            f"or a sourcing regression), never a legitimate slow case. See "
            f"the module docstring's convergence argument."
        )

    # Attribution (DECISION 6): telescoping three-way split of THIS SAME final
    # income vector — never a re-run of the fixed point with a component
    # dropped (that would resolve DIFFERENT tax-funding draws and the exact
    # `==` identity below would fail by cents).
    #
    # td-b PERFORMANCE (MC 10k timing gate): ``compute_federal_tax(x + 0.0,
    # ...)`` is EXACTLY ``compute_federal_tax(x, ...)`` — adding a literal
    # 0.0 changes no float bit — so when ``lever_extra`` (or ``rmd_excess``)
    # is exactly 0.0, calling again with it added is a byte-identical, purely
    # wasted call. Skipping it is not an approximation: DECISION 6 already
    # guarantees the two never co-occur, so at most one of the two guards
    # below is ever "false" in the SAME year, and every skip reuses the
    # PRECISE already-computed value rather than recomputing it.
    base_ordinary = ordinary_income - lever_extra - rmd_excess
    tax_base = _tax_total(base_ordinary, ltcg_income, n_65_plus, single)
    if lever_extra != 0.0:
        tax_base_lever = _tax_total(
            base_ordinary + lever_extra, ltcg_income, n_65_plus, single
        )
    else:
        tax_base_lever = tax_base
    if rmd_excess != 0.0:
        tax_base_lever_rmd = _tax_total(
            base_ordinary + lever_extra + rmd_excess, ltcg_income, n_65_plus, single
        )
    else:
        tax_base_lever_rmd = tax_base_lever
    spending_tax = tax_base + penalty
    voluntary_tax = tax_base_lever - tax_base
    forced_tax = tax_base_lever_rmd - tax_base_lever
    tax_total = spending_tax + voluntary_tax + forced_tax

    # Apply the tax-funding draws, THEN settle the transfers (load-bearing
    # order — module docstring "TRANSFER SETTLEMENT ORDER"): the lever/RMD
    # transfer moves gross, tax_deferred -> taxable, AFTER tax-funding has
    # already sourced from the PRE-transfer taxable balance.
    taxable = taxable_after_spend - tf_taxable_draw
    tax_deferred = tax_deferred_after_spend - tf_tax_deferred_draw
    roth = roth_after_spend - tf_roth_draw
    transfer = lever_extra + rmd_excess
    tax_deferred -= transfer
    taxable += transfer

    # Defensive float-noise floor (same discipline as tb1's step_buckets).
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
    )


@dataclass(frozen=True)
class TaxLocationPath:
    """One path's tax-location decomposition (deterministic line, or one cohort/trial).

    ``taxable`` / ``tax_deferred`` / ``roth`` are per-age END-of-year DRAGGED
    balances aligned to ``ages`` (start_age .. end_age) — under td1 these
    reflect the portfolio actually paying its own tax bill (DECISION 1). The
    depletion facts are the per-path signal the headline tb1 metric aggregates
    (UNCHANGED meaning, now computed off the dragged natural balances):

      - ``taxable_depletion_age`` — earliest age (<= 59) at which the taxable
        balance hit 0, or None if it never did pre-59½.
      - ``reachable_depletion_age`` — earliest age (<= 59) at which the REACHABLE
        money (taxable + roth) hit 0, or None — the forced-tax-deferred-draw
        event (the bridge failing).
      - ``dragged_depleted_age`` — td-b addition: the earliest age (FULL horizon,
        not pre-59½-limited) at which the DRAGGED canonical total
        (``taxable+tax_deferred+roth``) hit 0, or None if it never depleted. The
        tax-drag analogue of ``_simulate_cohort``'s bare ``depleted_age`` — this
        is what the Historical/MC fan re-point (DECISION 1 — the portfolio fan
        ITSELF becomes the dragged total for split scenarios) uses to recompute
        ``depletion_count`` / ``earliest_depletion_age`` off the DRAGGED world
        instead of the old pre-tax ``_simulate_cohort`` pass. Distinct from
        ``taxable_depletion_age`` / ``reachable_depletion_age`` above (which are
        pre-59½-only and measure a NARROWER "freely reachable money" signal, not
        whether the whole portfolio ran out).
      - ``forced_pre_595_draw`` — True iff a pre-59½ year had to source from
        tax_deferred, EITHER for spending OR for the tax bill itself (td1: the
        bill can now force a walled-year tax-deferred draw that wouldn't
        otherwise have happened — new vs ft2).

    Per-age DRAW + spending series (ft1, additive, unchanged meaning):
    ``taxable_draw`` / ``tax_deferred_draw`` / ``roth_draw`` are the SPENDING-
    driven natural per-account amounts (never the tax-funding draws — see
    ``DraggedYearStep``); ``spending`` is the year's INTENDED spend — the
    strategy's raw output, exactly what the fixed point and the dragged-total
    recurrence step with (the reconciliation tests pin the recurrence against
    THIS series, so it must never be capped).

      - ``realized_spend`` (td-b) — the year's REALIZED spend: ``min(spending,
        max_fundable_spend(dragged_begin_balance, r, gi))``, mirroring
        ``firebridgelab.sim_core._simulate_cohort``'s realized-spend discipline (same
        ``firebridgelab._step.max_fundable_spend`` formula, same begin-of-year
        operand) applied to the DRAGGED balance. Equal to ``spending`` on
        every non-depleting year; in the depletion year it caps at what the
        dragged balance could actually fund; post-depletion it is the
        affordable level (``min(intent, gi)`` — as low as 0.0 before
        GI starts). This is the CONSUMPTION series — the spend fan,
        worst-cohort drill-down, adjustment stats, stress trajectory points,
        the gross-outflow line, and the overlay's ``effective_rate`` all read
        it (recording the intended spend there overstated after-tax
        consumption on a $0 portfolio and inverted worst-cohort ranking).
        REPORTING ONLY:
        the engine still steps with ``spending`` (identical dragged totals —
        the same byte-stability argument ``max_fundable_spend``'s docstring
        makes for the scalar engines).

    td1 additions (the dragged decomposition's new recorded series, all
    per-age and aligned to ``ages``):

      - ``pretax_total`` — the PAIRED pre-tax total: the same year's
        spend/gi/r with NO tax ever subtracted, run in parallel with its OWN
        absorbing-depletion floor (see the module docstring's "PAIRED PRE-TAX
        TOTAL"). The td-b "estate cost of taxes" readout (DECISION 9) pairs
        this against ``taxable+tax_deferred+roth`` per path.
      - ``rmd_excess`` — the amount moved gross, tax_deferred -> taxable, this
        year because the actual tax-deferred draw fell short of the RMD
        (DECISION 4). 0.0 outside age >= 75.
      - ``lever_transfer`` — the amount moved gross, tax_deferred -> taxable,
        this year by the voluntary bracket-fill lever (DECISION 5). 0.0
        outside ages 60-74 or when ``bracket_fill_target`` is None. At most
        one of ``rmd_excess`` / ``lever_transfer`` is ever nonzero in a given
        year (the two windows never overlap).
      - ``ordinary_income`` / ``ltcg_income`` — the year's taxable-income
        components the fixed point converged on (pre-deduction).
      - ``tax`` — the converged fixed-point bill, INCLUDING the 10%
        early-withdrawal penalty (DECISION 3). 0.0 once the path has already
        depleted in a PRIOR year (the post-depletion convention).
      - ``spending_tax`` / ``voluntary_tax`` / ``forced_tax`` — the exact
        three-way telescoping attribution (DECISION 6):
        ``spending_tax + voluntary_tax + forced_tax == tax`` at every age,
        EXACT ``==`` by construction (never a re-run fixed point).
      - ``penalty`` — the 10% early-withdrawal penalty alone (a SUBSET of
        ``tax``, not additional to it — see ``gross_outflow`` below).
      - ``gross_outflow`` — "what the portfolio actually funds this year" =
        ``realized_spend + tax`` (td-b: the REALIZED consumption, so a
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
    # td-b addition — see the class docstring's "dragged_depleted_age" entry.
    # Defaulted (not every construction site needs to pass it — deterministic
    # single-path callers rarely read it) but always populated by
    # ``simulate_path_decomposition``.
    dragged_depleted_age: int | None = None

    taxable_draw: list[float] = field(default_factory=list)
    tax_deferred_draw: list[float] = field(default_factory=list)
    roth_draw: list[float] = field(default_factory=list)
    spending: list[float] = field(default_factory=list)
    # td-b: the REALIZED (fundable-capped) consumption series — see the class
    # docstring. spending = intent (the math input); realized_spend = what was
    # actually affordable (the reporting output).
    realized_spend: list[float] = field(default_factory=list)

    # td1 additions (see the class docstring above).
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


def year_tax_context_table(scenario) -> dict[int, tuple[bool, int, float]]:
    """Precompute the per-age ``(penalty_free, n_65_plus, pension_ss_ordinary)``
    tuple ONCE per scenario, keyed by age (td-b, MC 10k timing-gate optimization).

    All three depend ONLY on ``scenario`` + ``age`` — NEVER on the return path,
    the spend, or which cohort/trial is running — so every path in a
    Historical/MC run can share ONE lookup table instead of each path
    recomputing it fresh at every age (before this, a 10k-trial MC run called
    ``_pension_income_at_age`` / ``_ss_income_at_age`` 10,000x per age, all
    10,000 calls returning the identical number). ``simulate_path_decomposition``
    / ``simulate_dragged_depletion_only`` accept this table as the optional
    ``year_context`` — passing ``None`` (the default) recomputes inline,
    UNCHANGED from before this optimization landed, so every existing caller
    (the deterministic single path, the hand-anchored unit tests) is
    byte-for-byte unaffected.
    """
    table: dict[int, tuple[bool, int, float, bool]] = {}
    for age in range(scenario.start_age, scenario.end_age + 1):
        penalty_free = age >= TAX_LOCATION_ACCESS_AGE
        n_65_plus, single = _filing_profile(scenario, age)
        pension_ss_ordinary = _pension_income_at_age(
            scenario, age
        ) + 0.85 * _ss_income_at_age(scenario, age)
        table[age] = (penalty_free, n_65_plus, pension_ss_ordinary, single)
    return table


def simulate_path_decomposition(
    scenario,
    returns,
    strategy,
    *,
    year_context: dict[int, tuple[bool, int, float]] | None = None,
) -> TaxLocationPath:
    """Run the DRAGGED decomposition for ONE path (deterministic / cohort / trial).

    ``returns`` is the per-age real-return sequence (one entry per age step,
    start_age .. end_age) — the SAME sequence the scalar path for this line
    uses. ``strategy`` is the path's spending strategy (already built), read
    against the DRAGGED begin-of-year balance (DECISION 2 — "spending
    strategies keep reading the now-dragged begin-of-year balance"; for
    constant/lifecycle this is a no-op since spend ignores the balance).
    ``year_context`` is the OPTIONAL precomputed ``year_tax_context_table``
    (td-b performance) — Historical/MC callers build it ONCE per scenario and
    pass it to every cohort/trial; ``None`` (the default) recomputes the same
    three values inline per age, exactly as before this optimization existed.

    Per year: grow -> source spending (tb1, unchanged) -> resolve the fixed
    point (transfers + tax, ``step_buckets_dragged`` — see its docstring and
    the module docstring for the full argument) -> compute the dragged
    canonical total (with absorbing depletion) -> reconcile the three buckets
    to it exactly (same telescoping discipline tb1 established, roth as the
    ULP balancer) -> advance the PAIRED pre-tax total in parallel.

    POST-DEPLETION SHORT-CIRCUIT: once the dragged total has hit 0 in a PRIOR
    year, this loop skips ``step_buckets_dragged`` entirely for every
    subsequent year (cheap AND correct — see the module docstring's
    "post-depletion convention": a naive call would compute a real, nonzero
    bill on GI alone, which the convention says to suppress).
    """
    from firebridgelab.scenario import gi_at_age, spending_multiplier

    taxable, tax_deferred, roth = resolve_split(scenario)
    gains_fraction = scenario.taxable_gains_fraction
    bracket_fill_target = scenario.bracket_fill_target
    # The very first simulated year's RMD (if any) reads the scenario's
    # INITIAL tax-deferred balance as "last year's end balance" (no prior
    # simulated year exists) — same convention ft1/ft2 used.
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

    # The DRAGGED canonical running total (replaces tb1/ft-era's pre-tax
    # "portfolio" as the reconciliation target) and its absorbing-depletion
    # flag (extended: the tax bill can now also trigger it).
    dragged_portfolio = scenario.initial_portfolio
    dragged_depleted = False
    # td-b: the FIRST age depletion actually happens (full horizon), for the
    # Historical/MC fan re-point's depletion-count aggregation. Distinct from
    # ``taxable_depletion_age``/``reachable_depletion_age`` (pre-59½-only).
    dragged_depleted_age: int | None = None
    # The PAIRED pre-tax total: the SAME per-year spend/gi/r, no tax ever
    # subtracted, its OWN independent absorbing-depletion flag.
    pretax_portfolio = scenario.initial_portfolio
    pretax_depleted = False

    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        r = net_return(float(returns[step]), scenario.annual_fee)
        gi = gi_at_age(scenario, age)
        # DECISION 2: the strategy reads the DRAGGED begin-of-year balance.
        spending = strategy.spend_for_year(scenario, age, dragged_portfolio, gi)
        # Cost-of-living real drift (stress-test design slice 4) — mirrors
        # sim_core.py's _simulate_cohort post-strategy multiplier EXACTLY; KEEP
        # IN LOCKSTEP (same guard: none — unconditional for every spending
        # mode, incl. amortization, whose strategy solves drift-aware and
        # EXPECTS this post-multiply — see firebridgelab.spending's drift-aware
        # notes; same position: immediately after spend_for_year, before the
        # realized cap and the fixed point; same operand order; same
        # start_age-anchored exponent — the escalation anchors at the
        # SIMULATION start, never a re-solve age). A drift here the scalar
        # loops don't share (or vice versa) silently no-ops
        # split + spending_real_drift combos — the td-a..td-c gap this closed
        # left a split + sustained_cost_drift run ~4x too optimistic vs its own
        # pre-tax counterfactual. Inert at the default drift of 0.0
        # ((1+0)**k == 1.0, so every zero-drift path is byte-identical).
        spending *= spending_multiplier(scenario, age)
        # Realized-spend cap (td-b — mirrors sim_core._simulate_cohort's
        # discipline on the DRAGGED begin-of-year balance; see the
        # TaxLocationPath docstring): the strategy returns the INTENDED spend;
        # a depleting year can only fund what the dragged balance + GI cover.
        # REPORTING ONLY — the fixed point and the dragged-total recurrence
        # below still step with the INTENDED ``spending`` (byte-identical
        # totals; only the recorded consumption dollars change).
        realized_spend = min(spending, max_fundable_spend(dragged_portfolio, r, gi))

        if year_context is not None:
            penalty_free, n_65_plus, pension_ss_ordinary, single = year_context[age]
        else:
            penalty_free = age >= TAX_LOCATION_ACCESS_AGE
            n_65_plus, single = _filing_profile(scenario, age)
            pension_ss_ordinary = _pension_income_at_age(
                scenario, age
            ) + 0.85 * _ss_income_at_age(scenario, age)
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
                penalty_free,
                pension_ss_ordinary=pension_ss_ordinary,
                gains_fraction=gains_fraction,
                n_65_plus=n_65_plus,
                single=single,
                age=age,
                bracket_fill_target=bracket_fill_target,
                prior_tax_deferred=prior_tax_deferred,
            )
            taxable = step_result.taxable
            tax_deferred = step_result.tax_deferred
            roth = step_result.roth
            year_tax = step_result.tax

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

        # EXACT-SUM RECONSTRUCTION (tb1's discipline, now telescoping to the
        # DRAGGED total instead of the old pre-tax scalar): roth is the ULP
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
    )


def deterministic_blended_returns(scenario) -> list[float]:
    """The per-age CONSTANT-blend return sequence the deterministic engine uses.

    Factored out of ``simulate_tax_location_deterministic`` (td-b) so the
    solver's dragged deterministic probe (``firebridgelab.solver``, DECISION 8) builds
    the SAME return sequence ``simulate`` feeds ``step_year`` — never a second,
    possibly-drifting derivation of "what deterministic returns are".
    """
    from firebridgelab.scenario import blended_real_return_at_age

    return [
        blended_real_return_at_age(scenario, age)
        for age in range(scenario.start_age, scenario.end_age + 1)
    ]


def simulate_tax_location_deterministic(scenario) -> TaxLocationPath:
    """Deterministic tax-location decomposition (the single deterministic line).

    Builds the per-age CONSTANT-blend return sequence the deterministic engine
    uses (``deterministic_blended_returns`` — the SAME seam ``simulate`` feeds
    ``step_year``) and a fresh spending strategy (``make_strategy`` — the SAME
    the deterministic engine builds), then runs the shadow decomposition.
    Caller is responsible for only invoking this when ``has_tax_split`` is True
    and the scenario is in single sourcing mode.
    """
    from firebridgelab.spending import make_strategy

    returns = deterministic_blended_returns(scenario)
    strategy = make_strategy(scenario)
    return simulate_path_decomposition(scenario, returns, strategy)


def simulate_dragged_depletion_only(
    scenario,
    returns,
    strategy,
    *,
    year_context: dict[int, tuple[bool, int, float]] | None = None,
) -> int | None:
    """Lean, depletion-only dragged run — the solver's inner-loop primitive
    (td-b, DECISION 8's "LEAN dragged depletion-only evaluator").

    ``year_context`` is the OPTIONAL precomputed ``year_tax_context_table``
    (td-b MC/solver performance) — since ``annual_spending`` never enters that
    table, the SAME table serves every bisection probe of a solve, built once
    by the caller (mirrors ``simulate_path_decomposition``'s optimization).

    The tax-drag analogue of ``firebridgelab.sim_core._simulate_cohort``'s bare
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
    zeros) — the precompute-once discipline ``solver.py``'s module docstring
    describes for the plain engine, carried over to the dragged one.

    Returns the first depleted age, or ``None`` if the path never depletes.
    """
    from firebridgelab.scenario import gi_at_age, spending_multiplier

    taxable, tax_deferred, roth = resolve_split(scenario)
    gains_fraction = scenario.taxable_gains_fraction
    bracket_fill_target = scenario.bracket_fill_target
    # First year's RMD reads the scenario's INITIAL tax-deferred balance as
    # "last year's end balance" — same convention as the full loop.
    prior_tax_deferred = tax_deferred

    dragged_portfolio = scenario.initial_portfolio

    for age in range(scenario.start_age, scenario.end_age + 1):
        step = age - scenario.start_age
        r = net_return(float(returns[step]), scenario.annual_fee)
        gi = gi_at_age(scenario, age)
        spending = strategy.spend_for_year(scenario, age, dragged_portfolio, gi)
        # Cost-of-living real drift — the SAME post-strategy multiplier the
        # full loop (simulate_path_decomposition) and sim_core._simulate_cohort
        # apply; KEEP ALL THREE IN LOCKSTEP (this lean solver loop mirrors the
        # full loop's per-year setup EXACTLY, per this function's docstring —
        # a drift applied there but not here would make the solver bisect a
        # DIFFERENT spend path than the display engine renders). Inert at the
        # default drift of 0.0.
        spending *= spending_multiplier(scenario, age)

        if year_context is not None:
            penalty_free, n_65_plus, pension_ss_ordinary, single = year_context[age]
        else:
            penalty_free = age >= TAX_LOCATION_ACCESS_AGE
            n_65_plus, single = _filing_profile(scenario, age)
            pension_ss_ordinary = _pension_income_at_age(
                scenario, age
            ) + 0.85 * _ss_income_at_age(scenario, age)

        step_result = step_buckets_dragged(
            taxable,
            tax_deferred,
            roth,
            r,
            gi,
            spending,
            penalty_free,
            pension_ss_ordinary=pension_ss_ordinary,
            gains_fraction=gains_fraction,
            n_65_plus=n_65_plus,
            single=single,
            age=age,
            bracket_fill_target=bracket_fill_target,
            prior_tax_deferred=prior_tax_deferred,
        )
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


@dataclass(frozen=True)
class DepletionDistribution:
    """The depletion-age distribution among the paths that depleted pre-59½.

    All ages are ints; None when NO path depleted pre-59½. Mirrors the spirit of
    ``BufferSurvivalStats`` (median + worst sentinel) for the tax-location metric.

      - ``median`` — median depletion age among depleting paths.
      - ``p10`` — the 10th-percentile (earliest-tail) depletion age.
      - ``earliest`` — the single earliest depletion age across all paths.
    """

    median: int | None
    p10: int | None
    earliest: int | None


@dataclass(frozen=True)
class TaxLocationRisk:
    """Aggregated tax-location risk metric across cohorts / trials (the headline).

    The Historical / Monte Carlo analogue of ``BufferSurvivalStats``: across all
    paths, how often does the FREELY-REACHABLE money run dry before the penalty
    door opens at 59½, and how early.

      - ``count`` — number of paths covered.
      - ``pct_taxable_depleted_pre_595`` — % (0..100) of paths whose taxable
        balance hit 0 at age <= 59.
      - ``pct_reachable_depleted_pre_595`` — % (0..100) of paths whose reachable
        money (taxable + roth) hit 0 at age <= 59 (forced tax-deferred draw).
      - ``taxable_depletion`` — the depletion-age distribution among paths whose
        TAXABLE depleted pre-59½ (median / p10 / earliest).
      - ``reachable_depletion`` — same distribution for REACHABLE depletion.
    """

    count: int
    pct_taxable_depleted_pre_595: float
    pct_reachable_depleted_pre_595: float
    taxable_depletion: DepletionDistribution
    reachable_depletion: DepletionDistribution

    # Optional taxable percentile bands for charting the depletion spread (the
    # design's "optionally taxable percentile bands"). Per-age percentiles of the
    # taxable balance across paths; None on the deterministic single-path side
    # (which carries the explicit balance series instead).
    ages: list[int] | None = None
    taxable_p10: list[float] | None = None
    taxable_median: list[float] | None = None
    taxable_p90: list[float] | None = None

    # Per-account median balance series + the TOTAL-investments fan (txlb
    # hotfix). Median is not additive, so the three account medians do NOT stack
    # to ``total_median`` — that is expected and correct; the chart draws them as
    # LINES (typical composition) while the total fan carries the uncertainty.
    #   - ``tax_deferred_median`` / ``roth_median`` — per-age median across paths
    #     of each account's reconstructed end-of-year balance (taxable's median is
    #     ``taxable_median`` above).
    #   - ``total_p10`` / ``total_median`` / ``total_p90`` — per-age percentiles of
    #     the per-path TOTAL (= taxable + tax_deferred + roth). Under td1 this is
    #     the DRAGGED total (DIES: this used to match the still-pre-tax overall
    #     portfolio fan exactly; re-pointing the portfolio fan ITSELF to be the
    #     dragged total is a td-b deliverable — see
    #     ``tests/test_tax_location.py``'s re-pointed consistency test).
    tax_deferred_median: list[float] | None = None
    roth_median: list[float] | None = None
    total_p10: list[float] | None = None
    total_median: list[float] | None = None
    total_p90: list[float] | None = None


def _distribution(ages: list[int]) -> DepletionDistribution:
    """Median / p10 / earliest of a list of depletion ages (empty → all None)."""
    if not ages:
        return DepletionDistribution(median=None, p10=None, earliest=None)
    import numpy as np

    arr = np.array(ages, dtype=float)
    return DepletionDistribution(
        median=int(round(float(np.median(arr)))),
        p10=int(round(float(np.percentile(arr, 10)))),
        earliest=int(min(ages)),
    )


def aggregate_tax_location_risk(
    paths: list[TaxLocationPath],
) -> TaxLocationRisk:
    """Aggregate per-path decomposition facts into the headline risk metric.

    Shared by the Historical and Monte Carlo bucket paths (the SAME aggregation, so the
    two readouts can't drift — same discipline as ``compute_buffer_survival``).
    Percentages are 0..100. An empty input returns a zeroed metric rather than
    dividing by zero.
    """
    n = len(paths)
    if n == 0:
        empty = DepletionDistribution(median=None, p10=None, earliest=None)
        return TaxLocationRisk(
            count=0,
            pct_taxable_depleted_pre_595=0.0,
            pct_reachable_depleted_pre_595=0.0,
            taxable_depletion=empty,
            reachable_depletion=empty,
        )

    taxable_ages = [
        p.taxable_depletion_age for p in paths if p.taxable_depletion_age is not None
    ]
    reachable_ages = [
        p.reachable_depletion_age
        for p in paths
        if p.reachable_depletion_age is not None
    ]

    # Optional per-age taxable percentile bands (paths share the same age axis).
    ages = paths[0].ages
    import numpy as np

    taxable_matrix = np.array([p.taxable for p in paths], dtype=float)
    p10, median, p90 = (
        row.tolist()
        for row in np.percentile(taxable_matrix, (10, 50, 90), axis=0)
    )

    # Per-account median series (same per-age percentile machinery as taxable).
    tax_deferred_matrix = np.array([p.tax_deferred for p in paths], dtype=float)
    roth_matrix = np.array([p.roth for p in paths], dtype=float)
    tax_deferred_median = np.percentile(tax_deferred_matrix, 50, axis=0).tolist()
    roth_median = np.percentile(roth_matrix, 50, axis=0).tolist()

    # The TOTAL-investments fan: per-age percentiles of the per-path TOTAL
    # (= taxable + tax_deferred + roth). Under td1 this is the DRAGGED total by
    # construction (see the TaxLocationRisk field docstring).
    total_matrix = taxable_matrix + tax_deferred_matrix + roth_matrix
    total_p10, total_median, total_p90 = (
        row.tolist()
        for row in np.percentile(total_matrix, (10, 50, 90), axis=0)
    )

    return TaxLocationRisk(
        count=n,
        pct_taxable_depleted_pre_595=100.0 * len(taxable_ages) / n,
        pct_reachable_depleted_pre_595=100.0 * len(reachable_ages) / n,
        taxable_depletion=_distribution(taxable_ages),
        reachable_depletion=_distribution(reachable_ages),
        ages=ages,
        taxable_p10=p10,
        taxable_median=median,
        taxable_p90=p90,
        tax_deferred_median=tax_deferred_median,
        roth_median=roth_median,
        total_p10=total_p10,
        total_median=total_median,
        total_p90=total_p90,
    )


@dataclass(frozen=True)
class EstateCostOfTaxes:
    """"Estate cost of taxes" readout (td-b, DECISION 9) — framed as "how
    much smaller the estate ends because the portfolio paid the IRS", paired
    with the success-rate delta so a $0 estate cost can never misread as "taxes
    are free" in exactly the scenarios (both runs deplete) where they broke the
    plan.

    ``estate_cost = pretax_terminal - dragged_terminal`` PER PATH, where
    ``pretax_terminal`` comes from an INDEPENDENT run of the scenario's
    pre-tax/legacy world — a FRESH spending-strategy instance reading the
    UNTAXED balance, over the SAME return sequence — never the same-spend
    ``TaxLocationPath.pretax_total`` td-a recorded (that series is paired
    against the DRAGGED run's OWN spend decisions, kept for the drag-direction
    BINDS invariant; DECISION 9 wants the two runs to be honestly independent,
    "each is the honest run of its own world" — under a dynamic strategy the
    two can spend, and therefore deplete, at different times). Aggregation is
    the MEDIAN of per-path differences (paired by return sequence), never a
    difference of medians (that would let the two worlds' medians come from
    DIFFERENT paths).

      - ``count`` — number of paths (1 for the deterministic single pair).
      - ``median_estate_cost`` — median across paths of
        ``pretax_terminal[i] - dragged_terminal[i]``; the single value at
        ``count == 1``.
      - ``pretax_success_rate`` / ``dragged_success_rate`` — fraction (0..1) of
        paths that did NOT deplete by ``end_age`` in each independent world.
      - ``success_rate_delta`` — ``dragged_success_rate - pretax_success_rate``
        (typically <= 0: drag can only add outflow, never remove it, on a
        STATIC-spend fixture; a dynamic strategy's own reactive cuts can narrow
        or, in principle, flip this locally — the copy discloses that the two
        runs spend slightly differently under dynamic strategies).
    """

    count: int
    median_estate_cost: float
    pretax_success_rate: float
    dragged_success_rate: float
    success_rate_delta: float


def aggregate_estate_cost(
    pretax_terminals: list[float],
    dragged_terminals: list[float],
    pretax_success: list[bool],
    dragged_success: list[bool],
) -> EstateCostOfTaxes:
    """Aggregate the per-path (pretax, dragged) terminal pairs into the DECISION 9
    readout. All four lists must be the SAME length and index-aligned (path i's
    pretax terminal / dragged terminal / pretax success / dragged success), so
    the per-path DIFFERENCE (never a difference of medians) is well-defined.
    ``count == 1`` (the deterministic mode caller) degenerates correctly: the
    "median" of a single value is that value, so the same function serves both
    the deterministic single-pair case and the Historical/MC many-path case.

    An empty input returns a zeroed readout rather than dividing by zero /
    calling ``np.median`` on an empty array (defensive; callers only invoke
    this behind the same ``has_tax_split`` gate the rest of the split-mode
    machinery uses).
    """
    n = len(pretax_terminals)
    if n == 0:
        return EstateCostOfTaxes(
            count=0,
            median_estate_cost=0.0,
            pretax_success_rate=0.0,
            dragged_success_rate=0.0,
            success_rate_delta=0.0,
        )
    import numpy as np

    diffs = [pretax_terminals[i] - dragged_terminals[i] for i in range(n)]
    median_estate_cost = float(np.median(np.array(diffs, dtype=float)))
    pretax_rate = sum(1 for s in pretax_success if s) / n
    dragged_rate = sum(1 for s in dragged_success if s) / n
    return EstateCostOfTaxes(
        count=n,
        median_estate_cost=median_estate_cost,
        pretax_success_rate=pretax_rate,
        dragged_success_rate=dragged_rate,
        success_rate_delta=dragged_rate - pretax_rate,
    )
