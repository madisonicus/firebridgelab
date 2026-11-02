"""engine.allocation (split from the flat layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import functools
from dataclasses import dataclass, replace

if TYPE_CHECKING:
    from firebridgelab.engine.scenario import Scenario



# Valid allocation_mode values (glidepath design). "constant" (default) holds ONE
# allocation across the whole horizon — today's behavior, byte-for-byte unchanged.
# "glidepath" carries one allocation per DERIVED phase (a rising-equity / bond-tent
# glide), and the portfolio's real return at a given age is the blend of THAT age's
# phase's allocation. The toggle is orthogonal to `mode` and `spending_mode`.
ALLOCATION_MODE_CONSTANT = "constant"
ALLOCATION_MODE_GLIDEPATH = "glidepath"

# The ONE fixed boundary the glidepath adds (glidepath design "The core design
# decision"). The IRS penalty-free 401(k) withdrawal rule is age 59½; the engine
# uses an INTEGER annual age axis, so we model the boundary at age 60 — the first
# full year of penalty-free access. It is NOT a user input in this slice (it's a
# tax rule, not a preference). The pension + SS boundaries are NOT hardcoded here;
# they DERIVE from the existing GI start-age scenario inputs (see
# ``glidepath_boundaries``).
ALLOCATION_401K_ACCESS_AGE = 60

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

    from firebridgelab.returns.correlation import real_return_series_by_series_id
    from firebridgelab.returns.series import SERIES_FOR_CLASS

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
    from firebridgelab.returns.class_series import NEW_CLASS_SERIES, load_class_series

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
    from firebridgelab.returns.sampling import rebalanced_compound_return

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
