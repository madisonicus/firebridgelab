"""Pluggable spending strategies — the path-dependent generalization of
``spending_for_age`` (variable-spending design §"Architecture").

``scenario.spending_for_age(scenario, age)`` is a PURE function of age — perfect
for the ``constant`` and ``lifecycle`` shapes, which depend only on the age and
the scenario inputs. The Guyton-Klinger guardrail rule breaks that purity: this
year's spend depends on the *current portfolio* and the *prior spend* (the rule
carries state). So spending generalizes into a STATEFUL ``SpendingStrategy``
object the per-year loops call:

    strategy = make_strategy(scenario)          # ONCE, before the year loop
    spend = strategy.spend_for_year(scenario, age, begin_year_portfolio, gi)

Three strategies, one selector (``scenario.spending_mode``):

  - ``constant`` / ``lifecycle`` → ``PureAgeStrategy``: a thin wrapper over the
    EXISTING ``spending_for_age`` (ignores ``begin_year_portfolio``, carries no
    mutable state). This is the lowest-risk way to preserve byte-for-byte
    behavior — the canary depends on it (design: "constant and lifecycle modes
    must remain byte-for-byte unchanged").
  - ``guardrails`` → ``GuardrailStrategy``: the new stateful rule (below).

Why a strategy *object* and not a closure (plan-critic blocking fix): the
guardrail needs init-time anchors (initial portfolio, initial rate, the untapered
base) PLUS a mutable carried state (the nudged base). A stateless-looking
signature hid that required state and would force callers to improvise where it
lives. The object holds it explicitly, constructed once per simulation run
(per cohort / per trial / the single deterministic path) BEFORE the year loop.

The guardrail rule — pinned nudge→taper→clamp order (design §Architecture)
-------------------------------------------------------------------------
Each year, on the BEGIN-of-year portfolio (the balance BEFORE this year's growth
and withdrawal — in ``historical._simulate_cohort`` that is ``portfolio`` at the
top of the iteration):

  1. ``rate = max(0, base_spend - gi) / begin_year_portfolio`` — the portfolio-
     funded withdrawal rate on the UNtapered nudged base. If
     ``begin_year_portfolio <= 0``, SKIP nudging (the floor clamp governs).
  2. If ``rate > initial_rate*(1+GUARD)`` → ``base_spend *= (1 - ADJUST)`` (cut);
     elif ``rate < initial_rate*(1-GUARD)`` → ``base_spend *= (1 + ADJUST)``
     (raise); else hold. PERSIST the new ``base_spend`` as the carried state.
  3. ``tapered = base_spend * activity_multiplier(age)`` — the existing
     go-go/slow-go/no-go taper, composed as a post-multiplier on the nudged base.
  4. ``spend = clamp(tapered, floor(age), ceiling(age))``. FLOOR WINS if a tapered
     ceiling ever falls to/below the floor.

Carrying the *untapered* base as the state (not the post-taper number) is what
prevents the taper from double-applying at phase boundaries.

The rails (design §"The rails")
------------------------------
  - **Floor**: the OPERATIONAL ``spending_floor`` ($85k default — "we don't dip
    below this", the lowest level the household will accept), FLAT — NOT
    activity-tapered. The design: "spending can never drop below it"; the floor is
    a fixed viable-lifestyle ceiling-from-below, so it does not shrink with the
    activity taper. ($45k is the ABSOLUTE / survivable floor — context guaranteed
    income roughly reaches, drawn as a fainter reference line on the fan — NOT the
    clamp; see ``firebridgelab.scenario.ABSOLUTE_FLOOR``.)
  - **Ceiling**: ``spending_ceiling`` ($150k default), the satiation cap, IS
    activity-tapered (raises land mostly in the active years — "both rails are
    shaped by the existing activity taper" / "raises land mostly in the active
    years"). The floor is the rail the design holds flat; the ceiling tapers.

When ``spending_floor == annual_spending`` (the new default: $85k floor, $85k
start spend) the operational floor coincides with the start level, so a cut clamps
straight back to the floor and never registers as a spend below the start — the
adjustment stats then read ``pct_ever_cut`` → 0 and the deepest cut → the floor.
That is correct, not a bug: with the floor at the start level there is no room to
spend below it, and the squeeze shows up as years pinned AT the floor
(``median_years_at_floor``), the key stat in that regime.

Degenerate cases (plan-critic blocking fixes)
---------------------------------------------
  - ``initial_rate == 0`` (start spend ≤ start GI) → DISABLE the guardrail for the
    run: behave as plain tapered/clamped constant (nudging would otherwise make
    every nonzero rate a spurious cut, since both bands collapse to 0).
  - ``ADJUST == 0`` → the nudge is a no-op multiply (×1.0), so spend never moves:
    exactly tapered/clamped constant.
  - ``begin_year_portfolio <= 0`` (deep depletion) → step 1 skips nudging; the
    floor clamp governs the dollar spend.

Real-dollar engine — NO nominal inflation bookkeeping (design: "Real-dollar
engine — work in real terms throughout"). GK's nominal COLA rule does not map.
"""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.scenario import (
    SPENDING_MODE_AMORTIZATION,
    SPENDING_MODE_FR_GUARDRAIL,
    SPENDING_MODE_FR_SMOOTH,
    SPENDING_MODE_FR_TIERS,
    SPENDING_MODE_GUARDRAILS,
    Scenario,
    activity_multiplier,
    gi_at_age,
    is_survivor_year,
    spending_for_age,
)

# Default smoothness knob for the funded-ratio partial-adjustment strategy
# (fr-smooth). Each year the untapered base moves a fraction ``alpha`` of the way
# toward full funding: ``base *= (1 + alpha * (fr - 1))``. alpha = 1 would snap
# the base straight to the funded level each year (jumpy); alpha = 0 would freeze
# it (constant-equivalent). 0.5 damps the adjustment to roughly half-speed — the
# "smooth" in fr-smooth. Baked preset in v1 (NOT user-editable; the α input is a
# later slice). A module constant so a unit test can read the same value.
_FR_SMOOTH_ALPHA: float = 0.5

# Baked band-strategy preset for the funded-ratio GUARDRAIL strategy (fr-guardrail,
# FR feature slice 3). The FR analogue of the GUARD/ADJUST guardrails defaults, but
# the band is on the funded ratio itself rather than the withdrawal rate: hold while
# funding stays within ±20% of fully-funded (FR in [0.80, 1.20]); a ±10% nudge to the
# untapered base past either band. FR > 1.20 (over-funded) → raise; FR < 0.80
# (under-funded) → cut. Baked preset in v1 (NOT user-editable; a band/step input is a
# later slice). Module constants so a unit test can read the same values.
_FR_GR_UPPER: float = 1.20   # FR above this → raise the base
_FR_GR_LOWER: float = 0.80   # FR below this → cut the base
_FR_GR_STEP: float = 0.10    # per-trip proportional adjustment to the untapered base


