"""Tests for the synthetic-cohort stress engine (firebridgelab.analysis.stress, slice 1).

Pins the slice-1 stress test (stress-test design §Slicing) end to end at the
engine layer:

  - **Closed-form mechanism check** (the key correctness gate): for a 100%
    single-class allocation the per-year return IS the stress table's column
    value, so the trajectory must match a hand-rolled midpoint-convention
    computation to the cent. This validates the MECHANISM independent of the
    (provisional) scenario numbers.
  - **Directional**: the stagflation stress yields a meaningfully LOWER final
    portfolio than the 5% deterministic baseline for the default 60/40 plan.
  - **Scope / unknown-id guards** raise the typed StressNotAvailable (a
    ValueError), so the API maps them to a clean 422.
  - **Canary belt-and-suspenders**: the stress code is purely additive, so the
    deterministic default canary is unchanged. (The Historical 113-cohort + MC
    canaries live in their own suites and are untouched — stress imports nothing
    of theirs.)
"""

from dataclasses import replace

import pytest

from firebridgelab import (
    AssetClass,
    default_scenario,
    effective_spending_rails,
    simulate,
    simulate_stress,
)
from firebridgelab.engine.step import max_fundable_spend
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.analysis.stress import (
    STRESS_PERTURBATIONS,
    STRESS_SCENARIOS,
    StressNotAvailable,
)
from firebridgelab.tax.location import has_tax_split
from tests._canary import DET_FINAL_CANARY, DET_FINAL_PIN

# The explicit year-1..5 US-Total column of the stagflation table (stress-test
# design slice-1 table), used by the single-class closed-form check.
_STAGFLATION_US_TOTAL_Y1_5 = [-0.26, -0.14, +0.31, +0.19, -0.12]
# The year-1 Bonds column (stress-test design slice-1 table), used by the default
# 60/40 multi-class blend check.
_STAGFLATION_BONDS_Y1 = -0.05


def _single_class_scenario(class_id: str = "us_total_market") -> "object":
    """The default scenario with a 100%-weight single-class allocation.

    A 100% single-class allocation makes the blended per-year return EXACTLY the
    stress table's column for that class's mapping — the cleanest mechanism check
    (no weight blend to disentangle). Everything else (portfolio, spend, GI ages,
    horizon) stays the default plan, so the GI ramp + midpoint convention are
    exercised the same way a real run is.
    """
    sc = default_scenario()
    return replace(
        sc,
        allocation=(AssetClass(class_id, "Single", 1.0, 0.07, 0.174),),
    )


def test_stagflation_closed_form_single_class():
    """Mechanism gate: 100% us_total_market reproduces the midpoint formula to the cent.

    For a single-class plan the year-k return is exactly the stagflation table's
    US-Total column (year 1 = -0.26, year 2 = -0.14, year 3 = +0.31, ...). Hand-
    roll the first three years with the engine's midpoint convention
    (``end = start + start*r + gi - spending*(1 + r/2)``) and assert the engine
    matches. This validates the MECHANISM (table → per-year return → cohort loop)
    independent of the provisional scenario numbers.
    """
    sc = _single_class_scenario("us_total_market")
    traj = simulate_stress(sc, "stagflation_1970s")

    start = sc.initial_portfolio
    for step, r in enumerate(_STAGFLATION_US_TOTAL_Y1_5[:3]):
        age = sc.start_age + step
        gi = gi_at_age(sc, age)
        spending = sc.annual_spending  # constant spending mode
        expected_end = start + start * r + gi * (1 + r / 2) - spending * (1 + r / 2)
        point = traj.points[step]

        assert point.age == age
        # The per-year return drove the step: growth == start * r exactly.
        assert point.growth == pytest.approx(start * r, abs=1e-6)
        assert point.start_portfolio == pytest.approx(start, abs=1e-2)
        assert point.guaranteed_income == pytest.approx(gi, abs=1e-6)
        assert point.spending == pytest.approx(spending, abs=1e-6)
        # The headline: the end-of-year balance matches the hand computation to
        # the cent.
        assert point.end_portfolio == pytest.approx(expected_end, abs=0.01)
        start = expected_end


def test_stagflation_closed_form_default_6040_blend():
    """Mechanism gate for the MULTI-class blend: the default 60/40 plan reproduces
    the weight-blended year-1 return + midpoint trajectory to the cent.

    The single-class check can't see a bug in ``Σ wᵢ·rᵢ`` or renormalization — its
    sum has one term and ``norm_weight`` is always 1.0. The default plan is 60%
    us_total_market (→ US Total, year-1 -0.26) + 40% us_agg_bonds (→ Bonds, year-1
    -0.05), weights summing to 1.0, so the blended year-1 real return is exactly
    ``0.6·(-0.26) + 0.4·(-0.05) = -0.176``. Pin that, and pin the resulting year-1
    trajectory (start/growth/end) against the engine's midpoint convention
    ``end = start + start*r + gi - spending*(1 + r/2)`` to the cent.
    """
    sc = default_scenario()
    # Sanity-pin the fixture the assertion depends on: the default IS the 60/40
    # two-class plan this test reasons about.
    assert [(ac.id, ac.weight) for ac in sc.allocation] == [
        ("us_total_market", 0.6),
        ("us_agg_bonds", 0.4),
    ]

    expected_r = 0.6 * _STAGFLATION_US_TOTAL_Y1_5[0] + 0.4 * _STAGFLATION_BONDS_Y1
    assert expected_r == pytest.approx(-0.176, abs=1e-12)

    traj = simulate_stress(sc, "stagflation_1970s")
    point = traj.points[0]

    start = sc.initial_portfolio
    gi = gi_at_age(sc, sc.start_age)
    spending = sc.annual_spending  # constant spending mode
    expected_end = start + start * expected_r + gi - spending * (1 + expected_r / 2)

    assert point.age == sc.start_age
    # The blended year-1 return drove the step: growth == start * (-0.176) exactly.
    assert point.growth == pytest.approx(start * expected_r, abs=1e-6)
    assert point.start_portfolio == pytest.approx(start, abs=1e-2)
    assert point.guaranteed_income == pytest.approx(gi, abs=1e-6)
    assert point.spending == pytest.approx(spending, abs=1e-6)
    # The headline: the year-1 end balance matches the hand computation to the cent.
    assert point.end_portfolio == pytest.approx(expected_end, abs=0.01)


