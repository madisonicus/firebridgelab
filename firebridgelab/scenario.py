"""Scenario inputs for a deterministic projection.

Defines the Scenario dataclass (horizon, portfolio, per-person
guaranteed-income streams) and the AssetClass allocation model, whose
per-class real returns blend (weighted average) into the portfolio real
return. All monetary values are real (today's) dollars. Exposes
default_scenario() and blended_real_return().
"""

import functools
from dataclasses import dataclass, replace
from typing import Optional

from firebridgelab.ss_claim import effective_ss_amount


# Lifecycle-spending phase defaults (the "go-go / slow-go / no-go" pattern).
# Research-anchored constants per the design notes (locked
# 2026-05-25): the annual_spending input is the GO-GO (peak) level, and the two
# later phases scale DOWN from it. Phases are age-anchored (not years-since-
# retirement) because the taper tracks activity/health and the default household
# retires early at 50. Boundaries are the FIRST age of each later phase (inclusive lower bound).
#
# v1 exposes these only via the on/off spending_mode toggle — they are NOT
# user-editable yet. They live on the Scenario (below) as defaulted fields so a
# fast-follow card can make them editable WITHOUT an engine change.
_SLOW_GO_START_AGE = 75   # ages 75-84 → slow-go
_NO_GO_START_AGE = 85     # ages 85+   → no-go
_SLOW_GO_MULTIPLIER = 0.85
_NO_GO_MULTIPLIER = 0.75

# Valid spending_mode values. "constant" (default) reproduces today's behavior
# byte-for-byte; "lifecycle" activates the age-anchored phase curve; "guardrails"
# activates the two-sided Guyton-Klinger dynamic-spending strategy (variable-
# spending design). The guardrail is the first PATH-DEPENDENT spending shape — its
# spend depends on the current portfolio and the prior spend — so it is NOT served
# by the pure ``spending_for_age`` helper; it lives in the stateful
# ``firebridgelab.spending.GuardrailStrategy`` (see that module). constant/lifecycle stay
# byte-for-byte identical because their strategy wraps ``spending_for_age``.
SPENDING_MODE_CONSTANT = "constant"
SPENDING_MODE_LIFECYCLE = "lifecycle"
SPENDING_MODE_GUARDRAILS = "guardrails"
# "fr_smooth" activates the funded-ratio partial-adjustment spending strategy (FR
# feature slice 2). Like guardrails it is PATH-DEPENDENT — this year's spend
# depends on the smoothed-portfolio funded ratio and the carried untapered base —
# so it lives in the stateful ``firebridgelab.spending.FundedRatioSmoothStrategy`` and is
# view-only (the max-spend solver is fenced). FR = 1 means "the smoothed portfolio
# exactly funds the baseline annual_spending stream"; each year the base nudges a
# damped fraction (alpha) of the way toward funding, then composes the SAME
# go-go/slow-go/no-go taper + floor/ceiling clamp guardrails uses.
SPENDING_MODE_FR_SMOOTH = "fr_smooth"
# "fr_guardrail" activates the funded-ratio BAND spending strategy (FR feature
# slice 3) — a close sibling of fr_smooth that swaps the damped partial-adjustment
# for a DISCRETE band trip on the funded ratio: hold while funding stays within ±20%
# of fully-funded, a ±10% nudge to the untapered base past either band. Like
# guardrails / fr_smooth it is PATH-DEPENDENT (the spend depends on the smoothed-
# portfolio funded ratio and the carried base) so it lives in the stateful
# ``firebridgelab.spending.FundedRatioGuardrailStrategy`` and is view-only (the max-spend
# solver is fenced). Bands/step are baked v1 presets, not user inputs.
SPENDING_MODE_FR_GUARDRAIL = "fr_guardrail"
# "fr_tiers" activates the funded-ratio TIER LADDER spending strategy (FR feature
# slice 4 — the capstone): a FAITHFUL port of the legacy ``TierLadderPolicy``
# "christmas tree". A funded-ratio band ladder with hysteresis, a crash brake, and
# a hard floor, where the tier AMOUNTS encode the go-go/slow-go/later phase shape
# ($160k go-go MAX → $85k FLOOR). UNLIKE fr_smooth / fr_guardrail it does NOT
# apply the activity taper and does NOT use the operational spending_floor /
# spending_ceiling rails — its clamps are TIER-based (crash → BELT, hard floor →
# FLOOR), gated by its own portfolio-level floor threshold. Its NPV target is the
# MAX go-go tier amount ($160k), NOT annual_spending — the load-bearing invariant.
# Like the other dynamic modes it is PATH-DEPENDENT and view-only (the max-spend
# solver is fenced). The full ladder is a baked v1 preset (no user knobs); the math
# lives in ``firebridgelab.spending.FundedRatioTierStrategy``.
SPENDING_MODE_FR_TIERS = "fr_tiers"
# "amortization" activates the AMORTIZATION FRONTIER spending strategy (amortization-
# frontier design) — the lone DEPLETE-AWARE strategy (all six above are sustain-and-
# preserve). It is the funded-ratio machinery run BACKWARDS: each year it solves for
# the constant-real total spend ``L`` such that the portfolio exactly funds the
# remaining GI-netted gap stream, discounted at ``amortization_real_return`` over the
# hard plan horizon ``end_age`` (i.e. the target ``L`` where the funded ratio == 1).
# Because future GI is netted INSIDE the solve, ``L`` front-loads consumption into the
# pre-SS bridge years (GI-aware consumption smoothing — NOT myopic VPW). It is the
# upper boundary of the spending envelope ("the most you could responsibly pull each
# year if you amortize to zero by the plan horizon") — a benchmark you push toward,
# never "VPW" in the UI. UNLIKE the six clamped strategies it is DELIBERATELY
# UNCLAMPED (the floor/ceiling rails are chart reference lines only, never applied),
# because clamping destroys the very over-/under-funded diagnostic it exists for.
# Path-dependent (re-solved each year off the actual begin-of-year balance) and
# view-only — the max-spend solver is fenced, since the frontier IS the spend. The
# math lives in ``firebridgelab.spending.AmortizationFrontierStrategy``.
SPENDING_MODE_AMORTIZATION = "amortization"

# Valid allocation_mode values (glidepath design). "constant" (default) holds ONE
# allocation across the whole horizon — today's behavior, byte-for-byte unchanged.
# "glidepath" carries one allocation per DERIVED phase (a rising-equity / bond-tent
# glide), and the portfolio's real return at a given age is the blend of THAT age's
# phase's allocation. The toggle is orthogonal to `mode` and `spending_mode`.
ALLOCATION_MODE_CONSTANT = "constant"

MC_DISTRIBUTION_NORMAL = "normal"
MC_DISTRIBUTION_STUDENT_T = "student_t"
MC_DISTRIBUTIONS = (MC_DISTRIBUTION_NORMAL, MC_DISTRIBUTION_STUDENT_T)
PENSION_COLA_FULL = "full"
PENSION_COLA_FERS_DIET = "fers_diet"
PENSION_COLAS = (PENSION_COLA_FULL, PENSION_COLA_FERS_DIET)
SURVIVOR_DECEDENTS = ("a", "b")
ALLOCATION_MODE_GLIDEPATH = "glidepath"

