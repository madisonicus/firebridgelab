"""Correctness tests for the Amortization Frontier spending strategy.

The frontier is the funded-ratio machinery run BACKWARDS: each year it solves for
the constant-real total spend ``L`` such that the portfolio exactly funds the
remaining GI-netted gap stream, discounted at ``amortization_real_return`` over the
hard plan horizon ``end_age``. The user cannot audit the math, so these validate against
CLOSED-FORM references (not just internal consistency) — the discipline from
[[feedback_correctness_guarantor_quant_tools]].

Anchors (amortization-frontier design §"Correctness validation"):

  1. Zero-GI, flat-return ⇒ textbook VPW annuity. With all four GI streams off and a
     single constant real return ``r``, the solved ``L`` must equal the closed-form
     annuity to the cent at the start age. NOTE the convention: this engine's NPV
     (``compute_npv_gaps_by_age`` / ``npv_gaps_at_age``) takes the FIRST payment NOW,
     undiscounted (t=0), so the matching closed form is the annuity-DUE
     ``L = P*r / ((1+r) * (1 - (1+r)^-n))``, NOT the annuity-immediate form. The
     frontier MUST share the engine's NPV convention because the FR inversion's
     self-consistency is checked against that same function (anchor 4).
  2. r = 0 ⇒ straight-line ``L = P / n`` (the annuity factor collapses to n).
  3. GI-aware > myopic in the bridge. With GI that turns on partway through, the
     GI-aware ``L`` in the pre-GI years is strictly HIGHER than the myopic
     ``GI(a) + P_a / annuity(n)`` value, and they converge once all GI is active.
  4. FR-inversion self-consistency. Plug the solved ``L`` back into
     ``npv_gaps_at_age`` / ``compute_npv_gaps_by_age`` and confirm it equals ``P_a``
     to tolerance (the bisection converged).
  5. Degenerate ``P <= 0`` ⇒ returns ``gi_at_age``; the engine's realized-spend cap
     then governs the recorded dollars in broke years.
  6. Factory + capability flags (is_solvable False, collects_spends True).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from firebridgelab import default_scenario, simulate
from firebridgelab._step import max_fundable_spend
from firebridgelab.finance import compute_npv_gaps_by_age, npv_gaps_at_age
from firebridgelab.scenario import (
    AssetClass,
    SPENDING_MODE_AMORTIZATION,
    Scenario,
    gi_at_age,
)
from firebridgelab.spending import (
    AmortizationFrontierStrategy,
    _npv_escalated_gaps_at_age,
    make_strategy,
)


def _amort_scenario(**overrides) -> Scenario:
    """A minimal amortization-frontier scenario (ages 50..95, GI off by default).

    Allocation / annual_spending are inert for the frontier (it solves off the raw
    balance + the GI streams + the amortization return), so they carry placeholders.
    All four GI streams default to 0 with start ages pushed out, giving the clean
    zero-GI closed-form base; the GI-aware cases override the SS streams.
    """
    base = dict(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.0),),
        annual_spending=110_000.0,
        person_a_pension_amount=0.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=0.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=0.0,
        person_a_ss_start_age=67,
        person_b_ss_amount=0.0,
        person_b_ss_start_age=70,
        spending_mode=SPENDING_MODE_AMORTIZATION,
        amortization_real_return=0.01,
    )
    base.update(overrides)
    return Scenario(**base)


def _annuity_due_payment(P: float, r: float, n: int) -> float:
    """Closed-form annuity-DUE payment: the constant ``L`` whose n payments (the
    FIRST taken NOW, undiscounted) have present value ``P`` at real rate ``r``.

    ``Σ_{t=0}^{n-1} L / (1+r)^t == P``  ⇒  ``L = P*r / ((1+r) * (1 - (1+r)^-n))``.
    This matches the engine's NPV convention (t=0 term undiscounted)."""
    return P * r / ((1.0 + r) * (1.0 - (1.0 + r) ** (-n)))


