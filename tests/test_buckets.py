"""Tests for the 3-bucket withdrawal-sourcing strategy (bucket-strategy design).

Organized by the design's validation gates A–F (slice 1a, engine-side):

  A. Canary, byte-for-byte — single (default) sourcing is untouched.
  B. Plumbing identity, byte-for-byte — a single-bucket buckets config reproduces
     the single-balance percentile fans to the exact float, in all three modes.
  C. Defeasance limit — safe buckets sized >= the cumulative bridge gap → the
     growth bucket has zero outflows before GI lands, in every Historical cohort.
  D. One-sided vs symmetric — flipping the documented refill trigger moves
     forced-sale behaviour in the bad cohorts in the expected direction.
  E. Buffer-survival sanity — the forced-sale metric is 0 under full defeasance,
     > 0 with a thin cash bucket, and monotone-ish in N1.
  F. Forward-gap targets (GI-shrink + underfunded cap), the guardrails 422 fence,
     and the API round-trip.

Gate B is the STRICT byte-for-byte identity (incl. depletion): a single-(cash-)
bucket buckets config reproduces the single-balance engine EXACTLY in all three
modes — the percentile fans, the deterministic terminal/trajectory, AND the
depletion count/flag. This is the corrected design's non-negotiable plumbing
anchor. The corrected per-year mechanic grows each bucket, adds GI flat to the
grown cash bucket, sources GROSS spending cash-first against the GROWN + GI
balances (the single funded bucket absorbing the full spend incl. overdraw), then
applies ONE final floor — so GI is available-to-spend BEFORE the floor, matching
``step_year``'s floor-eats-GI semantics. A fans-match-but-depletion-differs result
is a FAIL (it means GI is surviving the floor in the bucket path but not the
single path — a floor-semantics difference that confounds the bucket-vs-single
head-to-head with something other than the sourcing difference we isolate). The
earlier ``s_cash = min(s, B_cash)`` cap was that bug; it is fixed.
"""

from dataclasses import replace

import pytest

import firebridgelab.buckets as buckets_module
from firebridgelab import (
    SOURCING_MODE_BUCKETS,
    AssetClass,
    Bucket,
    BucketConfig,
    cash_target,
    default_bucket_config,
    default_scenario,
    gap_for_age,
    income_target,
    simulate,
    simulate_historical,
    simulate_monte_carlo,
    solve_max_spend,
)
from firebridgelab.scenario import (
    SPENDING_MODE_GUARDRAILS,
    SPENDING_MODE_LIFECYCLE,
    _DEFAULT_BOND_VOLATILITY,
    _DEFAULT_EQUITY_VOLATILITY,
)
from firebridgelab.spending import BucketsGuardrailsNotComposable, BucketsNotSolvable
from tests._canary import DET_FINAL_CANARY


# ---------- shared fixtures ----------


def _single_bucket_config(scenario):
    """A buckets config that degenerates to the single-balance engine (Gate B).

    All three buckets hold the SAME default 60/40 allocation, and BOTH ``N1`` and
    ``N2`` are 0 so the cash + income forward-gap targets are 0 at every age —
    GROWTH holds the WHOLE portfolio and cash + income start empty. Each year the
    corrected mechanic grows growth, lands GI flat on the (empty) cash bucket,
    sources spending cash-first against the grown + GI balances (cash holds only
    this year's GI; income empty; the deepest funded bucket — GROWTH — absorbs the
    rest of the spend incl. any overdraw, going transiently negative), then the
    year-end rebalance RELEASES any leftover cash (GI above spend) back into growth
    (the safe side is over its 0 target) — a mass-preserving move between two
    buckets that carry the SAME allocation return. So the summed per-bucket update
    reduces EXACTLY to ``step_year``'s ``B + B*r + gi − s*(1+r/2)`` form, floored
    once — byte-for-byte INCLUDING the depletion count/flag.

    Why GROWTH-only (not the old huge-N1 cash-only) under the CORRECTED mechanic:
    the corrected rebalance RELEASES the over-funded safe side into growth, so a
    cash-only config (huge N1) no longer stays inert — once the portfolio outgrows
    the (end-age-capped, shrinking) cash target the release leg fires and the path
    diverges from a single balance. Routing everything through growth, with the
    safe targets pinned at 0, makes the rebalance genuinely MOOT (it only ever
    returns this year's unspent GI to growth), preserving the byte-for-byte identity
    the design requires.
    """
    return BucketConfig(
        cash=Bucket("cash", "Cash", scenario.allocation),
        income=Bucket("income", "Income", scenario.allocation),
        growth=Bucket("growth", "Growth", scenario.allocation),
        n1=0,
        n2=0,
    )


def _buckets(scenario, config):
    return replace(scenario, sourcing_mode=SOURCING_MODE_BUCKETS, buckets=config)


# ---------- Gate A: canary, byte-for-byte (single sourcing untouched) ----------


def test_gate_a_deterministic_canary():
    """Single (default) sourcing: deterministic terminal == the exact float canary."""
    assert simulate(default_scenario()).final_portfolio == DET_FINAL_CANARY


def test_gate_a_default_scenario_is_single_sourcing():
    """The default scenario sources single (buckets off), so the canary path holds."""
    s = default_scenario()
    assert s.sourcing_mode == "single"
    assert s.buckets is None


def test_gate_a_historical_canary():
    """Single Historical canary unchanged: 110 cohorts, 1871 start, 17 depletions."""
    h = simulate_historical(default_scenario())
    assert h.cohort_count == 110
    assert h.start_year == 1871
    assert h.depletion_count == 17
    assert h.earliest_depletion_age == 63
    # buffer_survival is buckets-only → None under single sourcing.
    assert h.buffer_survival is None


def test_gate_a_monte_carlo_canary():
    """Single Monte Carlo canary unchanged: 10k trials, 846 depletions, no buffer.
    """
    mc = simulate_monte_carlo(default_scenario())
    assert mc.trial_count == 10_000
    assert mc.depletion_count == 846
    assert mc.buffer_survival is None


# ---------- Gate B: plumbing identity, byte-for-byte (all three modes) ----------


def test_gate_b_deterministic_identity():
    """A single-bucket buckets config reproduces the deterministic terminal exactly."""
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    # Algebraic (not bit) identity since GI is credited mid-year (2026-10-11).
    assert simulate(sb).final_portfolio == pytest.approx(simulate(s).final_portfolio, rel=1e-12)
    assert simulate(s).final_portfolio == DET_FINAL_CANARY


def test_gate_b_deterministic_trajectory_identity():
    """Every deterministic YearPoint's end_portfolio matches the single path exactly."""
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = [p.end_portfolio for p in simulate(s).points]
    bucket = [p.end_portfolio for p in simulate(sb).points]
    # Algebraic (not bit) identity since GI is credited mid-year (2026-10-11).
    assert bucket == pytest.approx(single, rel=1e-12)


