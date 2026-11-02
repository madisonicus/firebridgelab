"""The ``fr_tiers`` spending mode: the funded-ratio tier ladder.

A port of the legacy ``TierLadderPolicy`` "christmas tree": a funded-ratio band
ladder with hysteresis, a crash brake and a hard floor, where the tier AMOUNTS
encode the go-go/slow-go/later phase shape ($160k go-go MAX → $85k FLOOR). It
applies no activity taper and ignores the ``spending_floor`` / ``spending_ceiling``
rails; its clamps are tier-based (crash → BELT, hard floor → FLOOR). Its NPV
target is the MAX go-go tier amount, not ``annual_spending``. Path-dependent and
view-only (the max-spend solver is fenced); the ladder is a fixed preset.
"""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.spending.base import AdjustmentEvent
from firebridgelab.spending.funded_ratio import FundedRatioStrategyBase
from firebridgelab.spending.modes import SPENDING_MODE_FR_TIERS
from firebridgelab.spending.registry import register


# --------------------------------------------------------------------------- #
# Funded-ratio TIER LADDER preset (fr-tiers).                                 #
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
# Baked preset config (see ``test_tier_ladder.py``) — NOT user-editable:
#   go_go (ages ≤ 65):  MAX≥1.50 $160k · BONUS≥1.25 $145k · FULL≥1.00 $130k ·
#                       BELT≥0.85 $118k · FLOOR≥0.00 $85k
#   slow_go (66–74):    MAX≥1.50 $130k · BONUS≥1.25 $121k · FULL≥0.85 $110k ·
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
    the FLOOR tier. Matches the legacy ``floor_threshold_fn`` built in
    ``test_tier_ladder.py`` (floor_minimum=$250k, multiplier=3.0).
    """
    annual_gap = max(_FR_TIER_FLOOR_TARGET - gi_at_age, 0.0)
    return max(250_000.0, 3.0 * annual_gap)


@register(SPENDING_MODE_FR_TIERS)
class FundedRatioTierStrategy(FundedRatioStrategyBase):
    """Funded-ratio TIER LADDER spending strategy.

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
    The constructor carries a diagnostic: if the configured NPV target is
    below the highest tier amount, that is the classic floor-vs-target bug, so it
    ``warnings.warn``s — the guard against a silent regression.

    The per-year tier decision is factored into a PURE method ``_tier_decision``
    that takes controlled inputs (age, fr, smoothed, prev_portfolio, prev_state, gi)
    and returns ``(amount, state_name)``. ``_decide`` wires the spine's carried state
    into it. The golden tests drive this pure method with the inputs the legacy
    ``PolicyContext`` tests used and assert identical outputs.

    The legacy ``compute()`` pipeline, ported IN ORDER:
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

    First-call (year-0) behavior reproduces the legacy ``initial_decision``: on
    the first ``spend_for_year`` call ``smoothed == begin_year_portfolio`` (no
    history yet), ``prev_state`` is None (no hysteresis hold), and ``prev_portfolio``
    is None (no crash signal) — so ``_tier_decision`` collapses to a plain unbanded
    lookup on the raw initial portfolio's FR.
    """

    # Normalized tier ladders: ``[{name, fr_min, amount}]`` sorted DESC by fr_min.
    # Built once at class scope from the baked preset (the preset is already
    # sorted, but we sort defensively so the contract holds if the preset is ever
    # reordered).
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

        # Diagnostic guard: the NPV target
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
        # returned spend.
        self.events: list[AdjustmentEvent] = []
        self._prev_gi: float | None = None

    # ----- pure tier-ladder helpers (golden-testable) -----

    @staticmethod
    def _strip_qualifier(state: str | None) -> str | None:
        """'BELT (crash)' → 'BELT'; pass through None / plain names."""
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
        """First tier with ``fr_min ≤ fr`` (tiers sorted DESC)."""
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
        """The PURE tier-ladder decision (the legacy ``compute()`` pipeline).

        ``fr`` is the SMOOTHED funded ratio; ``smoothed`` is the
        smoothed portfolio (the hard-floor test); ``begin_year_portfolio`` /
        ``prev_portfolio`` are the RAW current / prior balances (the crash YoY drop);
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
        # initial anchor, reproducing the legacy ``initial_decision``.
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
        # withdrawn spend (the tier ladder moved up, down, or held). The first year
        # has no prior spend → "hold".
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
