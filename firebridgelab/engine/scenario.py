"""The plan inputs: the ``Scenario`` dataclass and its defaults.

Defines ``Scenario`` (horizon, portfolio, allocation, spending, the four per-person
guaranteed-income streams, and the optional strategy, tax, stress and survivor
fields), the 3-bucket ``Bucket`` / ``BucketConfig`` types, and ``default_scenario()``.
All monetary values are real (today's) dollars. Every optional field defaults to
the plain behaviour (constant spending, one blended balance, no tax split, no fee),
so a field left at its default has no effect on the result.
"""

from dataclasses import dataclass
from typing import Optional

from firebridgelab.engine.allocation import (
    ALLOCATION_MODE_CONSTANT,
    AssetClass,
    CLASS_MENU,
    DEFAULT_CORRELATION,
    _legacy_default_volatility,
)
from firebridgelab.income.pension import PENSION_COLA_FULL
from firebridgelab.income.social_security import effective_ss_amount
from firebridgelab.spending.modes import SPENDING_MODE_CONSTANT


# Lifecycle-spending phase defaults (the "go-go / slow-go / no-go" pattern):
# annual_spending is the GO-GO (peak) level and the two later phases scale DOWN
# from it. Phases are age-anchored (not years-since-retirement) because the taper
# tracks activity/health and the default household retires early at 50.
# Boundaries are the FIRST age of each later phase (inclusive lower bound).
_SLOW_GO_START_AGE = 75   # ages 75-84 → slow-go
_NO_GO_START_AGE = 85     # ages 85+   → no-go
_SLOW_GO_MULTIPLIER = 0.85
_NO_GO_MULTIPLIER = 0.75

MC_DISTRIBUTION_NORMAL = "normal"
MC_DISTRIBUTION_STUDENT_T = "student_t"
MC_DISTRIBUTIONS = (MC_DISTRIBUTION_NORMAL, MC_DISTRIBUTION_STUDENT_T)

# Valid sourcing_mode values. "single" (default) holds ONE blended portfolio
# balance. "buckets" splits the portfolio into a cash / income / growth
# time-segmentation (the 3-bucket sourcing strategy), where withdrawals are sourced
# cash-first and the safe buckets are refilled ONE-SIDEDLY (rebalance in up years,
# ride in down years). The toggle is orthogonal to `mode`, `spending_mode`, and
# `allocation_mode`. In buckets mode the bucket allocations SUPERSEDE the
# single/glidepath allocation (glidepath is ignored, not an error).
SOURCING_MODE_SINGLE = "single"
SOURCING_MODE_BUCKETS = "buckets"

# Default bucket sizing in YEARS OF THE FORWARD GAP: the cash bucket targets N1
# years of gap, the income bucket the next N2 years. Gap = spending − guaranteed
# income, so as GI lands the safe targets shrink (the emergent GI-driven
# rising-equity glide). User-editable.
_DEFAULT_BUCKET_N1 = 2  # cash bucket: years of forward gap
_DEFAULT_BUCKET_N2 = 7  # income bucket: years of forward gap (after the cash years)

# Guyton-Klinger guardrail defaults (read only by the guardrail strategy):
#   GUARD  = the symmetric band half-width around the initial portfolio-funded
#            withdrawal rate (0.20 → trip a cut above rate×1.20, a raise below
#            rate×0.80).
#   ADJUST = the per-trip proportional spend adjustment (0.10 → ±10% on the
#            UNtapered base when a band trips). The dial that spans nudge→jump:
#            ADJUST→1.0 makes the first trip a de-facto jump to the rail.
#   floor  = the OPERATIONAL total-spending floor in real dollars ($85k — the
#            lowest level the plan will accept); guardrail spending clamps here
#            and never goes below it. Flat (not activity-tapered) — see the rails
#            note in GuardrailStrategy. The
#            $45k ABSOLUTE_FLOOR below is a survivable reference level; it is
#            context (a fainter reference line on the fan), NOT the clamp.
#   ceiling = the satiation cap in real dollars ($150k), the most QoL a year can
#            absorb; bounds the upside nudge. Activity-tapered like spending.
_GUARDRAIL_GUARD = 0.20
_GUARDRAIL_ADJUST = 0.10
_GUARDRAIL_FLOOR = 85_000.0
_GUARDRAIL_CEILING = 150_000.0