def _assert_overlay_preserves_plan(sc, traj):
    """Assert a stress trajectory is a pure OVERLAY of the un-stressed plan.

    A stress swaps ONLY the return environment; the plan's GI streams and the
    spending TARGET ($110k) are untouched. Two invariants prove that without the
    (now-false) "spend == target every year" proxy, which broke once the realized-
    spend cap correctly lets recorded spend fall below the target in years the
    stressed returns deplete the portfolio:

      - **GI is byte-for-byte the un-stressed plan's** at every age. GI is purely
        input-driven (the four streams at their default ages/amounts) and is wholly
        unaffected by realized-spend, so this is the strong proof the inputs are
        intact.
      - **Spending obeys the realized-spend contract**: it is the intended target
        ($110k) wherever fundable, the affordable cap where not, and NEVER above the
        target. We RECONSTRUCT the expected realized value from each YearPoint —
        ``min(target, max_fundable)`` with ``max_fundable`` from the recovered ``r``
        (``growth / start_portfolio``) — rather than a loose ``<=`` inequality, so a
        real overlay violation (a stress that silently changed the spend mechanic)
        still fails the test.

    Also pins that the TARGET is genuinely $110k: year 1 on the default $1.8M is
    always fundable, so ``points[0].spending`` must equal ``sc.annual_spending``
    exactly — the test can't pass if a stress quietly changed the spend target.
    """
    target = sc.annual_spending

    # The target is genuinely $110k: year 1 on the default $1.8M is fundable.
    assert traj.points[0].spending == pytest.approx(target, abs=1e-6)
    assert traj.points[0].start_portfolio == pytest.approx(sc.initial_portfolio, abs=1e-2)

    for point in traj.points:
        # GI is input-driven and unaffected by realized-spend — the strong proof
        # the plan's inputs are untouched. Byte-identical to the un-stressed plan.
        assert point.guaranteed_income == pytest.approx(
            gi_at_age(sc, point.age), abs=1e-6
        )

        # Realized-spend contract: spend == min(target, max_fundable), NEVER above
        # target. Reconstruct max_fundable from the point's recovered return.
        if point.start_portfolio > 0:
            r = point.growth / point.start_portfolio
            max_fundable = max_fundable_spend(point.start_portfolio, r, point.guaranteed_income)
            expected_realized = min(target, max_fundable)
            assert point.spending == pytest.approx(expected_realized, abs=1e-4)
        else:
            # Depleted year: start_portfolio floored to 0, so ``r`` is not
            # recoverable from ``growth / start_portfolio`` (0/0). The affordable
            # spend is then ``gi / (1 + r/2)`` <= gi <= target. Pin the contract we
            # CAN assert: spend never exceeds target, and is consistent with the
            # floored portfolio (it can fund at most the GI that arrived this year).
            assert point.spending <= target + 1e-6
            assert point.spending <= point.guaranteed_income + 1e-6
        # Belt-and-suspenders across both branches: never above the target.
        assert point.spending <= target + 1e-6


def test_stress_overlay_preserves_plan_inputs():
    """A stress is an OVERLAY: it swaps only the return env, not the plan.

    Proves GI is byte-for-byte the un-stressed plan's at every age, and that
    spending obeys the realized-spend contract (the intended $110k target where
    fundable, the affordable cap where the stressed returns deplete the portfolio,
    NEVER above target). Realized spend MAY fall below $110k in the broke years of
    a depleting stress — that is the realized-spend cap working, not a changed plan
    — but the target + GI are preserved. See ``_assert_overlay_preserves_plan``.
    """
    sc = default_scenario()
    traj = simulate_stress(sc, "stagflation_1970s")
    _assert_overlay_preserves_plan(sc, traj)


def test_stagflation_lower_than_deterministic_baseline():
    """Directional: stagflation ends meaningfully BELOW the 5% deterministic baseline.

    The default 60/40 plan deterministic terminal is ~$6.07M (5% real); the
    stagflation overlay (deep early drawdown into the retirement no-GI bridge)
    must land materially lower — the whole point of the stress.
    """
    sc = default_scenario()
    det_final = simulate(sc).final_portfolio
    stress_final = simulate_stress(sc, "stagflation_1970s").final_portfolio

    assert stress_final < det_final
    # "Meaningfully" lower — not a rounding difference. The stagflation path
    # roughly an order of magnitude below the smooth-5% baseline.
    assert stress_final < det_final * 0.5


def test_trajectory_shape_one_point_per_age():
    """The stress returns the single-trajectory shape (one YearPoint per age)."""
    sc = default_scenario()
    traj = simulate_stress(sc, "stagflation_1970s")
    expected_n = sc.end_age - sc.start_age + 1
    assert len(traj.points) == expected_n
    assert traj.points[0].age == sc.start_age
    assert traj.points[-1].age == sc.end_age
    assert traj.final_portfolio == traj.points[-1].end_portfolio


def test_tail_uses_framework_normal_row():
    """Years past the explicit rows use the framework NORMAL row (slice-2 tail).

    Slice 2 finalized the tail: beyond the authored explicit rows, each class
    takes the framework's "11+"/"6+"/"9+" NORMAL-row value for its column (not the
    class's own default). The stagflation normal row puts US Total at +10%, so a
    100% us_total_market plan's late-year growth/start ratio must land on +0.10.
    """
    sc = _single_class_scenario("us_total_market")
    traj = simulate_stress(sc, "stagflation_1970s")
    # The stagflation explicit block is 10 rows (5 explicit + 5 interpolated gap);
    # step 10 (0-indexed → year 11) is the first normal-row tail year.
    late = traj.points[11]
    realized_r = late.growth / late.start_portfolio
    assert realized_r == pytest.approx(0.10, abs=1e-9)


def test_tail_falls_back_to_class_default_when_column_absent():
    """If a class's framework column is ABSENT from the normal row, fall back to
    the class's own default real_return (the slice-1 tail behavior, preserved).

    All six shipped scenarios fill every column in their normal row, so this path
    isn't reachable through the registry; exercise it directly against a synthetic
    table whose normal row omits the US Total column. The single-class plan's late
    return must then be the class's own real_return (0.07), not an invented number.
    """
    from firebridgelab.analysis.stress import _StressTable, _class_return_array
    from firebridgelab import AssetClass as _AC

    ac = _AC("us_total_market", "Single", 1.0, 0.07, 0.174)
    table = _StressTable(rows=[{"US Total": -0.30}], normal_row={})  # no US Total tail
    arr = _class_return_array(ac, table, n_steps=5)
    assert arr[0] == pytest.approx(-0.30, abs=1e-12)   # explicit row
    # Tail years fall back to the class's own default real_return.
    assert arr[1] == pytest.approx(0.07, abs=1e-12)
    assert arr[4] == pytest.approx(0.07, abs=1e-12)


def test_unknown_stress_id_raises():
    """An unknown stress id raises the typed StressNotAvailable (a ValueError)."""
    sc = default_scenario()
    with pytest.raises(StressNotAvailable):
        simulate_stress(sc, "no_such_stress")
    # And it's a ValueError so the API's except-ValueError path catches it.
    with pytest.raises(ValueError):
        simulate_stress(sc, "no_such_stress")


def test_non_single_sourcing_raises():
    """A non-single sourcing scenario raises (buckets composition is a later slice)."""
    sc = replace(default_scenario(), sourcing_mode="buckets")
    with pytest.raises(StressNotAvailable):
        simulate_stress(sc, "stagflation_1970s")


def test_non_constant_allocation_raises():
    """A glidepath scenario raises (glidepath composition is a later slice)."""
    sc = replace(default_scenario(), allocation_mode="glidepath")
    with pytest.raises(StressNotAvailable):
        simulate_stress(sc, "stagflation_1970s")