# --------------------------------------------------------------------------- #
# Funded-ratio TIER LADDER preset (fr-tiers, FR feature slice 4 — the capstone).#
# --------------------------------------------------------------------------- #
#
# A FAITHFUL port of the legacy ``TierLadderPolicy`` "christmas tree": a funded-
# ratio band LADDER with hysteresis, a crash brake, and a hard floor. UNLIKE the
# other two FR variants (fr-smooth / fr-guardrail), fr-tiers is SELF-CONTAINED:
#
#   - It does NOT apply the go-go/slow-go/no-go activity taper. The tier AMOUNTS
#     already encode the phase shape ($160k go-go vs $130k slow-go vs $85k
#     later), so ``compute`` returns the tier amount DIRECTLY (with clamps).
#   - It does NOT use ``scenario.spending_floor`` / ``spending_ceiling``. Its
#     clamps are TIER-based (a market crash → the BELT tier amount; the hard
#     floor → the FLOOR tier amount) gated by its own ``_FR_TIER_FLOOR_THRESHOLD``
#     portfolio-level threshold, NOT the operational floor/ceiling rails.
#   - Its NPV target is the MAX go-go tier amount ($160k) — the load-bearing
#     invariant. The FR then reads "what fraction of the MAX scheduled spend can
#     the portfolio sustain forever", so the ladder starts conservative. Passing
#     the FLOOR here is the classic floor-vs-target bug (initial FR > 1 → the
#     policy launches at FULL/MAX and burns the portfolio); the strategy carries a
#     diagnostic warning that fires if the configured target is below the highest
#     tier amount, so that regression can never land silently.
#
# Baked preset config (see ``test_tier_ladder.py``) — v1 presets, NOT user-editable:
#   go_go (ages ≤ 65):  MAX≥1.50 $160k · BONUS≥1.25 $145k · FULL≥1.00 $130k ·
#                       BELT≥0.85 $118k · FLOOR≥0.00 $85k
#   slow_go (66–74):    MAX≥1.50 $130k · BONUS≥1.25 $85k · FULL≥0.85 $110k ·
#                       BELT≥0.70 $98k · FLOOR≥0.00 $85k
#   later (≥ 75):       flat $85k
# phase boundaries: go_go_end=65, slow_go_end=74. hysteresis band ±0.05.
# crash threshold 0.20 (a ≥20% YoY raw-portfolio drop AND current spend > BELT →
# clamp BELT, state "BELT (crash)"). hard floor → FLOOR, state "FLOOR (absolute)".
_FR_TIER_GO_GO_TIERS: tuple[tuple[str, float, float], ...] = (
    ("MAX",   1.50, 160_000.0),
    ("BONUS", 1.25, 145_000.0),
    ("FULL",  1.00, 130_000.0),
    ("BELT",  0.85, 118_000.0),
    ("FLOOR", 0.00, 85_000.0),
)
_FR_TIER_SLOW_GO_TIERS: tuple[tuple[str, float, float], ...] = (
    ("MAX",   1.50, 130_000.0),
    ("BONUS", 1.25, 121_000.0),
    ("FULL",  0.85, 110_000.0),
    ("BELT",  0.70, 98_000.0),
    ("FLOOR", 0.00, 85_000.0),
)
_FR_TIER_LATER_AMOUNT: float = 85_000.0
_FR_TIER_GO_GO_END: int = 65
_FR_TIER_SLOW_GO_END: int = 74
_FR_TIER_HYSTERESIS_BAND: float = 0.05
_FR_TIER_CRASH_THRESHOLD: float = 0.20
_FR_TIER_CRASH_CLAMP_NAME: str = "BELT"
_FR_TIER_FLOOR_CLAMP_NAME: str = "FLOOR"
# NPV target for the FR denominator = the MAX go-go tier amount ($160k). The
# load-bearing invariant (see the strategy docstring + the construction guard).
_FR_TIER_NPV_TARGET: float = 160_000.0
# Dynamic-floor target (the FLOOR tier amount, $85k) the floor-threshold formula
# discounts the GI gap against. NOT a spend clamp — it parameterizes the
# portfolio-level hard-floor threshold below.
_FR_TIER_FLOOR_TARGET: float = 85_000.0


def _fr_tier_floor_threshold(age: int, gi_at_age: float) -> float:
    """Legacy dynamic hard-floor threshold (portfolio level, real $).

    ``max(250_000, 3 × max(85_000 − GI, 0))`` — a $250k minimum, or 3× the
    annual gap below the $85k FLOOR-tier target, whichever is larger. When the
    SMOOTHED portfolio falls below this, the hard-floor clamp pulls spend down to
    the FLOOR tier. Faithful to the archived ``floor_threshold_fn`` built in
    ``test_tier_ladder.py`` (floor_minimum=$250k, multiplier=3.0).
    """
    annual_gap = max(_FR_TIER_FLOOR_TARGET - gi_at_age, 0.0)
    return max(250_000.0, 3.0 * annual_gap)


@dataclass(frozen=True)
class AdjustmentEvent:
    """One year's guardrail adjustment outcome (variable-spending design slice 2).

    The guardrail's per-year decision, recorded so the engine can surface the
    *cost of flexibility* — how often a cut/raise fires, when, and on what
    trigger. Purely descriptive: it does NOT change the spend (the spend is the
    strategy's return value, computed identically whether or not events are
    recorded), so recording events is byte-stable.

      - ``age``             — the age this decision was made at.
      - ``action``          — ``"cut"`` / ``"raise"`` / ``"hold"`` (what the band
        trip did to the untapered base this year; ``"hold"`` also covers a
        disabled guardrail or a non-positive begin balance, where no nudge fires).
      - ``spend``           — the WITHDRAWN real-dollar spend that year (post
        taper + clamp), so the drill-down can read the trigger in dollars
        ("cut to $108k") instead of the rate-based internal logic.
      - ``begin_portfolio`` — the begin-of-year portfolio (the rate denominator),
        so the trigger reads "portfolio fell to ~$1.7M".
      - ``gi``              — the year's summed guaranteed income.
      - ``gi_stepped_up``   — True iff GI rose vs the prior year (a pension/SS
        stream landed). The design's resolved decision: a raise in a GI-step year
        is GI-driven (the portfolio-funded rate dropped because GI arrived), NOT
        market outperformance — so the narrative must label it distinctly.
    """

    age: int
    action: str            # "cut" | "raise" | "hold"
    spend: float           # withdrawn real-dollar spend this year (post clamp)
    begin_portfolio: float
    gi: float
    gi_stepped_up: bool


