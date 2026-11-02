"""The spending-strategy contract shared by every rule in ``spending.rules``.

``spending_for_age(scenario, age)`` (``spending.lifecycle``) is a pure function
of age, which suffices for the ``constant`` and ``lifecycle`` shapes. The
path-dependent rules are not: this year's spend depends on the current
portfolio and on state carried from earlier years. So spending is a stateful
``SpendingStrategy`` object the per-year loops call:

    strategy = make_strategy(scenario)          # ONCE per run, before the year loop
    spend = strategy.spend_for_year(scenario, age, begin_year_portfolio, gi)

``make_strategy`` (``spending.registry``) picks the rule from
``scenario.spending_mode``. ``begin_year_portfolio`` is the balance BEFORE this
year's growth and withdrawal; ``gi`` is the year's summed guaranteed income.

Why an object and not a closure: a rule like the guardrail needs init-time
anchors (initial portfolio, initial rate, the untapered base) PLUS mutable
carried state (the nudged base). The object holds both explicitly, constructed
once per simulation run (per cohort / per trial / the single deterministic
path) before the year loop.

Real-dollar engine — no nominal inflation bookkeeping.

The module also holds ``AdjustmentEvent`` (the per-year decision log the
dynamic rules record) and the typed ``ValueError`` subclasses the engines and
solver raise for unsupported combinations.
"""

from __future__ import annotations

from dataclasses import dataclass

from firebridgelab.engine.scenario import Scenario


@dataclass(frozen=True)
class AdjustmentEvent:
    """One year's adjustment outcome from a dynamic spending rule.

    The rule's per-year decision, recorded so the engine can surface the
    *cost of flexibility* — how often a cut/raise fires, when, and on what
    trigger. Purely descriptive: it does NOT change the spend (the spend is the
    strategy's return value, computed identically whether or not events are
    recorded).

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
        stream landed). A raise in a GI-step year is GI-driven (the portfolio-funded
        rate dropped because GI arrived), NOT market outperformance — so the narrative
        must label it distinctly.
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

    A ``ValueError`` subclass so the API's ``except ValueError`` fallbacks
    still catch it, but typed so callers that care to
    distinguish "this strategy isn't solvable" from other engine ValueErrors can
    do so via ``except SpendingStrategyNotSolvable`` without string-matching.
    The message is the strategy's ``reason_not_solvable`` — the canonical,
    user-facing sentence explaining why the solver can't run.
    """


class BucketsGuardrailsNotComposable(ValueError):
    """Raised when a ``sourcing_mode=buckets`` run is asked to compose with the
    ``guardrails`` spending strategy; the combination is not supported.

    A ``ValueError`` subclass (so ``except ValueError`` fallbacks catch
    it) but typed — the same pattern as ``SpendingStrategyNotSolvable`` — so the
    API can translate it to a clear 422 by class, not by string-matching. The
    engine raises it (defense-in-depth) even though the API also validates the
    combination up front.
    """


class BucketsNotSolvable(ValueError):
    """Raised when the max-spend solver is asked to operate on a
    ``sourcing_mode=buckets`` scenario; buckets sourcing is view-only.

    A typed ``ValueError`` subclass (parallel to ``SpendingStrategyNotSolvable``)
    so the API maps it to a 422 by class. The bucket cash-first / one-sided refill
    makes survival non-trivially dependent on the spend dial.
    """


class TaxDragMonteCarloNotSolvable(ValueError):
    """Raised when the max-spend solver is asked to run Monte Carlo mode with a
    tax-location split supplied.

    Deterministic and Historical solves ride the dragged engine directly (a
    lean depletion-only evaluator, ``simulate_dragged_depletion_only`` in
    ``tax.location_path``), but Monte Carlo's ~15-probe bisection
    over 10k dragged trials each (a per-year FIXED POINT, not a cheap scalar
    step) is a minutes-scale solve — too slow for an interactive solve.
    A ``ValueError`` subclass (so ``except
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
    + adjustment readouts): True on the dynamic, path-dependent
    strategies whose spend varies across cohorts/trials (``GuardrailStrategy`` and
    the FR strategies via ``FundedRatioStrategyBase``), False on
    ``PureAgeStrategy`` whose spend is a pure function of age. The engines test
    this predicate instead of the mode string, so a new collecting strategy gets
    the fan/adjustment readouts without touching the engines.
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