def test_bucket_realized_spend_capped_at_fundable_after_depletion():
    """Realized-spend correctness fix in buckets: a depleting bucket year records the
    MIDPOINT-DISCOUNTED fundable spend, not the raw drained balance.

    Buckets runs constant/lifecycle only. A small portfolio + high spend depletes a
    deterministic bucket run; pre-fix every post-depletion year recorded the full
    $300k gross request even with $0 buckets + $0 GI. A first reporting fix capped at
    the raw available balance (``spending − overdraw``) — but draining a grown
    balance of ``avail`` only funds ``avail/(1 + r/2)`` of spend under the engine's
    midpoint convention, so that still OVERSTATED the depletion-year spend by the
    ``(1 + r/2)`` factor (a smaller copy of the exact bug this change set fixes). The
    fix records the cash-first sum of ``avail[i]/(1 + r_i/2)`` capped at the request.

    Pinned to the cent (hand-derived). Entering age 55 the only funded bucket is cash
    at ``$49,136.9920591472`` growing at the deterministic cash blend
    ``r_cash = 0.00325985278338381`` with no GI, so the grown+GI available is
    ``avail_cash = 49136.9920591472 * (1 + r_cash) = $49,297.171419...``. The OLD
    raw-balance cap recorded that $49,297.17; the midpoint-correct fundable spend is
    ``avail_cash / (1 + r_cash/2) = $49,216.95`` — the value pinned below. Fully-
    depleted years with no GI record exactly $0. The portfolio path / depletion age
    are unchanged (reporting-only fix)."""
    from firebridgelab.buckets import simulate_buckets_deterministic

    s = replace(default_scenario(), initial_portfolio=1_500_000.0, annual_spending=300_000.0)
    sb = _buckets(s, default_bucket_config())
    bt = simulate_buckets_deterministic(sb)
    assert bt.depleted_age == 55  # depletion age unchanged by the reporting fix

    by_age = {y.age: y for y in bt.years}
    # Pre-depletion: the full $300k is sourced (no cap).
    assert by_age[54].spending == pytest.approx(300_000.0)
    # Depletion year: recorded spend is the MIDPOINT-DISCOUNTED fundable amount,
    # pinned to the cent. Raw-balance cap would (wrongly) give $49,297.17; the
    # midpoint-correct value drains cash exactly under (1 + r_cash/2).
    assert by_age[55].spending == pytest.approx(49_216.95, abs=0.01)
    assert by_age[55].spending < 49_297.17  # strictly below the raw-balance overstatement
    assert by_age[55].spending > 0.0
    assert (by_age[55].cash + by_age[55].income + by_age[55].growth) == 0.0
    # Fully depleted, no GI (pre-60 bridge) → recorded spend exactly $0.
    assert by_age[55].guaranteed_income == 0.0
    assert by_age[56].spending == 0.0
    assert by_age[57].spending == 0.0


def test_bucket_degenerate_realized_spend_matches_single_portfolio_to_the_cent():
    """Gate-B identity extended to RECORDED SPEND: the single-(cash-)bucket degenerate
    records the SAME depletion-year realized spend as the single-portfolio path.

    Where Gate B pins the BALANCE identity byte-for-byte, this pins the SPEND
    identity to the cent. A single-bucket buckets config (all three buckets the same
    allocation, N1=N2=0 so growth holds the whole portfolio and GI lands flat on the
    cash bucket) routes the spend through a single funded bucket each year, so its
    per-year midpoint fund-cap ``avail/(1 + r/2)`` must reduce to the single path's
    recorded ``min(intended, max_fundable_spend(start, r, gi))`` — to the cent (they
    differ only by the ``start + start*r`` vs ``start*(1+r)`` last-bit regroup the
    engine documents, sub-cent). We compare the DEPLETION-YEAR recorded spend (where
    the cap actually bites) on a small/high-spend scenario that depletes in both
    paths, then every year for good measure."""
    from firebridgelab.buckets import simulate_buckets_deterministic

    s = replace(default_scenario(), initial_portfolio=1_500_000.0, annual_spending=300_000.0)
    single = simulate(s)  # single-portfolio path (records min(intended, max_fundable))
    sb = _buckets(s, _single_bucket_config(s))
    bt = simulate_buckets_deterministic(sb)

    single_by_age = {p.age: p.spending for p in single.points}
    bucket_by_age = {y.age: y.spending for y in bt.years}
    assert single.depleted_age == bt.depleted_age is not None  # both deplete (same age)

    # The depletion year: the recorded spend matches to the cent (the cap bites here).
    dep = bt.depleted_age
    assert bucket_by_age[dep] == pytest.approx(single_by_age[dep], abs=0.01)
    # And every age matches to the cent (non-depleting years are exact == intended).
    for age in single_by_age:
        assert bucket_by_age[age] == pytest.approx(single_by_age[age], abs=0.01)


def test_gate_b_historical_fan_identity():
    """The Historical percentile fan reproduces the single-balance fan to fp tolerance.

    All five per-age percentile series (p10/p25/median/p75/p90) match to within
    floating-point ULP. They are NOT byte-for-byte under the CORRECTED mechanic, and
    that is structural, not a regression: the single-bucket config routes the bulk
    through GROWTH while GI lands flat on the (separate) CASH bucket, so once GI is
    being spent the year's spend sources from TWO buckets and the midpoint term
    splits into ``s_cash*(1+r/2) + s_gro*(1+r/2)``. With the SAME allocation in both
    buckets that is economically identical to the single ``s*(1+r/2)``, but IEEE-754
    distributivity does not hold exactly — ``(a+b)*x != a*x + b*x`` at the last bit —
    so the fans agree to ~1e-8 relative, not byte-for-byte. (The OLD one-sided
    mechanic kept the bulk in ONE bucket and never released, so it could be byte-
    exact; the corrected mechanic's whole purpose is to MOVE money between buckets,
    which legitimately introduces the split.) The DETERMINISTIC terminal IS still
    byte-exact (test_gate_b_deterministic_*), and the depletion COUNT is exact in all
    three modes (the strict floor/GI-semantics anchor) — pinned separately below.
    """
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = simulate_historical(s)
    bucket = simulate_historical(sb)
    assert bucket.cohort_count == single.cohort_count == 110
    assert bucket.start_year == single.start_year == 1871
    assert bucket.median == pytest.approx(single.median, rel=1e-9, abs=1e-3)
    assert bucket.p10 == pytest.approx(single.p10, rel=1e-9, abs=1e-3)
    assert bucket.p25 == pytest.approx(single.p25, rel=1e-9, abs=1e-3)
    assert bucket.p75 == pytest.approx(single.p75, rel=1e-9, abs=1e-3)
    assert bucket.p90 == pytest.approx(single.p90, rel=1e-9, abs=1e-3)