class SpendingStrategyNotSolvable(ValueError):
    """Raised when the max-spend solver is asked to operate on a path-dependent
    strategy whose survival is non-monotonic in the spend dial.

    A ``ValueError`` subclass so the existing API ``except ValueError`` fallbacks
    still catch it (backwards-compat), but typed so callers that care to
    distinguish "this strategy isn't solvable" from other engine ValueErrors can
    do so via ``except SpendingStrategyNotSolvable`` without string-matching.
    The message is the strategy's ``reason_not_solvable`` — the canonical,
    user-facing sentence explaining why the solver can't run.
    """


class BucketsGuardrailsNotComposable(ValueError):
    """Raised when a ``sourcing_mode=buckets`` run is asked to compose with the
    ``guardrails`` spending strategy (bucket-strategy design: guardrails is FENCED
    in buckets mode — composing buckets + Guyton-Klinger is the committed slice 2,
    not this one).

    A ``ValueError`` subclass (so existing ``except ValueError`` fallbacks catch
    it) but typed — the same pattern as ``SpendingStrategyNotSolvable`` — so the
    API can translate it to a clear 422 by class, not by string-matching. The
    engine raises it (defense-in-depth) even though the API also validates the
    combination up front.
    """


class BucketsNotSolvable(ValueError):
    """Raised when the max-spend solver is asked to operate on a
    ``sourcing_mode=buckets`` scenario (bucket-strategy design: the solver is fenced
    in buckets mode, mirroring the guardrails solver fence — buckets sourcing is
    view-only in slice 1a).

    A typed ``ValueError`` subclass (parallel to ``SpendingStrategyNotSolvable``)
    so the API maps it to a 422 by class. The bucket cash-first / one-sided refill
    makes survival non-trivially dependent on the spend dial; a max-spend solve over
    buckets is deferred.
    """


class TaxDragMonteCarloNotSolvable(ValueError):
    """Raised when the max-spend solver is asked to run Monte Carlo mode with a
    tax-location split supplied (tax-drag design td1, DECISION 8's MC fence).

    Deterministic and Historical solves ride the dragged engine directly (a
    lean depletion-only evaluator, ``firebridgelab.tax_location.
    simulate_dragged_depletion_only``), but Monte Carlo's ~15-probe bisection
    over 10k dragged trials each (a per-year FIXED POINT, not a cheap scalar
    step) is a minutes-scale solve — too slow for an interactive solve, and not
    yet worth the engineering to make fast (a possible follow-up,
    per the design). A ``ValueError`` subclass (so existing ``except
    ValueError`` fallbacks still catch it) but typed — the same pattern as
    ``BucketsNotSolvable`` / ``SpendingStrategyNotSolvable`` — so the API maps
    it to a clear 422 by class, not by string-matching the message. Raised
    BEFORE any bisection runs (mirrors the other two solver fences).
    """


class SpendingStrategy:
    """Base spending strategy: maps a year to its real-dollar spend.

    ``spend_for_year(scenario, age, begin_year_portfolio, gi)`` returns the real
    spend for that year. ``begin_year_portfolio`` is the balance BEFORE this
    year's growth/withdrawal; ``gi`` is the year's summed guaranteed income. A
    strategy is constructed ONCE per simulation run (cohort / trial / the single
    deterministic path) and called once per age, in order, so a stateful strategy
    may carry mutable state across the calls.

    ``is_solvable`` / ``reason_not_solvable`` declare whether the max-spend
    solver can operate on this strategy (its survival is monotonic in the spend
    dial). The base defaults to solvable; only a path-dependent strategy
    (currently ``GuardrailStrategy`` / the FR strategies) overrides to False with
    a reason.

    ``collects_spends`` declares whether the engines should capture this
    strategy's per-year withdrawn spend + adjustment-event log (the spending fan
    + adjustment readouts). It is the strategy-level generalization of the old
    ``spending_mode == "guardrails"`` gate: True on the dynamic, path-dependent
    strategies whose spend varies across cohorts/trials (``GuardrailStrategy`` and
    the FR strategies via ``FundedRatioStrategyBase``), False on the byte-stable
    ``PureAgeStrategy`` whose spend is a pure function of age. The call sites in
    ``historical`` / ``monte_carlo`` test this predicate instead of the mode
    string, so a new collecting strategy gets the fan/adjustment readouts for
    free without touching those engines.
    """

    is_solvable: bool = True
    reason_not_solvable: str = ""
    collects_spends: bool = False

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        raise NotImplementedError


class PureAgeStrategy(SpendingStrategy):
    """The ``constant`` / ``lifecycle`` strategy — a thin wrapper over the pure
    ``spending_for_age`` helper.

    Ignores ``begin_year_portfolio`` and ``gi`` and carries NO mutable state, so a
    run under this strategy is byte-for-byte identical to calling
    ``spending_for_age`` directly the way the engines did before the strategy
    abstraction. The canary depends on this (design hard constraint).
    """

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        return spending_for_age(scenario, age)


