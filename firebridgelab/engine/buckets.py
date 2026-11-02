"""The 3-bucket withdrawal-sourcing mechanic (bucket-strategy design).

Where ``firebridgelab/_step.py:step_year`` steps ONE blended portfolio balance, this
module steps THREE buckets — cash / income / growth — with cash-first sourcing
and a ONE-SIDED refill (rebalance in up years, ride in down years). It is the
``sourcing_mode == "buckets"`` analogue of the single-balance ``_simulate_cohort``
the three engines share, so the per-year mechanic has ONE definition and cannot
drift between Deterministic / Historical / Monte Carlo.

Why one bucket reproduces the single-balance engine BYTE-FOR-BYTE (Gate B)
--------------------------------------------------------------------------
With a SINGLE (cash-)bucket holding the full scenario allocation (so its blended
return ``r`` equals the single-balance blend, income + growth empty), GI lands on
cash and ALL of the year's spending sources from that one funded bucket, so the
per-year update is::

    B_cash ← (B_cash + B_cash*r + gi) − s*(1 + r/2)   # grow, +GI, then source s

which is exactly ``step_year``'s canonical form
``end = start + start*r + (gi − spending)*(1 + r/2)`` — SAME operands, SAME LEFT-TO-
RIGHT association, byte-identical under IEEE-754, then floored ONCE. The grow-then-
add-GI ``(B + B*r) + gi`` regroups bit-for-bit to ``B + B*r + gi`` (left-assoc),
and the midpoint is the identical ``- s*(1 + r/2)`` expression, NOT the
algebraically-equal-but-not-bit-equal ``B*(1+r)`` form. CRUCIALLY the single
funded bucket absorbs the FULL spend ``s`` (including any overdraw beyond its
balance) BEFORE the one final floor — so a deep-drawdown year that floors to 0
under ``step_year`` (GI eaten by the floor) ALSO floors to 0 here, reproducing the
single-balance depletion count/flag, not just the percentile fans. That is the
rigorous plumbing anchor (Gate B): buckets-with-one-bucket == single, BYTE-FOR-
BYTE, INCLUDING depletion. (The earlier ``s_cash = min(s, B_cash)`` cap was the
bug: it stranded GI above the floor and gave the bucket path FEWER depletions than
single, confounding the bucket-vs-single head-to-head with a floor-semantics
difference instead of the sourcing difference.)

The per-year mechanic (CORRECTNESS-CRITICAL — pinned to the corrected design)
----------------------------------------------------------------------------
State entering year ``a``: three non-negative balances ``(B_cash, B_inc, B_gro)``
and per-bucket real returns ``(r_cash, r_inc, r_gro)`` (each bucket's allocation
blend at age ``a``, resolved per mode). Order of operations (the GI/floor order is
correctness-critical — see "Why this preserves the canary" above):

  1. Grow each bucket: ``B_i ← B_i + B_i*r_i``.
  2. Add GI flat to the grown cash bucket (no growth factor):
     ``B_cash ← B_cash + GI(a)``.
  3. Source GROSS spending ``s = spending_for_age(a)`` cash-first against the
     GROWN + GI balances: take ``min(remaining, available)`` from cash, then
     income, then growth (cascade). Each SOURCED bucket loses the midpoint-
     discounted withdrawal ``B_i ← B_i − s_i*(1 + r_i/2)``. The residual that
     exceeds the total available across the three buckets (a genuine overdraw /
     depletion) is charged to the LAST funded bucket touched (the deepest the
     cascade reached — growth when growth has dollars, else income, else cash), so
     it may go TRANSIENTLY NEGATIVE. We do NOT floor mid-cascade in a way that
     strands GI above zero — there is ONE final floor in step 5. (In the single-
     bucket degenerate, income + growth are empty, so cash is the last funded
     bucket and absorbs the full spend incl. overdraw — the Gate-B identity.)
  4. Year-end rebalance toward the (rolling, shrinking) targets — GROWTH IS THE
     RESIDUAL (the CORRECTED mechanic, which replaced an original
     one-sided refill stranding the freed safe-side dollars in income). With
     next-year targets ``cash_tgt_next`` / ``income_tgt_next`` (the forward gap —
     these SHRINK as GI lands and the gap collapses):
       (a) RELEASE EXCESS → GROWTH, ANY year. If the safe side is over-funded
           (``new_cash + new_inc > cash_tgt_next + income_tgt_next`` — happens
           precisely as pensions/SS land and the gap collapses), move the excess
           into GROWTH (buying growth is allowed in any market), then set cash →
           ``cash_tgt_next`` and income → ``income_tgt_next``. THIS is the leg the
           original spec was missing — it makes growth RISE as GI lands instead of
           the freed money stranding in income.
       (b) TOP-UP SHORTFALL, UP years only. If the safe side is below target
           (normal mid-bridge as it spends down), refill from growth to the targets
           ONLY when ``up = (r_gro > UP_TRIGGER_FLOOR)`` (sell high). In a down year
           do NOT sell growth to refill (ride) — leave the safe side under target;
           growth is only ever drawn for SPENDING when cash+income are genuinely
           exhausted (the buffer-survival failure mode, step 3).
       (c) Within the safe side, move freely between cash and income to hit each
           target (mass-preserving).
     All transfers are mass-preserving end-of-year-dollar reallocations (no growth
     / midpoint applied), on the post-source (still-unfloored) balances. The
     over-funded and under-funded branches are mutually exclusive (the safe sum is
     either above or below the target sum), so at most one fires. SYMMETRIC_REFILL
     forces the top-up leg every year (the Gate-D contrast); the release leg is
     unconditional regardless.
  5. ONE final floor per bucket at 0. The portfolio is depleted the first year the
     PRE-floor SUMMED balance goes strictly ``< 0`` (the cascade overdrew — spending
     could not be funded), evaluated BEFORE this floor — the SAME strictly-negative
     test ``step_year`` applies to its single pre-floor ``end_portfolio``. A year
     that lands EXACTLY on 0.0 is NOT depleted (it floored cleanly with nothing
     left), so the single-bucket Gate-B depletion identity is structural at the
     zero boundary, not merely empirical for the current cohorts.

A "forced growth sale" (the buffer-survival metric) = a year where dollars were
truly sold from the growth bucket (``sold_from_growth > 0`` = ``min(growth
request, available growth)``) because cash + income (the buffer) were exhausted in
a drawdown. A request beyond growth's balance is depletion (the portfolio can't
pay), NOT a "sale" of dollars that aren't there — so an empty growth bucket
records ZERO forced sale (the metric counts real sales, not phantom overdraws).
That is the real failure mode the strategy measures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import numpy as np

from firebridgelab.engine.scenario import Scenario
from firebridgelab.engine.step import net_return
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.spending.base import BucketsGuardrailsNotComposable, SpendingStrategy
from firebridgelab.spending.lifecycle import spending_multiplier, spending_for_age
from firebridgelab.spending.modes import (
    SPENDING_MODE_FR_GUARDRAIL,
    SPENDING_MODE_FR_SMOOTH,
    SPENDING_MODE_FR_TIERS,
    SPENDING_MODE_GUARDRAILS,
)
from firebridgelab.spending.registry import make_strategy


# Up/down refill trigger (bucket-strategy design "Knobs": "the single
# highest-leverage knob; expose it as at least a documented constant the
# verification can flip"). The default is a one-sided rule keyed on the growth
# bucket's real return being POSITIVE this year (``r_gro > UP_TRIGGER_FLOOR``,
# floor 0). Set ``UP_TRIGGER_FLOOR`` arbitrarily high (e.g. 1e9) to make every
# year a DOWN year (never refill — pure ride), or set ``SYMMETRIC_REFILL = True``
# to refill EVERY year regardless of the trigger (the symmetric / rebalance-both-
# -ways variant Gate D contrasts against). v1 ships the one-sided default; the
# verification flips these to exercise the thesis.
UP_TRIGGER_FLOOR: float = 0.0
SYMMETRIC_REFILL: bool = False

# The asset-class ids that count as EQUITY for the effective-equity glidepath
# readout (stocks + REITs). Everything else — all treasuries / agg bonds / TIPS,
# gold, broad commodities, cash / short-treasuries — is NOT equity.
EQUITY_CLASS_IDS: frozenset[str] = frozenset(
    {"us_total_market", "us_small_value", "intl_developed", "emerging_markets", "us_reit"}
)


@dataclass(frozen=True)
class BucketYear:
    """One year's snapshot of the 3-bucket sim.

    All balances are END-of-year (post-source, post-GI, post-refill, floored).
    ``forced_growth_sale`` is True iff spending had to be sourced from the growth
    bucket this year (cash + income exhausted) — the buffer-survival signal.
    """

    age: int
    cash: float
    income: float
    growth: float
    spending: float          # gross spending withdrawn this year
    guaranteed_income: float
    sourced_growth: float    # dollars taken from growth this year (the forced sale)
    forced_growth_sale: bool


@dataclass(frozen=True)
class BucketTrajectory:
    """Result of one 3-bucket run (one cohort / trial / the deterministic path)."""

    years: list[BucketYear]
    depleted_age: int | None     # first age all three buckets hit 0, else None
    final_portfolio: float       # cash + income + growth at end_age
    first_forced_sale_age: int | None  # first age a forced growth sale occurred


def _initial_balances(scenario: Scenario) -> tuple[float, float, float]:
    """Initial cash / income / growth balances at ``start_age``.

    bucket-strategy design "The forward gap": ``cash = cash_target(start)``,
    ``income = income_target(start)``, ``growth = initial_portfolio − cash −
    income``. If the safe buckets can't be funded (``cash + income >
    initial_portfolio``), scale cash and income down PROPORTIONALLY so growth = 0
    (the ``bucket_underfunded`` case — the engine must not crash on it). Not the
    default household's case (9yr gap ≪ $1.8M), but handled.
    """
    start = scenario.start_age
    cash = cash_target(scenario, start)
    income = income_target(scenario, start)
    safe = cash + income
    portfolio = scenario.initial_portfolio
    if safe > portfolio and safe > 0:
        # Proportional scale-down so growth lands at exactly 0.
        scale = portfolio / safe
        cash *= scale
        income *= scale
        growth = 0.0
    else:
        growth = portfolio - safe
    return cash, income, growth


def bucket_underfunded(scenario: Scenario) -> bool:
    """True iff the safe buckets can't be fully funded at ``start_age``.

    bucket-strategy design: surface a ``bucket_underfunded`` flag when
    ``cash_target(start) + income_target(start) > initial_portfolio`` (cash +
    income are then scaled down proportionally and growth = 0). The payload reads
    this so the UI can show a one-line note. Pure / side-effect-free.
    """
    start = scenario.start_age
    safe = cash_target(scenario, start) + income_target(scenario, start)
    return safe > scenario.initial_portfolio


def _step_buckets(
    balances: tuple[float, float, float],
    returns: tuple[float, float, float],
    gi: float,
    spending: float,
    cash_tgt_next: float,
    income_tgt_next: float,
) -> tuple[tuple[float, float, float], float, bool, float]:
    """One year of the 3-bucket mechanic.

    Returns ``(end_balances, sourced_growth, depleted_this_year, realized_spend)``,
    where ``realized_spend`` is the spend the sourced buckets could ACTUALLY fund
    under the midpoint convention — the cash-first sum of ``avail[i]/(1 + r_i/2)``
    capped at the request (== ``spending`` in any non-depleting year; the midpoint-
    discounted fundable amount once the buckets can't cover the request, NOT the raw
    drained balance). In the single-(cash-)bucket degenerate it equals the single-
    portfolio ``_step.max_fundable_spend`` to the cent (the Gate-B identity extended
    to recorded spend). It is REPORTED only and never alters the portfolio step
    (``draws`` / balances / depletion), so the Gate-B BALANCE identity holds byte-
    for-byte.

    ``balances`` = (cash, income, growth) at year start; ``returns`` =
    (r_cash, r_inc, r_gro); ``cash_tgt_next`` / ``income_tgt_next`` are the rolling
    targets for NEXT year (the refill brings the safe buckets up to where they need
    to be entering year a+1). Pinned to the corrected design order-of-operations
    (grow → +GI → source against grown+GI → refill → ONE final floor); the byte-
    for-byte single-bucket identity rests on the exact float expressions below
    (see the module docstring).

    ``depleted_this_year`` mirrors ``step_year``'s semantics EXACTLY: it flags the
    year as depleted iff the PRE-floor SUMMED bucket balance is strictly ``< 0`` —
    the cascade overdrew, spending could not be funded — evaluated BEFORE the final
    per-bucket floor. The refill is mass-preserving, so the pre-floor sum is
    invariant to it; in the single-(cash-)bucket degenerate (income + growth empty,
    no refill) the pre-floor sum IS ``step_year``'s ``end_portfolio``, so this test
    reduces to ``end_portfolio < 0`` byte-for-byte. A year that lands EXACTLY on 0.0
    is NOT flagged (matching ``step_year``), so the Gate-B depletion identity is
    STRUCTURAL at the zero boundary, not merely empirical for the current cohorts.
    """
    b_cash, b_inc, b_gro = balances
    r_cash, r_inc, r_gro = returns

    # 1. Grow each bucket. 2. Add GI flat to the GROWN cash bucket (no growth
    # factor). The cash expression is written in step_year's EXACT operand order —
    # ``b_cash + b_cash*r_cash + gi`` — and ``(B + B*r) + gi`` regroups bit-for-bit
    # to ``B + B*r + gi`` (left-assoc), so the single-(cash-)bucket degenerate
    # reproduces step_year BYTE-FOR-BYTE under IEEE-754 (Gate B). Income / growth
    # carry no GI.
    grown_cash = b_cash + b_cash * r_cash + gi * (1 + r_cash / 2)
    grown_inc = b_inc + b_inc * r_inc
    grown_gro = b_gro + b_gro * r_gro

    # 3. Source GROSS spending cash-first against the GROWN + GI balances. Each
    # bucket takes min(remaining_request, max(0, its grown balance)); the residual
    # that exceeds the total available (a genuine overdraw / depletion) is charged
    # to the LAST funded bucket the cascade reached, so it may go transiently
    # NEGATIVE — NO mid-cascade floor that would strand GI above zero (the bug).
    # The sourced amount is midpoint-discounted ``s_i*(1 + r_i/2)`` per bucket.
    avail = (max(0.0, grown_cash), max(0.0, grown_inc), max(0.0, grown_gro))
    # Each bucket reduces by ``draw*(1 + r_i/2)``, so a grown balance funds at most
    # ``avail[i]/(1 + r_i/2)`` of spend. Capping the cascade there drains an
    # exhausted bucket to exactly 0 (capping at ``avail[i]`` itself left it at
    # ``-draw*r_i/2``, which the year-end floor then erased — money created).
    fund_caps = (
        avail[0] / (1 + r_cash / 2),
        avail[1] / (1 + r_inc / 2),
        avail[2] / (1 + r_gro / 2),
    )
    remaining = spending
    draws = [0.0, 0.0, 0.0]
    last_funded = -1
    for i in range(3):
        take = min(remaining, fund_caps[i])
        if take > 0.0:
            last_funded = i
        draws[i] = take
        remaining -= take
    # Charge the overdraw (request beyond total availability) to the deepest funded
    # bucket — growth when growth has dollars, else income, else cash. In the
    # single-bucket degenerate income + growth are empty, so cash is last_funded and
    # absorbs the full spend incl. overdraw (the Gate-B identity). If NO bucket had
    # any availability (everything already at/under zero), charge cash so the
    # overdraw still registers against the portfolio (it floors to 0 either way).
    remaining_before_overdraw = max(0.0, remaining)
    if remaining > 0.0:
        draws[last_funded if last_funded >= 0 else 0] += remaining

    # REALIZED (recorded/charted) spend: what the sourced buckets could actually
    # fund under the midpoint convention — the request minus any overdraw the
    # cascade could not cover. Non-depleting years: ``realized == spending``.
    realized_spend = spending - remaining_before_overdraw

    # The ACTUAL forced growth sale = dollars truly withdrawn from the growth
    # balance = min(requested-from-growth, available growth). The overdraw possibly
    # parked on growth (when growth is the last funded bucket) is phantom — it is a
    # request beyond growth's balance (depletion), NOT a real sale — so it is
    # excluded: the buffer-survival metric counts real sales, not phantom overdraws.
    sold_from_growth = min(draws[2], fund_caps[2])

    # Apply the midpoint-discounted withdrawals to the grown+GI balances.
    new_cash = grown_cash - draws[0] * (1 + r_cash / 2)
    new_inc = grown_inc - draws[1] * (1 + r_inc / 2)
    new_gro = grown_gro - draws[2] * (1 + r_gro / 2)

    # 4. Year-end rebalance toward the (rolling, shrinking) targets — GROWTH IS THE
    # RESIDUAL (the corrected mechanic). The safe side is either over-funded (release
    # the excess to growth — the missing leg) or under-funded (top up from growth in
    # up years only — the one-sided ride-down protection); the two branches are
    # mutually exclusive on the summed safe side. All moves are mass-preserving (no
    # growth/midpoint applied), on the post-source (still-unfloored) balances.
    safe = new_cash + new_inc
    safe_target = cash_tgt_next + income_tgt_next
    if safe > safe_target:
        # (a) RELEASE EXCESS → GROWTH, ANY year. The safe buckets are over-funded
        # (the gap collapsed as GI landed): the excess goes to growth (buying growth
        # is allowed in any market), and cash/income snap to their targets. This is
        # what makes growth RISE as GI lands instead of stranding in income.
        #
        # GATE-B NOTE: the single-bucket byte-for-byte identity uses the GROWTH-only
        # degenerate (n1 = n2 = 0 → cash_tgt_next = income_tgt_next = 0, growth holds
        # the whole portfolio). There this branch fires ONLY when the cash bucket
        # retains GI above the spend (``safe = leftover GI > 0 = safe_target``), moving
        # that leftover into growth — but in the default household GI < spend every
        # year, so cash drains to (or below) 0 each year and ``safe > 0`` is False:
        # the else branch runs and leaves the balances bit-untouched. (The CORRECTED
        # release leg is precisely why the OLD huge-n1 cash-only fixture no longer
        # stays inert — once the portfolio outgrows the shrinking cash target the
        # release fires and the path diverges; growth-only sidesteps that by pinning
        # the safe targets to 0.)
        new_gro += safe - safe_target
        new_cash = cash_tgt_next
        new_inc = income_tgt_next
    else:
        # (b) TOP-UP SHORTFALL, UP years only (or SYMMETRIC_REFILL — the Gate-D
        # contrast, which tops up every year). In a down year we do NOT sell growth
        # to refill (ride): leave the safe side under target; growth is only drawn
        # for SPENDING when cash+income are exhausted (the buffer-survival failure
        # mode, step 3). Cash first then income, each capped by available growth
        # (sell high). With nothing to move (new_gro <= 0) both transfers are 0.
        up = SYMMETRIC_REFILL or (r_gro > UP_TRIGGER_FLOOR)
        if up and new_gro > 0:
            cash_need = max(0.0, cash_tgt_next - new_cash)
            transfer_cash = min(cash_need, new_gro)
            new_cash += transfer_cash
            new_gro -= transfer_cash

            income_need = max(0.0, income_tgt_next - new_inc)
            transfer_inc = min(income_need, new_gro)
            new_inc += transfer_inc
            new_gro -= transfer_inc
        # (c) Within the safe side, rebalance cash↔income toward each target (mass-
        # preserving — moving dollars between two SAFE buckets is never a growth
        # sale, so this runs regardless of the up/down trigger). Pull income up to
        # its target from any cash surplus above cash_tgt_next, or backfill cash from
        # income when cash is short — honoring cash-first sourcing. Guarded on a real
        # cash↔income imbalance vs the targets so the GROWTH-only single-bucket
        # degenerate (income empty, both safe targets 0, cash drained to ≤ 0 each
        # year) is left BIT-UNTOUCHED (Gate B): with new_inc == 0 and income_tgt_next
        # == 0 neither sub-branch fires, so no min/subtraction re-rounds new_cash.
        cash_surplus = new_cash - cash_tgt_next
        if cash_surplus > 0.0 and new_inc < income_tgt_next:
            # Move cash's surplus into income, up to income's remaining room.
            move = min(cash_surplus, income_tgt_next - new_inc)
            new_cash -= move
            new_inc += move
        elif new_cash < cash_tgt_next and new_inc > 0.0:
            # Cash is short and income has dollars: backfill cash from income.
            move = min(cash_tgt_next - new_cash, new_inc)
            new_cash += move
            new_inc -= move

    # Depletion is flagged on the PRE-floor SUMMED balance, strictly < 0 — the same
    # test step_year applies to its single pre-floor end_portfolio (the cascade
    # overdrew; spending could not be funded). Computed here, BEFORE the final floor.
    # The refill (step 4) is mass-preserving, so this sum is identical pre/post
    # refill; a year landing EXACTLY on 0.0 is NOT depleted (matching step_year), so
    # the Gate-B identity is structural at the zero boundary. In the single-bucket
    # degenerate (income + growth empty, no refill) this sum IS step_year's
    # end_portfolio, so the test reduces to ``end_portfolio < 0`` byte-for-byte.
    pre_floor_sum = new_cash + new_inc + new_gro
    depleted_this_year = pre_floor_sum < 0

    # 5. ONE final floor per bucket at 0 (the only floor — a sourced bucket can go
    # transiently negative in the cascade above; the portfolio is depleted the
    # first year all three buckets floor to 0). GI is available-to-spend BEFORE
    # this floor (folded into the cash balance in step 2), matching step_year's
    # floor-eats-GI semantics — the Gate-B depletion identity.
    new_cash = max(0.0, new_cash)
    new_inc = max(0.0, new_inc)
    new_gro = max(0.0, new_gro)

    return (new_cash, new_inc, new_gro), sold_from_growth, depleted_this_year, realized_spend


def simulate_buckets_cohort(
    scenario: Scenario,
    bucket_returns: dict[str, np.ndarray],
    strategy: SpendingStrategy | None = None,
) -> BucketTrajectory:
    """Run one 3-bucket trajectory given per-bucket per-year real-return arrays.

    The single shared 3-bucket loop — the ``sourcing_mode == "buckets"`` analogue
    of ``historical._simulate_cohort`` — so all three engines feed it (Deterministic
    a constant-per-bucket array, Historical the cohort per-class blend, MC the
    sampled per-class blend) and the mechanic can't drift between modes.

    ``bucket_returns`` maps each bucket id (``"cash"`` / ``"income"`` / ``"growth"``)
    to its length-``n_steps`` real-return array (one entry per age-step,
    start_age..end_age inclusive). Spending comes from a ``SpendingStrategy`` built
    ONCE before the loop (buckets compose with the byte-stable PureAgeStrategy —
    constant / lifecycle; guardrails is fenced at the API). The rolling cash/income
    TARGETS for next year are recomputed each step from the scenario (so they shrink
    as GI lands), and GI is the SAME four-stream sum the single path uses.

    Guardrails is FENCED in buckets mode (bucket-strategy design — composing buckets
    + Guyton-Klinger is slice 2): raise the typed ``BucketsGuardrailsNotComposable``
    here (defense-in-depth; the API also validates the combination up front).
    """
    if scenario.spending_mode in (
        SPENDING_MODE_GUARDRAILS,
        SPENDING_MODE_FR_SMOOTH,
        SPENDING_MODE_FR_GUARDRAIL,
        SPENDING_MODE_FR_TIERS,
    ):
        raise BucketsGuardrailsNotComposable(
            "dynamic spending (guardrails / funded-ratio) and bucket sourcing do "
            "not compose yet (a later slice) — use constant or lifecycle spending "
            "with buckets"
        )
    if strategy is None:
        strategy = make_strategy(scenario)

    bc = scenario.buckets
    cash, income, growth = _initial_balances(scenario)

    years: list[BucketYear] = []
    depleted_age: int | None = None
    first_forced_sale_age: int | None = None

    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        fee = scenario.annual_fee
        r_cash = net_return(float(bucket_returns[bc.cash.id][step]), fee)
        r_inc = net_return(float(bucket_returns[bc.income.id][step]), fee)
        r_gro = net_return(float(bucket_returns[bc.growth.id][step]), fee)
        gi = gi_at_age(scenario, age)
        # Spending from the strategy. The "begin-of-year portfolio" denominator the
        # guardrail rate uses isn't meaningful for a bucket split (and guardrails is
        # fenced anyway), so we pass the summed begin-of-year balance for signature
        # parity; the byte-stable PureAgeStrategy ignores it.
        begin_total = cash + income + growth
        spending = strategy.spend_for_year(scenario, age, begin_total, gi)
        # Cost-of-living real drift (stress-test design slice 4): the SAME post-strategy
        # multiplier the single-sourcing loops apply (firebridgelab.simulate /
        # firebridgelab.sim_core) — mirrored here so the two sourcing modes can't diverge on
        # drift semantics (the buckets path runs through Family-B perturbations too,
        # e.g. sustained_cost_drift). Applied BEFORE ``spending`` flows into ``_step_buckets``
        # so the buckets actually sell enough to fund the drifted spend. Default
        # spending_real_drift 0.0 ⇒ (1+0)**k == 1.0 ⇒ byte-identical (canary holds).
        spending *= spending_multiplier(scenario, age)

        # Rolling targets for NEXT year (the refill brings the safe buckets up to
        # where they need to be entering a+1). At end_age these clip to 0/empty.
        cash_tgt_next = cash_target(scenario, age + 1) if age < scenario.end_age else 0.0
        income_tgt_next = (
            income_target(scenario, age + 1) if age < scenario.end_age else 0.0
        )

        already_depleted = depleted_age is not None
        (cash, income, growth), sold_from_growth, depleted_this_year, realized_spend = (
            _step_buckets(
                (cash, income, growth),
                (r_cash, r_inc, r_gro),
                gi,
                spending,
                cash_tgt_next,
                income_tgt_next,
            )
        )
        # Absorbing depletion: once the portfolio has depleted, all
        # three buckets stay at 0 for every later year. ``_step_buckets`` is
        # memoryless — post-depletion, GI lands on the (empty) cash bucket and any
        # leftover above the year's spend would survive the floor (and the
        # release-excess refill leg would even move it INTO growth), resurrecting
        # the portfolio. Mirrors the same loop-level override in
        # ``sim_core._simulate_cohort`` (the single-balance analogue) so the two
        # sourcing modes can't drift. ``realized_spend`` is recorded BEFORE this
        # override and already caps at the GI-funded cascade level (cash fund cap
        # ``gi/(1 + r_cash/2)`` once the buckets are empty); ``depleted_age``
        # (first crossing) is handled below off ``depleted_this_year`` and never
        # moves. Byte-identical for any path that never recovers post-depletion
        # (all three buckets are already 0.0 then).
        if already_depleted:
            cash = income = growth = 0.0

        forced = sold_from_growth > 0.0
        if forced and first_forced_sale_age is None:
            first_forced_sale_age = age
        # Depletion flag mirrors step_year: the year the PRE-floor summed balance
        # went strictly < 0 (the cascade overdrew). _step_buckets computes it before
        # the final floor, so a year that lands EXACTLY on 0.0 is NOT flagged — the
        # Gate-B single-bucket identity holds structurally at the zero boundary, not
        # just empirically for the current cohorts (the old post-floor
        # ``cash <= 0 and income <= 0 and growth <= 0`` test flagged an exact-zero
        # landing that step_year would not).
        if depleted_this_year and depleted_age is None:
            depleted_age = age

        years.append(
            BucketYear(
                age=age,
                cash=cash,
                income=income,
                growth=growth,
                spending=realized_spend,
                guaranteed_income=gi,
                sourced_growth=sold_from_growth,
                forced_growth_sale=forced,
            )
        )

    final_portfolio = cash + income + growth
    return BucketTrajectory(
        years=years,
        depleted_age=depleted_age,
        final_portfolio=final_portfolio,
        first_forced_sale_age=first_forced_sale_age,
    )


def _bucket_geometric_returns(scenario: Scenario) -> dict[str, float]:
    """Each bucket's constant Deterministic real return = its allocation blend.

    Deterministic mode compounds one constant return per bucket — the
    rebalanced compound return of that bucket's allocation (the same
    ``rebalanced_compound_return`` the single-balance engine uses). Returned as
    ``{bucket_id: r}``.
    """
    from firebridgelab.returns.sampling import rebalanced_compound_return

    bc = scenario.buckets
    out: dict[str, float] = {}
    for bucket in (bc.cash, bc.income, bc.growth):
        out[bucket.id] = rebalanced_compound_return(
            replace(scenario, allocation=bucket.allocation, correlations=None)
        )
    return out


def simulate_buckets_deterministic(scenario: Scenario) -> BucketTrajectory:
    """Deterministic 3-bucket projection — constant per-bucket blended returns.

    Builds a constant-per-bucket return array (each bucket's geometric blend held
    flat across the horizon) and runs the shared ``simulate_buckets_cohort``. In
    Deterministic mode every year is an UP year (no down years exist), so the
    one-sided "ride" branch never fires and the buffer is never tested — that is
    expected (bucket-strategy design: surface a caption pointing the user to
    Historical / MC). The single-bucket degenerate config reproduces the
    deterministic single-balance terminal byte-for-byte (Gate B).
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    rs = _bucket_geometric_returns(scenario)
    bucket_returns = {
        bid: np.full(n_steps, r, dtype=float) for bid, r in rs.items()
    }
    return simulate_buckets_cohort(scenario, bucket_returns)


