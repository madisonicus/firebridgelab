"""spending.funded_ratio (split from the flat layout)."""

from __future__ import annotations

from firebridgelab.engine.scenario import Scenario
from firebridgelab.spending.base import SpendingStrategy


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
        from firebridgelab.spending.npv import compute_npv_gaps_by_age

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