def test_gate_b_monte_carlo_fan_identity():
    """The Monte Carlo percentile fan reproduces the single-balance fan to fp tolerance.

    Same seed + 10k trials. The single-bucket config samples the SAME union (one
    class set) and blends with one effective weight set, so the draws AND the blend
    are bit-identical to the single path's ``returns @ w``. The per-age percentiles
    then match to floating-point ULP (~1e-8 relative), not byte-for-byte, for the
    SAME reason as the Historical fan: GI lands on the cash bucket while the bulk
    sits in growth, so a year that spends GI splits the midpoint term across two
    buckets and IEEE-754 distributivity loses the last bit. The depletion count is
    pinned exact separately (test_gate_b_monte_carlo_depletion_identity).
    """
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = simulate_monte_carlo(s)
    bucket = simulate_monte_carlo(sb)
    assert bucket.trial_count == single.trial_count == 10_000
    assert bucket.median == pytest.approx(single.median, rel=1e-9, abs=1e-3)
    assert bucket.p10 == pytest.approx(single.p10, rel=1e-9, abs=1e-3)
    assert bucket.p25 == pytest.approx(single.p25, rel=1e-9, abs=1e-3)
    assert bucket.p75 == pytest.approx(single.p75, rel=1e-9, abs=1e-3)
    assert bucket.p90 == pytest.approx(single.p90, rel=1e-9, abs=1e-3)


def test_gate_b_historical_depletion_identity():
    """STRICT Gate B: single-bucket Historical depletion_count == single-balance == 17.

    The corrected mechanic grows the growth bucket, lands GI flat on the (empty) cash
    bucket, sources the GROSS spend cash-first against the grown+GI balances (cash
    holds only GI; the deepest funded bucket — growth — absorbs the rest incl. any
    overdraw, going transiently negative), then applies ONE final floor on the
    PRE-floor SUMMED balance. So GI is available-to-spend BEFORE the floor and is
    eaten by it in a deep drawdown EXACTLY as ``step_year`` eats it — the
    single-bucket bucket path counts the SAME 17 depletions the single-balance path
    does (incl. the same earliest age). A divergence here would mean GI survives the
    floor in the bucket path only — the floor-semantics confound the design calls a
    FAIL. (Depletion is the STRICT exact anchor; the fans match to fp tolerance — see
    test_gate_b_historical_fan_identity for why byte-for-byte fans are not possible
    under the corrected, money-moving mechanic.)
    """
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = simulate_historical(s)
    bucket = simulate_historical(sb)
    assert single.depletion_count == 17
    assert bucket.depletion_count == single.depletion_count == 17
    assert bucket.earliest_depletion_age == single.earliest_depletion_age
    # The bulk is in GROWTH here, so the bucket records growth "sourcing" every year
    # (the spend exceeds GI from age 60 on, and is wholly growth-sourced in the
    # bridge) — this is the single-bucket degenerate, not a buffer breach: every
    # cohort sources from growth because growth IS the funded bucket.
    assert bucket.buffer_survival.pct_cohorts_forced_to_sell_growth == pytest.approx(1.0)


def test_gate_b_monte_carlo_depletion_identity():
    """STRICT Gate B: single-bucket MC depletion_count == single-balance MC's exactly.

    The MC fan-identity test pins the percentile fans; this pins the depletion
    count too. The bucket MC path now reads each trial's per-cohort depletion flag
    (the corrected pre-floor ``< 0`` rule that mirrors ``step_year``), while the
    single-balance MC path derives depletion from the post-floor summed matrix
    (``_depletion_stats`` on ``== 0.0``). The two agree on every sampled trial — a
    continuous-return MC trial never lands its zero-crossing EXACTLY on 0.0, the one
    input where pre-floor ``< 0`` and post-floor ``== 0`` would diverge — so the
    count and earliest age match trial-for-trial.
    """
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = simulate_monte_carlo(s)
    bucket = simulate_monte_carlo(sb)
    assert bucket.depletion_count == single.depletion_count
    assert bucket.earliest_depletion_age == single.earliest_depletion_age


def test_gate_b_deterministic_depletion_identity():
    """STRICT Gate B: single-bucket deterministic depleted_age + terminal == single.

    The default scenario never depletes deterministically (depleted_age is None for
    BOTH paths), and the terminal is the exact canary float — the byte-for-byte
    plumbing identity incl. the depletion flag.
    """
    s = default_scenario()
    sb = _buckets(s, _single_bucket_config(s))
    single = simulate(s)
    bucket = simulate(sb)
    assert bucket.depleted_age == single.depleted_age is None
    # Algebraic (not bit) identity since GI is credited mid-year (2026-10-11).
    assert bucket.final_portfolio == pytest.approx(single.final_portfolio, rel=1e-12)
    assert single.final_portfolio == DET_FINAL_CANARY


def test_gate_b_exact_zero_boundary_matches_step_year():
    """STRICT Gate B (structural): the single-bucket depletion flag == step_year's.

    Pins the zero-crossing boundary that makes the Gate-B depletion identity
    STRUCTURAL rather than empirical. With one funded cash bucket (income + growth
    empty, no refill — the single-balance degenerate), ``_step_buckets``'s pre-floor
    summed balance IS ``step_year``'s ``end_portfolio``, so its ``depleted_this_year``
    flag must equal ``step_year``'s ``end_portfolio < 0`` flag EXACTLY:

      - a year that lands EXACTLY on 0.0 (start=100, gi=0, spend=100, r=0) is NOT
        depleted under either path — the old post-floor ``cash <= 0`` test would
        WRONGLY have flagged it;
      - a year that OVERDRAWS to < 0 pre-floor (spend just past the balance) IS
        depleted under both, and both floor the balance to 0.0.

    A divergence here is the empirical-vs-structural gap the fix closes.
    """
    from firebridgelab._step import step_year
    from firebridgelab.buckets import _step_buckets

    # No refill (cash_tgt_next = income_tgt_next = 0), income + growth empty, all
    # zero returns → _step_buckets reduces to step_year on the cash bucket alone.
    def bucket_step(start, gi, spend):
        (cash, inc, gro), _sold, depleted, _realized = _step_buckets(
            (start, 0.0, 0.0), (0.0, 0.0, 0.0), gi, spend, 0.0, 0.0
        )
        return cash, depleted

    # Exact-zero landing: NOT depleted (step_year flags on strictly < 0, not <= 0).
    s_end, s_dep = step_year(100.0, 0.0, 0.0, 100.0)
    b_end, b_dep = bucket_step(100.0, 0.0, 100.0)
    assert s_end == 0.0 and s_dep is False
    assert b_end == 0.0 and b_dep is False
    assert b_dep == s_dep  # structural identity at the exact-zero boundary

    # Overdraw past the balance: < 0 pre-floor → DEPLETED in both, floored to 0.0.
    s_end, s_dep = step_year(100.0, 0.0, 0.0, 100.01)
    b_end, b_dep = bucket_step(100.0, 0.0, 100.01)
    assert s_end == 0.0 and s_dep is True
    assert b_end == 0.0 and b_dep is True
    assert b_dep == s_dep


