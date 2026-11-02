"""Series registries and the class -> series binding for Historical and MC.

Maps allocation classes (by stable class id) to return-series ids, renormalizes
allocation weights over the classes that map, and computes the Historical
cohort window (the full-horizon common overlap of the selected series, no
backfill). The series data itself comes from ``returns.shiller`` (the two
Shiller series) and ``returns.class_series`` (the staged classes).
"""

from __future__ import annotations

from firebridgelab.engine.allocation import allocation_at_age
from firebridgelab.engine.scenario import Scenario
from firebridgelab.returns.class_series import NEW_CLASS_SERIES


# The Shiller-backed series. Maps a series id to the attribute on ShillerData
# holding its annual real returns; ShillerData.returns_for_period can serve only
# these two. Adding a Shiller-backed series is one entry here plus the data on
# ShillerData. The staged classes come from a SEPARATE source
# (returns.class_series) and live in SERIES_REGISTRY_NEW below.
SERIES_REGISTRY: dict[str, str] = {
    "us_equity": "stock_real_return",
    "us_bonds": "bond_real_return",
}

# The staged asset-class series (annual REAL returns in returns.class_series),
# keyed by series id. Kept distinct from SERIES_REGISTRY because these series come
# from a different source. class id == series id for all of these, so they
# double as the SERIES_FOR_CLASS values below.
SERIES_REGISTRY_NEW: dict[str, str] = {sid: sid for sid in NEW_CLASS_SERIES}

# Class-id → series-id map (the data-binding namespace). The KEY is the stable
# **class id** (AssetClass.id), the VALUE is the **series id**. The two Shiller
# classes bind under different names: class ``us_total_market`` → series
# ``us_equity``, class ``us_agg_bonds`` → series ``us_bonds`` (see AssetClass).
# The staged classes are registered here too, where class id == series id: they
# bind to the class_series data, NOT to ShillerData. A class id absent here is
# treated as UNMAPPED (its weight is renormalized out; see renormalized_weights).
# Exact-match on the class id (a stable token, not a display string — no
# case-folding).
SERIES_FOR_CLASS: dict[str, str] = {
    "us_total_market": "us_equity",
    "us_agg_bonds": "us_bonds",
    **{sid: sid for sid in NEW_CLASS_SERIES},
}


# The MC-mappable registry: the union of the Shiller series (SERIES_REGISTRY) and
# the staged classes (SERIES_REGISTRY_NEW). Monte Carlo is PARAMETRIC — it samples
# each class from a Normal(mean, vol) using the class's own ``real_return`` /
# ``volatility``, so any class whose series id has loaded real-return data
# (Shiller OR class_series) is samplable, and its cross-class correlation comes
# from the per-pair estimator (returns.correlation).
MC_SERIES_REGISTRY: dict[str, str] = {**SERIES_REGISTRY, **SERIES_REGISTRY_NEW}

# The Historical cohort-replay registry: the SAME union MC uses. Historical
# replays whatever class has a real ANNUAL series the multi-source accessor
# (``historical_series_by_id``) can serve. A class whose series id is NOT in this
# registry (e.g. an optional class whose data was not staged) is UNMAPPED →
# hidden + renormalized out.
HIST_SERIES_REGISTRY: dict[str, str] = {**SERIES_REGISTRY, **SERIES_REGISTRY_NEW}

# Selection epsilon for the window policy ("selected = weight > ε"). A mapped class
# whose weight is at or below this floor is mapped (its weight still rides the
# renormalized blend) but does NOT constrain the common-overlap window — so a
# preset listing many classes with some at exactly 0% can't silently collapse
# every window to the shortest-history class. Pinned, not a parameter; well below
# any meaningful allocation weight.
HISTORICAL_SELECTION_EPSILON: float = 1e-9


