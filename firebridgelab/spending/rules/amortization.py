"""spending.rules.amortization (split from the flat layout)."""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.spending.base import AdjustmentEvent, SpendingStrategy
from firebridgelab.spending.modes import SPENDING_MODE_AMORTIZATION
from firebridgelab.spending.registry import register


# Bisection bracket + tolerances for the amortization-frontier per-year solve.
# The upper bracket ``hi`` must dominate the true root for any in-horizon age: the
# NPV at target L is at most ``L`` discounted over the remaining horizon, so funding
# a portfolio P_a needs L no larger than roughly ``GI_max + P_a`` (the gap can't
# exceed L, and undiscounted the n-year gap sum >= P_a only once L*n >= P_a, i.e.
# L >= P_a/n). We bracket generously at ``GI_max + P_a + a fixed pad`` and ASSERT the
# root is bracketed (NPV(hi) >= P_a) before bisecting, so a mis-bracket fails loudly
# rather than returning a silently-capped L.
#
# Convergence: the loop runs until the NPV residual is within ``_AMORT_NPV_TOL`` OR
# the L-bracket has collapsed within ``_AMORT_L_TOL``. Both tolerances are deliberately
# SUB-CENT (1e-4) — the closed-form VPW-annuity validation requires the solved L to
# match the textbook annuity to the cent, and an L-bracket of $1 leaves up to ~$0.09
# of error (well over a cent). The cost is trivial: the solve recomputes a single
# forward NPV over a ~40-year window per step, and even sub-cent convergence is ~35
# bisection steps, so per-year cost is a few thousand float ops. Note the closed form
# the zero-GI case must match is the annuity-DUE form ``P*r/((1+r)*(1-(1+r)^-n))`` —
# the FIRST payment is taken NOW (undiscounted, t=0), matching this engine's existing
# ``compute_npv_gaps_by_age`` convention (the t=0 term is undiscounted). That is the
# convention the frontier MUST share, since the FR inversion's self-consistency is
# checked against that same NPV function.
_AMORT_NPV_TOL: float = 1e-4         # NPV within $1e-4 of the portfolio → converged
_AMORT_L_TOL: float = 1e-4           # bracket width within $1e-4 of L → converged
_AMORT_MAX_ITERS: int = 200          # bisection cap (≈ log2 of any sane bracket)
_AMORT_HI_PAD: float = 1_000_000.0   # additive pad on the upper bracket (safety)


def _npv_escalated_gaps_at_age(
    scenario: Scenario,
    age: int,
    l_base: float,
    discount_rate: float,
    drift: float,
) -> float:
    """Drift-aware NPV of the ESCALATED-spend gap stream (reference).

    The engine loops multiply the strategy's year-``t`` output by
    ``(1 + spending_real_drift) ** (t - start_age)`` AFTER the solve, so the spend
    the portfolio actually funds at year ``t`` is ``l_base`` escalated from the
    SIMULATION start age (NOT from the re-solve age — that anchor is the loop's
    exponent ``age - scenario.start_age``). The GI streams do NOT drift::

        NPV(age) = Σ_{t=age..end_age}
                   max(0, l_base·(1+drift)^(t-start_age) - GI(t))
                   / (1 + discount_rate)^(t-age)

    The drift-aware sibling of ``firebridgelab.finance.npv_gaps_at_age`` (which this
    reproduces exactly at ``drift == 0``): an INDEPENDENT brute-force reference
    (per-year ``gi_at_age`` calls, no precomputed tables) for the analytic
    drift-aware solve, used by ``_solve_l_bisect_drift`` and the equivalence
    tests. Monotonically increasing in ``l_base`` ⇒ bisection is valid.

    Pure: no I/O, no mutation of ``scenario``.
    """
    end_age = scenario.end_age
    discount = 1.0 + discount_rate
    growth = 1.0 + drift
    # Escalation anchored at the SIM start age: at t == age the spend is already
    # l_base * (1+drift)^(age - start_age) — exactly what the engine loop pays.
    esc = growth ** (age - scenario.start_age)
    npv = 0.0
    factor = 1.0  # 1 / (1 + r)^(t - age), accumulated forward
    for a in range(age, end_age + 1):
        gap = max(l_base * esc - gi_at_age(scenario, a), 0.0)
        npv += gap * factor
        factor /= discount
        esc *= growth
    return npv