# ---------- Gate C: defeasance limit ----------


def test_gate_c_full_defeasance_no_growth_sale_before_gi():
    """Safe buckets >= the cumulative bridge gap → growth is never sold before GI.

    The bridge is ages 50–59 (GI lands at 60); its cumulative gap is 10 × $110k =
    $1.1M, well under the $1.8M portfolio. Size cash for the full 10 bridge years
    (N1=10) plus a deep income tail (N2=20) → the safe buckets fully fund the bridge,
    so the growth bucket has ZERO outflows before guaranteed income arrives in
    EVERY Historical cohort. We assert the EARLIEST forced growth sale across all
    cohorts is at or after the first GI age (60) — i.e. never during the bridge.

    CORRECTED-MECHANIC NOTE: under the corrected one-sided rebalance, a down year
    deliberately does NOT sell growth to refill the safe side (it RIDES), so the safe
    buffer can finish a hair short and pull a sliver from growth in the FINAL year
    (age 95) in a few cohorts — a terminal-buffer-rounding artifact, well after GI.
    The OLD over-eager refill masked this by stranding everything in income. The
    bridge-defeasance claim is about no sale BEFORE GI (the sequence-risk window),
    which holds exactly; we no longer assert literally zero sales anywhere.
    """
    s = default_scenario()
    # Cumulative bridge gap sanity (50..59, GI = 0 there → gap = spending).
    sb_default = _buckets(s, default_bucket_config())
    bridge_gap = sum(gap_for_age(sb_default, k) for k in range(50, 60))
    assert bridge_gap == pytest.approx(1_100_000.0)
    assert bridge_gap < s.initial_portfolio

    defease = replace(default_bucket_config(), n1=10, n2=20)
    h = simulate_historical(_buckets(s, defease))
    bs = h.buffer_survival
    # Full defeasance of the bridge: no forced growth sale DURING THE BRIDGE — the
    # earliest sale anywhere is at or after the first GI age (60). Any sale that does
    # occur is the age-95 terminal-buffer artifact, far past the sequence-risk window.
    assert (
        bs.worst_forced_sale_age is None
        or bs.worst_forced_sale_age >= s.person_a_pension_start_age
    )


def test_gate_c_cohort_level_growth_untouched_during_bridge():
    """Per-cohort: with the bridge fully defeased, no cohort sells growth pre-60.

    Replays the dedicated bucket cohort path directly (not just the aggregate) so
    the assertion is per-cohort, not a summary: every cohort's first forced growth
    sale is None or >= the first GI age (60).
    """
    from firebridgelab.buckets import simulate_buckets_cohort
    from firebridgelab.historical import (
        _bucket_window,
        _cohort_blended_returns,
        _period_from_series,
    )

    s = default_scenario()
    defease = replace(default_bucket_config(), n1=10, n2=20)
    sb = _buckets(s, defease)
    bw, sel, start_years, sd = _bucket_window(sb)
    n_steps = s.end_age - s.start_age + 1
    cfg = sb.buckets
    for start_year in start_years:
        period = _period_from_series(sd, sel, start_year, n_steps)
        bret = {
            b.id: _cohort_blended_returns(bw[b.id], period)
            for b in (cfg.cash, cfg.income, cfg.growth)
        }
        traj = simulate_buckets_cohort(sb, bret)
        ffs = traj.first_forced_sale_age
        assert ffs is None or ffs >= s.person_a_pension_start_age, (
            f"cohort {start_year} forced a growth sale at {ffs}, before GI lands"
        )


# ---------- Gate C2: growth-rises-with-GI (the corrected mechanic) ----------


def test_gate_c2_growth_rises_with_gi_not_drained():
    """Growth dips mid-bridge then RISES as GI lands — NOT monotonically drained to 0.

    The corrected-mechanic behavior (the fix). For the default scenario in
    DETERMINISTIC mode the growth bucket should dip modestly through the early bridge
    (the safe buckets are large and growth is the residual of a $1.8M portfolio minus
    a ~$1M safe side), then RISE as guaranteed income lands from 60 to 70: the safe
    targets collapse (the gap shrinks from $110k to $11,760), so the over-funded safe
    side RELEASES its excess into growth, which then compounds. A run where growth
    monotonically drains to ~0 and income holds the residual is the PRE-FIX BUG.

    We assert: (1) growth is NOT ~0 at the end; (2) the LATE growth (post-67, and at
    95) is materially ABOVE the mid-bridge trough; (3) the post-67 share of the
    portfolio held in growth is high (growth ≈ portfolio − the shrunk safe targets).
    """
    from firebridgelab.buckets import simulate_buckets_deterministic

    s = default_scenario()
    sb = _buckets(s, default_bucket_config())  # N1=2, N2=7
    traj = simulate_buckets_deterministic(sb)
    by_age = {y.age: y for y in traj.years}

    # Growth at each landmark age.
    g50 = by_age[50].growth
    g95 = by_age[95].growth
    # Mid-bridge trough = the lowest growth balance at or before the first GI age (60).
    trough = min(y.growth for y in traj.years if y.age <= s.person_a_pension_start_age)

    # (1) Growth is NOT drained to ~0 — it ends as the dominant residual.
    assert g95 > 0.5 * traj.final_portfolio
    # (2) Late growth is MATERIALLY above the mid-bridge trough (it rose, didn't drain).
    assert g95 > 3.0 * trough
    # The trough itself is a real dip below the starting growth (it dips then recovers).
    assert trough < g50
    # (3) Growth RISES monotonically from the trough through the GI ramp onward — once
    #     GI lands the released safe-side excess feeds growth every year.
    post_trough = [y.growth for y in traj.years if y.age >= s.person_a_ss_start_age]
    assert post_trough == sorted(post_trough)  # non-decreasing from A's SS age onward
    # The pre-fix bug would leave growth at ~0 from ~age 70 on; assert the opposite.
    assert by_age[80].growth > g50
    assert by_age[90].growth > by_age[80].growth


def test_gate_c2_safe_targets_shrink_as_gi_lands():
    """The safe targets (cash_target + income_target) collapse as GI lands.

    The mechanism behind Gate C2: gap-based sizing makes the safe side SHRINK as
    pensions (60, 65) and SS (67, 70) arrive, so the released excess flows to growth.
    Assert the summed safe target at 67 is FAR below its level at 50 (≪), and
    strictly decreasing across the GI steps.
    """
    s = default_scenario()
    sb = _buckets(s, default_bucket_config())  # N1=2, N2=7

    def safe_target(age):
        return cash_target(sb, age) + income_target(sb, age)

    t50 = safe_target(50)
    t60 = safe_target(60)
    t65 = safe_target(65)
    t67 = safe_target(67)
    # Strictly shrinking across the GI steps.
    assert t60 < t50
    assert t65 < t60
    assert t67 < t65
    # ≪ : the safe side at 67 is less than a third of its bridge level.
    assert t67 < t50 / 3.0


