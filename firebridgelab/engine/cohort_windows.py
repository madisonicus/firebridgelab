"""Historical cohort windows: which start years are replayable, and their returns.

Builds each cohort's per-year blended real-return array for the constant,
glidepath and bucket paths, shared by ``engine.historical`` and the solver
(``cohort_return_sequences``). The window policy for multi-class mixes:

  - **Selected = weight > ε** (``HISTORICAL_SELECTION_EPSILON``). A 0%-weight
    (but mapped) class does NOT constrain the window — otherwise a preset that
    lists many classes at 0% would collapse every window to the shortest history.
  - **No-backfill common-overlap window.** Valid cohort starts are exactly the
    years Y where EVERY selected series has data for the FULL horizon
    (Y .. Y+horizon-1). A short-history class (e.g. ``us_reit``) restricts the
    cohorts honestly; missing pre-inception years are never backfilled.
  - **Hide / renormalize.** A class with NO loaded series is hidden and the
    remaining weights renormalized; a loaded-but-short class is NOT hidden — it
    constrains the window instead.

Series are aligned by integer year label (the calendar-phase convention in
``engine.historical``).
"""

from __future__ import annotations

import numpy as np

from firebridgelab.engine.allocation import ALLOCATION_MODE_GLIDEPATH, allocation_at_age
from firebridgelab.engine.scenario import Scenario
from firebridgelab.returns.series import (
    HISTORICAL_SELECTION_EPSILON,
    HIST_SERIES_REGISTRY,
    SERIES_REGISTRY,
    common_overlap_start_years,
    glidepath_phase_weights,
    historical_series_by_id,
    mapped_series_for,
    renormalized_weights,
    selected_series_for,
)
from firebridgelab.returns.shiller import ShillerData, load_shiller


class NoReplayableWindow(ValueError):
    """Raised when a Historical run's selected classes share no common stretch of
    history long enough to replay the requested horizon (every "no cohort has a
    full N-year window" condition: the multi-class / glidepath / bucket common
    overlap, the legacy Shiller-only span, and the comparison-window floor).

    A ``ValueError`` subclass (so the API's existing ``except ValueError``
    fallbacks still catch it) but typed — the same pattern as
    ``SpendingStrategyNotSolvable`` — so the API can translate it to a friendly
    422 by class, not by string-matching the cohort-span message text. The
    carried message is the technical cohort-window detail (n-year span, the
    per-series year ranges) — correct for a log, mapped to plain language at the
    API seam.
    """


def _apply_window_floor(
    start_years: list[int], window_start_year: int | None
) -> list[int]:
    """Restrict valid cohort starts to those at or after ``window_start_year``.

    The comparison-window alignment hook: when two scenarios with
    DIFFERENT native testable windows are compared, the web layer passes the
    COMMON (latest) floor = ``max(testable_since_A, testable_since_B)`` to BOTH so
    the head-to-head scores them on the SAME cohort set (otherwise a buckets
    scenario testable since 1954 is measured only on the brutal post-1954 cohorts
    while a 60/40 since 1871 is measured on all of history — misleading).

    This ONLY narrows: it drops cohort starts earlier than the floor. It never
    fabricates / expands cohorts (every kept year was already a valid full-horizon
    start), so a floor at or below the native earliest start is a no-op. ``None``
    (single, non-comparison runs) is the identity — the native window is kept.

    Raises ``ValueError`` (the same "no cohort has a full window" shape the window
    builders raise) if the floor removes EVERY cohort, so the API surfaces it as a
    422 rather than aggregating an empty cohort set.
    """
    if window_start_year is None:
        return start_years
    floored = [y for y in start_years if y >= window_start_year]
    if not floored:
        raise NoReplayableWindow(
            "no cohort has a full window at or after the requested "
            f"window_start_year {window_start_year} "
            f"(valid starts span {start_years[0]}-{start_years[-1]})"
            if start_years
            else f"no cohort has a full window at or after {window_start_year}"
        )
    return floored


def _cohort_blended_returns(
    weights: dict[str, float], period: dict[str, np.ndarray]
) -> np.ndarray:
    """Year-by-year blended real returns for one cohort.

    ``weights`` are the renormalized per-series weights; ``period`` maps each
    series id to its array of real returns for the cohort's window. The blend is
    the weight-weighted sum across series, computed per year.
    """
    blended = None
    for series_id, w in weights.items():
        contribution = w * period[series_id]
        blended = contribution if blended is None else blended + contribution
    return blended