@register(SPENDING_MODE_AMORTIZATION)
class AmortizationFrontierStrategy(SpendingStrategy):
    """The AMORTIZATION FRONTIER — the lone DEPLETE-AWARE spending strategy
    (amortization-frontier design). The funded-ratio machinery run BACKWARDS.

    Each year, given the begin-of-year portfolio ``P_a``, solve for the constant-real
    total spend ``L`` such that the portfolio EXACTLY funds the remaining GI-netted
    gap stream, discounted at ``scenario.amortization_real_return`` (``r``) over the
    hard plan horizon ``end_age``::

        Σ_{t=a}^{end_age} max(0, L - GI(t)) / (1 + r)^(t-a)  ==  P_a

    i.e. the target ``L`` at which the funded ratio == 1. Spend ``L`` this year; next
    year the engine calls again with the new balance and ``L`` is re-solved off it.
    The LHS is exactly ``npv_gaps_at_age(scenario, a, target_real=L, discount_rate=r)``
    and is monotonically INCREASING in ``L``, so we BISECT on ``L`` in ``[0, hi]``.

    DRIFT-AWARE: when ``scenario.spending_real_drift != 0`` (the
    sustained_cost_drift overlay) the engine loops escalate this strategy's output by
    ``(1+d)^(age - start_age)`` AFTER the solve, so the constant-real premise above
    would overstate the early frontier and deplete years before ``end_age``. The
    solve then amortizes the ESCALATED spend path instead — ``L·(1+d)^(t-start_age)``
    (anchored at the simulation start, matching the loop's exponent) net of the
    NON-drifting GI stream — keeping the benchmark self-consistent under drift.
    ``d == 0`` takes the exact pre-drift-aware code path (canaries byte-identical).

    GI-aware consumption smoothing (design decision 2): because future GI is netted
    INSIDE the solve, ``L`` front-loads consumption into the pre-SS bridge years (the
    portfolio is drawn down harder early BECAUSE it knows SS is coming, and a GI step
    raises ``L`` for free — the gap shrinks when a stream turns on). This is NOT myopic
    VPW (``GI(a) + P_a / annuity_factor``), which would starve the bridge / go-go years
    — the rejected alternative.

    DESIGN — subclasses ``SpendingStrategy`` directly, NOT ``FundedRatioStrategyBase``
    (design decision 5 / the implementation choice): the FR spine exists to support the
    six SUSTAIN-AND-PRESERVE variants — it builds ONE fixed-target NPV table at
    construction, maintains a 3-year SMOOTHED portfolio + crash/hysteresis carried
    state, and every concrete variant applies the activity TAPER and the
    floor/ceiling CLAMP. The frontier deliberately rejects ALL of that:

      - it RE-SOLVES for a NEW target ``L`` each year (the FR base bakes a single
        target once), so the prebuilt table doesn't fit;
      - it amortizes off the RAW begin-of-year balance, NOT the 3-year smoothed value
        (smoothing is an FR damping choice; the frontier amortizes off the honest
        actual balance);
      - it is UNCLAMPED — no ``spending_floor`` / ``spending_ceiling`` clamp and no
        activity taper. The rails are CHART REFERENCE LINES only (web side), never
        applied in the engine. Clamping would destroy the over-/under-funded
        diagnostic the frontier exists for, so the FR floor/ceiling guard does NOT
        generalize here — the role differs (a deplete-aware ceiling, not a
        sustain-and-preserve anchor). Inheriting the base would mean overriding or
        bypassing more than it reuses, so a direct subclass with its own per-year
        solve is the honest, lower-coupling design.

    Like the FR strategies it carries an ``AdjustmentEvent`` log + tracks GI steps, so
    it rides the EXISTING ``collects_spends`` fan / drill-down path with no engine-side
    changes (Deterministic → one recomputed line; Historical / MC → a fan per
    cohort/trial). The ``action`` label is the honest sign of the year-over-year change
    in the solved ``L`` (raise / cut / hold) — a real per-year fact, not a fabricated
    band trip.

    Degenerate handling: ``P_a <= 0`` → return ``gi_at_age(scenario, a)`` (you can only
    spend your guaranteed income once the portfolio is gone). The engine's realized-
    spend machinery (``_step.max_fundable_spend``, applied by every engine loop) then
    clamps the RECORDED spend to what's actually fundable in broke years — for
    ``P_a == 0`` that caps recorded spend at ``gi`` — so the recorded fan
    stays honest without this strategy fighting it.

    ``is_solvable = False``: the frontier IS the spend — there is no external target for
    the global max-spend solver to bisect (the per-year solve-for-``L`` is internal).
    ``collects_spends = True``: ``L`` varies across cohorts/trials, so the engines
    collect the per-year spend fan exactly like the FR family.
    """

    is_solvable: bool = False
    reason_not_solvable: str = (
        "max-spend solve is not available in amortization-frontier mode — the "
        "frontier IS the spend (view-only)"
    )
    collects_spends: bool = True

    def __init__(self, scenario: Scenario) -> None:
        # The planning real return the gap stream is discounted at (design decision 4).
        self.r = float(scenario.amortization_real_return)
        # Adjustment-event log + prior-state tracking (same shape/discipline as the
        # FR strategies). Purely descriptive — recording never alters the returned
        # spend. ``_prev_spend`` drives the honest raise/cut/hold action label;
        # ``_prev_gi`` flags a GI-step year (the solved L rises when a stream lands
        # because the netted gap stream shrinks).
        self.events: list[AdjustmentEvent] = []
        self._prev_spend: float | None = None
        self._prev_gi: float | None = None

        # ----------------------------------------------------------------- #
        # PERFORMANCE PRECOMPUTE (exact, results-preserving).               #
        #                                                                   #
        # GI(t) and the discount weights w(t) depend ONLY on the scenario + #
        # r, not on the portfolio path — so they are identical across every #
        # trial, every year, and (formerly) every bisection step. Compute   #
        # them ONCE here and turn the per-year solve into an O(#GI-steps)    #
        # analytic segment walk (``_solve_l_segments``) instead of a nested  #
        # bisection that re-looped the horizon ~40x per year (the ~400x MC   #
        # hot spot). Bisection is retained as ``_solve_l_bisect`` for the    #
        # defensive fallback (below) and as the equivalence-test reference.  #
        #                                                                   #
        # Weights are anchored at ``start_age`` (w(start)=1) rather than at  #
        # absolute t, so magnitudes stay near 1 and never underflow over a   #
        # 40+ year horizon; the common (1+r)^start_age factor cancels in     #
        # every L ratio, so the anchor choice does not change any result.    #
        # ----------------------------------------------------------------- #
        self._start_age = int(scenario.start_age)
        self._end_age = int(scenario.end_age)
        n = self._end_age - self._start_age + 1
        # Per-age GI(t), one ``gi_at_age`` call per age (was once per solve-step).
        self._gi = [gi_at_age(scenario, self._start_age + i) for i in range(n)]
        # Absolute discount weights w(t) = (1+r)^-(t-start_age), indexed by offset.
        disc = 1.0 + self.r
        w = [0.0] * n
        factor = 1.0
        for i in range(n):
            w[i] = factor
            factor /= disc
        self._w = w
        # SUFFIX sums so any suffix Σ_{t>=a} is an O(1) lookup. Length n+1; the
        # trailing 0 entry is the empty tail past end_age. ``_W[i]`` = Σ w over
        # offsets >= i; ``_GW[i]`` = Σ GI*w over offsets >= i.
        sW = [0.0] * (n + 1)
        sGW = [0.0] * (n + 1)
        for i in range(n - 1, -1, -1):
            sW[i] = sW[i + 1] + w[i]
            sGW[i] = sGW[i + 1] + self._gi[i] * w[i]
        self._W = sW
        self._GW = sGW

        # The segment solve relies on GI(t) being NON-DECREASING in t (the four
        # streams only turn ON as age crosses each start_age; constant-real after).
        # This is TRUE iff ``pension_real_drift == 0`` — a positive drift erodes the
        # two pension streams in real terms, so GI can DECREASE and the active set is
        # no longer a clean prefix. Check once; if violated, fall back to bisection
        # so correctness is never at risk regardless of inputs.
        self._gi_nondecreasing = all(
            self._gi[i + 1] >= self._gi[i] - 1e-9 for i in range(n - 1)
        )

        # ----------------------------------------------------------------- #
        # SPENDING-REAL-DRIFT AWARENESS.                         #
        #                                                                   #
        # The engine loops multiply the strategy's year-t output by         #
        # ``(1 + spending_real_drift) ** (t - start_age)`` AFTER the solve  #
        # (simulate.py / sim_core.py). A drift-blind constant-real solve     #
        # therefore violates its own premise under d > 0: the escalated     #
        # path overspends the balance and depletes YEARS before end_age     #
        # (sustained_cost_drift reproduced the default frontier dying at ~81/87   #
        # instead of riding to ~0 at 95). When d != 0 the per-year solve    #
        # must find the L whose ESCALATED spend path                        #
        # ``L · (1+d)^(t - start_age)`` — anchored at the SIMULATION start, #
        # matching the loop's exponent, NOT at the re-solve age (the        #
        # off-by-one that silently reintroduces the bug) — net of the       #
        # NON-drifting GI stream, exactly amortizes the balance to 0 at     #
        # end_age. Precompute (path-independent, once per run):             #
        #   _esc[i]    = (1+d)^i                  spend escalation factors  #
        #   _v[i]      = _esc[i] * w[i]           the spend leg's compound  #
        #                = ((1+d)/(1+r))^i        discount weights          #
        #   _gtilde[i] = GI(i) / _esc[i]          per-offset ACTIVE         #
        #                                          threshold: offset i has  #
        #                                          a positive gap iff       #
        #                                          L > _gtilde[i]           #
        #   _gtilde_order = offsets sorted ascending by _gtilde — the order #
        #                   offsets JOIN the active set as L rises (under   #
        #                   drift the active set is no longer a prefix in   #
        #                   t, but it is always a prefix in this order).    #
        # d == 0 builds none of this and takes the EXACT pre-drift-aware code path  #
        # in ``_solve_l`` — every no-drift canary stays byte-identical.     #
        # ----------------------------------------------------------------- #
        self.d = float(scenario.spending_real_drift)
        if self.d != 0.0:
            growth = 1.0 + self.d
            esc = [0.0] * n
            f = 1.0
            for i in range(n):
                esc[i] = f
                f *= growth
            self._esc = esc
            self._v = [esc[i] * w[i] for i in range(n)]
            self._gtilde = [self._gi[i] / esc[i] for i in range(n)]
            self._gtilde_order = sorted(range(n), key=self._gtilde.__getitem__)

    def _solve_l(
        self, scenario: Scenario, age: int, begin_year_portfolio: float
    ) -> float:
        """Solve for the constant-real spend ``L`` that funds ``begin_year_portfolio``
        exactly over the remaining GI-netted gap stream (the FR inversion).

        Fast path: the analytic O(#GI-steps) segment walk (``_solve_l_segments``),
        valid because GI(t) is non-decreasing so the active set ``{t>=a : GI(t) < L}``
        is always a PREFIX of the suffix. Falls back to the bisection reference
        (``_solve_l_bisect``) iff the non-decreasing assumption was violated at
        construction (e.g. ``pension_real_drift > 0``) or no segment validates —
        correctness is never at risk. Both return ``L`` with
        ``npv_gaps_at_age(scenario, age, L, r) == begin_year_portfolio`` to the cent.

        DRIFT-AWARE branch: when ``spending_real_drift != 0`` the
        engine loop escalates this solve's output by ``(1+d)^(age - start_age)``,
        so the self-consistent solve amortizes the ESCALATED spend path instead
        (``_solve_l_segments_drift`` analytic / ``_solve_l_bisect_drift``
        reference — same fast-path-with-fallback shape, same guard). Both return
        ``L`` with ``_npv_escalated_gaps_at_age(scenario, age, L, r, d) ==
        begin_year_portfolio`` to the cent. ``d == 0`` takes the exact pre-drift-aware
        path below — no-drift canaries byte-identical.
        """
        if self.d != 0.0:
            if self._gi_nondecreasing:
                l_seg = self._solve_l_segments_drift(age, begin_year_portfolio)
                if l_seg is not None:
                    return l_seg
            return self._solve_l_bisect_drift(scenario, age, begin_year_portfolio)
        if self._gi_nondecreasing:
            l_seg = self._solve_l_segments(age, begin_year_portfolio)
            if l_seg is not None:
                return l_seg
        return self._solve_l_bisect(scenario, age, begin_year_portfolio)

    def _solve_l_segments(
        self, age: int, begin_year_portfolio: float
    ) -> float | None:
        """Analytic per-year solve via the GI breakpoints (no bisection, no inner loop).

        Condition (multiply ``Σ_{t>=a} max(0,L-GI(t))(1+r)^-(t-a) == P_a`` by w(a)):

            Σ_{t>=a} max(0, L - GI(t)) * w(t) == P_a * w(a)

        For a fixed active set this is LINEAR in L. GI non-decreasing ⇒ the active set
        ``{t>=a : GI(t) < L}`` is a prefix ``[a, k)`` of the suffix, so for each
        candidate prefix end ``k`` (offsets a..end_age inclusive ⇒ k in (a, end])::

            L = (P_a * w(a) + (GW[a] - GW[k])) / (W[a] - W[k])

        accepted iff it satisfies the segment bound ``GI(k-1) < L <= GI(k)`` (the tail
        boundary ``L <= GI(k)`` is omitted past end_age). There are at most ~5 distinct
        GI breakpoints, so this is a handful of O(1) suffix-sum lookups per year.

        Returns the validated ``L`` or ``None`` (caller falls back to bisection).
        """
        a = age - self._start_age
        end = self._end_age - self._start_age  # last offset (inclusive)
        if a < 0 or a > end:
            return None  # out of horizon → let the bisection path raise loudly
        Pwa = begin_year_portfolio * self._w[a]
        Wa = self._W[a]
        GWa = self._GW[a]
        gi = self._gi
        W = self._W
        GW = self._GW
        # Walk candidate prefix ends k = a+1 .. end+1 (k=end+1 ⇒ whole suffix active).
        for k in range(a + 1, end + 2):
            denom = Wa - W[k]
            if denom <= 0.0:
                continue  # empty active set can't fund a positive P
            L = (Pwa + (GWa - GW[k])) / denom
            # Lower bound: the last active offset (k-1) must truly be a gap (GI < L).
            if not (gi[k - 1] < L):
                continue
            # Upper bound: the first INACTIVE offset (k) must have GI >= L. Past
            # end_age there is no such offset, so the bound is vacuously satisfied.
            if k <= end and not (L <= gi[k]):
                continue
            return L
        return None

    def _solve_l_segments_drift(
        self, age: int, begin_year_portfolio: float
    ) -> float | None:
        """Analytic drift-aware per-year solve — closed form, no bisection.

        Condition (the engine loop escalates year-t spend by ``(1+d)^(t-start)``;
        multiply ``Σ_{t>=a} max(0, L·(1+d)^(t-start) - GI(t))·(1+r)^-(t-a) == P_a``
        through by ``w(a)``)::

            Σ_{t>=a} max(0, L·esc(t) - GI(t)) * w(t) == P_a * w(a)

        For a fixed active set ``S`` this is LINEAR in L::

            L = (P_a·w(a) + Σ_{t∈S} GI(t)·w(t)) / Σ_{t∈S} v(t)

        where ``v(t) = esc(t)·w(t) = ((1+d)/(1+r))^(t-start)`` — the spend leg's
        COMPOUND discount weights (the GI leg keeps the plain ``w``). Offset t is
        active iff ``L·esc(t) > GI(t)`` ⇔ ``L > g̃(t) = GI(t)/esc(t)``. Under
        drift ``g̃`` is no longer monotone in t (it decays within constant-GI
        segments and jumps at GI steps), so the active set is NOT a prefix of the
        suffix — but it IS always a prefix of the offsets sorted ascending by
        ``g̃`` (offsets join the set in that order as L rises). So the segment
        walk generalizes: walk candidate prefixes of the precomputed
        ``_gtilde_order`` (restricted to offsets >= a), accumulate the two sums
        incrementally, and accept the L that satisfies the segment bounds
        ``g̃(last-added) < L <= g̃(next-candidate)`` (upper bound vacuous on the
        full set). At most ~horizon candidates of O(1) work each — still
        closed-form per year, no per-year bisection in the MC path.

        Returns the validated ``L`` or ``None`` (caller falls back to the
        drift-aware bisection reference).
        """
        a = age - self._start_age
        end = self._end_age - self._start_age  # last offset (inclusive)
        if a < 0 or a > end:
            return None  # out of horizon → let the bisection path raise loudly
        Pwa = begin_year_portfolio * self._w[a]
        gi = self._gi
        w = self._w
        v = self._v
        gtilde = self._gtilde
        # Candidate join order over the remaining horizon: ascending g̃, offsets >= a.
        cand = [i for i in self._gtilde_order if i >= a]
        m = len(cand)
        sum_v = 0.0   # Σ v over the candidate active set
        sum_gw = 0.0  # Σ GI·w over the candidate active set
        for k in range(m):
            i = cand[k]
            sum_v += v[i]
            sum_gw += gi[i] * w[i]
            L = (Pwa + sum_gw) / sum_v
            # Lower bound: the last-joined offset (max g̃ in the set) must truly
            # be a gap year (g̃ < L) — then so is every earlier-joined member.
            if not (gtilde[i] < L):
                continue
            # Upper bound: the next offset to join must NOT yet be active
            # (L <= its g̃). Vacuous when the whole remaining horizon is active.
            if k + 1 < m and not (L <= gtilde[cand[k + 1]]):
                continue
            return L
        return None

    def _solve_l_bisect(
        self, scenario: Scenario, age: int, begin_year_portfolio: float
    ) -> float:
        """Bisection REFERENCE solve (the original algorithm) — defensive fallback for
        the analytic path and the equivalence-test oracle.

        Returns ``L`` such that ``npv_gaps_at_age(scenario, age, L, r) ==
        begin_year_portfolio`` to within ``_AMORT_NPV_TOL`` (or the L-bracket has
        collapsed within ``_AMORT_L_TOL``). Pure given the scenario + inputs.
        """
        from firebridgelab.spending.npv import npv_gaps_at_age

        target_portfolio = begin_year_portfolio

        # The largest GI active anywhere in the remaining horizon — part of the upper
        # bracket so ``hi`` dominates the root even when GI is large (the gap at a
        # given L is L - GI, so funding any P needs L at least up around GI_max).
        gi_max = max(
            (gi_at_age(scenario, a) for a in range(age, scenario.end_age + 1)),
            default=0.0,
        )
        lo = 0.0
        hi = gi_max + target_portfolio + _AMORT_HI_PAD

        # NPV is monotincreasing in L. At L = lo = 0 every gap is 0 → NPV 0 <= P.
        # Assert the upper bracket dominates the root before bisecting, so a
        # mis-bracket fails loudly rather than returning a silently-capped L.
        npv_hi = npv_gaps_at_age(scenario, age, hi, self.r)
        assert npv_hi >= target_portfolio, (
            f"amortization frontier: root not bracketed at age {age} "
            f"(NPV({hi:,.2f}) = {npv_hi:,.2f} < P = {target_portfolio:,.2f})"
        )

        for _ in range(_AMORT_MAX_ITERS):
            mid = 0.5 * (lo + hi)
            npv_mid = npv_gaps_at_age(scenario, age, mid, self.r)
            if abs(npv_mid - target_portfolio) <= _AMORT_NPV_TOL:
                return mid
            if npv_mid < target_portfolio:
                lo = mid
            else:
                hi = mid
            if hi - lo <= _AMORT_L_TOL:
                break
        return 0.5 * (lo + hi)

    def _solve_l_bisect_drift(
        self, scenario: Scenario, age: int, begin_year_portfolio: float
    ) -> float:
        """Drift-aware bisection REFERENCE solve — defensive fallback
        for ``_solve_l_segments_drift`` and the drift equivalence-test oracle; also
        the primary drift path when the non-decreasing-GI guard tripped
        (``pension_real_drift > 0``).

        Bisects on the BASE ``L`` against the independent escalated-gap NPV
        (``_npv_escalated_gaps_at_age`` — per-year ``gi_at_age``, no precomputed
        tables), which is monotonically increasing in L. Returns ``L`` such that
        ``_npv_escalated_gaps_at_age(scenario, age, L, r, d) ==
        begin_year_portfolio`` to within ``_AMORT_NPV_TOL`` (or the L-bracket has
        collapsed within ``_AMORT_L_TOL``). Pure given the scenario + inputs.
        Deterministic-only in practice (the MC path takes the analytic solve).
        """
        d = self.d
        target_portfolio = begin_year_portfolio

        gi_max = max(
            (gi_at_age(scenario, a) for a in range(age, scenario.end_age + 1)),
            default=0.0,
        )
        # The escalation factor (1+d)^(t-start) is monotone in t, so its MINIMUM
        # over [age, end_age] is at one endpoint. Dividing the no-drift bracket by
        # that minimum guarantees hi·esc(t) >= gi_max + P + pad at EVERY remaining
        # age — the year-``age`` gap alone (undiscounted) then covers P, so the
        # root is bracketed for either drift sign.
        growth = 1.0 + d
        esc_min = min(
            growth ** (age - scenario.start_age),
            growth ** (scenario.end_age - scenario.start_age),
        )
        lo = 0.0
        hi = (gi_max + target_portfolio + _AMORT_HI_PAD) / esc_min

        npv_hi = _npv_escalated_gaps_at_age(scenario, age, hi, self.r, d)
        assert npv_hi >= target_portfolio, (
            f"amortization frontier (drift-aware): root not bracketed at age {age} "
            f"(NPV({hi:,.2f}) = {npv_hi:,.2f} < P = {target_portfolio:,.2f})"
        )

        for _ in range(_AMORT_MAX_ITERS):
            mid = 0.5 * (lo + hi)
            npv_mid = _npv_escalated_gaps_at_age(scenario, age, mid, self.r, d)
            if abs(npv_mid - target_portfolio) <= _AMORT_NPV_TOL:
                return mid
            if npv_mid < target_portfolio:
                lo = mid
            else:
                hi = mid
            if hi - lo <= _AMORT_L_TOL:
                break
        return 0.5 * (lo + hi)

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        # Degenerate: portfolio gone → you can only spend guaranteed income. (The
        # engine's max_fundable_spend then caps the RECORDED spend to gi.)
        if begin_year_portfolio <= 0:
            spend = gi_at_age(scenario, age)
        else:
            spend = self._solve_l(scenario, age, begin_year_portfolio)

        # Honest year-over-year action label (no taper, no clamp, no band — the
        # frontier IS the spend). First year has no prior → "hold".
        if self._prev_spend is None or abs(spend - self._prev_spend) <= 1e-6:
            action = "hold"
        elif spend > self._prev_spend:
            action = "raise"
        else:
            action = "cut"

        gi_stepped_up = self._prev_gi is not None and gi > self._prev_gi + 1e-9
        self.events.append(
            AdjustmentEvent(
                age=age,
                action=action,
                spend=spend,
                begin_portfolio=begin_year_portfolio,
                gi=gi,
                gi_stepped_up=gi_stepped_up,
            )
        )
        self._prev_spend = spend
        self._prev_gi = gi
        return spend