def test_gate_c2_release_only_adds_to_growth_never_down_year_sale_in_deterministic():
    """In deterministic mode the safe→growth release only ADDS to growth (no down-year
    sale): every year is up, growth never falls, and it ends far above its start.

    Deterministic has no down years, so the one-sided "ride" branch never fires and
    the only cross-bucket move that touches growth is the RELEASE (excess safe side →
    growth). Growth therefore never decreases once the release regime begins, and no
    forced growth sale is ever recorded (no buffer exhaustion in deterministic).
    """
    from firebridgelab.buckets import simulate_buckets_deterministic

    s = default_scenario()
    sb = _buckets(s, default_bucket_config())
    traj = simulate_buckets_deterministic(sb)
    # No forced growth sale anywhere in the deterministic path.
    assert traj.first_forced_sale_age is None
    assert all(not y.forced_growth_sale for y in traj.years)


# ---------- Gate D: one-sided vs symmetric (thesis) ----------


def _cohort_returns(scenario, start_year):
    """The per-bucket per-year real-return arrays for one start-year cohort."""
    from firebridgelab.historical import (
        _bucket_window,
        _cohort_blended_returns,
        _period_from_series,
    )

    cfg = scenario.buckets
    bw, sel, _start_years, sd = _bucket_window(scenario)
    n_steps = scenario.end_age - scenario.start_age + 1
    period = _period_from_series(sd, sel, start_year, n_steps)
    return {
        b.id: _cohort_blended_returns(bw[b.id], period)
        for b in (cfg.cash, cfg.income, cfg.growth)
    }


def _down_year_growth_outflows(
    scenario, symmetric: bool, start_year: int, through_age: int | None = None
):
    """Count + total of DOWN-YEAR growth outflows for one cohort, under a refill rule.

    The thesis quantity (corrected mechanic): a "down-year growth outflow" is a year
    with a NEGATIVE growth return where the growth balance fell by MORE than its own
    market move alone — i.e. dollars LEFT growth in a down year (sold at a loss). The
    one-sided rule rides (never refills from growth in a down year, so this is 0
    except a genuine spend-from-growth when the buffer is exhausted); the SYMMETRIC
    rule buys the dip (refills the safe side from growth in down years), realizing
    these losses. Flips the documented module-level ``SYMMETRIC_REFILL`` knob
    (restored after). ``through_age`` (inclusive) limits the count to years up to
    that age. Returns ``(count, total_dollars_sold)``.
    """
    from firebridgelab.buckets import simulate_buckets_cohort

    bret = _cohort_returns(scenario, start_year)
    rg = bret["growth"]
    original = buckets_module.SYMMETRIC_REFILL
    buckets_module.SYMMETRIC_REFILL = symmetric
    try:
        traj = simulate_buckets_cohort(scenario, bret)
    finally:
        buckets_module.SYMMETRIC_REFILL = original

    count = 0
    total_sold = 0.0
    prev = None
    for k, y in enumerate(traj.years):
        if through_age is not None and y.age > through_age:
            break
        if prev is not None and rg[k] <= 0.0:  # a down year
            # Growth's balance if NOTHING left it this year (market move only).
            implied = prev * (1.0 + rg[k])
            if y.growth < implied - 1.0:  # dollars left growth beyond the market move
                count += 1
                total_sold += implied - y.growth
        prev = y.growth
    return count, total_sold


def test_gate_d_one_sided_avoids_down_year_growth_sales():
    """One-sided realizes ZERO down-year growth sales; symmetric realizes several.

    THE thesis, measured the honest way under the corrected mechanic. The 1928-start
    cohort eats the 1929–1932 crash early in retirement. The SYMMETRIC rule
    rebalances BOTH ways: in those down years it SELLS growth to refill the safe
    buckets — buying the dip and LOCKING IN the loss. The ONE-SIDED rule rides the
    downturn on the buffer and leaves growth alone to recover, so it never sells
    growth in a down year (the rising-equity sequence buffer).

    Documented, not a terminal-wealth race. Under the corrected mechanic growth
    COMPOUNDS (the released safe-side excess flows back to growth), so ex-post a
    cohort whose crash is followed by a historic recovery (1928 → the 1933+ bull) can
    end with HIGHER terminal wealth under symmetric precisely BECAUSE it bought the
    dip — the design flags terminal wealth as a mirage ("≈ wash; do NOT promise"). The
    real, robust difference is the RISK SHAPE: one-sided is never FORCED to sell
    growth at a loss, symmetric repeatedly is. We assert that shape directly.

    The count stops one year before ``end_age``: in this cohort the final year
    (age 95) is a down year in which the one-sided buffer, already wound down,
    pulls a sliver (~$80) from growth — the same terminal-buffer artifact Gate C
    documents, outside the sequence-risk window this thesis is about.
    """
    s = default_scenario()
    # Moderate buffer (N1=2, N2=5) — deep enough to ride the crash, shallow enough
    # that the symmetric rule's down-year sales bite. Earliest cohort (1928).
    sb = _buckets(s, replace(default_bucket_config(), n1=2, n2=5))
    from firebridgelab.historical import _bucket_window

    _bw, _sel, start_years, _sd = _bucket_window(sb)
    bad_year = start_years[0]
    last = s.end_age - 1
    one_count, one_sold = _down_year_growth_outflows(
        sb, symmetric=False, start_year=bad_year, through_age=last
    )
    sym_count, sym_sold = _down_year_growth_outflows(
        sb, symmetric=True, start_year=bad_year, through_age=last
    )
    # One-sided rides: it never sells growth in a down year in this cohort.
    assert one_count == 0
    assert one_sold == pytest.approx(0.0)
    # Symmetric buys the dip: it repeatedly sells growth at a loss to refill.
    assert sym_count >= 3
    assert sym_sold > 100_000.0


def test_gate_d_refill_knob_round_trips():
    """The SYMMETRIC_REFILL knob is restored after a flip (no test cross-talk)."""
    assert buckets_module.SYMMETRIC_REFILL is False
    s = default_scenario()
    sb = _buckets(s, default_bucket_config())
    from firebridgelab.historical import _bucket_window

    _bw, _sel, start_years, _sd = _bucket_window(sb)
    _down_year_growth_outflows(sb, symmetric=True, start_year=start_years[0])
    assert buckets_module.SYMMETRIC_REFILL is False


# ---------- Gate E: buffer-survival sanity ----------


def test_gate_e_zero_forced_sales_under_full_defeasance():
    """Full bridge defeasance → no forced growth sale through the sequence-risk window.

    With the bridge fully defeased (N1=9, N2=20) no cohort sells growth during the
    bridge OR through the GI ramp — the earliest forced sale anywhere is the age-95
    terminal-buffer artifact (the corrected mechanic RIDES down years rather than
    over-refilling, so a cohort can finish a sliver short in the very last year). The
    sanity claim is that the buffer carries every cohort through the dangerous early
    decades, which holds: the earliest sale is None or deep in old age (>= 90), never
    in the bridge or the GI ramp.
    """
    s = default_scenario()
    defease = replace(default_bucket_config(), n1=10, n2=20)
    h = simulate_historical(_buckets(s, defease))
    bs = h.buffer_survival
    assert bs.worst_forced_sale_age is None or bs.worst_forced_sale_age >= 90