class GuardrailStrategy(SpendingStrategy):
    """Two-sided Guyton-Klinger guardrail (the new stateful strategy).

    Captures its init-time anchors at construction (design):

      - ``initial_portfolio`` = ``scenario.initial_portfolio``
      - ``base_spend_0``      = ``scenario.annual_spending`` (the UNtapered base)
      - ``initial_gi``        = the GI active at ``start_age`` (0 for the default household)
      - ``initial_rate``      = ``max(0, base_spend_0 - initial_gi) / initial_portfolio``

    and carries mutable state ``base_spend`` (the nudged, untapered level) across
    years. If ``initial_rate == 0`` the guardrail is DISABLED (the run behaves as
    plain tapered/clamped constant) — ``enabled`` records that.

    The nudge→taper→clamp arithmetic per year is pinned in the module docstring;
    this is the executable form.
    """

    is_solvable: bool = False
    reason_not_solvable: str = (
        "max-spend solve is not available in guardrails mode (view-only)"
    )
    # Guardrails' spend varies across cohorts/trials → the engines collect the
    # per-year spend fan + adjustment events (the predicate that replaced the
    # ``spending_mode == "guardrails"`` gate at the historical/MC call sites).
    collects_spends: bool = True

    def __init__(self, scenario: Scenario) -> None:
        self.initial_portfolio = scenario.initial_portfolio
        self.base_spend_0 = scenario.annual_spending
        self.initial_gi = gi_at_age(scenario, scenario.start_age)
        # Portfolio-funded withdrawal rate at the start age, on the untapered base.
        # A non-positive initial portfolio (guarded at the API as >0) or a base ≤ GI
        # both yield initial_rate == 0 → guardrail disabled.
        if self.initial_portfolio > 0:
            self.initial_rate = max(0.0, self.base_spend_0 - self.initial_gi) / self.initial_portfolio
        else:
            self.initial_rate = 0.0
        # Degenerate: initial_rate == 0 disables the guardrail for the run.
        self.enabled = self.initial_rate > 0.0
        # Mutable carried state: the nudged, UNtapered base.
        self.base_spend = self.base_spend_0
        # Adjustment-event log (variable-spending design slice 2): one
        # AdjustmentEvent per spend_for_year call, in age order. Purely
        # descriptive — recording it never alters the returned spend, so the
        # canary holds. ``_prev_gi`` carries the prior year's GI so a raise in a
        # GI-step year can be flagged GI-driven (the resolved design decision).
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        guard = scenario.guard_band
        adjust = scenario.guard_adjust

        # 1+2. NUDGE (only when enabled and the begin-of-year balance is positive).
        # The rate is on the UNtapered nudged base, net of GI, over the begin-of-
        # year portfolio. begin_year_portfolio <= 0 skips nudging (floor governs).
        action = "hold"
        if self.enabled and begin_year_portfolio > 0:
            # After a first death the household spends ``survivor_spending_fraction``
            # of the rule's output (applied post-strategy by the engine loop), so
            # the withdrawal the rule sees must carry the same scale.
            household = (
                scenario.survivor_spending_fraction
                if is_survivor_year(scenario, age)
                else 1.0
            )
            rate = max(0.0, self.base_spend * household - gi) / begin_year_portfolio
            if rate > self.initial_rate * (1.0 + guard):
                self.base_spend *= (1.0 - adjust)   # cut toward the floor
                action = "cut"
            elif rate < self.initial_rate * (1.0 - guard):
                self.base_spend *= (1.0 + adjust)   # raise toward the ceiling
                action = "raise"
            # else: hold. Persist self.base_spend (it is the carried state).

        # Optional base clamp (review 2026-10-11): keep the carried state inside
        # the rails it is clamped to, so a long run of cuts at the floor (or
        # raises at the ceiling) cannot leave a hidden budget far outside what is
        # actually spent. Off by default (the original rule).
        if scenario.guard_clamp_base:
            taper = activity_multiplier(scenario, age)
            lo = scenario.spending_floor / taper if taper > 0 else self.base_spend
            hi = scenario.spending_ceiling
            self.base_spend = min(max(self.base_spend, lo), max(lo, hi))
        # 3. TAPER: the activity multiplier is a post-multiplier on the nudged base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 4. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor.
        floor = scenario.spending_floor
        ceiling = scenario.spending_ceiling * activity_multiplier(scenario, age)
        if ceiling < floor:
            spend = floor
        elif tapered < floor:
            spend = floor
        elif tapered > ceiling:
            spend = ceiling
        else:
            spend = tapered

        # Record the year's event (additive — does not touch the computed spend).
        # GI stepped up iff it rose vs the prior year; the first year has no prior,
        # so it is never a "step" (the start GI is the baseline, not an arrival).
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
        self._prev_gi = gi
        return spend