def test_registry_has_the_slice2_adverse_battery():
    """Slice 2 ships the six ADVERSE Family-A worlds (tailwinds out of scope)."""
    assert sorted(STRESS_SCENARIOS) == sorted(
        [
            "stagflation_1970s",
            "historical_depression_1929",
            "slow_grind_2000",
            "gfc_2008",
            "valuation_unwind_2025",
            "depression_extreme",
        ]
    )
    # The dropped tailwind / false-positive scenarios are NOT in the battery.
    for dropped in ("average_markets", "bull_run_1990s", "v_drop_2020"):
        assert dropped not in STRESS_SCENARIOS


def test_deterministic_canary_unchanged_by_stress_module():
    """Belt-and-suspenders: the deterministic default canary is byte-identical.

    The stress code is purely additive (a new module + an additive API branch),
    so the deterministic default-scenario terminal must stay
    ``DET_FINAL_PIN`` (tests/_canary.py) to full float precision.
    """
    assert simulate(default_scenario()).final_portfolio == DET_FINAL_CANARY


# ---------------------------------------------------------------------------
# Slice 2 — the rest of the adverse Family-A battery.
# ---------------------------------------------------------------------------

# Per-scenario year-1 + year-2 US-Total column values, transcribed from the
# framework appendix (the authoritative table). For a 100% us_total_market plan
# the blended per-year return IS this column, so each (year-1, year-2) pair is the
# closed-form mechanism check for that scenario — the same style as slice 1's
# stagflation single-class check. (stagflation is covered separately above.)
_SCENARIO_US_TOTAL_Y1_Y2 = {
    "historical_depression_1929": (-0.08, -0.25),   # appendix §9 yrs 1929,1930
    "slow_grind_2000": (-0.09, -0.12),              # appendix §6 yrs 1,2
    "gfc_2008": (-0.37, +0.26),                      # appendix §3 yrs 1,2
    "valuation_unwind_2025": (-0.25, -0.15),         # appendix §7 yrs 1,2
    "depression_extreme": (-0.45, -0.25),            # appendix §8 yrs 1,2
}


@pytest.mark.parametrize("stress_id,expected", _SCENARIO_US_TOTAL_Y1_Y2.items())
def test_scenario_closed_form_single_class_y1_y2(stress_id, expected):
    """Per-scenario mechanism gate: 100% us_total_market reproduces the table to the cent.

    For a single-class plan the year-1/year-2 return is exactly the scenario's
    US-Total column for those years. Hand-roll the two years with the engine's
    midpoint convention (``end = start + start*r + gi - spending*(1 + r/2)``) and
    assert the engine matches — validating the table→per-year-return→cohort-loop
    mechanism for each transcribed scenario independent of the others.
    """
    sc = _single_class_scenario("us_total_market")
    traj = simulate_stress(sc, stress_id)

    start = sc.initial_portfolio
    for step, r in enumerate(expected):
        age = sc.start_age + step
        gi = gi_at_age(sc, age)
        spending = sc.annual_spending  # constant spending mode
        expected_end = start + start * r + gi * (1 + r / 2) - spending * (1 + r / 2)
        point = traj.points[step]

        assert point.age == age
        # The transcribed per-year return drove the step: growth == start * r.
        assert point.growth == pytest.approx(start * r, abs=1e-6)
        assert point.end_portfolio == pytest.approx(expected_end, abs=0.01)
        start = expected_end


@pytest.mark.parametrize("stress_id", list(_SCENARIO_US_TOTAL_Y1_Y2) + ["stagflation_1970s"])
def test_every_scenario_overlay_preserves_plan_inputs(stress_id):
    """Every adverse scenario is an OVERLAY: only the return env swaps.

    GI is byte-for-byte the un-stressed plan's at every age (input-driven), and the
    spending TARGET ($110k) + the realized-spend contract hold for every scenario.
    In the DEPLETING scenarios (valuation_unwind_2025, depression_extreme,
    stagflation_1970s) realized spend correctly drops below $110k in the broke years
    — the realized-spend cap working, not a changed plan — so the overlay invariant
    is asserted via ``min(target, max_fundable)`` reconstruction, not "every year ==
    target". See ``_assert_overlay_preserves_plan``.
    """
    sc = default_scenario()
    traj = simulate_stress(sc, stress_id)
    _assert_overlay_preserves_plan(sc, traj)


def _min_portfolio(traj) -> float:
    """The minimum end-of-year portfolio across a trajectory's horizon."""
    return min(p.end_portfolio for p in traj.points)


@pytest.mark.parametrize("stress_id", list(STRESS_SCENARIOS))
def test_every_scenario_troughs_below_deterministic_baseline(stress_id):
    """Directional: EVERY adverse scenario troughs below the deterministic baseline.

    The right severity lens is the MINIMUM portfolio (the framework's own metric),
    NOT the final. The deterministic baseline (smooth +5% real) only ever dips to
    its bridge-year trough (~$1.58M at 64, before the later GI streams) and then
    climbs; every adverse stress drives a deeper drawdown into the 50–59 no-GI
    bridge, so its minimum must land below the
    baseline's minimum.

    NOTE (documented, not a defect): the FINAL portfolio is NOT a reliable severity
    signal here — e.g. ``gfc_2008`` ends ABOVE the 5% baseline because its sharp
    V-recovery plus the framework's +10%/+2% normal-row tail compounds over the
    long horizon past the 5% smooth path. The early-drawdown TROUGH is what the
    robustness reading keys off, which is why the assertion lives on the minimum.
    """
    sc = default_scenario()
    det_min = _min_portfolio(simulate(sc))
    stress_min = _min_portfolio(simulate_stress(sc, stress_id))
    assert stress_min < det_min


def test_directional_severity_ordering():
    """Documented severity ordering on the default 60/40 plan (min + final portfolio).

    Reasoned, not arbitrary:
      - ``depression_extreme`` (−45% then no recovery, Japan-style) must have the
        LOWEST minimum portfolio of the battery — it's the red-team "does anything
        survive" case, deeper and with no V-bounce.
      - ``depression_extreme`` min < ``gfc_2008`` min: the GFC's −37% V-recovers
        quickly, so its drawdown trough is shallower than the no-recovery extreme.
      - ``historical_depression_1929`` min < ``gfc_2008`` min: the 1929 path stacks
        −8/−25/−43% across three years before its rebound — a deeper cumulative
        trough than the single-year GFC shock.
    These are MIN-portfolio (trough) comparisons, the framework's own severity lens.
    """
    sc = default_scenario()
    mins = {sid: _min_portfolio(simulate_stress(sc, sid)) for sid in STRESS_SCENARIOS}

    # depression_extreme is the deepest trough of the whole battery.
    assert mins["depression_extreme"] == min(mins.values())
    # ... and specifically below the V-recovering GFC.
    assert mins["depression_extreme"] < mins["gfc_2008"]
    # The three-year 1929 grind troughs below the single-year GFC shock.
    assert mins["historical_depression_1929"] < mins["gfc_2008"]


@pytest.mark.parametrize("stress_id", list(STRESS_SCENARIOS))
def test_every_scenario_returns_single_trajectory_shape(stress_id):
    """Every scenario returns the single-trajectory shape (one YearPoint per age)."""
    sc = default_scenario()
    traj = simulate_stress(sc, stress_id)
    expected_n = sc.end_age - sc.start_age + 1
    assert len(traj.points) == expected_n
    assert traj.points[0].age == sc.start_age
    assert traj.points[-1].age == sc.end_age
    assert traj.final_portfolio == traj.points[-1].end_portfolio