def test_gate_e_thin_cash_forces_sales():
    """A thin cash + income buffer (N1=1, N2=1) forces growth sales in bad cohorts."""
    s = default_scenario()
    thin = replace(default_bucket_config(), n1=1, n2=1)
    h = simulate_historical(_buckets(s, thin))
    assert h.buffer_survival.pct_cohorts_forced_to_sell_growth > 0.0
    assert h.buffer_survival.worst_forced_sale_age is not None


def test_gate_e_median_buffer_lasts_longer_with_more_cash():
    """median_years_buffer_lasted is monotone-ish in N1 (a bigger buffer lasts longer).

    Holding the income bucket empty (N2=0) so the CASH years alone drive the buffer,
    the median age of the first forced growth sale rises as N1 grows (the cash buffer
    carries the retirement further before growth is touched), and the fraction of
    cohorts ever forced to sell falls. (N1 is held in the regime where the buffer is
    breached in every cohort — pct_forced == 1.0 — so the median age is always
    DEFINED, not None; under the corrected, genuinely-protective mechanic a few more
    cash years past this regime drives pct_forced to 0 / the median to "never", which
    is the SAME monotone direction, not a regression.)
    """
    s = default_scenario()

    def survival(n1):
        cfg = replace(default_bucket_config(), n1=n1, n2=0)
        return simulate_historical(_buckets(s, cfg)).buffer_survival

    s1, s2, s3 = survival(1), survival(2), survival(3)
    # Median first-forced-sale age rises with N1 (buffer lasts longer): 54 -> 58 -> 64.
    assert (
        s1.median_years_buffer_lasted
        <= s2.median_years_buffer_lasted
        <= s3.median_years_buffer_lasted
    )
    # Fraction ever forced to sell does not rise with N1 (a bigger buffer never hurts).
    assert s1.pct_cohorts_forced_to_sell_growth >= s3.pct_cohorts_forced_to_sell_growth


def test_gate_e_monte_carlo_buffer_survival_present():
    """Monte Carlo carries the same buffer-survival readout (the MC twin)."""
    s = default_scenario()
    thin = replace(default_bucket_config(), n1=1, n2=1)
    mc = simulate_monte_carlo(_buckets(s, thin))
    bs = mc.buffer_survival
    assert bs is not None
    assert bs.count == 10_000
    assert 0.0 < bs.pct_cohorts_forced_to_sell_growth <= 1.0


# ---------- Gate F: forward-gap targets, fences, API round-trip ----------


def test_gate_f_forward_gap_shrinks_as_gi_lands():
    """The forward gap shrinks as guaranteed income arrives (GI-driven glide).

    gap = max(0, spending − GI). With $110k spend: $110k in the bridge (GI 0),
    $110k − $24,000 = $86,000 at 60 (A's pension), $110k − $33,000 = $77,000 at 65
    (both pensions), $110k − $66,000 = $44,000 at 67 (+ A's SS) and
    $110k − $98,240 = $11,760 at 70 (+ B's SS, $26,000 × 1.24). Strictly decreasing
    across the GI steps.
    """
    s = _buckets(default_scenario(), default_bucket_config())
    assert gap_for_age(s, 50) == pytest.approx(110_000.0)
    assert gap_for_age(s, 59) == pytest.approx(110_000.0)
    assert gap_for_age(s, 60) == pytest.approx(86_000.0)
    assert gap_for_age(s, 64) == pytest.approx(86_000.0)
    assert gap_for_age(s, 65) == pytest.approx(77_000.0)
    assert gap_for_age(s, 67) == pytest.approx(44_000.0)
    assert gap_for_age(s, 70) == pytest.approx(11_760.0)
    assert gap_for_age(s, 95) == pytest.approx(11_760.0)


def test_gate_f_cash_target_sums_forward_gap_years():
    """cash_target(a) = sum of the next N1 years of gap; income_target the next N2."""
    s = _buckets(default_scenario(), default_bucket_config())  # N1=2, N2=7
    # At 50 (bridge): cash = 2 × $110k; income = 7 × $110k (ages 52..58 all bridge).
    assert cash_target(s, 50) == pytest.approx(220_000.0)
    assert income_target(s, 50) == pytest.approx(770_000.0)


def test_gate_f_targets_clip_at_end_age():
    """The rolling windows clip to end_age (no out-of-horizon years summed)."""
    s = _buckets(default_scenario(), default_bucket_config())  # N1=2, N2=7
    # At 94, the 2-year cash window is just 94..95 → 2 × $11,760.
    assert cash_target(s, 94) == pytest.approx(2 * 11_760.0)
    assert cash_target(s, 95) == pytest.approx(11_760.0)
    # The income window past end_age is empty → 0.
    assert income_target(s, 95) == pytest.approx(0.0)


def test_gate_f_lifecycle_gap_uses_tapered_spending():
    """The forward gap reads lifecycle-tapered spending (not the flat go-go peak).

    Under lifecycle, slow-go (75–84) spends ×0.85 and no-go (85+) ×0.75, so the gap
    at those ages reflects the tapered spend net of GI — the same spending_for_age
    the engines use, so the bucket targets can't drift from the spending shape.
    Spending is raised to $140k here so the tapered spend still exceeds the
    default GI ($98,240 from 70) and the gap is visible.
    """
    s = replace(
        _buckets(default_scenario(), default_bucket_config()),
        spending_mode=SPENDING_MODE_LIFECYCLE,
        annual_spending=140_000.0,
    )
    # No-go age 90: spend = 140k × 0.75 = 105k; GI = 98,240 → gap = 6,760.
    assert gap_for_age(s, 90) == pytest.approx(105_000.0 - 98_240.0)
    # With Person B's SS raised to $40,000, GI = 106,000 > 105k → gap clamped to 0.
    assert gap_for_age(replace(s, person_b_ss_amount=40_000.0), 90) == pytest.approx(0.0)
    # Slow-go age 80: spend = 140k × 0.85 = 119k; GI 98,240 → gap = 20,760.
    assert gap_for_age(s, 80) == pytest.approx(119_000.0 - 98_240.0)


def test_gate_f_underfunded_cap_scales_safe_buckets_no_crash():
    """If the safe buckets can't be funded, scale them down so growth = 0 (no crash).

    A tiny portfolio with a deep safe target (N1=9, N2=20) can't fund the safe
    buckets; the engine scales cash + income proportionally so growth lands at 0 and
    runs without crashing (bucket-strategy design underfunded case).
    """
    from firebridgelab.buckets import _initial_balances, bucket_underfunded

    s = replace(default_scenario(), initial_portfolio=100_000.0)
    underfunded = _buckets(s, replace(default_bucket_config(), n1=9, n2=20))
    assert bucket_underfunded(underfunded) is True
    cash, income, growth = _initial_balances(underfunded)
    assert growth == pytest.approx(0.0)
    assert cash + income == pytest.approx(100_000.0)
    # And the deterministic projection runs (does not raise / crash).
    traj = simulate(underfunded)
    assert traj.points[0].age == 50