# Valid sourcing_mode values (bucket-strategy design). "single" (default) holds ONE
# blended portfolio balance and is byte-for-byte unchanged — the canary path.
# "buckets" splits the portfolio into a cash / income / growth time-segmentation
# (the 3-bucket sourcing strategy), where withdrawals are sourced cash-first and
# the safe buckets are refilled ONE-SIDEDLY (rebalance in up years, ride in down
# years). The toggle is a NEW orthogonal dimension alongside `mode`,
# `spending_mode`, and `allocation_mode`. In buckets mode the bucket allocations
# SUPERSEDE the single/glidepath allocation (glidepath is ignored, not an error —
# see the design "Locked design (v1)" out-of-scope note).
SOURCING_MODE_SINGLE = "single"
SOURCING_MODE_BUCKETS = "buckets"

# Default bucket sizing in YEARS OF THE FORWARD GAP (bucket-strategy design
# "Bucket definition"): the cash bucket targets N1 years of gap, the income bucket
# the next N2 years. Gap = spending − guaranteed income, so as GI lands the safe
# targets shrink (the emergent GI-driven rising-equity glide). User-editable knobs
# (slice 1b makes the UI editable); carried as the v1 research defaults here.
_DEFAULT_BUCKET_N1 = 2  # cash bucket: years of forward gap
_DEFAULT_BUCKET_N2 = 7  # income bucket: years of forward gap (after the cash years)

# The ONE fixed boundary the glidepath adds (glidepath design "The core design
# decision"). The IRS penalty-free 401(k) withdrawal rule is age 59½; the engine
# uses an INTEGER annual age axis, so we model the boundary at age 60 — the first
# full year of penalty-free access. It is NOT a user input in this slice (it's a
# tax rule, not a preference). The pension + SS boundaries are NOT hardcoded here;
# they DERIVE from the existing GI start-age scenario inputs (see
# ``glidepath_boundaries``).
ALLOCATION_401K_ACCESS_AGE = 60

# Guyton-Klinger guardrail research defaults (variable-spending design §"Locked
# design"). These are NEW defaulted Scenario fields below — additive, so the
# constant/lifecycle canary is untouched. They are NOT yet UI-editable (that's
# slice 3); slice 1 carries the research defaults:
#   GUARD  = the symmetric band half-width around the initial portfolio-funded
#            withdrawal rate (0.20 → trip a cut above rate×1.20, a raise below
#            rate×0.80).
#   ADJUST = the per-trip proportional spend adjustment (0.10 → ±10% on the
#            UNtapered base when a band trips). The dial that spans nudge→jump:
#            ADJUST→1.0 makes the first trip a de-facto jump to the rail.
#   floor  = the OPERATIONAL total-spending floor in real dollars ($85k — the
#            lowest level the plan will accept); guardrail spending clamps here
#            and never goes below it. Flat
#            (not activity-tapered) — see the rails note in GuardrailStrategy. The
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

# Shiller-derived Monte Carlo defaults (computed from the same 1871-2025 series
# Historical replays — the design notes Decision #3). Per-class
# annual real-return standard deviations and a single equity-bond real-return
# correlation. These are the v1 defaults wherever an AssetClass / Scenario is
# constructed without explicit values, so the Monte Carlo sampler has honest,
# data-calibrated inputs. The Deterministic and Historical engines never read
# them, so threading these in leaves those paths byte-for-byte unchanged.
#
# _DEFAULT_EQUITY_VOLATILITY / _DEFAULT_BOND_VOLATILITY are hand-ROUNDED
# FALLBACK figures, not the canonical values. They exist only for the two call
# sites that can't reach the series at the moment they're needed: the
# ``AssetClass.volatility`` dataclass field default (evaluated at import time,
# so it must be a plain float literal) and the API's unrecognized-class-id
# resolver fallback (a class id the series lookup doesn't know). Everywhere
# the legacy class id IS known at runtime — ``default_scenario()``,
# ``default_bucket_config()``, and ``/api/classes``'s ``_class_menu_entries()``
# — the CANONICAL per-legacy-class volatility comes from the exact series
# stdev via ``_legacy_default_volatility()`` below, so there is one computation,
# not three independently hand-rounded copies of it (those copies
# had drifted from each other and from the exact figure by ~2e-4).
_DEFAULT_EQUITY_VOLATILITY = 0.174  # S&P real total return annual stdev (rounded fallback)
_DEFAULT_BOND_VOLATILITY = 0.088    # 10yr Treasury real total return annual stdev (rounded fallback)
DEFAULT_CORRELATION = 0.20          # full-history equity-bond real-return correlation


@functools.lru_cache(maxsize=None)
def _legacy_default_volatility(class_id: str) -> float:
    """Exact per-legacy-class real-return volatility.

    Computes the SAME figure ``api.main._class_menu_entries()`` publishes via
    ``GET /api/classes`` for a legacy class — ``np.std(returns, ddof=1)`` over
    that class's real-return series — so ``default_scenario()`` and
    ``default_bucket_config()`` can build with the exact value instead of the
    hand-rounded ``_DEFAULT_EQUITY_VOLATILITY`` / ``_DEFAULT_BOND_VOLATILITY``
    constants above. Before this fix the two were computed independently and
    had drifted apart by ~2e-4 per class — a vol-less parsed allocation (which
    resolves to the exact published figure) diverged from the default
    scenario's own Monte Carlo run.

    Defined for any class id in ``firebridgelab.historical.SERIES_FOR_CLASS``; raises
    ``KeyError`` only for an id absent from that map. In practice the sole
    caller — the ``_class_menu_entries()`` ``else`` branch — reaches it only for
    the two legacy ids (``us_total_market`` / ``us_agg_bonds``, the keys that map
    to a series id rather than to themselves); the other ten classes take the
    ``CLASS_MENU`` branch, so the exact same computation serves all twelve.

    Lazy-imports numpy + the series-id mapping + the series loader (same
    cycle-avoidance pattern ``_build_class_menu`` uses below: ``firebridgelab.
    historical`` imports ``firebridgelab.scenario`` at module top, so importing it
    back here at module top would be circular) and memoizes via
    ``functools.lru_cache`` since this must be called at RUNTIME — never at
    module import time, which is when the ``AssetClass.volatility`` dataclass
    field default above is evaluated.
    """
    import numpy as np

    from firebridgelab.correlation import real_return_series_by_series_id
    from firebridgelab.historical import SERIES_FOR_CLASS

    series_id = SERIES_FOR_CLASS[class_id]
    series = real_return_series_by_series_id()
    return float(np.std(list(series[series_id].values()), ddof=1))


# Display labels for the slice-1 NEW classes (asset-class design D5 table).
# Presentation-only — never a dict key or query token. Keyed by class id; used
# to build the CLASS_MENU entries below (after AssetClass is defined).
_NEW_CLASS_LABELS: dict[str, str] = {
    # slice 1
    "us_small_value": "US Small-Cap Value",
    "lt_treasury": "US Treasury Bonds (10-yr, Damodaran)",
    "it_treasury": "US Intermediate Treasuries",
    "st_treasury": "US Short Treasuries / T-bills",
    "us_reit": "US REITs",
    # slice 4a (design D5 table display names)
    "gold": "Gold",
    "intl_developed": "International Developed (ex-US)",
    "emerging_markets": "Emerging Markets",
    "commodities": "Broad Commodities",
    "tips": "TIPS",
}