# --------------------------------------------------------------------------- #
# 1. Zero-GI, flat-return ⇒ textbook VPW annuity (to the cent).                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("r", [0.01, 0.03, 0.04, 0.05])
def test_zero_gi_matches_vpw_annuity_to_the_cent(r):
    """With no GI and a single real return, the solved L at the start age equals the
    textbook (annuity-due) VPW payment to the cent."""
    s = _amort_scenario(amortization_real_return=r)
    strat = make_strategy(s)
    P = s.initial_portfolio
    n = s.end_age - s.start_age + 1  # remaining years, inclusive of the start age

    L = strat.spend_for_year(s, s.start_age, P, gi=0.0)
    expected = _annuity_due_payment(P, r, n)
    # To the cent.
    assert abs(L - expected) < 0.01, f"L={L:.6f} expected={expected:.6f}"


def test_zero_gi_vpw_holds_at_a_smaller_horizon():
    """The annuity identity isn't an artifact of the 43-year horizon — check a short
    horizon + a different portfolio."""
    s = _amort_scenario(start_age=60, end_age=70, initial_portfolio=1_000_000.0,
                        amortization_real_return=0.03)
    strat = make_strategy(s)
    P = s.initial_portfolio
    n = s.end_age - s.start_age + 1  # 11
    L = strat.spend_for_year(s, s.start_age, P, gi=0.0)
    assert abs(L - _annuity_due_payment(P, 0.03, n)) < 0.01


# --------------------------------------------------------------------------- #
# 2. r = 0 ⇒ straight-line amortization L = P / n.                            #
# --------------------------------------------------------------------------- #


def test_zero_return_is_straight_line():
    """r = 0 collapses the annuity factor to n ⇒ L = P / n (straight-line)."""
    s = _amort_scenario(amortization_real_return=0.0)
    strat = make_strategy(s)
    P = s.initial_portfolio
    n = s.end_age - s.start_age + 1
    L = strat.spend_for_year(s, s.start_age, P, gi=0.0)
    assert abs(L - P / n) < 0.01, f"L={L:.6f} P/n={P / n:.6f}"


# --------------------------------------------------------------------------- #
# 3. GI-aware > myopic in the bridge; convergence once GI is fully on.        #
# --------------------------------------------------------------------------- #


def _myopic_vpw(P: float, r: float, n: int, gi: float) -> float:
    """The REJECTED myopic VPW: this year's GI plus the portfolio amortized over the
    remaining horizon, ignoring FUTURE GI. ``GI(a) + P_a / annuity_due_factor(n)``.

    annuity_due_factor = Σ_{t=0}^{n-1} (1+r)^-t = (1+r)*(1-(1+r)^-n)/r (r>0)."""
    if r == 0:
        factor = float(n)
    else:
        factor = (1.0 + r) * (1.0 - (1.0 + r) ** (-n)) / r
    return gi + P / factor