def mapped_series_for(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[int, str]:
    """Indices of allocation classes that map to a known series → its series id.

    A class maps iff its **class id** (``ac.id``) is in SERIES_FOR_CLASS and the
    mapped series id is in ``registry`` (defaults to ``SERIES_REGISTRY`` — the
    Shiller-replayable series the Historical engine uses; Monte Carlo passes the
    wider ``MC_SERIES_REGISTRY`` so the parametrically-samplable staged classes
    also map). Keys off the stable class id (not the display label), so the
    binding can't drift with presentation. Unmapped classes are omitted, so their
    weight is excluded from the renormalized blend.
    """
    reg = SERIES_REGISTRY if registry is None else registry
    mapped: dict[int, str] = {}
    for i, ac in enumerate(scenario.allocation):
        series_id = SERIES_FOR_CLASS.get(ac.id)
        if series_id is not None and series_id in reg:
            mapped[i] = series_id
    return mapped


def renormalized_weights(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[str, float]:
    """Per-series weights, renormalized over mapped classes to sum to 1.

    Reads weights ONLY from classes that map to a known series (per ``registry``,
    defaulting to the Historical ``SERIES_REGISTRY``; MC passes
    ``MC_SERIES_REGISTRY``), then divides by their total so the kept weights sum
    to 1 (a stale/unmapped weight can never leak into a blend). Sums weights per
    series id (so two classes mapping to the same series add). Raises ``ValueError``
    if no class maps.
    """
    reg = SERIES_REGISTRY if registry is None else registry
    mapped = mapped_series_for(scenario, reg)
    if not mapped:
        raise ValueError(
            "no asset class maps to a known historical series "
            f"(have {sorted(reg)})"
        )
    total = sum(scenario.allocation[i].weight for i in mapped)
    if total <= 0:
        raise ValueError("mapped allocation weights sum to zero")
    weights: dict[str, float] = {}
    for i, series_id in mapped.items():
        weights[series_id] = weights.get(series_id, 0.0) + scenario.allocation[i].weight / total
    return weights


def historical_series_by_id() -> dict[str, dict[int, float]]:
    """Unified ``{series_id: {year: real_return}}`` view for cohort replay.

    The single multi-source accessor the Historical multi-class replay reads.
    Reuses ``returns.correlation.real_return_series_by_series_id`` — the SAME
    year-keyed view the per-pair correlation estimator uses — so Historical and
    the MC correlation matrix draw from one canonical source of historical real
    returns (the Shiller pair from ``returns.shiller``; the staged classes from
    ``returns.class_series``). Year-keyed so a class's data window is just the key
    set, and a cohort's per-year cell is a direct lookup.

    Imported lazily to avoid a module-level import cycle (``correlation`` does
    not import this module today, but keeping the import local matches the
    pattern the rest of the engine uses for the cross-module loaders).
    """
    from firebridgelab.returns.correlation import real_return_series_by_series_id

    return real_return_series_by_series_id()


def selected_series_for(
    scenario: Scenario, registry: dict[str, str] | None = None
) -> dict[str, float]:
    """Mapped series whose summed weight clears the selection epsilon.

    Maps each allocation class to its series id (via ``mapped_series_for`` over
    ``registry``, defaulting to the Historical ``HIST_SERIES_REGISTRY``), sums
    raw (un-renormalized) weights per series id, and keeps only series whose
    summed weight exceeds ``HISTORICAL_SELECTION_EPSILON``. These — and ONLY
    these — constrain the common-overlap window (a 0%-weight mapped class does
    not bind the window). Returns ``{series_id: summed_raw_weight}``.
    """
    reg = HIST_SERIES_REGISTRY if registry is None else registry
    mapped = mapped_series_for(scenario, reg)
    summed: dict[str, float] = {}
    for i, series_id in mapped.items():
        summed[series_id] = summed.get(series_id, 0.0) + scenario.allocation[i].weight
    return {
        sid: w for sid, w in summed.items() if w > HISTORICAL_SELECTION_EPSILON
    }


def common_overlap_start_years(
    selected_series: list[str],
    n_steps: int,
    series_data: dict[str, dict[int, float]] | None = None,
) -> list[int]:
    """Valid cohort start years = full-horizon common overlap of selected series.

    The no-backfill window policy: a start year Y is valid iff EVERY series
    in ``selected_series`` has real data for the FULL horizon Y .. Y+n_steps-1.
    Computed from each series' actual year set (no fabricated / backfilled
    years). The intersection of the per-series valid-start sets, sorted ascending.

    ``n_steps`` is the horizon length (``end_age - start_age + 1``). A short-
    history class restricts the set (its earliest full-window start is later);
    if the selected series have no common full-horizon year, the result is empty
    (the caller raises). ``series_data`` defaults to ``historical_series_by_id``.
    """
    data = historical_series_by_id() if series_data is None else series_data

    valid: set[int] | None = None
    for sid in selected_series:
        years = data[sid].keys()
        present = set(years)
        # A start year Y is full-horizon-valid for this series iff every year in
        # Y .. Y+n_steps-1 is present (no gaps, no backfill). Built directly from
        # the present-year set so a hole inside a window correctly disqualifies Y.
        series_starts = {
            y for y in present
            if all((y + k) in present for k in range(n_steps))
        }
        valid = series_starts if valid is None else (valid & series_starts)

    return sorted(valid) if valid else []


def glidepath_phase_weights(
    scenario: Scenario, registry: dict[str, str]
) -> list[dict[str, float]]:
    """Per-phase renormalized per-series weights for a glidepath scenario.

    Under a glidepath the per-year blend uses the
    weights of the phase in force that year, so the cohort/MC blend must vary its
    weights per phase (not just the constant-blend ``r``). Returns one
    ``{series_id: renormalized_weight}`` map per glidepath phase, in phase order
    (aligned to ``scenario.glidepath_boundaries``).

    Each phase's weights are computed by ``renormalized_weights`` over a scenario
    swapped to that phase's allocation — the SAME renormalization the constant
    path uses, so a glidepath whose every phase equals the single allocation
    blends exactly like the constant path (the degenerate-glide identity).
    """
    from dataclasses import replace

    from firebridgelab.engine.allocation import glidepath_boundaries

    boundaries = glidepath_boundaries(scenario)
    phase_weights: list[dict[str, float]] = []
    for start_age in boundaries:
        # The allocation in force at this phase's start age IS this phase's
        # allocation (phases are lower-bound inclusive). Renormalize it over the
        # mapped series exactly as the constant path does.
        phase_alloc = allocation_at_age(scenario, start_age)
        phase_scenario = replace(scenario, allocation=phase_alloc)
        phase_weights.append(renormalized_weights(phase_scenario, registry))
    return phase_weights