@dataclass(frozen=True)
class AssetClass:
    """One asset class in a portfolio allocation.

    Two id namespaces (asset-class design, round-2 fix): ``id`` is the **class
    id** — the stable menu / allocation / query key (e.g. ``us_total_market``);
    ``label`` is the **display** name, presentation-only and NEVER a dict key or
    query token. The *series* id (the data key) is a third thing entirely, held
    in ``historical.SERIES_FOR_CLASS`` as a class-id → series-id map. The two
    coincide for the 10 new classes but NOT for the two legacy ones — class
    ``us_total_market`` binds to the existing series ``us_equity``, and
    ``us_agg_bonds`` to ``us_bonds``.

    weight is a fraction in [0, 1]; real_return is the annual real return for
    the class (e.g. 0.07 = 7%). The portfolio's real return is the
    weight-weighted average across all classes.

    ``volatility`` is the class's annual standard deviation of real return,
    used ONLY by the Monte Carlo engine (firebridgelab.monte_carlo) to sample a
    Normal(mean, vol) return path. It defaults to the Shiller-derived figure so
    the field exists everywhere an AssetClass is built without the Deterministic
    or Historical engines (which never read it) changing at all — same
    defaulted-field pattern the lifecycle phase fields used. The MC UI makes it
    editable in a later build; v1 carries the default.
    """

    id: str             # stable class id (menu / allocation / query key)
    label: str          # display name (presentation-only, never a key)
    weight: float       # fraction 0..1
    real_return: float  # annual real return, e.g. 0.07 = 7%
    volatility: float = _DEFAULT_EQUITY_VOLATILITY  # annual real-return stdev (MC only)


def _build_class_menu() -> dict[str, "AssetClass"]:
    """Seed the class menu from the staged annual REAL series (asset-class design
    slice 1, D2/D5).

    Each NEW class's Deterministic default ``real_return`` is its **geometric
    mean** real return (the constant rate reproducing realized cumulative
    growth — D2), and its ``volatility`` is the series' annual real-return
    stdev (for the MC sampler in slice 2). All carry ``weight=0.0`` — they are
    INERT menu entries, NOT part of the default allocation (which stays length-2,
    so the numeric value canary survives — D4). Presets / the matrix UI pull
    them into an editable allocation in later slices.

    Imported lazily (inside the function) to avoid a module-level import cycle
    with firebridgelab.class_series; the loader is memoized, so this parses at most
    once per process.
    """
    from firebridgelab.class_series import NEW_CLASS_SERIES, load_class_series

    try:
        series = load_class_series()
    except FileNotFoundError:
        # No data fetched yet: import still works (so `firebridgelab-fetch-data`
        # can run); anything that needs returns raises the loader's error later.
        return {}
    menu: dict[str, AssetClass] = {}
    for class_id, label in _NEW_CLASS_LABELS.items():
        if class_id not in NEW_CLASS_SERIES:
            continue  # optional class whose raw data isn't installed
        cs = series[class_id]
        menu[class_id] = AssetClass(
            id=class_id,
            label=label,
            weight=0.0,
            real_return=cs.geometric_mean,
            volatility=cs.stdev,
        )
    return menu


# The class menu/registry (asset-class design round-2 fix B1): the NEW classes
# live HERE, NOT in default_scenario()'s allocation, so the default path stays
# length-2 and the value canary survives. Presets and the multi-class matrix UI
# pull classes from this menu into the editable allocation in later slices.
# Slice 1 populates the long-history, free-sourced classes (US small-value, the
# Treasury/cash ladder, US REITs) with their geometric-mean real returns + vols
# from the staged annual real series; the remaining classes (intl/EM, TIPS,
# gold, commodities) land as their series are sourced in later slices. Keyed by
# class id. Built at import via the staged-series loader (memoized).
CLASS_MENU: dict[str, "AssetClass"] = _build_class_menu()