# The ABSOLUTE / survivable floor: a viable-but-modest reference level. It is
# NOT the operational clamp (that's ``spending_floor``, default $85k above); it is drawn
# on the spending fan as a fainter "absolute floor (survivable)" reference line
# for context, so the operational floor reads against the survivable one. Pinned
# (not a knob): it's the structural GI-backed level, not a user dial.
ABSOLUTE_FLOOR = 45_000.0

# The default household's Social Security benefits at full retirement age (67).
# default_scenario() stores the EFFECTIVE benefit at each claim age.
DEFAULT_PERSON_A_SS_FRA = 33_000.0
DEFAULT_PERSON_B_SS_FRA = 26_000.0


@dataclass(frozen=True)
class Bucket:
    """One time-segmentation bucket in the 3-bucket sourcing strategy.

    Each bucket holds its OWN asset allocation (the same ``tuple[AssetClass, ...]``
    representation the single / glidepath path uses, drawn from the existing 12-class
    menu) — the cash bucket is short Treasuries, the income bucket aggregate bonds, the
    growth bucket total market by default, all user-editable. ``id`` is the stable key
    (``cash`` / ``income`` / ``growth``), ``name`` the display label. The bucket's real
    return at a given age is the weight-weighted blend of its allocation (Deterministic
    geometric; Historical cohort per-class; MC sampled per-class).

    The cash + income buckets are SIZED in years of the forward gap (N1 / N2 on
    BucketConfig); the growth bucket is the remainder of the portfolio (it has no
    size target). So years-target is a property of the CONFIG, not the bucket, and
    is held there rather than duplicated per bucket.
    """

    id: str                              # stable key: "cash" / "income" / "growth"
    name: str                            # display label
    allocation: tuple["AssetClass", ...]  # this bucket's own asset allocation


@dataclass(frozen=True)
class BucketConfig:
    """The 3-bucket sourcing configuration.

    Three buckets — cash / income / growth — each with its own allocation, plus the
    forward-gap sizing ``n1`` (cash years) and ``n2`` (income years). Held as a
    single frozen field on the Scenario (``buckets``; ``None`` in single mode).
    ``n1`` / ``n2`` and the per-bucket allocations are user-editable. All tuples
    keep the frozen Scenario hashable / immutable.
    """

    cash: Bucket
    income: Bucket
    growth: Bucket
    n1: int = _DEFAULT_BUCKET_N1  # cash bucket: years of forward gap
    n2: int = _DEFAULT_BUCKET_N2  # income bucket: years of forward gap


def default_bucket_config() -> BucketConfig:
    """The default 3-bucket config: cash = 100% short Treasuries, income = 100% US
    aggregate bonds, growth = 100% US total market, with N1=2 cash years and N2=7 income
    years.

    The cash bucket pulls its class params (return, volatility) from ``CLASS_MENU``
    (``st_treasury`` — its data-derived geometric-mean default), exactly like the
    single-allocation matrix. The income (``us_agg_bonds``) and growth
    (``us_total_market``) classes are the two LEGACY classes, which are NOT in
    CLASS_MENU (they live only in the default scenario's length-2 allocation — see
    ``engine.allocation._build_class_menu``); so they reuse the SAME canonical
    legacy params the default scenario carries: real_return 2%/7% and
    volatility from ``_legacy_default_volatility()`` (the exact
    series-derived stdev, not the rounded ``_DEFAULT_BOND_VOLATILITY`` /
    ``_DEFAULT_EQUITY_VOLATILITY`` fallback constants). That makes a single bucket
    holding the default allocation reproduce the single-balance return exactly.
    Each bucket allocation is a length-1, 100%-weight tuple.
    """
    cash_class = CLASS_MENU["st_treasury"]
    return BucketConfig(
        cash=Bucket(
            id="cash",
            name="Cash",
            allocation=(
                AssetClass(
                    cash_class.id, cash_class.label,
                    1.0, cash_class.real_return, cash_class.volatility,
                ),
            ),
        ),
        income=Bucket(
            id="income",
            name="Income",
            allocation=(
                AssetClass(
                    "us_agg_bonds", "US Bonds (10-yr Treasury proxy)",
                    1.0, 0.02, _legacy_default_volatility("us_agg_bonds"),
                ),
            ),
        ),
        growth=Bucket(
            id="growth",
            name="Growth",
            allocation=(
                AssetClass(
                    "us_total_market", "US Total Stock Market",
                    1.0, 0.07, _legacy_default_volatility("us_total_market"),
                ),
            ),
        ),
        n1=_DEFAULT_BUCKET_N1,
        n2=_DEFAULT_BUCKET_N2,
    )