def test_gate_f_well_funded_not_underfunded():
    """The default ($1.8M, 10yr × $110k bridge) is NOT underfunded — growth > 0."""
    from firebridgelab.buckets import _initial_balances, bucket_underfunded

    s = _buckets(default_scenario(), default_bucket_config())
    assert bucket_underfunded(s) is False
    cash, income, growth = _initial_balances(s)
    assert growth > 0.0


def test_gate_f_guardrails_fence_engine_raises_typed():
    """Buckets + guardrails raises the typed BucketsGuardrailsNotComposable (engine)."""
    s = replace(
        _buckets(default_scenario(), default_bucket_config()),
        spending_mode=SPENDING_MODE_GUARDRAILS,
    )
    with pytest.raises(BucketsGuardrailsNotComposable):
        simulate_historical(s)
    with pytest.raises(BucketsGuardrailsNotComposable):
        simulate_monte_carlo(s)


def test_gate_f_solver_fence_buckets_raises_typed():
    """The max-spend solver is fenced in buckets mode (typed BucketsNotSolvable)."""
    s = _buckets(default_scenario(), default_bucket_config())
    with pytest.raises(BucketsNotSolvable):
        solve_max_spend(s, threshold=0.8, mode="historical")
    with pytest.raises(BucketsNotSolvable):
        solve_max_spend(s, threshold=1.0, mode="deterministic")


def test_gate_f_lifecycle_composes_with_buckets():
    """Buckets composes with lifecycle spending (constant/lifecycle are allowed)."""
    s = replace(
        _buckets(default_scenario(), default_bucket_config()),
        spending_mode=SPENDING_MODE_LIFECYCLE,
    )
    # Runs in all three modes without raising.
    assert simulate(s).points[0].age == 50
    assert simulate_historical(s).buffer_survival is not None
    assert simulate_monte_carlo(s).buffer_survival is not None


# ---------- Slice 1b: worst-cohort bucket-balance series (the visualization) ----------


def test_worst_bucket_cohort_series_matches_summed_fan_per_age():
    """The worst cohort's per-age cash+income+growth sums equal that cohort's own run.

    The drill-down series is sliced straight off the worst cohort's BucketTrajectory
    end-of-year balances — the SAME balances the aggregate fan summed — so re-running
    that exact cohort reproduces the series age-for-age (no drift between the
    drill-down and the engine's per-cohort math).
    """
    from firebridgelab.buckets import simulate_buckets_cohort
    from firebridgelab.historical import (
        _bucket_window,
        _cohort_blended_returns,
        _period_from_series,
    )

    s = default_scenario()
    sb = _buckets(s, default_bucket_config())
    h = simulate_historical(sb)
    wc = h.worst_bucket_cohort
    assert wc is not None
    assert len(wc.ages) == s.end_age - s.start_age + 1
    assert len(wc.cash) == len(wc.income) == len(wc.growth) == len(wc.ages)

    # Re-run the SAME (worst) cohort directly and compare age-for-age.
    bw, sel, start_years, sd = _bucket_window(sb)
    n_steps = s.end_age - s.start_age + 1
    cfg = sb.buckets
    period = _period_from_series(sd, sel, wc.start_year, n_steps)
    bret = {
        b.id: _cohort_blended_returns(bw[b.id], period)
        for b in (cfg.cash, cfg.income, cfg.growth)
    }
    traj = simulate_buckets_cohort(sb, bret)
    for k, y in enumerate(traj.years):
        assert wc.cash[k] == pytest.approx(y.cash)
        assert wc.income[k] == pytest.approx(y.income)
        assert wc.growth[k] == pytest.approx(y.growth)


def test_worst_bucket_cohort_is_earliest_forced_sale_when_breached():
    """When the buffer IS breached, the worst cohort = the earliest forced sale.

    A thin buffer (N1=1, N2=1) forces growth sales in bad cohorts; the selected
    worst cohort's first-forced-sale age then equals the aggregate
    ``worst_forced_sale_age`` headline (the earliest breach across all cohorts) —
    the design's "reuse the worst/earliest-forced-sale cohort" selection.
    """
    s = default_scenario()
    thin = replace(default_bucket_config(), n1=1, n2=1)
    h = simulate_historical(_buckets(s, thin))
    bs = h.buffer_survival
    wc = h.worst_bucket_cohort
    assert bs.worst_forced_sale_age is not None  # the buffer was breached somewhere
    assert wc is not None
    assert wc.first_forced_sale_age == bs.worst_forced_sale_age


def test_worst_bucket_cohort_falls_back_to_lowest_terminal_when_never_breached():
    """No forced sale anywhere → worst cohort = lowest terminal (the fallback path).

    Tests ``_select_worst_bucket_cohort`` directly with synthetic trajectories whose
    ``first_forced_sale_age`` is None for EVERY cohort (the buffer never breached
    anywhere). The earliest-forced-sale key then ties for all cohorts (the sentinel),
    so the deterministic tie-break must pick the LOWEST-terminal cohort, and its
    ``first_forced_sale_age`` is None — the buffer-never-breached fallback the chart
    relies on.

    Driven synthetically (not off a live run) because under the CORRECTED mechanic
    the default household always sells a sliver of growth in the FINAL year (age 95)
    in a few cohorts — the ride-don't-refill terminal-buffer artifact — so there is no
    real default config that breaches NOWHERE. The fallback CODE PATH still matters
    (a short-horizon / large-GI household can avoid the artifact), so we exercise it
    head-on rather than via a contrived scenario.
    """
    from firebridgelab.buckets import BucketTrajectory, BucketYear
    from firebridgelab.historical import _select_worst_bucket_cohort

    s = default_scenario()
    ages = list(range(s.start_age, s.end_age + 1))

    def _traj(terminal: float) -> BucketTrajectory:
        # All-in-growth flat trajectory ending at ``terminal``; never a forced sale.
        years = [
            BucketYear(
                age=a, cash=0.0, income=0.0, growth=terminal,
                spending=0.0, guaranteed_income=0.0,
                sourced_growth=0.0, forced_growth_sale=False,
            )
            for a in ages
        ]
        return BucketTrajectory(
            years=years, depleted_age=None,
            final_portfolio=terminal, first_forced_sale_age=None,
        )

    start_years = [1900, 1901, 1902]
    terminals = [3_000_000.0, 1_500_000.0, 4_000_000.0]  # 1901 is the lowest
    trajectories = [_traj(t) for t in terminals]
    wc = _select_worst_bucket_cohort(s, start_years, trajectories)
    assert wc is not None
    assert wc.first_forced_sale_age is None  # buffer never breached
    assert wc.start_year == 1901  # the lowest-terminal cohort
    assert wc.growth[-1] == pytest.approx(min(terminals))