@dataclass(frozen=True)
class Bucket:
    """One time-segmentation bucket in the 3-bucket sourcing strategy.

    bucket-strategy design "Bucket definition": each bucket holds its OWN asset
    allocation (the same ``tuple[AssetClass, ...]`` representation the single /
    glidepath path uses, drawn from the existing 12-class menu) — the cash bucket
    is short Treasuries, the income bucket aggregate bonds, the growth bucket total
    market by default, all user-editable. ``id`` is the stable key
    (``cash`` / ``income`` / ``growth``), ``name`` the display label. The bucket's
    real return at a given age is the weight-weighted blend of its allocation
    (Deterministic geometric; Historical cohort per-class; MC sampled per-class).

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
    """The 3-bucket sourcing configuration (bucket-strategy design "Locked design").

    Three buckets — cash / income / growth — each with its own allocation, plus the
    forward-gap sizing ``n1`` (cash years) and ``n2`` (income years). Held as a
    single frozen field on the Scenario (``buckets``) so the constant/single path
    is byte-for-byte unchanged when it is ``None``. ``n1`` / ``n2`` are the
    user-editable knobs; the per-bucket allocations are the other editable surface
    (slice 1b). All tuples keep the frozen Scenario hashable / immutable.
    """

    cash: Bucket
    income: Bucket
    growth: Bucket
    n1: int = _DEFAULT_BUCKET_N1  # cash bucket: years of forward gap
    n2: int = _DEFAULT_BUCKET_N2  # income bucket: years of forward gap


def default_bucket_config() -> BucketConfig:
    """The default 3-bucket config (bucket-strategy design "Bucket
    definition"): cash = 100% short Treasuries, income = 100% US aggregate bonds,
    growth = 100% US total market, with N1=2 cash years and N2=7 income years.

    The cash bucket pulls its class params (return, volatility) from ``CLASS_MENU``
    (``st_treasury`` — its data-derived geometric-mean default), exactly like the
    single-allocation matrix. The income (``us_agg_bonds``) and growth
    (``us_total_market``) classes are the two LEGACY classes, which are NOT in
    CLASS_MENU (they live only in the default scenario's length-2 allocation — see
    ``_build_class_menu``); so they reuse the SAME canonical legacy params the
    default scenario carries: real_return 2%/7% (the design's hardcoded values) and
    volatility from ``_legacy_default_volatility()`` (the exact
    series-derived stdev, not the rounded ``_DEFAULT_BOND_VOLATILITY`` /
    ``_DEFAULT_EQUITY_VOLATILITY`` fallback constants). That makes a single bucket
    holding the default allocation reproduce the single-balance return exactly
    (Gate B). Each bucket allocation is a length-1, 100%-weight tuple.
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

    All monetary values are real (today's) dollars. Inflation is handled
    implicitly by working in real-dollar terms — the brief explicitly
    notes no separate inflation modeling is needed for slice 1.
    """

    # Identity / horizon
    start_age: int
    end_age: int

    # Portfolio
    initial_portfolio: float
    # Asset-class allocation. The portfolio's real return is the
    # weight-weighted average of the classes' real returns (see
    # blended_real_return). Replaces the former scalar real_return.
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
    # spends annual_spending flat in real terms forever — today's behavior,
    # unchanged. "lifecycle" treats annual_spending as the GO-GO peak and scales
    # the two later phases down by their multipliers (see spending_for_age). The
    # phase boundaries + multipliers are carried as defaulted fields (not bare
    # module constants) so a fast-follow card can make them user-editable without
    # touching the engine; v1 leaves them at the research defaults.
    spending_mode: str = SPENDING_MODE_CONSTANT
    slow_go_start_age: int = _SLOW_GO_START_AGE
    no_go_start_age: int = _NO_GO_START_AGE
    slow_go_multiplier: float = _SLOW_GO_MULTIPLIER
    no_go_multiplier: float = _NO_GO_MULTIPLIER

    # Guyton-Klinger guardrail knobs (variable-spending design). Read ONLY by the
    # ``guardrails`` spending strategy (firebridgelab.spending.GuardrailStrategy); the
    # constant/lifecycle path never touches them, so they're purely additive and
    # the canary survives. Carried as defaulted fields (same pattern as the
    # lifecycle phase fields) so slice 3 can make them user-editable WITHOUT an
    # engine change; slice 1 leaves them at the research defaults. ``guard_band``
    # / ``guard_adjust`` are the symmetric band half-width + per-trip proportional
    # adjustment; ``spending_floor`` (default $85k) is the real-dollar OPERATIONAL
    # floor — "we don't dip below this", the clamp; ``spending_ceiling`` is the
    # satiation ceiling. ($45k is the absolute/survivable floor — ``ABSOLUTE_FLOOR``
    # above — context, not the clamp.)
    guard_band: float = _GUARDRAIL_GUARD
    guard_adjust: float = _GUARDRAIL_ADJUST
    spending_floor: float = _GUARDRAIL_FLOOR
    spending_ceiling: float = _GUARDRAIL_CEILING

    # Cross-class real-return correlation, used ONLY by the Monte Carlo engine.
    # SCALAR FAST-PATH for N <= 2 ONLY (the asset-class design, round-2 fix N1):
    # a single equity-bond scalar broadcast onto the implied 2x2 matrix. For the
    # default allocation (2 classes) this is exactly the equity-bond pair, and
    # its Cholesky factor correlates the sampled draws. Defaults to the
    # full-history Shiller figure. Like volatility, the Deterministic and
    # Historical engines never read it, so it leaves those paths unchanged. The
    # valid range is [-0.99, +0.99] (the API guards |correlation| > 0.99, since
    # exactly +/-1 is rank-deficient and would break the Cholesky factor).
    #
    # For N > 2 this scalar is NEVER broadcast (a uniform off-diagonal is a
    # silent wrong default, not an error) — the sampler requires an explicit
    # ``correlations`` matrix and raises a clear error if one is missing.
    correlation: float = DEFAULT_CORRELATION

    # Optional ordered N x N correlation matrix, aligned to allocation order
    # (row/col i is allocation[i]). The multi-class representation (asset-class
    # design): when present and N > 2, the MC sampler uses this matrix instead of
    # the scalar fast-path. Held as a tuple-of-tuples so the frozen Scenario
    # stays hashable / immutable; ``None`` means "no explicit matrix" (the N <= 2
    # scalar fast-path applies; N > 2 with no matrix is an error). The per-pair
    # historical estimation that produces a *default* matrix is slice 2 — slice 0
    # only ever runs the 2-class default, so this stays None on the default path.
    correlations: Optional[tuple[tuple[float, ...], ...]] = None

    # Allocation shape (glidepath / bond-tent). "constant" (default) holds the
    # single ``allocation`` above across all ages — today's behavior, byte-for-byte
    # unchanged. "glidepath" carries one allocation per DERIVED phase in
    # ``phase_allocations`` (the rising-equity / bond-tent glide), and the
    # portfolio's real return at a given age is the blend of THAT age's phase's
    # allocation (see blended_real_return_at_age). Additive defaulted fields (same
    # pattern as the lifecycle / guardrail knobs) so the constant/Deterministic
    # canary is untouched.
    allocation_mode: str = ALLOCATION_MODE_CONSTANT
    # Per-phase allocations, one tuple-of-AssetClass per DERIVED phase, in phase
    # order (the same phase order ``glidepath_boundaries`` produces). Read ONLY
    # when ``allocation_mode == "glidepath"``; ``None`` under constant mode (the
    # single ``allocation`` carries). Held as a tuple-of-tuples so the frozen
    # Scenario stays hashable / immutable. The count is NOT pinned to the field —
    # the engine derives the phase boundaries from the GI ages at run time and maps
    # each phase to its allocation by index (see blended_real_return_at_age), so a
    # mismatch is clamped to the last supplied phase rather than crashing.
    phase_allocations: Optional[tuple[tuple["AssetClass", ...], ...]] = None

    # Withdrawal SOURCING shape (bucket-strategy design). "single" (default) holds
    # the single blended balance above across all years — today's behavior,
    # byte-for-byte unchanged. "buckets" splits the portfolio into the 3-bucket
    # cash/income/growth time-segmentation in ``buckets`` and sources withdrawals
    # cash-first with a one-sided refill. Additive defaulted fields (same pattern
    # as the lifecycle / guardrail / glidepath knobs) so the single canary is
    # untouched. In buckets mode the bucket allocations supersede the
    # single/glidepath allocation (``allocation_mode`` is ignored, not an error).
    sourcing_mode: str = SOURCING_MODE_SINGLE
    # The 3-bucket configuration, read ONLY when ``sourcing_mode == "buckets"``.
    # ``None`` under single mode (the single ``allocation`` carries). Held as a
    # frozen ``BucketConfig`` so the Scenario stays immutable / hashable.
    buckets: Optional["BucketConfig"] = None

    # Tax-location split (tax-buckets design tb1). Three optional account balances
    # that partition ``initial_portfolio`` by TAX LOCATION (a DIFFERENT axis from
    # the cash/income/growth ``buckets`` above — never conflate them). The feature
    # is OFF when all three are ``None`` (the default): the engine runs the
    # undifferentiated single pool and the canary is byte-for-byte preserved. When
    # ANY is supplied, all three are treated as supplied (a missing one is 0) and
    # they must sum to ``initial_portfolio`` within $1 (validated in
    # firebridgelab.tax_location.resolve_split). Additive defaulted fields — same pattern
    # as the buckets / glidepath / guardrail knobs — so the single canary is
    # untouched. When supplied, a PARALLEL "shadow" decomposition tracks the three
    # balances through the sim with access-aware sourcing (taxable → roth pre-59½,
    # taxable → tax_deferred → roth post-59½); it never perturbs the scalar math.
    # Read ONLY in single sourcing mode (no-op under sourcing_mode == "buckets" —
    # tb1 is single-mode only; the 3×3 product is a later card).
    taxable_balance: Optional[float] = None
    tax_deferred_balance: Optional[float] = None
    roth_balance: Optional[float] = None

    # Taxable-gains fraction knob (federal-tax overlay design ft1, DECISION 4). The
    # flat share of each taxable-account WITHDRAWAL treated as long-term capital
    # gain for the federal-tax overlay (the remainder is basis, tax-free). A
    # single user-set knob rather than real cost-basis tracking (a design
    # choice — leaner, one fewer state variable); documented consequence: the REAL gains
    # fraction drifts upward as the account compounds, so a static knob
    # understates late-plan LTCG. Read ONLY by
    # ``firebridgelab.federal_tax.compute_tax_overlay``; every other engine path
    # ignores it, so this is purely additive (the canary is untouched).
    # Validated 0..1 in ``validate_scenario`` below (same pattern as the
    # tax-location balances above).
    taxable_gains_fraction: float = 0.5

    # Per-year REAL-drift knobs (stress-test design slice 4 — COLA / cost-of-living
    # treatment). Both default to 0.0 so every existing run is byte-identical (a 0.0
    # rate makes the per-year multiplier 1.0 ⇒ a no-op, the same additive-defaulted-
    # field discipline the lifecycle / guardrail / glidepath / bucket knobs use).
    # They are NOT a base-case change — only the inflation-bearing Family-B stresses
    # set them (government_austerity, sustained_cost_drift); the base engine stays full-COLA
    # real.
    #   pension_real_drift — annual REAL erosion rate of the two PENSION streams only
    #     (the FERS COLA-freeze model). When > 0 each pension stream's real value
    #     decays at this rate from its OWN start age: a stream active at ``age``
    #     contributes ``amount * (1 - pension_real_drift) ** (age - start_age)``. The
    #     two SS streams are LEFT UNCHANGED. Applied in ``gi_at_age`` — the single
    #     canonical GI sum every engine + the guardrail anchor + NPV + buckets read —
    #     so the erosion flows everywhere consistently.
    #   spending_real_drift — annual REAL growth rate of SPENDING (cost-of-living
    #     drift). When > 0 spending grows at this rate per year from ``start_age``:
    #     the funded spend is multiplied by ``(1 + spending_real_drift) ** (age -
    #     start_age)`` AFTER the strategy computes it, BEFORE step_year — a post-
    #     strategy cost-of-living multiplier that composes with ANY spending strategy.
    pension_real_drift: float = 0.0
    spending_real_drift: float = 0.0

    # Amortization-frontier planning real return (amortization-frontier design
    # decision 4). The fixed, conservative real return the frontier discounts the
    # remaining GI-netted gap stream at when it solves each year for the constant-
    # real total spend ``L`` that exactly funds the portfolio to zero by ``end_age``
    # (the funded-ratio machinery run BACKWARDS). Read ONLY by the ``amortization``
    # spending strategy (firebridgelab.spending.AmortizationFrontierStrategy); every other
    # mode never touches it, so this is purely additive and the canary is untouched
    # (same defaulted-field discipline as ``pension_real_drift``). Defaults to 0.01
    # (1% real) — the SAME value ``compute_npv_gaps_by_age`` already discounts at, so
    # the knob just EXPOSES a dial already buried in the FR strategies rather than
    # introducing a new concept. It is a deliberately STABLE planning return (NOT the
    # allocation's expected return): a frontier you push toward must be a fixed,
    # honest reference, not a ceiling that floats up with a spicier allocation (the
    # artificial-confidence failure mode). The UI makes it an adjustable slider so the
    # prototype can settle decision 4's open question empirically (drag it toward the
    # allocation return and watch the frontier rise / the late-life fan crater).
    amortization_real_return: float = 0.01

    # Voluntary post-59½ bracket-fill drawdown lever (tax-drag design td1,
    # DECISION 5). ``None`` (default) means the lever is OFF — every existing
    # run (split or not) is unaffected, and old saved scenarios load with the
    # lever off (schema-bump backward compat). When set, each year in the
    # fixed ages-60-74 window (``firebridgelab.tax_location.LEVER_MIN_AGE`` /
    # ``LEVER_MAX_AGE``) draws extra from tax-deferred to fill ordinary taxable
    # income up to this dollar target — "just pulling out a little more after
    # 59.5 than needed every year into taxable" (td1 scoping), to shrink
    # the RMD base 15 years out. Read ONLY by the dragged decomposition
    # (``firebridgelab.tax_location.step_buckets_dragged``); has no effect without a
    # tax-location split (the lever needs tax-deferred/taxable accounts to
    # move money between). Validated 0..5,000,000 in ``validate_scenario``
    # below (same pattern as the tax-location balances / gains-fraction knob).
    bracket_fill_target: Optional[float] = None

    # --- Review 2026-10-11 additions (all default to inert / byte-identical) ---
    # Annual all-in investment cost (expense ratios + advisory), as a fraction of
    # assets. Every engine nets it out of each year's real return:
    # ``r_net = (1 + r) * (1 - annual_fee) - 1`` (``firebridgelab._step.net_return``).
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


def fers_diet_cola(inflation: float) -> float:
    """FERS annual COLA for a given CPI change (5 U.S.C. 8462): full CPI up to
    2%, flat 2% between 2% and 3%, CPI minus 1% above 3%."""
    if inflation <= 0.02:
        return inflation
    if inflation <= 0.03:
        return 0.02
    return inflation - 0.01


def _pension_multiplier(scenario: Scenario, age: int, start_age: int) -> float:
    """Real value of $1 of a pension's entered amount at ``age`` (it began at
    ``start_age``): pre-commencement nominal freeze, post-commencement COLA
    shortfall, and the stress-only ``pension_real_drift``."""
    years_paid = age - start_age
    mult = (1.0 - scenario.pension_real_drift) ** years_paid
    infl = scenario.assumed_inflation
    if scenario.pension_nominal_until_start:
        mult /= (1.0 + infl) ** max(0, start_age - scenario.start_age)
    if scenario.pension_cola == PENSION_COLA_FERS_DIET:
        mult *= ((1.0 + fers_diet_cola(infl)) / (1.0 + infl)) ** years_paid
    return mult


def is_survivor_year(scenario: Scenario, age: int) -> bool:
    """True once the first death has happened (``survivor_age`` reached)."""
    return scenario.survivor_age is not None and age >= scenario.survivor_age


def pension_income_at_age(scenario: Scenario, age: int) -> float:
    """Summed real pension income at ``age`` (both pensions; see the Scenario
    pension-inflation and survivor fields)."""
    streams = (
        ("a", scenario.person_a_pension_amount, scenario.person_a_pension_start_age),
        ("b", scenario.person_b_pension_amount, scenario.person_b_pension_start_age),
    )
    survivor = is_survivor_year(scenario, age)
    total = 0.0
    for who, amount, start in streams:
        if age < start:
            continue
        value = amount * _pension_multiplier(scenario, age, start)
        if survivor and who == scenario.survivor_decedent:
            value *= scenario.survivor_pension_fraction
        total += value
    return total


def ss_income_at_age(scenario: Scenario, age: int) -> float:
    """Summed Social Security income at ``age``. After the first death the
    survivor keeps the larger of the two benefits in payment (SSA survivor
    rule; a simplification — it ignores the survivor's own early-claim limit)."""
    streams = (
        ("a", scenario.person_a_ss_amount, scenario.person_a_ss_start_age),
        ("b", scenario.person_b_ss_amount, scenario.person_b_ss_start_age),
    )
    active = {who: amount for who, amount, start in streams if age >= start}
    if not is_survivor_year(scenario, age):
        return sum(active.values())
    return max(active.values(), default=0.0)


def gi_at_age(scenario: Scenario, age: int) -> float:
    """Summed guaranteed income active at ``age`` (the four per-person streams).

    The single canonical GI sum: the engines (deterministic, historical, Monte
    Carlo), the guardrail's ``initial_gi`` anchor, the NPV gap table, the bucket
    forward-gap helpers and the tax split (``pension_income_at_age`` /
    ``ss_income_at_age``) all read these functions rather than re-deriving the
    sum. A stream is active iff ``age >= its start age``. Pension real value
    follows ``_pension_multiplier``; the survivor rules follow
    ``is_survivor_year``. With every review-2026-10 field at its default this
    is the plain four-stream sum.
    """
    return pension_income_at_age(scenario, age) + ss_income_at_age(scenario, age)


def spending_multiplier(scenario: Scenario, age: int) -> float:
    """Post-strategy multiplier on a year's spend: cost-of-living drift
    (``spending_real_drift``) times the survivor spending fraction."""
    mult = (1.0 + scenario.spending_real_drift) ** (age - scenario.start_age)
    if is_survivor_year(scenario, age):
        mult *= scenario.survivor_spending_fraction
    return mult


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


def glidepath_boundaries(scenario: Scenario) -> list[int]:
    """The glidepath phase-START ages, derived from the plan's inflection ages.

    Glidepath design "The core design decision": the phase boundaries are the
    household's real financial inflection ages, DERIVED (not separately entered):

        sorted distinct set of
          { ALLOCATION_401K_ACCESS_AGE }                       # fixed IRS constant
          ∪ { person_a_pension_start_age, person_b_pension_start_age,  # existing inputs
              person_a_ss_start_age,      person_b_ss_start_age }
        clipped to [start_age, end_age]

    Returned as the ascending list of phase-START ages, ALWAYS beginning with
    ``start_age`` (the first phase opens at the start regardless of the first
    inflection). With the defaults (401k 60, pensions 60 and 65, SS 67 and 70)
    this is ``[50, 60, 65, 67, 70]`` → five phases
    ``50–59 / 60–64 / 65–66 / 67–69 / 70–95``.

    Clipping: an inflection at or below ``start_age`` collapses into the first
    phase (it doesn't add a boundary before the start); an inflection above
    ``end_age`` is dropped (no phase would open in-horizon). Distinct ages only —
    two streams claiming at the same age add ONE boundary, not two. If Person A and
    Person B claim at different ages, an extra boundary (and phase) simply appears;
    that is the whole point of deriving rather than entering.

    SINGLE-AGE-AXIS LIMITATION (glidepath design "Known limitation"): the model
    treats both people as the same age — every GI stream fires on
    ``age >= start_age`` against ONE timeline, and these boundaries live on that
    same single axis. True per-person event timing (an age gap that makes Person B's
    "62" land at a different timeline age) needs a two-person age axis, which is a
    separate, larger card. Do not infer two-person ages from this helper.
    """
    inflections = {
        ALLOCATION_401K_ACCESS_AGE,
        scenario.person_a_pension_start_age,
        scenario.person_b_pension_start_age,
        scenario.person_a_ss_start_age,
        scenario.person_b_ss_start_age,
    }
    # Keep only inflections strictly inside the horizon — one that opens AFTER
    # start_age and is reachable AT or before end_age. (An inflection == start_age
    # is already the first boundary; one <= start_age collapses into it; one >
    # end_age opens no in-horizon phase.)
    boundaries = {
        age for age in inflections if scenario.start_age < age <= scenario.end_age
    }
    boundaries.add(scenario.start_age)
    return sorted(boundaries)


def phase_index_for_age(scenario: Scenario, age: int) -> int:
    """Index of the glidepath phase that contains ``age`` (lower-bound inclusive).

    Phases are contiguous, half-open ``[boundary_k, boundary_{k+1})`` with the
    lower bound INCLUSIVE; the last phase runs through ``end_age``. So at a
    boundary age the LATER phase wins — age 65 (a pension boundary) belongs to the
    pension phase, NOT the prior one. ``age`` at or below the first boundary maps
    to phase 0 (the first phase starts at ``start_age`` regardless).

    Returns the 0-based phase index into ``glidepath_boundaries(scenario)`` (and,
    by the same index, into ``phase_allocations``).
    """
    boundaries = glidepath_boundaries(scenario)
    # The phase is the count of boundaries at or below this age, minus one: each
    # boundary the age has reached advances it one phase (lower-bound inclusive).
    index = 0
    for k, start in enumerate(boundaries):
        if age >= start:
            index = k
        else:
            break
    return index


def activity_multiplier(scenario: Scenario, age: int) -> float:
    """The go-go / slow-go / no-go activity taper factor at a given age.

    The pure age→multiplier curve underneath ``lifecycle`` spending: go-go (below
    ``slow_go_start_age``) → 1.00, slow-go (``slow_go_start_age``..``no_go_start_age``-1)
    → ``slow_go_multiplier``, no-go (``no_go_start_age``+) → ``no_go_multiplier``.
    With the v1 defaults: ≤74 → ×1.00, 75-84 → ×0.85, ≥85 → ×0.75.

    Factored out so the ``guardrails`` strategy can COMPOSE the same activity taper
    with its dynamic nudge (variable-spending design nudge→taper→clamp order: the
    taper is a post-multiplier on the nudged base). ``spending_for_age`` reads the
    SAME factor, so the lifecycle math is unchanged (byte-for-byte — the inline
    branch it replaced computed the identical multiplier).
    """
    if age >= scenario.no_go_start_age:
        return scenario.no_go_multiplier
    if age >= scenario.slow_go_start_age:
        return scenario.slow_go_multiplier
    return 1.0


def spending_for_age(scenario: Scenario, age: int) -> float:
    """Real annual spending at a given age — the pure (path-independent) shapes.

    The deterministic and historical/MC engines call this (via the strategy
    wrapper in firebridgelab.spending) for the ``constant`` and ``lifecycle`` modes, so
    the spending math can't drift between modes (same discipline as the
    midpoint-convention equivalence test). It is a PURE function of age — it does
    NOT serve the path-dependent ``guardrails`` mode (that's GuardrailStrategy).

    - ``constant`` mode → returns ``scenario.annual_spending`` unchanged (today's
      behavior, byte-for-byte).
    - ``lifecycle`` mode → ``annual_spending`` is the GO-GO (peak) level scaled by
      the ``activity_multiplier`` taper. With the v1 defaults: go-go ≤74 → ×1.00,
      slow-go 75-84 → ×0.85, no-go ≥85 → ×0.75.

    Any non-lifecycle mode (including ``guardrails``, whose dollar path comes from
    its own strategy and never flows through here) returns the flat
    ``annual_spending`` — but the engines never call this for guardrail mode.
    """
    if scenario.spending_mode != SPENDING_MODE_LIFECYCLE:
        return scenario.annual_spending
    return scenario.annual_spending * activity_multiplier(scenario, age)


# Recognized mode value sets, for validate_scenario's "is this a known mode"
# checks. Kept beside the validator (and built from the mode constants above) so
# the membership test can't drift from the constants the engine actually honors.
_VALID_SPENDING_MODES = frozenset(
    {
        SPENDING_MODE_CONSTANT,
        SPENDING_MODE_LIFECYCLE,
        SPENDING_MODE_GUARDRAILS,
        SPENDING_MODE_FR_SMOOTH,
        SPENDING_MODE_FR_GUARDRAIL,
        SPENDING_MODE_FR_TIERS,
        SPENDING_MODE_AMORTIZATION,
    }
)
_VALID_ALLOCATION_MODES = frozenset(
    {ALLOCATION_MODE_CONSTANT, ALLOCATION_MODE_GLIDEPATH}
)
_VALID_SOURCING_MODES = frozenset({SOURCING_MODE_SINGLE, SOURCING_MODE_BUCKETS})

# Allocation-weight sum tolerance for validate_scenario. Matches the API's
# _parse_allocation 100%-sum check (1e-6); deliberately NOT stricter than the
# default scenario / existing fixtures satisfy (all sum to 1.0 exactly). The
# Historical / Monte Carlo engines RENORMALIZE weights, so this is a sanity guard
# against a clearly-broken allocation, not a hard engine precondition.
_ALLOCATION_SUM_TOLERANCE = 1e-6

# Permissive age sanity bounds for validate_scenario. Intentionally WIDER than the
# API's editorial 18..110 window (api.main._AGE_MIN/_AGE_MAX) — this is the
# low-layer "is this age a plausible integer, not garbage" guard, not the product
# range. Anything the API admits already sits inside this; tests that construct
# extreme horizons (e.g. start_age=0/end_age=199) drive the engine DIRECTLY and
# never flow through validate_scenario, so they are unaffected.
_VALIDATE_AGE_MIN = 0
_VALIDATE_AGE_MAX = 150


class ScenarioValidationError(ValueError):
    """A Scenario violates a construction invariant.

    Raised by ``validate_scenario`` on the FIRST violated invariant, with a
    specific, named-field message. Subclasses ``ValueError`` so existing
    engine-error → 422 translation paths (which catch ``ValueError``) keep
    working if a caller routes it through them.
    """


def validate_scenario(scenario: Scenario) -> None:
    """Validate a Scenario's construction invariants; raise on the first failure.

    Turns the previously-IMPLICIT preconditions the engine relies on into ONE
    explicit, testable seam. A malformed scenario used to crash DEEP and opaquely
    (``scenario.buckets.n1`` ``AttributeError`` when buckets is ``None`` in buckets
    mode; a glidepath with too few phases silently clamping; the tax split sum only
    checked inside ``tax_location.resolve_split``). This raises a single
    ``ScenarioValidationError`` naming the offending field on the FIRST violated
    invariant instead.

    Pure (no I/O). Lives in the low scenario layer and uses only this module's
    derived helpers plus a function-local import of the tax-split sum rule (to
    reuse its single source of truth without a module-top cycle). It does NOT
    import higher engine layers.

    Behavior-preserving: ``validate_scenario(default_scenario())`` passes, and so
    does every scenario the existing suite constructs. At the API boundary it runs
    AFTER the existing ``_validate_*`` checks, so it only converts
    previously-uncaught DEEP crashes into clean errors; inputs the API already
    rejects keep their existing message.
    """
    # --- Recognized mode constants -------------------------------------------
    if scenario.spending_mode not in _VALID_SPENDING_MODES:
        raise ScenarioValidationError(
            f"spending_mode '{scenario.spending_mode}' is not a recognized "
            f"spending mode"
        )
    if scenario.allocation_mode not in _VALID_ALLOCATION_MODES:
        raise ScenarioValidationError(
            f"allocation_mode '{scenario.allocation_mode}' is not a recognized "
            f"allocation mode"
        )
    if scenario.sourcing_mode not in _VALID_SOURCING_MODES:
        raise ScenarioValidationError(
            f"sourcing_mode '{scenario.sourcing_mode}' is not a recognized "
            f"sourcing mode"
        )

    # --- Portfolio / spending magnitudes -------------------------------------
    if scenario.initial_portfolio <= 0:
        raise ScenarioValidationError(
            f"initial_portfolio must be > 0 (got {scenario.initial_portfolio})"
        )
    if scenario.annual_spending < 0:
        raise ScenarioValidationError(
            f"annual_spending must be >= 0 (got {scenario.annual_spending})"
        )

    # --- Horizon / ages ------------------------------------------------------
    age_fields = (
        "start_age",
        "end_age",
        "person_a_pension_start_age",
        "person_b_pension_start_age",
        "person_a_ss_start_age",
        "person_b_ss_start_age",
    )
    for name in age_fields:
        value = getattr(scenario, name)
        if not (_VALIDATE_AGE_MIN <= value <= _VALIDATE_AGE_MAX):
            raise ScenarioValidationError(
                f"{name} must be between {_VALIDATE_AGE_MIN} and "
                f"{_VALIDATE_AGE_MAX} (got {value})"
            )
    if scenario.start_age >= scenario.end_age:
        raise ScenarioValidationError(
            f"start_age ({scenario.start_age}) must be less than end_age "
            f"({scenario.end_age})"
        )

    # --- Allocation ----------------------------------------------------------
    if not scenario.allocation:
        raise ScenarioValidationError(
            "allocation must contain at least one asset class"
        )
    weight_sum = sum(ac.weight for ac in scenario.allocation)
    if abs(weight_sum - 1.0) > _ALLOCATION_SUM_TOLERANCE:
        raise ScenarioValidationError(
            f"allocation weights must sum to 1.0 within "
            f"{_ALLOCATION_SUM_TOLERANCE:g} (got {weight_sum})"
        )

    # --- Spending rails ------------------------------------------------------
    # The operational floor must not exceed the satiation ceiling, or the
    # guardrail / funded-ratio clamps have an inverted band. Checked
    # unconditionally (cheap, and a supplied-but-broken pair shouldn't pass just
    # because the active spending mode ignores the rails).
    if scenario.spending_floor > scenario.spending_ceiling:
        raise ScenarioValidationError(
            f"spending_floor ({scenario.spending_floor}) must be <= "
            f"spending_ceiling ({scenario.spending_ceiling})"
        )

    # --- Real-drift knobs (stress-test design slice 4) ------------------------
    # Sanity bounds on the per-year real-drift rates: a plausible annual real
    # erosion/growth rate, not garbage. ±50% bounds the COLA-freeze / cost-of-
    # living drift well outside any real stress (the shipped stresses use 0.025
    # and 0.015). Default 0.0 always passes.
    for name in ("pension_real_drift", "spending_real_drift"):
        value = getattr(scenario, name)
        if not (-0.5 <= value <= 0.5):
            raise ScenarioValidationError(
                f"{name} must be between -0.5 and 0.5 (got {value})"
            )

    # --- Amortization-frontier planning return -------------------------------
    # The frontier discounts the gap stream at this real return. A value <= -1.0
    # makes the discount factor (1 + r) non-positive (division blows up); the upper
    # bound mirrors the drift knobs' plausible-real-return window. The slider only
    # ever sends [0, allocation-return]; bound it permissively here so garbage 422s
    # cleanly. Default 0.01 always passes.
    if not (-0.5 <= scenario.amortization_real_return <= 0.5):
        raise ScenarioValidationError(
            f"amortization_real_return must be between -0.5 and 0.5 (got "
            f"{scenario.amortization_real_return})"
        )

    # --- Bucket sourcing -----------------------------------------------------
    # buckets mode reads scenario.buckets.{n1,n2,...} unconditionally; a None
    # config is the opaque deep AttributeError this seam replaces.
    if scenario.sourcing_mode == SOURCING_MODE_BUCKETS:
        if scenario.buckets is None:
            raise ScenarioValidationError(
                "sourcing_mode is 'buckets' but no bucket config was provided "
                "(buckets is None)"
            )
        if scenario.buckets.n1 < 0 or scenario.buckets.n2 < 0:
            raise ScenarioValidationError(
                f"bucket sizing years must be non-negative (got n1="
                f"{scenario.buckets.n1}, n2={scenario.buckets.n2})"
            )

    # --- Glidepath allocation ------------------------------------------------
    # glidepath mode maps each DERIVED phase to phase_allocations by index. The
    # engine CLAMPS a SHORT list to the last supplied phase rather than crashing
    # (see allocation_at_age), so the phase COUNT is intentionally NOT validated
    # here — a short list is a legal run. Only two things are enforced: the list
    # must be PRESENT + NON-EMPTY (a MISSING / EMPTY phase_allocations under
    # glidepath mode is the silent degrade-to-constant the seam rejects), and no
    # individual phase may be empty (an empty phase tuple crashes the engine with
    # "no asset class maps to a known historical series").
    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        if not scenario.phase_allocations:
            raise ScenarioValidationError(
                "allocation_mode is 'glidepath' but no phase_allocations were "
                "provided"
            )
        for i, phase in enumerate(scenario.phase_allocations):
            if not phase:
                raise ScenarioValidationError(
                    f"glidepath phase_allocations[{i}] is empty; each phase must "
                    f"contain at least one asset class"
                )

    # --- Tax-location split --------------------------------------------------
    # When ANY of the three tax-location balances is supplied, all three are
    # treated as supplied (missing → 0) and must sum to initial_portfolio within
    # tolerance. Reuse the canonical sum rule from firebridgelab.tax_location via a
    # function-local import (tax_location imports scenario only lazily, so there
    # is no module-top cycle); this keeps the $1 tolerance single-sourced.
    from firebridgelab.tax_location import has_tax_split, resolve_split

    if has_tax_split(scenario):
        try:
            resolve_split(scenario)
        except ValueError as exc:
            raise ScenarioValidationError(str(exc)) from exc

    # --- Taxable-gains fraction (federal-tax overlay design ft1) --------------
    if not (0.0 <= scenario.taxable_gains_fraction <= 1.0):
        raise ScenarioValidationError(
            "taxable_gains_fraction must be between 0 and 1 (got "
            f"{scenario.taxable_gains_fraction})"
        )

    # --- Bracket-fill lever target (tax-drag design td1, DECISION 5) ----------
    # None = lever off, always valid. When set, bounded 0..5,000,000 (the
    # design's own range — a UI dollar field with presets at the real bracket
    # tops, not a rate, so the bound is a plausible dollar-income sanity check,
    # not a percentage).
    if scenario.bracket_fill_target is not None and not (
        0.0 <= scenario.bracket_fill_target <= 5_000_000.0
    ):
        raise ScenarioValidationError(
            "bracket_fill_target must be between 0 and 5,000,000 (got "
            f"{scenario.bracket_fill_target})"
        )
    # --- Review 2026-10-11 knobs ---------------------------------------------
    if not (0.0 <= scenario.annual_fee <= 0.03):
        raise ScenarioValidationError(
            f"annual_fee must be between 0 and 0.03 (got {scenario.annual_fee})"
        )
    if scenario.mc_distribution not in MC_DISTRIBUTIONS:
        raise ScenarioValidationError(
            f"mc_distribution must be one of {MC_DISTRIBUTIONS} "
            f"(got {scenario.mc_distribution!r})"
        )
    if not (2.5 <= scenario.mc_t_df <= 100.0):
        raise ScenarioValidationError(
            f"mc_t_df must be between 2.5 and 100 (got {scenario.mc_t_df})"
        )
    if scenario.pension_cola not in PENSION_COLAS:
        raise ScenarioValidationError(
            f"pension_cola must be one of {PENSION_COLAS} (got {scenario.pension_cola!r})"
        )
    if not (0.0 <= scenario.assumed_inflation <= 0.10):
        raise ScenarioValidationError(
            f"assumed_inflation must be between 0 and 0.10 (got {scenario.assumed_inflation})"
        )
    if scenario.survivor_age is not None and not (
        scenario.start_age < scenario.survivor_age <= scenario.end_age
    ):
        raise ScenarioValidationError(
            f"survivor_age must be after start_age and at most end_age "
            f"(got {scenario.survivor_age})"
        )
    if scenario.survivor_decedent not in SURVIVOR_DECEDENTS:
        raise ScenarioValidationError(
            f"survivor_decedent must be one of {SURVIVOR_DECEDENTS} "
            f"(got {scenario.survivor_decedent!r})"
        )
    for name in ("survivor_pension_fraction", "survivor_spending_fraction"):
        value = getattr(scenario, name)
        if not (0.0 <= value <= 1.0):
            raise ScenarioValidationError(f"{name} must be between 0 and 1 (got {value})")


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


def blended_real_return(scenario: Scenario) -> float:
    """Portfolio real return = weight-weighted average of class real returns.

    With a well-formed allocation (weights summing to 1) this is the
    portfolio's single deterministic real return. Default 60/40 Equity/Bonds
    @ 7%/2% → 0.60·0.07 + 0.40·0.02 = 0.050.
    """
    return sum(ac.weight * ac.real_return for ac in scenario.allocation)


def allocation_at_age(scenario: Scenario, age: int) -> tuple["AssetClass", ...]:
    """The allocation in force at ``age`` (glidepath design).

    Under ``constant`` allocation_mode (the default) this is always the single
    ``scenario.allocation`` — today's behavior, unchanged. Under ``glidepath`` it
    is the allocation of the phase containing ``age`` (phases are contiguous,
    half-open, lower-bound inclusive — see ``phase_index_for_age``).

    If glidepath is on but ``phase_allocations`` is missing/empty, this falls back
    to the single ``allocation`` (so a half-built glidepath scenario degrades to
    the constant blend rather than crashing). If there are FEWER supplied phase
    allocations than derived phases, the last supplied allocation carries the tail
    phases (clamp, don't crash) — a GI-age edit can add a phase between when the UI
    seeded ``phase_allocations`` and when the run fires.
    """
    if scenario.allocation_mode != ALLOCATION_MODE_GLIDEPATH:
        return scenario.allocation
    phases = scenario.phase_allocations
    if not phases:
        return scenario.allocation
    index = phase_index_for_age(scenario, age)
    # Clamp to the last supplied phase if the derived phase count outran the
    # supplied allocations (e.g. a fresh GI-age edit added a phase).
    if index >= len(phases):
        index = len(phases) - 1
    return phases[index]


def validate_glidepath_class_consistency(scenario: Scenario) -> None:
    """Enforce the v1 glidepath contract: per-class return ASSUMPTIONS are global.

    A glidepath changes WEIGHTS across phases; the per-class ``real_return`` and
    ``volatility`` are a property of the class, not of the phase. If the SAME class
    id carries DIFFERING ``real_return`` or ``volatility`` across phases, the engines
    silently disagree: the deterministic path reads each phase's own ``real_return``
    (``blended_real_return_at_age``), while the Monte Carlo sampler dedups the union
    of phase classes by id (first occurrence wins — one vol per sampled series), so
    the two would describe DIFFERENT portfolios with no error. Rather than blend
    silently, reject the inconsistency loudly.

    Raises ``ValueError`` naming the offending class. A no-op under ``constant``
    mode, when ``phase_allocations`` is empty, or when every phase agrees on each
    class's params (the legal case: same id, same params, different WEIGHTS).
    """
    if scenario.allocation_mode != ALLOCATION_MODE_GLIDEPATH:
        return
    phases = scenario.phase_allocations
    if not phases:
        return
    seen: dict[str, tuple[float, float | None]] = {}
    for phase in phases:
        for ac in phase:
            params = (ac.real_return, ac.volatility)
            prior = seen.get(ac.id)
            if prior is None:
                seen[ac.id] = params
            elif prior != params:
                raise ValueError(
                    f"class '{ac.id}' has conflicting real_return/volatility "
                    f"across glidepath phases; per-class return assumptions must be "
                    f"consistent — vary weights, not returns"
                )


def blended_real_return_at_age(scenario: Scenario, age: int) -> float:
    """Portfolio real return at a given age — the Deterministic engine's ``r``.

    Returns the rebalanced compound return of the allocation in force at
    ``age`` (``firebridgelab.monte_carlo.rebalanced_compound_return``): the weighted
    arithmetic mean less half the portfolio variance, from the same moments and
    correlations Monte Carlo samples. Under ``constant`` allocation_mode it is
    the same value every year; under ``glidepath`` it follows the active phase.
    ``blended_real_return`` (the plain weighted average of the class knobs)
    stays the display/validation figure.
    """

    # Lazy import: monte_carlo imports this module.
    from firebridgelab.monte_carlo import rebalanced_compound_return

    if scenario.allocation_mode != ALLOCATION_MODE_GLIDEPATH:
        return rebalanced_compound_return(scenario)
    allocation = allocation_at_age(scenario, age)
    correlations = scenario.correlations
    if correlations is not None and len(correlations) != len(allocation):
        correlations = None  # a union-order matrix does not fit one phase; estimate
    return rebalanced_compound_return(
        replace(
            scenario,
            allocation=allocation,
            allocation_mode=ALLOCATION_MODE_CONSTANT,
            phase_allocations=None,
            correlations=correlations,
        )
    )