@dataclass(frozen=True)
class Scenario:
    """Inputs to a single deterministic projection.

    All monetary values are real (today's) dollars; inflation is handled
    implicitly by working in real terms (``assumed_inflation`` is read only by
    the pension-inflation fields).
    """

    # Identity / horizon
    start_age: int
    end_age: int

    # Portfolio
    initial_portfolio: float
    # Asset-class allocation. The portfolio's real return is the
    # weight-weighted average of the classes' real returns (see
    # engine.allocation.blended_real_return).
    allocation: tuple["AssetClass", ...]

    # Spending (real dollars per year, constant)
    annual_spending: float

    # Guaranteed income streams (real dollars per year, activating at the
    # listed age and continuing thereafter). Split per-person (Person A /
    # Person B) so the two can claim independently — e.g. Person B delays SS to
    # 70 while Person A claims at 67. Four named streams; the engine sums
    # whichever are active at a given age. (Not a generic stream abstraction —
    # exactly these four.)
    person_a_pension_amount: float
    person_a_pension_start_age: int
    person_b_pension_amount: float
    person_b_pension_start_age: int
    person_a_ss_amount: float
    person_a_ss_start_age: int
    person_b_ss_amount: float
    person_b_ss_start_age: int

    # Spending shape (lifecycle / go-go-slow-go-no-go). "constant" (default)
    # spends annual_spending flat in real terms. "lifecycle" treats annual_spending
    # as the GO-GO peak and scales the two later phases down by their multipliers
    # (see spending.lifecycle.spending_for_age). Other modes are listed in
    # spending.modes.
    spending_mode: str = SPENDING_MODE_CONSTANT
    slow_go_start_age: int = _SLOW_GO_START_AGE
    no_go_start_age: int = _NO_GO_START_AGE
    slow_go_multiplier: float = _SLOW_GO_MULTIPLIER
    no_go_multiplier: float = _NO_GO_MULTIPLIER

    # Guyton-Klinger guardrail knobs. ``guard_band`` / ``guard_adjust`` are read
    # only by the ``guardrails`` strategy (spending.rules.guardrails); the
    # floor/ceiling rails are also read by the funded-ratio and amortization
    # strategies. ``guard_band``
    # / ``guard_adjust`` are the symmetric band half-width + per-trip proportional
    # adjustment; ``spending_floor`` (default $85k) is the real-dollar OPERATIONAL
    # floor — "we don't dip below this", the clamp; ``spending_ceiling`` is the
    # satiation ceiling. ($45k is the absolute/survivable floor — ``ABSOLUTE_FLOOR``
    # above — context, not the clamp.)
    guard_band: float = _GUARDRAIL_GUARD
    guard_adjust: float = _GUARDRAIL_ADJUST
    spending_floor: float = _GUARDRAIL_FLOOR
    spending_ceiling: float = _GUARDRAIL_CEILING

    # Cross-class real-return correlation, used by the Monte Carlo sampler and the
    # rebalanced compound return (returns.sampling). SCALAR FAST-PATH for N <= 2 ONLY: a
    # single equity-bond scalar broadcast onto the implied 2x2 matrix. For the default
    # allocation (2 classes) this is exactly the equity-bond pair, and its Cholesky
    # factor correlates the sampled draws. Defaults to the full-history Shiller figure.
    # The Historical engine never reads it. The valid range is [-0.99, +0.99] (the API
    # guards |correlation| > 0.99, since exactly +/-1 is rank-deficient and would break
    # the Cholesky factor).
    #
    # For N > 2 this scalar is NEVER broadcast (a uniform off-diagonal is a
    # silent wrong default, not an error) — the sampler requires an explicit
    # ``correlations`` matrix and raises a clear error if one is missing.
    correlation: float = DEFAULT_CORRELATION

    # Optional ordered N x N correlation matrix, aligned to allocation order
    # (row/col i is allocation[i]). When present and N > 2, the MC sampler uses
    # this matrix instead of the scalar fast-path. Held as a tuple-of-tuples so
    # the frozen Scenario stays hashable / immutable; ``None`` means "no explicit
    # matrix" (the N <= 2 scalar fast-path applies; N > 2 with no matrix is an
    # error). The default scenario leaves it None.
    correlations: Optional[tuple[tuple[float, ...], ...]] = None

    # Allocation shape (glidepath / bond-tent). "constant" (default) holds the
    # single ``allocation`` above across all ages. "glidepath" carries one
    # allocation per DERIVED phase in ``phase_allocations`` (the rising-equity /
    # bond-tent glide), and the portfolio's real return at a given age is the
    # blend of THAT age's phase's allocation (see
    # engine.allocation.blended_real_return_at_age).
    allocation_mode: str = ALLOCATION_MODE_CONSTANT
    # Per-phase allocations, one tuple-of-AssetClass per DERIVED phase, in phase
    # order (the same phase order ``engine.allocation.glidepath_boundaries``
    # produces). Read ONLY
    # when ``allocation_mode == "glidepath"``; ``None`` under constant mode (the
    # single ``allocation`` carries). Held as a tuple-of-tuples so the frozen
    # Scenario stays hashable / immutable. The count is NOT pinned to the field —
    # the engine derives the phase boundaries from the GI ages at run time and maps
    # each phase to its allocation by index (see blended_real_return_at_age), so a
    # mismatch is clamped to the last supplied phase rather than crashing.
    phase_allocations: Optional[tuple[tuple["AssetClass", ...], ...]] = None

    # Withdrawal SOURCING shape. "single" (default) holds the single blended
    # balance above across all years. "buckets" splits the portfolio into the
    # 3-bucket cash/income/growth time-segmentation in ``buckets`` and sources
    # withdrawals cash-first with a one-sided refill (engine.buckets). In buckets
    # mode the bucket allocations supersede the single/glidepath allocation
    # (``allocation_mode`` is ignored, not an error).
    sourcing_mode: str = SOURCING_MODE_SINGLE
    # The 3-bucket configuration, read ONLY when ``sourcing_mode == "buckets"``.
    # ``None`` under single mode (the single ``allocation`` carries). Held as a
    # frozen ``BucketConfig`` so the Scenario stays immutable / hashable.
    buckets: Optional["BucketConfig"] = None

    # Tax-location split. Three optional account balances that partition
    # ``initial_portfolio`` by TAX LOCATION (a DIFFERENT axis from the
    # cash/income/growth ``buckets`` above — never conflate them). The feature is
    # OFF when all three are ``None`` (the default): the engine runs the
    # undifferentiated single pool. When ANY is supplied, all three are treated as
    # supplied (a missing one is 0) and they must sum to ``initial_portfolio``
    # within $1 (validated in tax.location.resolve_split). When supplied, a
    # PARALLEL "shadow" decomposition tracks the three balances through the sim
    # with access-aware sourcing (taxable → roth pre-59½, taxable → tax_deferred →
    # roth post-59½); it never perturbs the scalar math. Read ONLY in single
    # sourcing mode (no-op under sourcing_mode == "buckets").
    taxable_balance: Optional[float] = None
    tax_deferred_balance: Optional[float] = None
    roth_balance: Optional[float] = None

    # Taxable-gains fraction. The flat share of each taxable-account WITHDRAWAL treated
    # as long-term capital gain for the federal-tax overlay (the remainder is basis,
    # tax-free). A single user-set knob rather than real cost-basis tracking (one fewer
    # state variable); consequence: the REAL gains fraction drifts upward as the account
    # compounds, so a static knob understates late-plan LTCG. Read ONLY by
    # ``tax.overlay.compute_tax_overlay``. Validated 0..1 in
    # ``engine.validation.validate_scenario``.
    taxable_gains_fraction: float = 0.5

    # Per-year REAL-drift knobs (COLA / cost-of-living stresses). Both default to
    # 0.0, which makes the per-year multiplier 1.0 (a no-op). Only the
    # inflation-bearing Family-B stresses set them (government_austerity,
    # sustained_cost_drift); the base engine stays full-COLA real.
    #   pension_real_drift — annual REAL erosion rate of the two PENSION streams only
    #     (the FERS COLA-freeze model). When > 0 each pension stream's real value
    #     decays at this rate from its OWN start age: a stream active at ``age``
    #     contributes ``amount * (1 - pension_real_drift) ** (age - start_age)``. The
    #     two SS streams are LEFT UNCHANGED. Applied in ``income.guaranteed.gi_at_age``
    #     (the single canonical GI sum), so the erosion flows everywhere consistently.
    #   spending_real_drift — annual REAL growth rate of SPENDING (cost-of-living
    #     drift). When > 0 spending grows at this rate per year from ``start_age``:
    #     the funded spend is multiplied by ``(1 + spending_real_drift) ** (age -
    #     start_age)`` AFTER the strategy computes it, BEFORE ``step_year`` — a post-
    #     strategy cost-of-living multiplier that composes with ANY spending strategy.
    pension_real_drift: float = 0.0
    spending_real_drift: float = 0.0

    # Amortization-frontier planning real return. The fixed, conservative real return
    # the frontier discounts the remaining GI-netted gap stream at when it solves each
    # year for the constant-real total spend ``L`` that exactly funds the portfolio to
    # zero by ``end_age`` (the funded-ratio machinery run BACKWARDS). Read ONLY by the
    # ``amortization`` spending strategy
    # (spending.rules.amortization.AmortizationFrontierStrategy). Defaults to 0.01 (1%
    # real) — the SAME value ``spending.npv.compute_npv_gaps_by_age`` discounts at. It
    # is a deliberately STABLE planning return (NOT the allocation's expected return): a
    # frontier you push toward must be a fixed, honest reference, not a ceiling that
    # floats up with a spicier allocation (the artificial-confidence failure mode).
    amortization_real_return: float = 0.01

    # Voluntary post-59½ bracket-fill drawdown lever. ``None`` (default) means
    # the lever is OFF (old saved scenarios load with it off). When set, each year
    # in the fixed ages-60-74 window (``tax.location.LEVER_MIN_AGE`` /
    # ``LEVER_MAX_AGE``) draws extra from tax-deferred into taxable to fill
    # ordinary taxable income up to this dollar target, to shrink the RMD base
    # 15 years out. Read ONLY by the dragged decomposition
    # (``tax.location.step_buckets_dragged``); has no effect without a
    # tax-location split (the lever needs tax-deferred/taxable accounts to move
    # money between). Validated 0..5,000,000 in ``engine.validation.validate_scenario``.
    bracket_fill_target: Optional[float] = None

    # The fields below all default to a no-op.
    # Annual all-in investment cost (expense ratios + advisory), as a fraction of
    # assets. Every engine nets it out of each year's real return:
    # ``r_net = (1 + r) * (1 - annual_fee) - 1`` (``engine.step.net_return``).
    annual_fee: float = 0.0
    # Monte Carlo return distribution. "normal" (default) draws correlated
    # Normals; "student_t" draws a multivariate Student-t with ``mc_t_df``
    # degrees of freedom, rescaled to the SAME mean, vol and correlation, so
    # only the tails change (fatter crash years).
    mc_distribution: str = MC_DISTRIBUTION_NORMAL
    mc_t_df: float = 5.0
    # Pension inflation treatment. A FERS deferred annuity is fixed in NOMINAL
    # dollars from separation until it starts, then receives COLAs. When
    # ``pension_nominal_until_start`` is True each pension amount is read as the
    # nominal check at commencement and deflated to plan-start dollars at
    # ``assumed_inflation`` over (pension_start_age - start_age) years.
    # ``pension_cola`` selects the post-commencement adjustment: "full" (CPI,
    # real value constant) or "fers_diet" (CPI if <=2%, 2% if 2-3%, CPI-1% if
    # >3%, evaluated at ``assumed_inflation``). These compose with the
    # stress-only ``pension_real_drift`` above.
    pension_nominal_until_start: bool = False
    pension_cola: str = PENSION_COLA_FULL
    assumed_inflation: float = 0.025
    # Survivor case. ``survivor_age`` (None = both live to end_age) is the
    # household age at which ``survivor_decedent`` ("a" or "b") dies. From
    # then on: the survivor keeps the LARGER of the two Social Security benefits
    # (SSA survivor rule, both already claimed), the decedent's pension continues
    # at ``survivor_pension_fraction`` (FERS full survivor annuity = 0.5), spending
    # scales by ``survivor_spending_fraction``, and federal tax uses single-filer
    # brackets.
    survivor_age: Optional[int] = None
    survivor_decedent: str = "b"
    survivor_pension_fraction: float = 0.5
    survivor_spending_fraction: float = 0.75
    # Guardrails: when True the carried base spend is clamped to the
    # [floor, ceiling] rails after each nudge, so the rule's state never drifts
    # far outside what is actually spent (default False = the original rule).
    guard_clamp_base: bool = False