class FundedRatioStrategyBase(SpendingStrategy):
    """Carried-state SPINE for the funded-ratio spending strategies (FR feature
    slice 1, FR foundation). NOT a user-facing strategy on its own — the concrete
    variants (slices 2-4) subclass this and implement ``_decide``.

    The archived legacy framework centralized the FR plumbing in a
    ``PolicyContext`` the runner assembled each year. The current engine has a
    leaner seam: a strategy is built ONCE per run (cohort / trial / the single
    deterministic path) and ``spend_for_year(scenario, age, begin_year_portfolio,
    gi)`` is called once per year IN ORDER, so a strategy can carry the same state
    itself across the per-year calls. This base maintains everything the archived
    ``PolicyContext`` provided centrally:

      - **3-year smoothed portfolio** — the mean of the last ≤3
        ``begin_year_portfolio`` values (first year = the current value), matching
        the archived ``simulation.py`` smoothing ``sum(history[-3:]) / n``.
      - **previous begin-of-year portfolio** — for the crash YoY drop fraction
        ``(prev - cur) / prev`` (``None`` on the first call → no crash signal,
        exposed as ``crash_drop = 0.0`` / ``prev_portfolio = None``).
      - **previous tier/state name** — for hysteresis in the concrete variants
        (``None`` on the first call).
      - **previous spend** — for partial-adjustment / guardrail FR variants
        (``None`` on the first call).
      - **npv_gaps_by_age** + ``funded_ratio(age)`` — the NPV table is built ONCE
        in ``__init__`` from the scenario + the subclass-supplied
        ``npv_target_real``; ``funded_ratio(age) = smoothed / npv[age]`` with the
        archived's safe handling (npv ≤ 0 → a large sentinel ``_FR_SENTINEL``,
        i.e. GI covers everything → the portfolio is infinite-ratio safe).

    ``is_solvable = False`` — like ``GuardrailStrategy``, an FR strategy is
    path-dependent (its spend depends on the smoothed portfolio history + the
    prior state), so its survival is non-monotonic in a spend dial and the
    max-spend solver is fenced. ``collects_spends = True`` so the engines surface
    the FR variants' spending fan + adjustment readouts (same predicate as
    guardrails).

    The NPV target differs by variant — this is the load-bearing FR invariant,
    so ``npv_target_real`` is a CONSTRUCTOR PARAMETER each variant supplies:

      - ``fr-xmastree``  → the MAX go-go tier amount ($160k legacy). Discount the
        MAX tier, NOT the floor: the FR then reads "what fraction of MAX can the
        portfolio sustain forever", so the ladder starts conservative. Passing the
        floor here is the classic bug (initial FR > 1 → the policy launches at
        FULL/MAX and burns the portfolio).
      - ``fr-smooth`` / ``fr-guardrail`` → ``scenario.annual_spending`` (the
        baseline desired / go-go-peak spend), so FR = 1 means "the portfolio
        exactly funds the baseline".

    Concrete variants override ``_decide`` to turn the per-year state into a spend
    (and an optional state/tier name). The base's ``spend_for_year`` updates ALL
    the carried state, computes ``fr``, delegates the spend decision to
    ``_decide``, then records the returned ``(spend, state_name)`` as this year's
    ``prev_spend`` / ``prev_state`` for the next call.

    Slice-1 deliberately registers NO ``SPENDING_MODE_*`` and adds NO
    ``make_strategy`` branch — the spine is internal until the concrete variants
    land. The included trivial-echo unit test exercises the carried state directly.
    """

    is_solvable: bool = False
    reason_not_solvable: str = (
        "max-spend solve is not available in funded-ratio mode (view-only)"
    )
    collects_spends: bool = True

    # Smoothing window (years of begin-of-year portfolio averaged into the
    # FR numerator), matching the archived simulation.py ``history[-3:]`` window.
    _SMOOTH_WINDOW: int = 3
    # FR sentinel when the NPV denominator is ≤ 0 (GI covers the target at every
    # remaining age → the portfolio is infinite-ratio safe). Matches the archived
    # TierLadderPolicy ``_funded_ratio`` sentinel.
    _FR_SENTINEL: float = 999.0

    def __init__(self, scenario: Scenario, npv_target_real: float) -> None:
        # Imported lazily to avoid a module import cycle: firebridgelab.finance imports
        # from this engine layer, so importing it at module top would risk a
        # cycle. The loader is cheap and runs once per strategy construction.
        from firebridgelab.finance import compute_npv_gaps_by_age

        self.npv_target_real = float(npv_target_real)
        # The FR denominator table, built ONCE from the scenario + the variant's
        # target. Pure (no mutation of the scenario).
        self.npv_gaps_by_age: dict[int, float] = compute_npv_gaps_by_age(
            scenario, target_real=self.npv_target_real
        )

        # Carried state across the per-year calls (all None/empty at construction;
        # the first ``spend_for_year`` call populates the first-year values).
        self._history: list[float] = []        # last ≤3 begin-of-year portfolios
        self.smoothed: float = 0.0             # 3-year smoothed portfolio
        self.prev_portfolio: float | None = None   # prior begin-of-year balance
        self.crash_drop: float = 0.0           # YoY drop fraction (0 on first call)
        self.prev_state: str | None = None     # prior tier/state name (hysteresis)
        self.prev_spend: float | None = None   # prior year's withdrawn spend

    def funded_ratio(self, age: int) -> float:
        """FR = smoothed portfolio / NPV-of-gaps[age].

        Returns the large sentinel ``_FR_SENTINEL`` when the NPV is ≤ 0 (GI covers
        the target at every remaining age → infinite-ratio safe), matching the
        archived ``TierLadderPolicy._funded_ratio``. An age outside the precomputed
        table falls back to a denominator of 1.0 (defensive — callers only ever ask
        for in-horizon ages).
        """
        npv = self.npv_gaps_by_age.get(age, 1.0)
        if npv <= 0.0:
            return self._FR_SENTINEL
        return self.smoothed / npv

    def _decide(
        self,
        scenario: Scenario,
        age: int,
        fr: float,
        begin_year_portfolio: float,
        gi: float,
    ) -> tuple[float, str | None]:
        """Turn this year's funded-ratio context into ``(spend, state_name)``.

        The hook the concrete FR variants (slices 2-4) implement. By the time it
        runs the base has already updated the carried state for THIS year, so the
        implementation can read ``self.smoothed``, ``self.crash_drop``,
        ``self.prev_portfolio``, ``self.prev_state`` (the prior year's tier — not
        yet overwritten), ``self.prev_spend`` (the prior year's spend), and the
        passed ``fr`` / ``begin_year_portfolio`` / ``gi``. It must return the
        withdrawn real-dollar spend and an optional state/tier name (the name is
        carried into ``prev_state`` for next year's hysteresis; ``None`` is allowed
        for variants without a tier vocabulary).

        The base does NOT implement a spend policy — a concrete subclass must
        override this. The slice-1 spine ships only the carried-state machinery.
        """
        raise NotImplementedError(
            "FundedRatioStrategyBase._decide must be overridden by a concrete "
            "funded-ratio variant (slices 2-4)"
        )

    def spend_for_year(
        self,
        scenario: Scenario,
        age: int,
        begin_year_portfolio: float,
        gi: float,
    ) -> float:
        # --- update carried state for THIS year (before the decision) ---
        # Crash YoY drop fraction off the PRIOR begin-of-year balance. None prior
        # (first call) or a non-positive prior → no crash signal (0.0).
        if self.prev_portfolio is not None and self.prev_portfolio > 0.0:
            self.crash_drop = (
                self.prev_portfolio - begin_year_portfolio
            ) / self.prev_portfolio
        else:
            self.crash_drop = 0.0

        # Rolling ≤3-year smoothing buffer of begin-of-year portfolios. First year
        # the mean is just the current value (matches the archived
        # ``sum(history[-3:]) / n``).
        self._history.append(begin_year_portfolio)
        window = self._history[-self._SMOOTH_WINDOW:]
        self.smoothed = sum(window) / len(window)

        # --- funded ratio + delegated spend decision ---
        fr = self.funded_ratio(age)
        spend, state_name = self._decide(scenario, age, fr, begin_year_portfolio, gi)

        # --- advance carried state for the NEXT year's call ---
        self.prev_portfolio = begin_year_portfolio
        self.prev_state = state_name
        self.prev_spend = spend
        return spend