def test_worst_bucket_cohort_lowest_terminal_among_earliest_breach_cohorts():
    """Real defeasance run: worst cohort = lowest terminal among earliest-breach cohorts.

    Under the corrected mechanic full bridge defeasance (N1=9, N2=20) still leaves a
    handful of cohorts selling a sliver of growth in the FINAL year (age 95 — the
    ride-don't-refill terminal artifact). The worst-cohort selection therefore picks
    among those earliest-(age-95)-breach cohorts, tie-broken to the LOWEST terminal —
    confirming the selection key (earliest forced sale, then lowest terminal) on a
    LIVE run, not just the synthetic fallback above.
    """
    from firebridgelab.buckets import simulate_buckets_cohort
    from firebridgelab.historical import (
        _bucket_window,
        _cohort_blended_returns,
        _period_from_series,
    )

    s = default_scenario()
    defease = replace(default_bucket_config(), n1=10, n2=20)
    sb = _buckets(s, defease)
    h = simulate_historical(sb)
    wc = h.worst_bucket_cohort
    bs = h.buffer_survival
    assert wc is not None
    # The selected worst cohort breached at the earliest breach age across all cohorts.
    assert wc.first_forced_sale_age == bs.worst_forced_sale_age

    # Among the cohorts that breached at that earliest age, the selected one has the
    # lowest terminal (the deterministic tie-break) — recompute every cohort directly.
    bw, sel, start_years, sd = _bucket_window(sb)
    n_steps = s.end_age - s.start_age + 1
    cfg = sb.buckets
    earliest = bs.worst_forced_sale_age
    earliest_breach_terminals = []
    for start_year in start_years:
        period = _period_from_series(sd, sel, start_year, n_steps)
        bret = {
            b.id: _cohort_blended_returns(bw[b.id], period)
            for b in (cfg.cash, cfg.income, cfg.growth)
        }
        traj = simulate_buckets_cohort(sb, bret)
        if traj.first_forced_sale_age == earliest:
            earliest_breach_terminals.append(traj.final_portfolio)
    selected_terminal = wc.cash[-1] + wc.income[-1] + wc.growth[-1]
    assert selected_terminal == pytest.approx(min(earliest_breach_terminals))


def test_single_sourcing_omits_worst_bucket_cohort():
    """Single sourcing leaves worst_bucket_cohort None (buckets-only field)."""
    assert simulate_historical(default_scenario()).worst_bucket_cohort is None


# ---------- Effective-equity glidepath (surfacing the imposed allocation) ----------


def _eq_share_at(glidepath, age):
    """The effective-equity share at ``age`` in a glidepath list-of-dicts."""
    return next(p["equity_share"] for p in glidepath if p["age"] == age)


def test_effective_equity_default_start_share_is_initial_growth_fraction():
    """DEFAULT ($110k) buckets: effective equity at 50 ≈ growth $810k / $1.8M = 45%.

    Cash + income are 0% equity (short Treasuries / agg bonds), growth is 100%
    equity (us_total_market). The begin-of-year (age-50) balances are the initial
    bucket balances: cash = 2×$110k, income = 7×$110k, growth = the $810k residual,
    so the effective-equity share is $810k / $1.8M = 0.45 exactly.
    """
    from firebridgelab import effective_equity_glidepath

    s = _buckets(default_scenario(), default_bucket_config())  # $110k, N1=2, N2=7
    gp = effective_equity_glidepath(s)
    assert _eq_share_at(gp, 50) == pytest.approx(0.45, abs=0.005)


def test_effective_equity_default_rises_toward_full_equity_late():
    """DEFAULT: the effective-equity share RISES toward ~100% by the late ages.

    As guaranteed income lands the gap-based safe targets collapse, so the
    over-funded safe side releases into growth and the effective-equity share climbs
    from 45% to nearly all-equity by the end of the horizon — the GI-driven rising-
    equity glide the gap sizing imposes.
    """
    from firebridgelab import effective_equity_glidepath

    s = _buckets(default_scenario(), default_bucket_config())
    gp = effective_equity_glidepath(s)
    # Monotone-ish rise: 50 < 67 < 95, ending near all-equity.
    assert _eq_share_at(gp, 50) < _eq_share_at(gp, 67) < _eq_share_at(gp, 95)
    assert _eq_share_at(gp, 95) > 0.95
    # Full per-age series spans the whole horizon.
    assert [p["age"] for p in gp] == list(range(s.start_age, s.end_age + 1))


def test_effective_equity_high_spend_start_share_lower():
    """HIGH SPEND ($140k) buckets: effective equity at 50 ≈ 30% (safe buckets eat more).

    A higher spend makes the forward gap — and the safe-bucket targets — larger, so
    growth (the residual) is a SMALLER slice of the portfolio at the start: cash =
    2×$140k, income = 7×$140k → safe = $1.26M, growth = $540k, share = $540k / $1.8M
    = 0.30. This is exactly the silent allocation the readout surfaces: the same
    $1.8M portfolio carries far less equity at a high spend than a single 60/40.
    """
    from firebridgelab import effective_equity_glidepath

    s = _buckets(replace(default_scenario(), annual_spending=140_000.0), default_bucket_config())
    gp = effective_equity_glidepath(s)
    assert _eq_share_at(gp, 50) == pytest.approx(0.30, abs=0.005)


def test_effective_equity_counts_equity_class_in_income_bucket():
    """Putting an EQUITY class (us_reit) in the income bucket raises effective equity.

    The equity weight of a bucket is the sum of its EQUITY_CLASS_IDS weights, so an
    income bucket that is half us_reit (an equity class) is no longer 0% equity —
    the age-50 effective-equity share rises above the all-bonds-income baseline.
    """
    from firebridgelab import EQUITY_CLASS_IDS, effective_equity_glidepath

    assert "us_reit" in EQUITY_CLASS_IDS  # REITs count as equity
    base_cfg = default_bucket_config()
    s_base = _buckets(default_scenario(), base_cfg)
    base_share = _eq_share_at(effective_equity_glidepath(s_base), 50)

    # Income bucket: 50% us_agg_bonds (not equity) / 50% us_reit (equity).
    reit_income = Bucket(
        "income",
        "Income",
        (
            AssetClass("us_agg_bonds", "US Bonds (10-yr Treasury proxy)", 0.5, 0.02, _DEFAULT_BOND_VOLATILITY),
            AssetClass("us_reit", "US REITs", 0.5, 0.07, _DEFAULT_EQUITY_VOLATILITY),
        ),
    )
    s_reit = _buckets(default_scenario(), replace(base_cfg, income=reit_income))
    reit_share = _eq_share_at(effective_equity_glidepath(s_reit), 50)
    assert reit_share > base_share