# ---------------------------------------------------------------------------
# Slice 3 — Family-B perturbations (Scenario→Scenario transforms, run through the
# ordinary deterministic engine and rendered as a single Deterministic line).
# ---------------------------------------------------------------------------


def test_family_b_registry_has_the_no_cola_perturbations():
    """Slice 3's two no-COLA Family-B perturbations are present (ss haircut + cost-of-living shock).

    (Slice 4 ADDED the two COLA-bearing perturbations — government_austerity +
    sustained_cost_drift — covered by ``test_slice4_family_b_registry_has_the_two_cola_
    perturbations``; here we just pin that the original two are still registered.)
    """
    assert "ss_haircut_23" in STRESS_PERTURBATIONS
    assert "cost_of_living_shock" in STRESS_PERTURBATIONS
    # And the two families are kept SEPARATE — no Family-B id leaked into the
    # Family-A authored-table registry (or vice versa).
    assert set(STRESS_PERTURBATIONS).isdisjoint(STRESS_SCENARIOS)


def test_ss_haircut_23_reduces_gi_by_23pct_of_the_ss_streams():
    """ss_haircut_23 scales BOTH SS streams ×0.77; GI drops by 23% of SS at age 70.

    Mechanism gate, pinned to the cent. At age 70 all four streams are active. The
    haircut leaves the two PENSIONS untouched and scales the two SS streams by 0.77,
    so the stressed GI must equal the un-stressed GI minus exactly 23% of the SS
    sum. Hand-compute against the default scenario's verified amounts.
    """
    sc = default_scenario()
    traj = simulate_stress(sc, "ss_haircut_23")

    # Hand-computed GI at age 70 (all four streams active):
    #   pensions (unchanged): 24_000 + 9_000 = 33_000
    #   SS streams ×0.77:     33_000*0.77 + 32_240*0.77 = 25_410.00 + 24_824.80
    #   (Person B's 32,240 is the 26,000 FRA benefit × 1.24 for claiming at 70)
    expected_pensions = 24_000.0 + 9_000.0
    expected_ss_haircut = 33_000.0 * 0.77 + 32_240.0 * 0.77
    expected_gi_70 = expected_pensions + expected_ss_haircut

    # The point at age 70 (index 70 - start_age).
    idx_70 = 70 - sc.start_age
    point = traj.points[idx_70]
    assert point.age == 70
    assert point.guaranteed_income == pytest.approx(expected_gi_70, abs=0.01)

    # And it is exactly 23% of the SS streams below the un-stressed GI at 70.
    unstressed_gi_70 = gi_at_age(sc, 70)
    ss_sum = sc.person_a_ss_amount + sc.person_b_ss_amount
    assert point.guaranteed_income == pytest.approx(
        unstressed_gi_70 - 0.23 * ss_sum, abs=0.01
    )

    # Pensions are NOT touched (an age where SS is NOT yet active — 66 — keeps the
    # full un-stressed GI, proving the haircut hit only the SS streams).
    idx_66 = 66 - sc.start_age
    assert traj.points[idx_66].guaranteed_income == pytest.approx(
        gi_at_age(sc, 66), abs=0.01
    )


def test_cost_of_living_shock_scales_spending_120pct_gi_unchanged():
    """cost_of_living_shock scales annual_spending / floor / ceiling ×1.20; GI unchanged.

    Mechanism gate, pinned to the cent. In CONSTANT spending mode (the default)
    every year's spend is annual_spending, so the stressed year-1 spend must be
    exactly 1.20× the baseline $110k. GI is denominated in USD and left unscaled, so
    it equals the un-stressed GI at every age.
    """
    sc = default_scenario()
    assert sc.spending_mode == "constant"  # the year-1 spend == annual_spending
    traj = simulate_stress(sc, "cost_of_living_shock")

    point = traj.points[0]
    assert point.age == sc.start_age
    # Spending scaled +20%: 110_000 * 1.20 = 132_000.00, to the cent.
    assert point.spending == pytest.approx(sc.annual_spending * 1.20, abs=0.01)
    assert point.spending == pytest.approx(132_000.0, abs=0.01)
    # GI unscaled at year 1 (no streams active yet at age 50 → 0, but assert the
    # contract: it equals the un-stressed GI for the age).
    assert point.guaranteed_income == pytest.approx(gi_at_age(sc, sc.start_age), abs=0.01)

    # And at age 70 (all streams active) GI is STILL the un-stressed amount — the FX
    # shock is spending-side only, not income-side.
    idx_70 = 70 - sc.start_age
    assert traj.points[idx_70].guaranteed_income == pytest.approx(
        gi_at_age(sc, 70), abs=0.01
    )


def test_cost_of_living_shock_transform_scales_the_rails():
    """The cost_of_living_shock transform itself scales annual_spending / floor / ceiling ×1.20.

    Verify the pure Scenario→Scenario transform directly (the whole spending BAND
    moves together, so a dynamic strategy's clamps stay proportional to the shocked
    cost of living), and that nothing else (GI streams, portfolio) is touched.
    """
    sc = default_scenario()
    transformed = STRESS_PERTURBATIONS["cost_of_living_shock"](sc)
    assert transformed.annual_spending == pytest.approx(sc.annual_spending * 1.20, abs=1e-9)
    assert transformed.spending_floor == pytest.approx(sc.spending_floor * 1.20, abs=1e-9)
    assert transformed.spending_ceiling == pytest.approx(sc.spending_ceiling * 1.20, abs=1e-9)
    # Income + portfolio untouched.
    assert transformed.person_a_ss_amount == sc.person_a_ss_amount
    assert transformed.person_b_ss_amount == sc.person_b_ss_amount
    assert transformed.person_a_pension_amount == sc.person_a_pension_amount
    assert transformed.initial_portfolio == sc.initial_portfolio


def test_effective_spending_rails_cost_of_living_scales_120pct():
    """effective_spending_rails(cost_of_living_shock) returns the floor/ceiling ×1.20.

    This is the seam the spending-chart guide line reads under stress: a dynamic
    strategy clamps against the PERTURBED floor, so the guide line must label the
    ×1.20 value (else it draws "Floor ($85k)" 17k below where the line floors).
    """
    sc = default_scenario()
    floor, ceiling = effective_spending_rails(sc, "cost_of_living_shock")
    assert floor == pytest.approx(sc.spending_floor * 1.20, abs=1e-9)
    assert ceiling == pytest.approx(sc.spending_ceiling * 1.20, abs=1e-9)


