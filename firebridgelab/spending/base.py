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

from firebridgelab.engine.scenario import Scenario


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