class FundedRatioSmoothStrategy(FundedRatioStrategyBase):
    """Funded-ratio PARTIAL-ADJUSTMENT spending strategy (FR feature slice 2).

    Mirrors ``GuardrailStrategy``'s pinned nudge→taper→clamp order so the two are
    directly A/B-comparable on the spending fan — the ONLY difference is the
    nudge: guardrails trips a discrete band on the withdrawal RATE; fr-smooth moves
    the untapered base a damped fraction of the way toward full funding each year,
    driven by the funded ratio (smoothed portfolio / NPV-of-the-baseline-stream).

    NPV target = ``scenario.annual_spending`` (the baseline desired / go-go-peak
    spend), baked at construction. So FR = 1 means "the smoothed portfolio exactly
    funds the baseline spending stream"; FR > 1 → over-funded (raise toward the
    ceiling); FR < 1 → under-funded (cut toward the floor).

    The carried state is the UNtapered base ``base_spend`` (seeded on the first
    call at ``scenario.annual_spending``). Carrying the untapered level — NOT the
    post-taper number — is what prevents the activity taper from double-applying at
    phase boundaries (the same invariant the guardrail base relies on).

    Per-year ``_decide`` (the executable form of the pinned order):

      1. PARTIAL-ADJUSTMENT (the design's recurrence)::

             base_spend = base_spend * (1 + alpha * (fr - 1))

         with ``alpha = _FR_SMOOTH_ALPHA`` (0.5). FR = 1 → ×1.0 (hold); FR > 1 →
         raise; FR < 1 → cut; alpha damps the speed. The new base PERSISTS as the
         carried state (set on ``self``, not recomputed from scratch).
      2. TAPER: ``tapered = base_spend * activity_multiplier(age)`` — the existing
         go-go/slow-go/no-go taper, a post-multiplier on the untapered base
         (exactly as ``GuardrailStrategy`` applies it).
      3. CLAMP: floor flat (``scenario.spending_floor``); ceiling activity-tapered
         (``scenario.spending_ceiling * activity_multiplier(age)``); FLOOR WINS if
         the tapered ceiling ever falls to/below the floor. (Copied verbatim from
         ``GuardrailStrategy`` so the rails behave identically.)

    Degenerate handling (mirrors guardrails): if ``begin_year_portfolio <= 0`` the
    FR adjustment is SKIPPED for the year (the base is held; the floor clamp
    governs the dollar spend). The FR is also clamped non-negative implicitly — a
    non-positive begin balance can't fund anything, so skipping is the honest move
    and it keeps the base from ever being driven non-positive by a runaway ``fr``.
    The ``(1 + alpha*(fr-1))`` factor with alpha = 0.5 and fr ≥ 0 is ≥ 0.5 > 0, so
    a positive base stays strictly positive on every adjusting year.

    Adjustment-stats readout (design decision — option (a)): fr-smooth emits one
    ``AdjustmentEvent`` per year so the EXISTING cost-of-flexibility readout +
    worst-cohort drill-down work unchanged. The ``action`` label is the SIGN of the
    FR-driven change to the untapered base this year — ``"raise"`` if the base went
    up, ``"cut"`` if down, ``"hold"`` if unchanged (FR ≈ 1, alpha = 0, or a
    non-positive begin balance). This is HONEST: it is a real, per-year, discrete
    fact about the direction the partial-adjustment moved the base — NOT a
    fabricated guardrail-band-trip. The aggregate stats (pct_ever_cut, years at
    floor, deepest cut) are computed from the realized spend SERIES vs the
    start/floor levels, which is mode-agnostic and equally meaningful here. The
    ``gi_stepped_up`` flag is tracked the same way (FR rises when GI lands because
    the NPV-of-gaps denominator shrinks), so a raise in a GI-step year is still
    labeled GI-driven in the drill-down.
    """

    def __init__(self, scenario: Scenario) -> None:
        # NPV target = the baseline desired spend → FR = 1 funds the baseline.
        super().__init__(scenario, npv_target_real=scenario.annual_spending)
        # Carried UNtapered base, seeded at the baseline (the go-go-peak level).
        self.base_spend: float = scenario.annual_spending
        # Adjustment-event log (option (a)) + prior-GI tracking for GI-driven-raise
        # labeling — same shape/discipline as GuardrailStrategy. Purely descriptive:
        # recording never alters the returned spend, so the canary holds.
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    def _decide(
        self,
        scenario: Scenario,
        age: int,
        fr: float,
        begin_year_portfolio: float,
        gi: float,
    ) -> tuple[float, str | None]:
        alpha = _FR_SMOOTH_ALPHA

        # 1. PARTIAL-ADJUSTMENT (skipped on a non-positive begin balance — the
        # floor clamp governs there; the base is held so it can't be driven
        # non-positive by a degenerate FR).
        action = "hold"
        if begin_year_portfolio > 0:
            factor = 1.0 + alpha * (fr - 1.0)
            new_base = self.base_spend * factor
            if new_base > self.base_spend + 1e-9:
                action = "raise"
            elif new_base < self.base_spend - 1e-9:
                action = "cut"
            self.base_spend = new_base   # PERSIST the carried state.

        # 2. TAPER: activity multiplier as a post-multiplier on the untapered base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 3. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor. (Verbatim from GuardrailStrategy.)
        floor = scenario.spending_floor
        ceiling = scenario.spending_ceiling * activity_multiplier(scenario, age)
        if ceiling < floor:
            spend = floor
        elif tapered < floor:
            spend = floor
        elif tapered > ceiling:
            spend = ceiling
        else:
            spend = tapered

        # Record the year's event (additive — does not touch the computed spend).
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
        self._prev_gi = gi
        return spend, action


class FundedRatioGuardrailStrategy(FundedRatioStrategyBase):
    """Funded-ratio BAND (guardrail) spending strategy (FR feature slice 3).

    A close sibling of ``FundedRatioSmoothStrategy``: same spine (3-year smoothed
    portfolio, NPV-of-the-baseline-stream denominator), same NPV target
    (``scenario.annual_spending`` → FR = 1 funds the baseline), same UNtapered
    carried base, the same nudge→taper→clamp order, and the same floor/ceiling
    rails. The ONLY difference is the per-year adjustment rule: where fr-smooth
    moves the base a damped fraction toward full funding every year, fr-guardrail
    trips a DISCRETE band on the funded ratio — hold inside the band, a fixed ±%
    nudge outside it (the FR analogue of the Guyton-Klinger rate band):

      - ``fr > _FR_GR_UPPER`` (over-funded) → ``base_spend *= (1 + _FR_GR_STEP)``
        (raise), action ``"raise"``;
      - ``fr < _FR_GR_LOWER`` (under-funded) → ``base_spend *= (1 - _FR_GR_STEP)``
        (cut), action ``"cut"``;
      - else (FR within ±20% of full) → hold, action ``"hold"``.

    Baked preset: ``_FR_GR_UPPER`` = 1.20, ``_FR_GR_LOWER`` = 0.80,
    ``_FR_GR_STEP`` = 0.10 (no user inputs in v1). The new base PERSISTS as the
    carried state. This variant has a genuine discrete cut/raise/hold each year, so
    the ``AdjustmentEvent`` vocabulary fits it natively (no honest-labeling
    gymnastics — the band literally trips or it doesn't).

    Degenerate handling (identical to fr-smooth / guardrails): a non-positive
    ``begin_year_portfolio`` SKIPS the adjustment for the year (the base is held;
    the floor clamp governs the dollar spend), which also keeps the base from being
    driven non-positive. The ±10% step on a positive base keeps the base strictly
    positive on every adjusting year.
    """

    def __init__(self, scenario: Scenario) -> None:
        # NPV target = the baseline desired spend → FR = 1 funds the baseline
        # (the SAME invariant fr-smooth uses).
        super().__init__(scenario, npv_target_real=scenario.annual_spending)
        # Carried UNtapered base, seeded at the baseline (the go-go-peak level).
        self.base_spend: float = scenario.annual_spending
        # Adjustment-event log + prior-GI tracking (same shape/discipline as
        # GuardrailStrategy / FundedRatioSmoothStrategy). Purely descriptive:
        # recording never alters the returned spend, so the canary holds.
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    def _decide(
        self,
        scenario: Scenario,
        age: int,
        fr: float,
        begin_year_portfolio: float,
        gi: float,
    ) -> tuple[float, str | None]:
        # 1. BAND TRIP (skipped on a non-positive begin balance — the floor clamp
        # governs there; the base is held so it can't be driven non-positive).
        action = "hold"
        if begin_year_portfolio > 0:
            if fr > _FR_GR_UPPER:
                self.base_spend *= (1.0 + _FR_GR_STEP)   # raise toward the ceiling
                action = "raise"
            elif fr < _FR_GR_LOWER:
                self.base_spend *= (1.0 - _FR_GR_STEP)   # cut toward the floor
                action = "cut"
            # else: hold (FR within the band). Persist self.base_spend.

        # 2. TAPER: activity multiplier as a post-multiplier on the untapered base.
        tapered = self.base_spend * activity_multiplier(scenario, age)

        # 3. CLAMP: floor flat, ceiling activity-tapered; FLOOR WINS if the tapered
        # ceiling ever falls to/below the floor. (Verbatim from GuardrailStrategy /
        # FundedRatioSmoothStrategy so the rails behave identically.)
        floor = scenario.spending_floor
        ceiling = scenario.spending_ceiling * activity_multiplier(scenario, age)
        if ceiling < floor:
            spend = floor
        elif tapered < floor:
            spend = floor
        elif tapered > ceiling:
            spend = ceiling
        else:
            spend = tapered

        # Record the year's event (additive — does not touch the computed spend).
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
        self._prev_gi = gi
        return spend, action