def default_scenario() -> Scenario:
    """The built-in default scenario: a FICTIONAL two-person household.

    Ages 50 → 95, a $1.8M portfolio, $110k/yr real spending, and four
    guaranteed-income streams:

        Person A pension at 60                 $24,000
        Person B pension at 65                  $9,000
        Person A SS, $33,000 at FRA, claim 67  $33,000
        Person B SS, $26,000 at FRA, claim 70  $32,240  (26,000 × 1.24)

    Social Security amounts on the Scenario are the EFFECTIVE benefit at the
    claim age (``effective_ss_amount``); the API and UI take the FRA amount plus
    a claim age and derive the same value. The default allocation is 60% US
    Total Stock Market @ 7% / 40% US Bonds (10-yr Treasury proxy) @ 2% real,
    which blends to 0.60·0.07 + 0.40·0.02 = 0.050. The other classes live in
    CLASS_MENU, not in this default allocation.
    """
    return Scenario(
        start_age=50,
        end_age=95,
        initial_portfolio=1_800_000.0,
        allocation=(
            AssetClass(
                "us_total_market", "US Total Stock Market",
                0.60, 0.07, _legacy_default_volatility("us_total_market"),
            ),
            AssetClass(
                "us_agg_bonds", "US Bonds (10-yr Treasury proxy)",
                0.40, 0.02, _legacy_default_volatility("us_agg_bonds"),
            ),
        ),
        annual_spending=110_000.0,
        person_a_pension_amount=24_000.0,
        person_a_pension_start_age=60,
        person_b_pension_amount=9_000.0,
        person_b_pension_start_age=65,
        person_a_ss_amount=effective_ss_amount(DEFAULT_PERSON_A_SS_FRA, 67),
        person_a_ss_start_age=67,
        person_b_ss_amount=effective_ss_amount(DEFAULT_PERSON_B_SS_FRA, 70),
        person_b_ss_start_age=70,
    )