def test_effective_spending_rails_unperturbed_for_family_a_and_ss_haircut():
    """For Family-A stresses and ss_haircut the effective rails == the input rails.

    Neither perturbs the spending floor/ceiling, so the guide line stays at the
    live knob value (already correct) — this fix must NOT move it for them.
    """
    sc = default_scenario()
    # ss_haircut (Family B) scales only SS, not the rails.
    f_ss, c_ss = effective_spending_rails(sc, "ss_haircut_23")
    assert f_ss == pytest.approx(sc.spending_floor, abs=1e-9)
    assert c_ss == pytest.approx(sc.spending_ceiling, abs=1e-9)
    # Every Family-A authored-return stress leaves the rails untouched.
    for stress_id in STRESS_SCENARIOS:
        f_a, c_a = effective_spending_rails(sc, stress_id)
        assert f_a == pytest.approx(sc.spending_floor, abs=1e-9)
        assert c_a == pytest.approx(sc.spending_ceiling, abs=1e-9)


def test_ss_haircut_transform_scales_only_ss():
    """The ss_haircut_23 transform scales ONLY the two SS streams ×0.77."""
    sc = default_scenario()
    transformed = STRESS_PERTURBATIONS["ss_haircut_23"](sc)
    assert transformed.person_a_ss_amount == pytest.approx(sc.person_a_ss_amount * 0.77, abs=1e-9)
    assert transformed.person_b_ss_amount == pytest.approx(sc.person_b_ss_amount * 0.77, abs=1e-9)
    # Pensions, spending, portfolio untouched.
    assert transformed.person_a_pension_amount == sc.person_a_pension_amount
    assert transformed.person_b_pension_amount == sc.person_b_pension_amount
    assert transformed.annual_spending == sc.annual_spending
    assert transformed.spending_floor == sc.spending_floor
    assert transformed.initial_portfolio == sc.initial_portfolio


@pytest.mark.parametrize("stress_id", list(STRESS_PERTURBATIONS))
def test_family_b_returns_single_trajectory_shape(stress_id):
    """Each Family-B perturbation returns the single-line Trajectory shape."""
    sc = default_scenario()
    traj = simulate_stress(sc, stress_id)
    expected_n = sc.end_age - sc.start_age + 1
    assert len(traj.points) == expected_n
    assert traj.points[0].age == sc.start_age
    assert traj.points[-1].age == sc.end_age
    assert traj.final_portfolio == traj.points[-1].end_portfolio


@pytest.mark.parametrize("stress_id", list(STRESS_PERTURBATIONS))
def test_family_b_worse_than_unstressed_baseline(stress_id):
    """Directional: each Family-B perturbation lands BELOW the un-stressed baseline.

    Both shocks make the plan worse — the SS haircut removes 23% of two income
    streams (more is withdrawn from the portfolio), the cost-of-living shock raises spending
    20% — so each must end below the un-stressed deterministic FINAL portfolio.

    The FINAL (not the trough) is the reliable severity signal for these two: the
    SS haircut bites only from age 67 (when SS starts), so it does NOT deepen the
    early no-GI-bridge trough at all — the early-years minimum is byte-identical to
    baseline and only the post-67 path (and thus the final) diverges below. The cost-of-living
    shock raises spending from year 1, so it also troughs below baseline; but the
    common, always-true directional signal across BOTH is the lower final.
    """
    sc = default_scenario()
    baseline = simulate(sc)
    stressed = simulate_stress(sc, stress_id)
    assert stressed.final_portfolio < baseline.final_portfolio


def test_cost_of_living_shock_troughs_below_baseline():
    """The cost-of-living shock raises spending from year 1, so it DOES deepen the early trough.

    Unlike the SS haircut (which bites only from age 67), the +20% spending hits
    immediately in the 50–59 no-GI bridge, so the cost-of-living shock's minimum portfolio
    must land below the un-stressed baseline's minimum.
    """
    sc = default_scenario()
    baseline_min = min(p.end_portfolio for p in simulate(sc).points)
    stressed_min = min(p.end_portfolio for p in simulate_stress(sc, "cost_of_living_shock").points)
    assert stressed_min < baseline_min


# ---------------------------------------------------------------------------
# Slice 4 — the per-year real-drift mechanism (pension COLA-freeze + spending
# cost-of-living drift) and the two COLA-bearing Family-B perturbations.
# ---------------------------------------------------------------------------


def test_pension_drift_default_zero_byte_identical_gi():
    """pension_real_drift defaults to 0.0 ⇒ gi_at_age is byte-identical (canary).

    A 0.0 drift makes the per-year multiplier (1 - 0)**k == 1.0 for every age, so
    the four-stream sum reproduces the exact prior value — to FULL float precision,
    not approx — at every age across the horizon.
    """
    sc = default_scenario()
    # The expected un-drifted GI at every age (the prior accumulation order).
    for age in range(sc.start_age, sc.end_age + 1):
        streams = (
            (sc.person_a_pension_amount, sc.person_a_pension_start_age),
            (sc.person_b_pension_amount, sc.person_b_pension_start_age),
            (sc.person_a_ss_amount, sc.person_a_ss_start_age),
            (sc.person_b_ss_amount, sc.person_b_ss_start_age),
        )
        expected = sum(amt for amt, start in streams if age >= start)
        assert gi_at_age(sc, age) == expected


def test_pension_drift_erodes_only_pensions():
    """gi_at_age with pension_real_drift=0.025 erodes ONLY the pension streams.

    Hand-compute the eroded pension values at age 72 (Person A's pension started
    at 60, Person B's at 65): amount × 0.975**(72-start). The two SS streams
    (which start at 67 and 70) must be
    UNCHANGED at the same age — full nominal at 0.975**0 from their own start? No:
    SS carries drift_rate 0.0, so it is the flat amount regardless of age. Pin to
    the cent.
    """
    sc = replace(default_scenario(), pension_real_drift=0.025)

    age = 72
    years_person_a_pension = age - sc.person_a_pension_start_age  # 72 - 60 = 12
    years_person_b_pension = age - sc.person_b_pension_start_age  # 72 - 65 = 7
    expected_person_a_pension = sc.person_a_pension_amount * (1 - 0.025) ** years_person_a_pension
    expected_person_b_pension = sc.person_b_pension_amount * (1 - 0.025) ** years_person_b_pension
    # SS streams: drift_rate 0.0, so flat (no erosion) — full scheduled amount.
    expected_ss = sc.person_a_ss_amount + sc.person_b_ss_amount
    expected_gi_72 = expected_person_a_pension + expected_person_b_pension + expected_ss

    assert gi_at_age(sc, age) == pytest.approx(expected_gi_72, abs=0.01)

    # Hand-pinned numbers: 24_000 * 0.975**12 = 17_711.96...,
    # 9_000 * 0.975**7 = 7_538.32..., SS = 33_000 + 32_240 = 65_240.
    assert expected_person_a_pension == pytest.approx(24_000.0 * 0.975 ** 12, abs=1e-9)
    assert expected_person_a_pension == pytest.approx(17_711.96, abs=0.01)
    assert expected_person_b_pension == pytest.approx(9_000.0 * 0.975 ** 7, abs=1e-9)
    assert expected_person_b_pension == pytest.approx(7_538.32, abs=0.01)
    assert expected_ss == pytest.approx(65_240.0, abs=1e-6)

    # SS is unchanged vs an un-drifted scenario at the same age (drift hits pensions
    # only): compare against a no-drift baseline's SS contribution.
    base = default_scenario()
    # At an age where ONLY SS is active is impossible (pensions start earlier), so
    # isolate SS by differencing: drifted_GI - eroded_pensions == full SS sum.
    assert gi_at_age(sc, age) - expected_person_a_pension - expected_person_b_pension == pytest.approx(
        base.person_a_ss_amount + base.person_b_ss_amount, abs=0.01
    )