class FundedRatioTierStrategy(FundedRatioStrategyBase):
    """Funded-ratio TIER LADDER spending strategy (FR feature slice 4 — capstone).

    A FAITHFUL port of the legacy ``TierLadderPolicy`` "christmas tree": a funded-
    ratio band ladder with hysteresis, a crash brake, and a hard floor. The tier
    AMOUNTS encode the phase shape directly, so — UNLIKE fr-smooth / fr-guardrail —
    this strategy does NOT apply the activity taper and does NOT use the operational
    ``spending_floor`` / ``spending_ceiling`` rails. Its only clamps are tier-based,
    governed by its own portfolio-level floor threshold. See the module-level
    ``_FR_TIER_*`` preset block for the baked legacy config and the load-bearing
    NPV-target rationale.

    NPV target = the MAX go-go tier amount (``_FR_TIER_NPV_TARGET`` = $160k), passed
    to the base so the FR denominator is the highest scheduled spend (NOT the floor).
    The constructor carries the archived diagnostic: if the configured NPV target is
    below the highest tier amount, that is the classic floor-vs-target bug, so it
    ``warnings.warn``s — the guard against a silent regression.

    The per-year tier decision is factored into a PURE method ``_tier_decision``
    that takes controlled inputs (age, fr, smoothed, prev_portfolio, prev_state, gi)
    and returns ``(amount, state_name)``. ``_decide`` wires the spine's carried state
    into it. This pure method is what the golden tests drive with the exact inputs
    the archived ``PolicyContext`` tests used, asserting IDENTICAL outputs — the
    mechanism that proves bit-for-bit parity with the oracle.

    The archived ``compute()`` pipeline, ported IN ORDER:
      1. Phase routing — go_go (age ≤ 65) / slow_go (66–74) / later (≥ 75, flat).
      2. Unbanded tier lookup — scan tiers sorted DESC by fr_min, pick the first
         whose ``fr_min ≤ FR`` (FR uses the SMOOTHED portfolio).
      3. Hysteresis — if the prior state (qualifier stripped) is in this phase's
         ladder, hold the prior tier unless FR clears the next-higher fr_min+band
         OR drops below the current fr_min−band.
      4. Crash override — a YoY drop ≥ crash_threshold on the RAW portfolio (NOT
         smoothed) AND current spend > the BELT amount → clamp BELT, "BELT (crash)".
      5. Hard floor — the SMOOTHED portfolio < the dynamic floor threshold AND
         current spend > the FLOOR amount → clamp FLOOR, "FLOOR (absolute)".

    First-call (year-0) behavior reproduces the archived ``initial_decision``: on
    the first ``spend_for_year`` call ``smoothed == begin_year_portfolio`` (no
    history yet), ``prev_state`` is None (no hysteresis hold), and ``prev_portfolio``
    is None (no crash signal) — so ``_tier_decision`` collapses to a plain unbanded
    lookup on the raw initial portfolio's FR, exactly as the archived anchor did.
    """

    # Normalized tier ladders: ``[{name, fr_min, amount}]`` sorted DESC by fr_min.
    # Built once at class scope from the baked preset (the archived
    # ``_normalize_tiers`` discipline; the preset is already sorted, but we sort
    # defensively so the contract holds if the preset is ever reordered).
    _GO_GO = sorted(
        [{"name": n, "fr_min": f, "amount": a} for (n, f, a) in _FR_TIER_GO_GO_TIERS],
        key=lambda t: t["fr_min"], reverse=True,
    )
    _SLOW_GO = sorted(
        [{"name": n, "fr_min": f, "amount": a} for (n, f, a) in _FR_TIER_SLOW_GO_TIERS],
        key=lambda t: t["fr_min"], reverse=True,
    )

    def __init__(self, scenario: Scenario) -> None:
        # NPV target = the MAX go-go tier amount ($160k) — the load-bearing invariant.
        super().__init__(scenario, npv_target_real=_FR_TIER_NPV_TARGET)

        # Diagnostic guard (ported from the archived constructor): the NPV target
        # must be ≥ the highest tier amount across both phases. If it's lower, the
        # caller has almost certainly conflated the floor-protection threshold with
        # the NPV target (the classic floor-vs-target bug) — warn so it can never
        # silently regress.
        max_tier_amount = max(
            t["amount"] for t in self._GO_GO + self._SLOW_GO
        )
        if self.npv_target_real < max_tier_amount:
            import warnings
            warnings.warn(
                f"FundedRatioTierStrategy: npv_target_real "
                f"(${self.npv_target_real:,.0f}) is below the max tier amount "
                f"(${max_tier_amount:,.0f}). This is almost always the "
                "floor-vs-target bug — the FR denominator must be the MAX "
                "(scheduled) tier spend, not the floor.",
                UserWarning,
                stacklevel=2,
            )

        # Adjustment-event log + prior-GI tracking (same shape/discipline as the
        # sibling FR strategies). Purely descriptive — recording never alters the
        # returned spend, so the canary holds.
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    # ----- pure tier-ladder helpers (golden-testable) -----

    @staticmethod
    def _strip_qualifier(state: str | None) -> str | None:
        """'BELT (crash)' → 'BELT'; pass through None / plain names. (Archived.)"""
        if not state:
            return state
        return state.split()[0] if " " in state else state

    @classmethod
    def _phase(cls, age: int) -> str:
        if age <= _FR_TIER_GO_GO_END:
            return "go_go"
        if age <= _FR_TIER_SLOW_GO_END:
            return "slow_go"
        return "later"

    @classmethod
    def _tiers_for_phase(cls, phase: str) -> list[dict] | None:
        if phase == "go_go":
            return cls._GO_GO
        if phase == "slow_go":
            return cls._SLOW_GO
        return None

    @staticmethod
    def _unbanded_lookup(fr: float, tiers: list[dict]) -> dict:
        """First tier with ``fr_min ≤ fr`` (tiers sorted DESC). (Archived.)"""
        for t in tiers:
            if fr >= t["fr_min"]:
                return t
        return tiers[-1]   # safety: FR below the lowest band → the lowest tier

    @staticmethod
    def _find_tier(tiers: list[dict], name: str) -> dict | None:
        for t in tiers:
            if t["name"] == name:
                return t
        return None

    def _tier_decision(
        self,
        age: int,
        fr: float,
        smoothed: float,
        prev_portfolio: float | None,
        prev_state: str | None,
        gi: float,
        begin_year_portfolio: float,
    ) -> tuple[float, str]:
        """The PURE tier-ladder decision (the archived ``compute()`` pipeline).

        Faithful port. ``fr`` is the SMOOTHED funded ratio; ``smoothed`` is the
        smoothed portfolio (the hard-floor test); ``begin_year_portfolio`` /
        ``prev_portfolio`` are the RAW current / prior balances (the crash YoY drop,
        matching the archived ``ctx.portfolio_value`` / ``ctx.prev_portfolio_value``);
        ``prev_state`` is the prior tier/state name (hysteresis); ``gi`` is the
        year's guaranteed income (the dynamic floor threshold). Returns
        ``(amount, state_name)``.
        """
        # 1. Phase routing.
        phase = self._phase(age)
        if phase == "later":
            return _FR_TIER_LATER_AMOUNT, "LATER"

        tiers = self._tiers_for_phase(phase)
        assert tiers is not None  # non-None when phase != "later"

        # 2. Unbanded tier lookup as the baseline choice.
        raw_tier = self._unbanded_lookup(fr, tiers)

        # 3. Hysteresis. If prev_state strips to a name in this phase's ladder, hold
        # the prior tier unless FR has cleared the next-higher band+epsilon OR
        # dropped below the current band−epsilon.
        prev_clean = self._strip_qualifier(prev_state)
        chosen = raw_tier
        if prev_clean is not None:
            prev_tier = self._find_tier(tiers, prev_clean)
            if prev_tier is not None:
                prev_idx = next(
                    i for i, t in enumerate(tiers) if t["name"] == prev_clean
                )
                prev_fr_min = prev_tier["fr_min"]
                band = _FR_TIER_HYSTERESIS_BAND

                moved = False
                # Move UP only if FR clears the next-higher fr_min + band.
                if prev_idx > 0:
                    next_higher = tiers[prev_idx - 1]
                    if fr >= next_higher["fr_min"] + band:
                        chosen = self._unbanded_lookup(fr, tiers)
                        moved = True
                # Move DOWN only if FR drops below current fr_min − band.
                if not moved and fr < prev_fr_min - band:
                    chosen = self._unbanded_lookup(fr, tiers)
                    moved = True

                if not moved:
                    chosen = prev_tier

        amount = chosen["amount"]
        state_name = chosen["name"]

        # 4. Crash override. YoY RAW-portfolio drop ≥ crash_threshold AND current
        # spend > the crash-clamp (BELT) amount.
        if prev_portfolio is not None and prev_portfolio > 0:
            drop = (prev_portfolio - begin_year_portfolio) / prev_portfolio
            if drop >= _FR_TIER_CRASH_THRESHOLD:
                clamp = self._find_tier(tiers, _FR_TIER_CRASH_CLAMP_NAME)
                if clamp is not None and amount > clamp["amount"]:
                    amount = clamp["amount"]
                    state_name = f"{clamp['name']} (crash)"

        # 5. Hard floor. SMOOTHED portfolio < the dynamic floor threshold AND current
        # spend > the floor-clamp (FLOOR) amount.
        floor_threshold = _fr_tier_floor_threshold(age, gi)
        if smoothed < floor_threshold:
            clamp = self._find_tier(tiers, _FR_TIER_FLOOR_CLAMP_NAME)
            if clamp is not None and amount > clamp["amount"]:
                amount = clamp["amount"]
                state_name = f"{clamp['name']} (absolute)"

        return amount, state_name

    def _decide(
        self,
        scenario: Scenario,
        age: int,
        fr: float,
        begin_year_portfolio: float,
        gi: float,
    ) -> tuple[float, str | None]:
        # Wire the spine's carried state into the pure tier decision. The spine has
        # already updated ``self.smoothed`` for THIS year; ``self.prev_portfolio`` /
        # ``self.prev_state`` still hold last year's values (the next-year advance
        # happens after _decide returns). On the FIRST call prev_portfolio /
        # prev_state are None and smoothed == begin_year_portfolio → the unbanded
        # initial anchor, reproducing the archived ``initial_decision``.
        amount, state_name = self._tier_decision(
            age=age,
            fr=fr,
            smoothed=self.smoothed,
            prev_portfolio=self.prev_portfolio,
            prev_state=self.prev_state,
            gi=gi,
            begin_year_portfolio=begin_year_portfolio,
        )

        # Adjustment-event direction: compare THIS year's tier amount to the prior
        # withdrawn spend (a real, honest per-year fact — the tier ladder moved up,
        # down, or held). No fabricated guardrail-band semantics. The first year has
        # no prior spend → "hold".
        if self.prev_spend is None or abs(amount - self.prev_spend) <= 1e-9:
            action = "hold"
        elif amount > self.prev_spend:
            action = "raise"
        else:
            action = "cut"

        gi_stepped_up = self._prev_gi is not None and gi > self._prev_gi + 1e-9
        self.events.append(
            AdjustmentEvent(
                age=age,
                action=action,
                spend=amount,
                begin_portfolio=begin_year_portfolio,
                gi=gi,
                gi_stepped_up=gi_stepped_up,
            )
        )
        self._prev_gi = gi
        return amount, state_name


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
        from firebridgelab.finance import npv_gaps_at_age

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


def make_strategy(scenario: Scenario) -> SpendingStrategy:
    """Construct the spending strategy for a run from ``scenario.spending_mode``.

    Called ONCE per simulation run (cohort / trial / the single deterministic
    path), BEFORE the year loop, so a stateful strategy's anchors and carried
    state are scoped to that one run. ``guardrails`` → ``GuardrailStrategy``;
    everything else (``constant`` / ``lifecycle``) → the byte-stable
    ``PureAgeStrategy`` wrapper over ``spending_for_age``.
    """
    if scenario.spending_mode == SPENDING_MODE_GUARDRAILS:
        return GuardrailStrategy(scenario)
    if scenario.spending_mode == SPENDING_MODE_FR_SMOOTH:
        return FundedRatioSmoothStrategy(scenario)
    if scenario.spending_mode == SPENDING_MODE_FR_GUARDRAIL:
        return FundedRatioGuardrailStrategy(scenario)
    if scenario.spending_mode == SPENDING_MODE_FR_TIERS:
        return FundedRatioTierStrategy(scenario)
    if scenario.spending_mode == SPENDING_MODE_AMORTIZATION:
        return AmortizationFrontierStrategy(scenario)
    return PureAgeStrategy()