def _glidepath_cohort_blended_returns(
    scenario: Scenario,
    period: dict[str, np.ndarray],
    registry: dict[str, str],
) -> np.ndarray:
    """Year-by-year blended real returns under glidepath (phase-varying weights).

    The glidepath analogue of ``_cohort_blended_returns``: instead of one constant
    weight set, each age-step uses the renormalized per-series weights of the phase
    in force at that age (``phase_index_for_age``). ``period`` maps each series id
    to its cohort-window return array (length ``n_steps``).

    Built per-step so the weights track the phase boundaries exactly the
    deterministic engine does. A series a given phase doesn't weight contributes
    0 that year (``weights.get(sid, 0.0)``), so phases that select different
    classes compose cleanly. Reduces to ``_cohort_blended_returns`` when every
    phase shares the same weights (the degenerate-glide identity).
    """
    from firebridgelab.engine.allocation import phase_index_for_age

    phase_weights = glidepath_phase_weights(scenario, registry)
    n_steps = len(next(iter(period.values())))
    blended = np.zeros(n_steps, dtype=float)
    for step, age in enumerate(range(scenario.start_age, scenario.end_age + 1)):
        index = phase_index_for_age(scenario, age)
        if index >= len(phase_weights):
            index = len(phase_weights) - 1
        weights = phase_weights[index]
        total = 0.0
        for series_id, arr in period.items():
            total += weights.get(series_id, 0.0) * arr[step]
        blended[step] = total
    return blended


def _period_from_series(
    series_data: dict[str, dict[int, float]],
    series_ids: list[str],
    start_year: int,
    n_steps: int,
) -> dict[str, np.ndarray]:
    """One cohort's per-series real-return arrays from the year-keyed accessor.

    The multi-class analogue of ``ShillerData.returns_for_period``: for each
    series id, slices years ``start_year .. start_year+n_steps-1`` out of the
    year-keyed map into a parallel array. Aligns all series by their shared
    integer year label (the calendar-phase convention documented in
    ``engine.historical``). Assumes the window is valid for every series (the caller built
    ``start_year`` from ``common_overlap_start_years``, so every cell exists).
    """
    period: dict[str, np.ndarray] = {}
    for sid in series_ids:
        by_year = series_data[sid]
        period[sid] = np.array(
            [by_year[start_year + k] for k in range(n_steps)], dtype=float
        )
    return period


def _is_legacy_shiller_only(scenario: Scenario) -> bool:
    """True iff the mapped allocation is exactly the two Shiller series.

    The legacy-path gate: when the only mapped+selected classes resolve to the
    Shiller pair (``us_equity`` / ``us_bonds``), the window policy is a no-op
    (both 1871+), and we run the ``ShillerData`` cohort path so the default
    2-class Historical run is unchanged. Any class that maps to a NON-Shiller
    series routes to the multi-class path instead. A 0%-weight non-Shiller class still counts as
    mapped here, but it is dropped by ``selected_series_for`` in the multi-class
    path, so the window stays correct either way; we route on *mapped* (not
    selected) so even a 0%-weight new class takes the multi-class accessor (whose
    blend is identical for the Shiller pair, since the year-keyed view is built
    from the same Shiller data).
    """
    mapped = mapped_series_for(scenario, HIST_SERIES_REGISTRY)
    series = set(mapped.values())
    return bool(series) and series <= set(SERIES_REGISTRY)


def _glidepath_phase_scenarios(scenario: Scenario) -> list[Scenario]:
    """One single-allocation scenario per glidepath phase (for series selection).

    Each phase's allocation as its OWN constant-allocation scenario, so the
    existing series-selection helpers (``mapped_series_for`` / ``selected_series_for``)
    can be reused per phase without teaching them about glidepath. Used only to
    derive which series each phase touches and the window union — the actual blend
    uses ``_glidepath_cohort_blended_returns`` over the real (glidepath) scenario.
    """
    from dataclasses import replace

    from firebridgelab.engine.allocation import glidepath_boundaries

    boundaries = glidepath_boundaries(scenario)
    return [
        replace(scenario, allocation=allocation_at_age(scenario, start_age))
        for start_age in boundaries
    ]