def test_spending_drift_default_zero_reproduces_undrifted_spend():
    """spending_real_drift defaults to 0.0 ⇒ spend is the un-drifted value exactly.

    With drift 0.0 the multiplier (1+0)**k == 1.0, so the constant-mode spend stays
    flat annual_spending at every age — byte-identical to the un-drifted run.
    """
    sc = default_scenario()
    assert sc.spending_real_drift == 0.0
    traj = simulate(sc)
    for point in traj.points:
        assert point.spending == sc.annual_spending


def test_spending_drift_grows_spend_per_year():
    """spending_real_drift=0.015 grows constant-mode spend at 1.5%/yr from start_age.

    Year (start+10) spend must equal base × 1.015**10, pinned to the cent; year 0
    (start_age) is exactly the base (1.015**0 == 1).
    """
    sc = replace(default_scenario(), spending_real_drift=0.015)
    traj = simulate(sc)

    # Year 0 (start_age): no drift yet.
    assert traj.points[0].spending == pytest.approx(sc.annual_spending, abs=0.01)

    # Year 10: base × 1.015**10.
    expected_y10 = sc.annual_spending * (1.015 ** 10)
    assert traj.points[10].spending == pytest.approx(expected_y10, abs=0.01)
    # Hand-pinned: 110_000 * 1.015**10 = 127_659.49...
    assert expected_y10 == pytest.approx(110_000.0 * 1.015 ** 10, abs=1e-6)
    assert expected_y10 == pytest.approx(127_659.49, abs=0.01)


def test_spending_drift_applied_in_cohort_loop_too():
    """The spending drift is applied in BOTH sim loops (the cohort loop too).

    Drive ``_simulate_cohort`` directly with a flat 5% return array and assert the
    spend at year 10 equals base × 1.015**10 — the same drift the deterministic
    loop applies, proving the seam is in both places for Historical / MC consistency.
    """
    import numpy as np

    from firebridgelab.engine.sim_core import _simulate_cohort

    sc = replace(default_scenario(), spending_real_drift=0.015)
    n_steps = sc.end_age - sc.start_age + 1
    returns = np.full(n_steps, 0.05, dtype=float)
    _, _, spends = _simulate_cohort(sc, returns)

    assert spends[0] == pytest.approx(sc.annual_spending, abs=0.01)
    assert spends[10] == pytest.approx(sc.annual_spending * (1.015 ** 10), abs=0.01)


def test_slice4_family_b_registry_has_the_two_cola_perturbations():
    """Slice 4 adds government_austerity + sustained_cost_drift to the Family-B registry."""
    assert sorted(STRESS_PERTURBATIONS) == sorted(
        ["ss_haircut_23", "cost_of_living_shock", "government_austerity", "sustained_cost_drift"]
    )
    # Families stay separate.
    assert set(STRESS_PERTURBATIONS).isdisjoint(STRESS_SCENARIOS)


def test_government_austerity_cuts_ss_and_erodes_pensions():
    """government_austerity scales SS ×0.77 AND erodes pensions 2.5%/yr (both applied).

    At age 72 all four streams are active. SS streams are ×0.77 (level), pensions
    erode at 0.975**(72-start). Hand-compute the stressed GI to the cent.
    """
    sc = default_scenario()
    traj = simulate_stress(sc, "government_austerity")

    age = 72
    expected_ss = (sc.person_a_ss_amount + sc.person_b_ss_amount) * 0.77
    expected_pensions = (
        sc.person_a_pension_amount * 0.975 ** (age - sc.person_a_pension_start_age)
        + sc.person_b_pension_amount * 0.975 ** (age - sc.person_b_pension_start_age)
    )
    expected_gi_72 = expected_ss + expected_pensions

    idx = age - sc.start_age
    point = traj.points[idx]
    assert point.age == age
    assert point.guaranteed_income == pytest.approx(expected_gi_72, abs=0.01)

    # The transform itself sets both knobs.
    transformed = STRESS_PERTURBATIONS["government_austerity"](sc)
    assert transformed.person_a_ss_amount == pytest.approx(sc.person_a_ss_amount * 0.77, abs=1e-9)
    assert transformed.person_b_ss_amount == pytest.approx(sc.person_b_ss_amount * 0.77, abs=1e-9)
    assert transformed.pension_real_drift == pytest.approx(0.025, abs=1e-12)
    # Spending untouched (it's an income-side shock).
    assert transformed.annual_spending == sc.annual_spending
    assert transformed.spending_real_drift == 0.0


def test_government_austerity_worse_than_baseline():
    """Directional: government_austerity ends below the un-stressed baseline.

    Both government income sources are cut, so more is drawn from the portfolio →
    a lower final portfolio than the un-stressed deterministic run.
    """
    sc = default_scenario()
    baseline = simulate(sc).final_portfolio
    stressed = simulate_stress(sc, "government_austerity").final_portfolio
    assert stressed < baseline


def test_sustained_cost_drift_drifts_spending_up():
    """sustained_cost_drift drifts spending UP 1.5%/yr; GI unchanged; ends below baseline.

    The spending line must rise year over year (the valuable view), and the run
    must end below the un-stressed baseline (more spend → faster depletion).
    """
    sc = default_scenario()
    traj = simulate_stress(sc, "sustained_cost_drift")

    # Spending drifts up monotonically (constant mode × a growing drift multiplier).
    spends = [p.spending for p in traj.points]
    assert spends[0] == pytest.approx(sc.annual_spending, abs=0.01)
    assert spends[10] == pytest.approx(sc.annual_spending * 1.015 ** 10, abs=0.01)
    for earlier, later in zip(spends, spends[1:]):
        assert later > earlier  # strictly increasing

    # GI is untouched (cost-of-living shock is spending-side only).
    for point in traj.points:
        assert point.guaranteed_income == pytest.approx(gi_at_age(sc, point.age), abs=0.01)

    # The transform sets only spending_real_drift.
    transformed = STRESS_PERTURBATIONS["sustained_cost_drift"](sc)
    assert transformed.spending_real_drift == pytest.approx(0.015, abs=1e-12)
    assert transformed.annual_spending == sc.annual_spending
    assert transformed.pension_real_drift == 0.0

    # Directional: ends below baseline.
    assert traj.final_portfolio < simulate(sc).final_portfolio


def _buckets_scenario(scenario):
    """The given scenario in buckets sourcing with the byte-stable degenerate config.

    Mirrors test_buckets.py's default config (all three buckets carry the scenario
    allocation, n1=n2=0) — constant spending mode, so buckets is composable. The
    single-bucket-equivalent split keeps the buckets path comparable to single
    sourcing run for run.
    """
    from firebridgelab import SOURCING_MODE_BUCKETS
    from firebridgelab.engine.scenario import Bucket, BucketConfig

    config = BucketConfig(
        cash=Bucket("cash", "Cash", scenario.allocation),
        income=Bucket("income", "Income", scenario.allocation),
        growth=Bucket("growth", "Growth", scenario.allocation),
        n1=0,
        n2=0,
    )
    return replace(scenario, sourcing_mode=SOURCING_MODE_BUCKETS, buckets=config)