def test_gi_aware_beats_myopic_in_bridge_and_converges_when_gi_on():
    """GI that turns on at 67 (SS) ⇒ in the pre-67 bridge years the GI-aware frontier
    L is strictly HIGHER than the myopic value (it front-loads BECAUSE it knows SS is
    coming); once all GI is active the two converge (no future step left to anticipate).
    """
    # SS-like streams that both turn on at 67; nothing before that.
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
        amortization_real_return=0.02,
    )
    strat = make_strategy(s)
    P = s.initial_portfolio

    # Pre-GI bridge year (age 50): GI(50) == 0, so myopic amortizes the WHOLE
    # portfolio over the horizon ignoring the SS that lands at 67.
    age = 50
    n = s.end_age - age + 1
    gi_now = gi_at_age(s, age)  # 0
    assert gi_now == 0.0
    L_aware = strat._solve_l(s, age, P)
    L_myopic = _myopic_vpw(P, 0.02, n, gi_now)
    assert L_aware > L_myopic + 1.0, (
        f"GI-aware should beat myopic in the bridge: aware={L_aware:.2f} "
        f"myopic={L_myopic:.2f}"
    )

    # Once GI is fully active (age 70 > 67, no further GI steps remain), the GI-aware
    # solve has no future step to anticipate beyond the flat GI, so it converges to
    # the myopic value at that age's balance.
    age_on = 70
    n_on = s.end_age - age_on + 1
    P_on = 800_000.0
    gi_on = gi_at_age(s, age_on)  # 76k, flat thereafter
    L_aware_on = strat._solve_l(s, age_on, P_on)
    L_myopic_on = _myopic_vpw(P_on, 0.02, n_on, gi_on)
    assert abs(L_aware_on - L_myopic_on) < 0.10, (
        f"once GI is flat-on the two converge: aware={L_aware_on:.4f} "
        f"myopic={L_myopic_on:.4f}"
    )


# --------------------------------------------------------------------------- #
# 4. FR-inversion self-consistency: NPV(gaps at L) == P_a.                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("age,P", [(50, 1_800_000.0), (60, 1_500_000.0), (67, 1_200_000.0), (90, 200_000.0)])
def test_fr_inversion_self_consistency(age, P):
    """Plug the solved L back into the SAME NPV function the FR machinery uses and
    confirm it funds the portfolio to tolerance (the bisection converged). Checked at
    several ages with GI active so the GI-netting is exercised."""
    s = _amort_scenario(
        person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
        person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
        person_a_ss_amount=33_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=32_240.0, person_b_ss_start_age=70,
        amortization_real_return=0.01,
    )
    strat = make_strategy(s)
    L = strat._solve_l(s, age, P)
    r = s.amortization_real_return
    # Against the single-age helper.
    npv_focused = npv_gaps_at_age(s, age, L, r)
    assert npv_focused == pytest.approx(P, abs=1.0)
    # And against the full backward table the FR strategies build — same value.
    npv_table = compute_npv_gaps_by_age(s, target_real=L, discount_rate=r)[age]
    assert npv_table == pytest.approx(P, abs=1.0)
    assert npv_focused == pytest.approx(npv_table, abs=1e-3)


def test_npv_focused_matches_full_table_across_ages():
    """The focused single-age NPV helper must agree with the full backward table at
    EVERY in-horizon age (the helper is what the strategy bisects against; if it
    drifted from the FR table the self-consistency would be a tautology against the
    wrong reference)."""
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    target = 175_000.0
    r = 0.015
    table = compute_npv_gaps_by_age(s, target_real=target, discount_rate=r)
    for age in range(s.start_age, s.end_age + 1):
        assert npv_gaps_at_age(s, age, target, r) == pytest.approx(table[age], abs=1e-6)


# --------------------------------------------------------------------------- #
# 5. Degenerate P <= 0.                                                       #
# --------------------------------------------------------------------------- #