def _bucket_equity_weight(bucket) -> float:
    """The fraction of one bucket's allocation that is EQUITY (per EQUITY_CLASS_IDS).

    The weight-weighted sum of the equity classes within the bucket's allocation —
    e.g. a 100% us_total_market growth bucket is 1.0, a 100% st_treasury cash bucket
    is 0.0, and a bucket split 70% us_total_market / 30% us_agg_bonds is 0.7. Pure /
    side-effect-free; reads the SAME class ids the allocation matrix uses.
    """
    return sum(ac.weight for ac in bucket.allocation if ac.id in EQUITY_CLASS_IDS)


def effective_equity_glidepath(scenario: Scenario) -> list[dict]:
    """The per-age effective-equity share along the DETERMINISTIC buckets run.

    SURFACES the allocation the bucket setup silently imposes (bucket-strategy
    "effective-equity glidepath"). Gap-based sizing puts most of a high-spend
    portfolio in the low-equity safe buckets and the rest in growth, so the
    effective equity share is far below the nominal growth-bucket allocation early
    and RISES toward the growth-bucket weight as guaranteed income lands (the safe
    targets shrink → the growth share grows). This makes that glide explicit.

    The share at age ``a`` is computed off the BEGIN-of-year (entering-``a``)
    bucket balances — the allocation the portfolio carries INTO that year — not the
    end-of-year balances, so the ``start_age`` share is exactly the initial
    allocation (e.g. growth / portfolio for an all-equity growth bucket). For
    ``start_age`` the begin balances are ``_initial_balances``; for every later age
    they are the prior year's end-of-year balances (the deterministic
    ``BucketTrajectory`` records those). The deterministic run is the natural
    age-anchored reference: returns-independent enough to read as "what allocation
    does this setup target by age".

    ``effective_equity_share(a) = Σ_buckets[ balance_a(bucket) × equity_weight(bucket) ]
    / total_a``, where ``equity_weight(bucket)`` sums the weights of the bucket's
    EQUITY classes (EQUITY_CLASS_IDS). When the portfolio has run to zero
    (``total_a == 0``) the share is reported as 0.0 (no equity because nothing is
    left). Returns a list of ``{"age", "equity_share"}`` for every age in the
    horizon (start_age..end_age inclusive); buckets-only (callers gate on
    ``sourcing_mode == "buckets"``). Reuses ``simulate_buckets_deterministic`` — it
    does NOT re-derive the per-year mechanic.
    """
    bc = scenario.buckets
    eq_w = {
        "cash": _bucket_equity_weight(bc.cash),
        "income": _bucket_equity_weight(bc.income),
        "growth": _bucket_equity_weight(bc.growth),
    }

    traj = simulate_buckets_deterministic(scenario)
    # Begin-of-year balances entering each age: start_age uses the initial
    # balances; every later age inherits the prior year's end-of-year balances.
    begins: list[tuple[int, float, float, float]] = []
    cash, income, growth = _initial_balances(scenario)
    for y in traj.years:
        begins.append((y.age, cash, income, growth))
        cash, income, growth = y.cash, y.income, y.growth

    out: list[dict] = []
    for age, c, i, g in begins:
        total = c + i + g
        if total > 0.0:
            equity = c * eq_w["cash"] + i * eq_w["income"] + g * eq_w["growth"]
            share = equity / total
        else:
            share = 0.0
        out.append({"age": age, "equity_share": share})
    return out