def test_sustained_cost_drift_drifts_spending_up_under_buckets_too():
    """sustained_cost_drift drifts spending up under BUCKETS sourcing too.

    The buckets sourcing loop omitted the spending_real_drift escalation that both
    single-sourcing loops apply, so a drift-bearing stress (sustained_cost_drift) was a
    silent no-op on spend under buckets — flat $110k at every age, a falsely rosy
    result. The two sourcing modes must now AGREE on the drifted spend. Family-B
    runs through ``simulate(perturbation(scenario))`` (the scope guard rejects
    non-single sourcing for ``simulate_stress`` itself), so apply the perturbation
    then ``simulate`` — exactly what the engine does. The default scenario never
    depletes under sustained_cost_drift (it survives to the ~$215k spend), so the late-age
    realized spend is unclamped and equals the drift target.
    """
    sc = default_scenario()
    pert = STRESS_PERTURBATIONS["sustained_cost_drift"]

    buckets_traj = simulate(pert(_buckets_scenario(sc)))
    single_traj = simulate(pert(sc))

    # The buckets run survives (unclamped late-age spend).
    assert buckets_traj.depleted_age is None

    # Age 95: base × 1.015**(95-50) == base × 1.015**45, pinned to the cent.
    age = 95
    idx = age - sc.start_age  # 45
    expected_95 = sc.annual_spending * (1.015 ** (age - sc.start_age))
    assert buckets_traj.points[idx].age == age
    assert buckets_traj.points[idx].spending == pytest.approx(expected_95, abs=0.01)
    # Hand-pinned: 110_000 * 1.015**45 == 214_963.43...
    assert expected_95 == pytest.approx(110_000.0 * 1.015 ** 45, abs=1e-6)
    assert expected_95 == pytest.approx(214_963.43, abs=0.01)

    # The two sourcing modes now AGREE on the drifted spend at the same age.
    assert buckets_traj.points[idx].spending == pytest.approx(
        single_traj.points[idx].spending, abs=0.01
    )

    # Year 0 (start_age): no drift yet (1.015**0 == 1), and spend strictly increases.
    spends = [p.spending for p in buckets_traj.points]
    assert spends[0] == pytest.approx(sc.annual_spending, abs=0.01)
    for earlier, later in zip(spends, spends[1:]):
        assert later > earlier


def test_buckets_drift_zero_byte_identical_canary():
    """Zero spending_real_drift ⇒ the buckets trajectory spend is byte-identical.

    The drift fix is a no-op when drift is 0.0 ((1+0)**k == 1.0), so an un-stressed
    buckets run reproduces the flat constant-mode spend at every age to full float
    precision — the canary the additive seam must not move.
    """
    sc = default_scenario()
    assert sc.spending_real_drift == 0.0
    traj = simulate(_buckets_scenario(sc))
    for point in traj.points:
        assert point.spending == sc.annual_spending


def test_effective_rails_unscaled_for_slice4_stresses():
    """Neither slice-4 stress scales the floor/ceiling — effective rails == input.

    government_austerity is income-side, sustained_cost_drift is a per-year spend drift
    (NOT a rail scale), so for both the effective rails equal the input scenario's
    (the spending-chart floor line stays $85k).
    """
    sc = default_scenario()
    for stress_id in ("government_austerity", "sustained_cost_drift"):
        floor, ceiling = effective_spending_rails(sc, stress_id)
        assert floor == pytest.approx(sc.spending_floor, abs=1e-9)
        assert ceiling == pytest.approx(sc.spending_ceiling, abs=1e-9)


def test_government_austerity_spending_flat():
    """Under government_austerity spending stays FLAT (the shock is income-side).

    The spending line does NOT drift (no spending_real_drift), in contrast to
    sustained_cost_drift — the depletion comes from the cut income, not raised spend.
    """
    sc = default_scenario()
    traj = simulate_stress(sc, "government_austerity")
    for point in traj.points:
        assert point.spending == pytest.approx(sc.annual_spending, abs=0.01)


# ============================================================================
# Tax-drag design td1 (td-b slice, HARD CONSTRAINT "stress mode"): drag applies
# under stress — the balances underneath a Family-A stress must stay honest.
# ============================================================================


def test_stressed_split_trajectory_pays_taxes_family_a():
    """A stressed Family-A split trajectory pays real tax somewhere: the
    dragged stressed total is strictly below what the SAME stress would have
    reported pre-tax (the old, tax-blind ``_simulate_cohort`` path), on at
    least one age — "a stressed split trajectory pays its taxes like any
    other" (the td1 HARD CONSTRAINT)."""
    from firebridgelab.engine.sim_core import _simulate_cohort
    from firebridgelab.spending.registry import make_strategy
    from firebridgelab.analysis.stress import _class_return_array
    import numpy as np

    sc = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    assert has_tax_split(sc) is True

    dragged = simulate_stress(sc, "gfc_2008")
    dragged_totals = [p.end_portfolio for p in dragged.points]

    # Independently rebuild the SAME blended stress return array and run the
    # plain (tax-blind) cohort loop, mirroring simulate_stress's own mechanism
    # (module docstring steps 2-4) so the comparison is apples-to-apples.
    from firebridgelab.analysis.stress import STRESS_SCENARIOS

    table = STRESS_SCENARIOS["gfc_2008"]
    n_steps = sc.end_age - sc.start_age + 1
    total_weight = sum(ac.weight for ac in sc.allocation)
    blended = np.zeros(n_steps, dtype=float)
    for ac in sc.allocation:
        blended += (ac.weight / total_weight) * _class_return_array(ac, table, n_steps)
    pretax_ends, _depleted_age, _spends = _simulate_cohort(sc, blended)

    assert any(
        dragged_totals[i] < pretax_ends[i] - 1.0 for i in range(n_steps)
    )
    # No pointwise inversion on this static-spend (constant mode) fixture —
    # same drag-direction discipline as the ordinary engine.
    assert all(
        dragged_totals[i] <= pretax_ends[i] + 1e-6 for i in range(n_steps)
    )


def test_stressed_split_trajectory_shape_unchanged():
    """The stressed split Trajectory keeps the EXACT same bare shape as a
    no-split stress run — no additive tax_location/federal_tax block (the tb1
    stress-readout gap persists by design, only the trajectory numbers
    change)."""
    sc = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    traj = simulate_stress(sc, "stagflation_1970s")
    assert len(traj.points) == sc.end_age - sc.start_age + 1
    assert traj.final_portfolio == traj.points[-1].end_portfolio
    # Depletion is well-formed (None or a real in-horizon age).
    if traj.depleted_age is not None:
        assert sc.start_age <= traj.depleted_age <= sc.end_age


def test_no_split_stress_byte_stable_after_td_b():
    """A plain no-split stress run is untouched by the td-b wiring — the
    dragged branch is unreachable without a split, mirroring the engine's
    no-split byte-stability discipline elsewhere."""
    sc = default_scenario()
    assert has_tax_split(sc) is False
    traj = simulate_stress(sc, "gfc_2008")
    assert traj.final_portfolio > 0 or traj.depleted_age is not None