def _glidepath_window(
    scenario: Scenario,
) -> tuple[list[str], list[int], dict[str, dict[int, float]]]:
    """Series union + common-overlap window for a glidepath scenario (Historical).

    Glidepath generalizes the window policy across phases: the cohort window
    must cover EVERY series ANY phase selects, so the selected-series set is the
    UNION of each phase's selected series (a series only one phase uses still
    needs full-horizon data wherever that phase fires). Computes the no-backfill
    common-overlap window over that union.

    Returns ``(selected_series, start_years, series_data)``. Raises ``ValueError``
    (no mapped class anywhere / no class clears epsilon / no common window) with
    the same messages the constant multi-class path uses, so the API's 422
    mapping is unchanged.
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    phase_scenarios = _glidepath_phase_scenarios(scenario)

    union: set[str] = set()
    any_mapped = False
    for ps in phase_scenarios:
        if mapped_series_for(ps, HIST_SERIES_REGISTRY):
            any_mapped = True
        union |= set(selected_series_for(ps, HIST_SERIES_REGISTRY))
    if not any_mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )
    if not union:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    series_data = historical_series_by_id()
    selected_series = sorted(union)
    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )
    return selected_series, start_years, series_data


def _bucket_scenarios(scenario: Scenario) -> dict[str, Scenario]:
    """One single-allocation scenario per bucket (for series selection / blending).

    Each bucket's allocation as its OWN constant-allocation, single-sourcing
    scenario, so the existing series-selection + blend helpers
    (``mapped_series_for`` / ``selected_series_for`` / ``renormalized_weights`` /
    ``_cohort_blended_returns``) can be reused per bucket WITHOUT teaching them
    about buckets. ``sourcing_mode`` is reset to single + ``allocation_mode`` to
    constant on each so those helpers see a plain allocation. Keyed by bucket id.
    """
    from dataclasses import replace

    bc = scenario.buckets
    out: dict[str, Scenario] = {}
    for bucket in (bc.cash, bc.income, bc.growth):
        out[bucket.id] = replace(
            scenario,
            allocation=bucket.allocation,
            sourcing_mode="single",
            allocation_mode="constant",
            phase_allocations=None,
            buckets=None,
        )
    return out


def _bucket_window(
    scenario: Scenario,
) -> tuple[dict[str, dict[str, float]], list[str], list[int], dict[str, dict[int, float]]]:
    """Per-bucket renormalized weights + common-overlap window for a bucket scenario.

    The buckets analogue of ``_multiclass_window``: the cohort window must cover
    EVERY series ANY bucket selects, so the selected-series set is the UNION of
    each bucket's selected series, and the no-backfill common-overlap window is
    computed over that union (a series only the income bucket uses still needs
    full-horizon data). Each bucket's blend weights are its OWN renormalized
    per-series weights (``renormalized_weights`` over the bucket's allocation).

    Returns ``(bucket_weights, selected_series, start_years, series_data)`` where
    ``bucket_weights[bucket_id]`` is ``{series_id: renormalized_weight}`` for that
    bucket. Raises ``ValueError`` (no mapped class anywhere / no class clears the
    epsilon / no common window) with the same messages the multi-class path uses,
    so the API's 422 mapping is unchanged.
    """
    n_steps = scenario.end_age - scenario.start_age + 1
    bucket_scenarios = _bucket_scenarios(scenario)

    bucket_weights: dict[str, dict[str, float]] = {}
    union: set[str] = set()
    any_mapped = False
    for bid, bs in bucket_scenarios.items():
        if mapped_series_for(bs, HIST_SERIES_REGISTRY):
            any_mapped = True
        bucket_weights[bid] = renormalized_weights(bs, HIST_SERIES_REGISTRY)
        union |= set(selected_series_for(bs, HIST_SERIES_REGISTRY))
    if not any_mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )
    if not union:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    series_data = historical_series_by_id()
    selected_series = sorted(union)
    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )
    return bucket_weights, selected_series, start_years, series_data


def cohort_return_sequences(
    scenario: Scenario, data: ShillerData | None = None
) -> tuple[list[int], list[np.ndarray]]:
    """Per-cohort blended real-return arrays + their start years (spend-free).

    The single source of cohort construction shared by ``simulate_historical``
    and the solver's lean depletion-only loop, so the two can't drift in which
    cohorts they run or how returns blend. Dispatches on the SAME legacy-path
    gate (``_is_legacy_shiller_only``):

    - Legacy Shiller path: ``ShillerData.all_starting_years`` +
      ``returns_for_period`` (window policy is a no-op for the 1871+ pair).
    - Multi-class path: the window policy (selected = weight > ε, no-backfill
      common-overlap window, hide/renormalize) over the year-keyed accessor.

    Returns ``(start_years, cohort_returns)`` where ``start_years[c]`` is cohort
    c's start year (ascending; ``start_years[0]`` = testable_since) and
    ``cohort_returns[c]`` is its length-``n_steps`` blended real-return array.
    Raises ``ValueError`` (no mapped class / no common window) like the engines.

    Note: the ``data`` arg (injected ``ShillerData``) ONLY applies to the legacy
    Shiller-only path. The multi-class path always reads
    ``historical_series_by_id()`` and ignores ``data``.
    """
    n_steps = scenario.end_age - scenario.start_age + 1

    # Glidepath: phase-varying weights, so always take the
    # glidepath window + per-phase blend (mirrors simulate_historical's routing,
    # so the solver runs the SAME cohorts/returns the display path does).
    if scenario.allocation_mode == ALLOCATION_MODE_GLIDEPATH:
        selected_series, start_years, series_data = _glidepath_window(scenario)
        cohort_returns = [
            _glidepath_cohort_blended_returns(
                scenario,
                _period_from_series(series_data, selected_series, sy, n_steps),
                HIST_SERIES_REGISTRY,
            )
            for sy in start_years
        ]
        return start_years, cohort_returns

    if _is_legacy_shiller_only(scenario):
        if data is None:
            data = load_shiller()
        weights = renormalized_weights(scenario)
        start_years = data.all_starting_years(n_steps)
        if not start_years:
            raise NoReplayableWindow(
                f"no cohort has a full {n_steps}-year window in the historical "
                f"data (data spans {data.first_year}-{data.last_year})"
            )
        cohort_returns = [
            _cohort_blended_returns(weights, data.returns_for_period(sy, n_steps))
            for sy in start_years
        ]
        return start_years, cohort_returns

    weights, selected_series, start_years, series_data = _multiclass_window(scenario)
    cohort_returns = [
        _cohort_blended_returns(
            weights, _period_from_series(series_data, selected_series, sy, n_steps)
        )
        for sy in start_years
    ]
    return start_years, cohort_returns


def _multiclass_window(
    scenario: Scenario,
) -> tuple[dict[str, float], list[str], list[int], dict[str, dict[int, float]]]:
    """Resolve the multi-class blend weights + the common-overlap window.

    Shared by ``engine.historical._simulate_historical_multiclass`` and ``cohort_return_sequences``.
    Returns ``(weights, selected_series, start_years, series_data)``:

    - ``weights``: ``{series_id: renormalized_weight}`` over the SELECTED series.
    - ``selected_series``: sorted series ids with weight > ε (the window binders
      and the blend members).
    - ``start_years``: ascending valid cohort starts (no-backfill common overlap).
    - ``series_data``: the year-keyed real-return accessor (returned so callers
      reuse the one load).

    Raises ``ValueError`` if no class maps, no class clears the epsilon, or the
    selected classes share no full-horizon overlap year.
    """
    n_steps = scenario.end_age - scenario.start_age + 1

    if not mapped_series_for(scenario, HIST_SERIES_REGISTRY):
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(HIST_SERIES_REGISTRY)})"
        )

    selected = selected_series_for(scenario, HIST_SERIES_REGISTRY)
    if not selected:
        raise ValueError(
            "no asset class has weight above the selection epsilon "
            f"({HISTORICAL_SELECTION_EPSILON:g})"
        )

    total = sum(selected.values())
    weights = {sid: w / total for sid, w in selected.items()}

    series_data = historical_series_by_id()
    selected_series = sorted(selected)

    start_years = common_overlap_start_years(selected_series, n_steps, series_data)
    if not start_years:
        spans = ", ".join(
            f"{sid} {min(series_data[sid])}-{max(series_data[sid])}"
            for sid in selected_series
        )
        raise NoReplayableWindow(
            f"no cohort has a full {n_steps}-year window common to the selected "
            f"classes ({spans})"
        )

    return weights, selected_series, start_years, series_data