def gap_for_age(scenario: Scenario, age: int) -> float:
    """The forward GAP at ``age`` = max(0, spending − guaranteed income).

    bucket-strategy design "The forward gap": ``gap(k) = max(0,
    spending_for_age(scenario, k) − GI(k))``. The safe buckets are sized in years
    of THIS gap (not gross spending), so as GI lands the gap — and the safe
    targets — shrink. Uses the SAME ``spending_for_age`` the engines use for
    constant / lifecycle and the SAME GI sum (``gi_at_age``), so the bucket targets
    can't drift from the engine's spending / GI. (Buckets compose with constant /
    lifecycle only; guardrails is fenced — see the API.)
    """
    return max(0.0, spending_for_age(scenario, age) - gi_at_age(scenario, age))


def cash_target(scenario: Scenario, age: int) -> float:
    """Rolling cash-bucket target at ``age`` = sum of the next N1 years of gap.

    bucket-strategy design: ``cash_target(a) = Σ_{k=a}^{min(a+N1−1, end)} gap(k)``.
    The sum runs from ``age`` through ``age + N1 − 1``, clipped to ``end_age``
    (inclusive). Recomputed each year, so the target shrinks as GI lands. N1 = 0
    yields a 0 target (the empty range). Requires a bucket config (callers only
    invoke this in buckets mode).
    """
    n1 = scenario.buckets.n1
    last = min(age + n1 - 1, scenario.end_age)
    return sum(gap_for_age(scenario, k) for k in range(age, last + 1))


def income_target(scenario: Scenario, age: int) -> float:
    """Rolling income-bucket target at ``age`` = the N2 years of gap after the cash
    window.

    bucket-strategy design:
    ``income_target(a) = Σ_{k=a+N1}^{min(a+N1+N2−1, end)} gap(k)``. The sum runs
    from ``age + N1`` through ``age + N1 + N2 − 1``, clipped to ``end_age``
    (inclusive). Recomputed each year. If the cash window already reaches past
    ``end_age`` the income range is empty (target 0). Requires a bucket config.
    """
    n1 = scenario.buckets.n1
    n2 = scenario.buckets.n2
    first = age + n1
    last = min(age + n1 + n2 - 1, scenario.end_age)
    if first > last:
        return 0.0
    return sum(gap_for_age(scenario, k) for k in range(first, last + 1))