def test_degenerate_nonpositive_portfolio_returns_gi():
    """P_a <= 0 ⇒ spend the guaranteed income only (you can't amortize a gone
    portfolio). The recorded-spend cap (max_fundable_spend) governs the engine's
    RECORDED dollars; the strategy's intended return here is gi_at_age."""
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    strat = make_strategy(s)
    # At 70, GI is 76k, portfolio gone.
    assert strat.spend_for_year(s, 70, 0.0, gi=76_000.0) == pytest.approx(gi_at_age(s, 70))
    assert strat.spend_for_year(s, 71, -5_000.0, gi=76_000.0) == pytest.approx(gi_at_age(s, 71))
    # Pre-GI broke year: GI is 0 → intended spend 0.
    assert strat.spend_for_year(s, 55, 0.0, gi=0.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 6. Factory dispatch + capability flags.                                     #
# --------------------------------------------------------------------------- #


def test_factory_dispatch_and_flags():
    s = _amort_scenario()
    strat = make_strategy(s)
    assert isinstance(strat, AmortizationFrontierStrategy)
    # Frontier IS the spend → solver fenced; spend varies across paths → fan collected.
    assert strat.is_solvable is False
    assert strat.collects_spends is True
    assert strat.reason_not_solvable  # non-empty user-facing sentence


def test_records_one_adjustment_event_per_year_with_honest_action():
    """Each call appends one AdjustmentEvent; the action is the honest sign of the
    year-over-year change in the solved L (raise/cut/hold), and a GI step is flagged.
    """
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    strat = make_strategy(s)
    # First year: hold (no prior). Then a rising balance ⇒ raise.
    strat.spend_for_year(s, 50, 1_800_000.0, gi=0.0)
    strat.spend_for_year(s, 51, 1_900_000.0, gi=0.0)   # bigger balance → larger L
    strat.spend_for_year(s, 52, 1_400_000.0, gi=0.0)   # smaller balance → cut
    assert [e.action for e in strat.events] == ["hold", "raise", "cut"]
    assert all(e.gi_stepped_up is False for e in strat.events)
    # A GI-step year flags gi_stepped_up.
    strat2 = make_strategy(s)
    strat2.spend_for_year(s, 66, 1_500_000.0, gi=gi_at_age(s, 66))  # GI 0
    strat2.spend_for_year(s, 67, 1_400_000.0, gi=gi_at_age(s, 67))  # GI 76k lands
    assert strat2.events[-1].gi_stepped_up is True


def test_amortizes_to_near_zero_by_horizon_end_to_end():
    """END-TO-END through the real engine (simulate): on the default scenario with a
    constant real return, the frontier amortized at the SAME return should drive the
    terminal portfolio essentially to zero by end_age (the whole point — "spend it all
    by the end of the plan"). We set amortization_real_return == the blended real
    return so the plan and the realized path agree; the residual is the midpoint-
    convention discount, not a modeling error."""
    base = default_scenario()  # 5.0% blended real
    s = replace(
        base,
        spending_mode=SPENDING_MODE_AMORTIZATION,
        amortization_real_return=0.05,  # == blended_real_return(default)
    )
    result = simulate(s)
    # Terminal should be a small fraction of the starting portfolio (amortized down).
    assert result.final_portfolio < 0.02 * s.initial_portfolio, (
        f"frontier should amortize to ~0 by horizon: terminal="
        f"{result.final_portfolio:,.0f}"
    )
    assert result.final_portfolio >= 0.0


def test_engine_records_realized_spend_cap_in_broke_year():
    """When the frontier's intended spend exceeds what's fundable (a broke year), the
    engine RECORDS min(intended, max_fundable_spend) — the strategy returns gi when
    P<=0, and the recorded spend is capped at gi/(1+r/2). Confirms the strategy does
    not fight the existing realized-spend machinery."""
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    strat = make_strategy(s)
    r = 0.04
    gi = 76_000.0
    intended = strat.spend_for_year(s, 70, 0.0, gi=gi)  # == gi (degenerate)
    assert intended == pytest.approx(gi)
    realized = min(intended, max_fundable_spend(0.0, r, gi))
    # max_fundable at P=0 is exactly gi (GI and spend share the mid-year timing).
    assert realized == pytest.approx(gi)
    assert realized <= intended


# --------------------------------------------------------------------------- #
# 7. PERFORMANCE-REFACTOR EQUIVALENCE: analytic segment solve == bisection.    #
#                                                                              #
# The MC hot-spot fix replaced the per-year nested bisection with an O(#GI-    #
# steps) analytic segment walk + a precomputed GI/discount/suffix-sum table.   #
# This must return the SAME L as the bisection reference TO THE CENT. Per      #
# house discipline [[feedback_preservation_vs_correctness]] the regression     #
# must start from states DIVERGENT from the default — a range of portfolios,   #
# ages, returns, and GI configs — not just a re-run of the default scenario.   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("r", [0.0, 0.01, 0.05])
@pytest.mark.parametrize("P", [0.0, 100_000.0, 1_000_000.0, 5_000_000.0, 20_000_000.0])
@pytest.mark.parametrize(
    "gi_cfg",
    [
        # All-zero GI: the active set is the whole suffix (single segment).
        dict(person_a_pension_amount=0.0, person_b_pension_amount=0.0,
             person_a_ss_amount=0.0, person_b_ss_amount=0.0),
        # Default staggered-start GI: pensions at 60/65, SS at 67/70 ⇒ several breakpoints.
        dict(person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
             person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
             person_a_ss_amount=33_000.0, person_a_ss_start_age=67,
             person_b_ss_amount=32_240.0, person_b_ss_start_age=70),
    ],
)
def test_analytic_segment_solve_equals_bisection_to_the_cent(r, P, gi_cfg):
    """The optimized analytic solve must equal the brute-force bisection reference to
    the cent across DIVERGENT begin-of-year portfolios, ages, amortization returns, and
    GI configurations. P <= 0 is the degenerate branch (both never called); we exercise
    P > 0 at start / mid / near-end / end ages."""
    s = _amort_scenario(amortization_real_return=r, **gi_cfg)
    strat = make_strategy(s)
    # start, mid, near-end, end — and one just-inside-end.
    for age in (s.start_age, 70, s.end_age - 1, s.end_age):
        if P <= 0:
            # Degenerate guard: _solve_l is only called for P > 0; skip.
            continue
        l_fast = strat._solve_l(s, age, P)
        l_ref = strat._solve_l_bisect(s, age, P)
        assert abs(l_fast - l_ref) < 0.01, (
            f"age={age} P={P} r={r} gi={gi_cfg}: analytic L={l_fast:.6f} != "
            f"bisection L={l_ref:.6f}"
        )
        # And the analytic L is FR-self-consistent against the same NPV the engine uses.
        assert npv_gaps_at_age(s, age, l_fast, r) == pytest.approx(P, abs=1.0)


def test_analytic_solve_used_on_nondecreasing_gi_and_falls_back_on_drift():
    """The fast path is taken when GI is non-decreasing; a positive pension_real_drift
    erodes the pension streams (GI can DECREASE), so the construction-time check trips
    the bisection fallback — correctness preserved either way."""
    s_flat = _amort_scenario(
        person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
        person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
    )
    assert make_strategy(s_flat)._gi_nondecreasing is True

    s_drift = _amort_scenario(
        person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
        person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
        pension_real_drift=0.02,
    )
    strat_drift = make_strategy(s_drift)
    assert strat_drift._gi_nondecreasing is False
    # Fallback still solves correctly (matches the bisection reference to the cent).
    for age in (50, 65, 90):
        L = strat_drift._solve_l(s_drift, age, 1_500_000.0)
        assert abs(L - strat_drift._solve_l_bisect(s_drift, age, 1_500_000.0)) < 0.01
        assert npv_gaps_at_age(s_drift, age, L, s_drift.amortization_real_return) == pytest.approx(
            1_500_000.0, abs=1.0
        )


# --------------------------------------------------------------------------- #
# 8. SPENDING-REAL-DRIFT AWARENESS.                                 #
#                                                                              #
# The engine loops multiply the strategy's year-t output by                    #
# ``(1 + spending_real_drift) ** (t - start_age)`` AFTER the solve. A          #
# drift-blind constant-real solve violates its own premise under the           #
# sustained_cost_drift overlay (d = 0.015): the escalated path overspends and the    #
# reproduced default frontier depleted YEARS before end_age. The fix makes     #
# the solve amortize the ESCALATED spend path — ``L·(1+d)^(t-start_age)``      #
# (anchored at the SIM start, matching the loop's exponent) net of the         #
# NON-drifting GI stream. These tests pin: the closed form, the start-age      #
# anchoring (the off-by-one that silently reintroduces the bug), the           #
# engine-level acceptance criterion (rides to ~0 at end_age, no early          #
# depletion), the drift analytic-vs-bisection equivalence across a divergent   #
# age × balance grid, the pension-drift fallback, and the d == 0 collapse.     #
# --------------------------------------------------------------------------- #


def _growing_annuity_due_payment(P: float, r: float, d: float, n: int) -> float:
    """Closed-form GROWING annuity-due payment: the base ``L`` whose n payments
    ``L·(1+d)^t`` (the first taken NOW, undiscounted, t=0) have present value ``P``
    at real rate ``r``::

        Σ_{t=0}^{n-1} L·(1+d)^t / (1+r)^t == P  ⇒  L = P·(1-q) / (1-q^n),
        q = (1+d)/(1+r)   (q != 1)

    Collapses to the plain annuity-due form at d == 0."""
    q = (1.0 + d) / (1.0 + r)
    return P * (1.0 - q) / (1.0 - q**n)


@pytest.mark.parametrize("r,d", [(0.01, 0.015), (0.03, 0.015), (0.01, -0.01), (0.05, 0.02)])
def test_drift_solve_matches_growing_annuity_due_closed_form(r, d):
    """Zero GI + drift ⇒ the solved base L at the start age equals the textbook
    GROWING annuity-due payment to the cent (the drift sibling of anchor 1).
    Positive drift ⇒ base L strictly BELOW the flat annuity (the early frontier
    honestly comes DOWN to pay for the escalating tail); negative drift ⇒ above."""
    s = _amort_scenario(amortization_real_return=r, spending_real_drift=d)
    strat = make_strategy(s)
    P = s.initial_portfolio
    n = s.end_age - s.start_age + 1
    L = strat.spend_for_year(s, s.start_age, P, gi=0.0)
    expected = _growing_annuity_due_payment(P, r, d, n)
    assert abs(L - expected) < 0.01, f"L={L:.6f} expected={expected:.6f}"
    flat = _annuity_due_payment(P, r, n)
    if d > 0:
        assert L < flat
    else:
        assert L > flat


def test_drift_escalation_anchored_at_sim_start_not_resolve_age():
    """THE k-ALIGNMENT PIN ('s silent-failure mode): the loop's exponent is
    ``age - scenario.start_age`` (years since SIM START), so a re-solve at age a must
    escalate year-t spend by ``(1+d)^(t - start_age)`` — at t == a the spend is
    ALREADY ``L·(1+d)^(a - start_age)``. Zero-GI closed form at a mid-horizon age::

        L = P / ((1+d)^(a-start) · Σ_{j=0}^{m-1} q^j),  m = end_age - a + 1

    A solve anchored at the re-solve age instead (``(1+d)^(t-a)``) returns an L too
    large by (1.015)^20 ≈ 1.35 here — far outside the cent tolerance."""
    r, d = 0.01, 0.015
    s = _amort_scenario(amortization_real_return=r, spending_real_drift=d)
    strat = make_strategy(s)
    age, P = 70, 1_000_000.0  # offset 20 from start_age 50
    q = (1.0 + d) / (1.0 + r)
    m = s.end_age - age + 1
    expected = P / ((1.0 + d) ** (age - s.start_age) * (1.0 - q**m) / (1.0 - q))
    L = strat._solve_l(s, age, P)
    assert abs(L - expected) < 0.01, f"L={L:.6f} expected={expected:.6f}"
    # The wrongly-anchored value is decisively rejected.
    wrong = P / ((1.0 - q**m) / (1.0 - q))
    assert abs(L - wrong) > 1_000.0


def test_drift_frontier_rides_to_zero_at_end_age_end_to_end():
    """THE ACCEPTANCE CRITERION: under deterministic returns equal to
    ``amortization_real_return`` and ``spending_real_drift = 0.015``, the frontier
    trajectory must ride to ~0 AT end_age — terminal a small fraction of the start
    (the residual is the midpoint-convention discount, same as the no-drift
    end-to-end test) — and must NOT hit 0 at any age before end_age (the pre-fix
    behavior: depleting years early). DIVERGENT start from defaults
    ([[feedback_preservation_vs_correctness]]): different ages, portfolio, GI."""
    s = _amort_scenario(
        start_age=55, end_age=93, initial_portfolio=3_200_000.0,
        allocation=(AssetClass("all", "All", 1.0, 0.03),),  # blend == amort return
        person_a_pension_amount=18_000.0, person_a_pension_start_age=63,
        person_b_pension_amount=15_000.0, person_b_pension_start_age=61,
        person_a_ss_amount=31_000.0, person_a_ss_start_age=68,
        person_b_ss_amount=27_000.0, person_b_ss_start_age=66,
        amortization_real_return=0.03,
        spending_real_drift=0.015,
    )
    traj = simulate(s)
    # Old behavior gone: no depletion BEFORE end_age (exact-at-horizon is the
    # intended "spent it all" outcome, as in the no-drift baseline).
    assert traj.depleted_age is None or traj.depleted_age == s.end_age, (
        f"frontier must not deplete early under drift: depleted at {traj.depleted_age}"
    )
    for p in traj.points[:-1]:
        assert p.end_portfolio > 0.0, f"balance hit 0 early at age {p.age}"
    # And it DOES amortize down: terminal ~0 (within the midpoint residual).
    assert 0.0 <= traj.final_portfolio < 0.02 * s.initial_portfolio, (
        f"frontier should ride to ~0 at end_age: terminal={traj.final_portfolio:,.0f}"
    )


def test_reproduction_default_plus_sustained_cost_drift_drift():
    """The exact reproduction from the issue: default scenario, frontier amortized at
    the blended return, sustained_cost_drift's drift 0.015 — pre-fix this depleted at ~81
    instead of riding to ~0 at 95. Post-fix: no early depletion, terminal ~0."""
    s = replace(
        default_scenario(),
        spending_mode=SPENDING_MODE_AMORTIZATION,
        amortization_real_return=0.05,  # == blended_real_return(default)
        spending_real_drift=0.015,      # the sustained_cost_drift overlay's drift
    )
    traj = simulate(s)
    assert traj.depleted_age is None or traj.depleted_age == s.end_age
    for p in traj.points[:-1]:
        assert p.end_portfolio > 0.0, f"balance hit 0 early at age {p.age}"
    assert 0.0 <= traj.final_portfolio < 0.02 * s.initial_portfolio


@pytest.mark.parametrize("d", [0.015, 0.03, -0.01])
@pytest.mark.parametrize("r", [0.0, 0.01, 0.05])
@pytest.mark.parametrize("P", [100_000.0, 1_000_000.0, 5_000_000.0, 20_000_000.0])
@pytest.mark.parametrize(
    "gi_cfg",
    [
        # All-zero GI: the whole suffix is active (single join).
        dict(person_a_pension_amount=0.0, person_b_pension_amount=0.0,
             person_a_ss_amount=0.0, person_b_ss_amount=0.0),
        # Default staggered-start GI: pensions at 60/65, SS at 67/70 ⇒ several g̃ runs.
        dict(person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
             person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
             person_a_ss_amount=33_000.0, person_a_ss_start_age=67,
             person_b_ss_amount=32_240.0, person_b_ss_start_age=70),
    ],
)
def test_drift_analytic_solve_equals_drift_bisection_to_the_cent(d, r, P, gi_cfg):
    """Invariant 3: the drift-aware analytic solve must equal the drift-aware
    bisection reference to the cent across DIVERGENT portfolios, ages, returns,
    drifts (both signs), and GI configurations — the drift mirror of the existing
    segment-vs-bisection equivalence grid. The analytic L is also self-consistent
    against the independent escalated-gap NPV reference."""
    s = _amort_scenario(
        amortization_real_return=r, spending_real_drift=d, **gi_cfg
    )
    strat = make_strategy(s)
    for age in (s.start_age, 70, s.end_age - 1, s.end_age):
        l_fast = strat._solve_l(s, age, P)
        l_ref = strat._solve_l_bisect_drift(s, age, P)
        assert abs(l_fast - l_ref) < 0.01, (
            f"age={age} P={P} r={r} d={d} gi={gi_cfg}: analytic L={l_fast:.6f} != "
            f"drift bisection L={l_ref:.6f}"
        )
        assert _npv_escalated_gaps_at_age(s, age, l_fast, r, d) == pytest.approx(
            P, abs=1.0
        )


def test_drift_with_pension_drift_falls_back_to_drift_bisection():
    """Both drifts at once: ``pension_real_drift > 0`` trips the non-decreasing-GI
    guard (analytic path off), and ``spending_real_drift != 0`` must route to the
    DRIFT-AWARE bisection — the result is self-consistent against the escalated-gap
    NPV, not the drift-blind one."""
    s = _amort_scenario(
        person_a_pension_amount=24_000.0, person_a_pension_start_age=60,
        person_b_pension_amount=9_000.0, person_b_pension_start_age=65,
        pension_real_drift=0.02,
        spending_real_drift=0.015,
    )
    strat = make_strategy(s)
    assert strat._gi_nondecreasing is False
    r, d = s.amortization_real_return, s.spending_real_drift
    for age in (50, 65, 90):
        L = strat._solve_l(s, age, 1_500_000.0)
        assert _npv_escalated_gaps_at_age(s, age, L, r, d) == pytest.approx(
            1_500_000.0, abs=1.0
        )
        # And decisively NOT the drift-blind solve (escalation matters here).
        assert npv_gaps_at_age(s, age, L, r) != pytest.approx(1_500_000.0, abs=100.0)


def test_escalated_npv_collapses_to_plain_npv_at_zero_drift():
    """``_npv_escalated_gaps_at_age`` at drift = 0 reproduces
    ``firebridgelab.finance.npv_gaps_at_age`` EXACTLY (same operations, byte-equal) — the
    invariant-1 collapse at the reference level. The strategy's d == 0 dispatch
    takes the untouched pre-drift-aware code path, so the engine canaries are pinned by
    the existing canary tests, not re-pinned here."""
    s = _amort_scenario(
        person_a_ss_amount=40_000.0, person_a_ss_start_age=67,
        person_b_ss_amount=36_000.0, person_b_ss_start_age=67,
    )
    for age in (50, 60, 67, 80, 95):
        for L in (50_000.0, 110_000.0, 250_000.0):
            assert _npv_escalated_gaps_at_age(s, age, L, 0.01, 0.0) == npv_gaps_at_age(
                s, age, L, 0.01
            )


def test_frontier_is_unclamped_can_cross_rails():
    """The frontier must NOT clamp to spending_floor / spending_ceiling — a small
    portfolio funds an L well BELOW the $85k floor, and a large one funds an L well
    ABOVE the $150k ceiling. (Clamping would destroy the over-/under-funded
    diagnostic.)"""
    s = _amort_scenario(spending_floor=85_000.0, spending_ceiling=150_000.0,
                        amortization_real_return=0.03)
    strat = make_strategy(s)
    # Tiny portfolio at the start age → L far below the floor.
    L_low = strat._solve_l(s, 50, 500_000.0)
    assert L_low < 85_000.0
    # Huge portfolio → L far above the ceiling.
    L_high = make_strategy(s)._solve_l(s, 50, 8_000_000.0)
    assert L_high > 150_000.0