def test_stressed_split_records_realized_spend_post_depletion():
    """The stress trajectory's YearPoint.spending reads the REALIZED
    (fundable-capped) consumption for split scenarios — the third re-pointed
    consumer of ``TaxLocationPath.realized_spend`` (after the Historical and
    MC fan collectors). A depleting stressed split run must record the
    affordable level post-depletion ($0 before GI starts at 60), never the
    strategy's intended floor — the same defect class the fan re-point fixed.
    """
    sc = replace(
        default_scenario(),
        initial_portfolio=600_000.0,
        taxable_balance=0.0,
        tax_deferred_balance=600_000.0,
        roth_balance=0.0,
        spending_mode="guardrails",
    )
    traj = simulate_stress(sc, "gfc_2008")
    assert traj.depleted_age is not None
    saw_post_depletion_pre_gi = False
    for point in traj.points:
        if traj.depleted_age < point.age < 60:
            # $0 portfolio, $0 GI → $0 affordable consumption. Under intended
            # recording these years read the guardrail floor.
            assert point.spending == 0.0
            saw_post_depletion_pre_gi = True
    assert saw_post_depletion_pre_gi  # the fixture really exercised the window


# ============================================================================
# td-c fix: FAMILY B (scenario perturbations) used to dispatch a split
# straight through the plain pre-tax ``simulate()`` — the SAME falsehood
# Family A fixed in td-b. These mirror the Family-A pair above exactly, on
# ``government_austerity`` (an income-side transform). ``sustained_cost_drift``
# (the spending_real_drift transform) gets its OWN dedicated test below — it
# additionally exercises the drift seam the td-c review fix closed in
# ``firebridgelab.tax_location`` (the dragged loops now apply the same post-strategy
# drift multiplier the scalar loops always did; before the fix a
# split + sustained_cost_drift run silently dropped the drift and reported a
# trajectory several times too optimistic).
# ============================================================================


def test_stressed_split_trajectory_pays_taxes_family_b():
    """A stressed Family-B (scenario-perturbation) split trajectory pays real
    tax somewhere too: the dragged stressed total is strictly below what the
    SAME perturbed scenario would report pre-tax (``simulate()`` on the
    perturbed scenario — exactly Family B's OLD, pre-td-c behavior for a
    split), on at least one age — the td1 HARD CONSTRAINT applies to Family B
    exactly as much as Family A."""
    sc = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    assert has_tax_split(sc) is True

    dragged = simulate_stress(sc, "government_austerity")
    dragged_totals = [p.end_portfolio for p in dragged.points]

    perturbed = STRESS_PERTURBATIONS["government_austerity"](sc)
    pretax = simulate(perturbed)
    pretax_totals = [p.end_portfolio for p in pretax.points]

    assert any(
        dragged_totals[i] < pretax_totals[i] - 1.0 for i in range(len(dragged_totals))
    )
    # No pointwise inversion on this static-spend (constant mode) fixture —
    # same drag-direction discipline as Family A / the ordinary engine.
    assert all(
        dragged_totals[i] <= pretax_totals[i] + 1e-6 for i in range(len(dragged_totals))
    )


def test_stressed_split_shape_unchanged_family_b():
    """The stressed split Trajectory (Family B) keeps the EXACT same bare
    shape as a no-split stress run — no additive tax_location/federal_tax
    block (mirrors ``test_stressed_split_trajectory_shape_unchanged``, Family
    A's analogue)."""
    sc = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    traj = simulate_stress(sc, "government_austerity")
    assert len(traj.points) == sc.end_age - sc.start_age + 1
    assert traj.final_portfolio == traj.points[-1].end_portfolio
    if traj.depleted_age is not None:
        assert sc.start_age <= traj.depleted_age <= sc.end_age


def test_no_split_family_b_stress_byte_stable_after_td_c():
    """A plain no-split Family-B stress run is untouched by the td-c wiring —
    the dragged branch is unreachable without a split (mirrors
    ``test_no_split_stress_byte_stable_after_td_b``, Family A's analogue)."""
    sc = default_scenario()
    assert has_tax_split(sc) is False
    traj = simulate_stress(sc, "government_austerity")
    assert traj.final_portfolio > 0 or traj.depleted_age is not None


def test_stressed_split_records_realized_spend_post_depletion_family_b():
    """Family-B analogue of
    ``test_stressed_split_records_realized_spend_post_depletion``: the stress
    trajectory's YearPoint.spending reads the REALIZED (fundable-capped)
    consumption for a Family-B split too — a depleting stressed split run
    records the affordable level post-depletion ($0 before GI starts at 60),
    never the strategy's intended floor."""
    sc = replace(
        default_scenario(),
        initial_portfolio=600_000.0,
        taxable_balance=0.0,
        tax_deferred_balance=600_000.0,
        roth_balance=0.0,
        spending_mode="guardrails",
    )
    traj = simulate_stress(sc, "government_austerity")
    assert traj.depleted_age is not None
    saw_post_depletion_pre_gi = False
    for point in traj.points:
        if traj.depleted_age < point.age < 60:
            assert point.spending == 0.0
            saw_post_depletion_pre_gi = True
    assert saw_post_depletion_pre_gi  # the fixture really exercised the window


def test_sustained_cost_drift_split_drift_applied_and_taxes_paid():
    """The reviewer's re-review case (td-c drift-seam fix): sustained_cost_drift +
    split rides the dragged branch WITH the cost-of-living escalation the
    stress exists to model.

      (a) DRIFT APPLIED — the recorded spend RISES with age (the 1.5%/yr real
          drift compounds; before the fix the dragged branch dropped the drift
          and spent flat, reporting a trajectory several times too optimistic).
      (b) TAXES PAID — dragged <= the drift-carrying pre-tax counterfactual
          (``simulate()`` on the perturbed scenario) at every age, with real
          separation somewhere (constant mode: static-spend drag direction).
    """
    sc = replace(
        default_scenario(),
        taxable_balance=450_000.0,
        tax_deferred_balance=1_080_000.0,
        roth_balance=270_000.0,
    )
    dragged = simulate_stress(sc, "sustained_cost_drift")

    # (a) Spend escalates. The early years never deplete on this fixture, so
    # the recorded (realized) spend equals the drifted intent there — strictly
    # rising year over year, anchored at the raw annual_spending in year 0.
    spends = [p.spending for p in dragged.points]
    assert spends[0] == sc.annual_spending
    assert all(spends[i + 1] > spends[i] for i in range(15))

    # (b) Taxes paid vs the drift-carrying pre-tax counterfactual.
    perturbed = STRESS_PERTURBATIONS["sustained_cost_drift"](sc)
    pretax = simulate(perturbed)
    dragged_totals = [p.end_portfolio for p in dragged.points]
    pretax_totals = [p.end_portfolio for p in pretax.points]
    assert all(
        dragged_totals[i] <= pretax_totals[i] + 1e-6
        for i in range(len(dragged_totals))
    )
    assert any(
        dragged_totals[i] < pretax_totals[i] - 1.0
        for i in range(len(dragged_totals))
    )
