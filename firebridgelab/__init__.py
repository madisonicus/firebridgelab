"""firebridgelab: a retirement-planning model in real (inflation-adjusted) dollars.

The documented public API:

- ``Scenario`` / ``default_scenario()``: the plan inputs (a frozen dataclass;
  derive variants with ``dataclasses.replace``).
- ``simulate(scenario)``: one deterministic projection → ``Trajectory``.
- ``simulate_historical(scenario)``: every historical cohort → ``HistoricalResult``.
- ``simulate_monte_carlo(scenario, n_trials=..., seed=...)`` → ``MonteCarloResult``.
- ``solve_max_spend(scenario, threshold, mode)``: the largest ``annual_spending``
  whose survival fraction clears ``threshold`` → ``SolverResult``.
- ``simulate_stress(scenario, stress_id)``: an authored adverse return path
  (ids in ``STRESS_SCENARIOS``) → ``Trajectory``.
- ``data_dir()``: where the fetched data lives (see ``firebridgelab.paths``).

Everything else exported here is supported but more specialised. Modules not
re-exported here are internal. See docs/library.md for a quickstart.
"""

__version__ = "0.1.0"

from firebridgelab.paths import data_dir

from firebridgelab.buckets import (
    EQUITY_CLASS_IDS,
    effective_equity_glidepath,
)
from firebridgelab.federal_tax import (
    FederalTaxOverlayAggregate,
    FederalTaxPath,
    FederalTaxYear,
    aggregate_tax_overlay,
    compute_federal_tax,
    compute_tax_overlay,
)
from firebridgelab.historical import (
    HistoricalResult,
    renormalized_weights,
    simulate_historical,
)
from firebridgelab.monte_carlo import (
    MonteCarloResult,
    simulate_monte_carlo,
)
from firebridgelab.scenario import (
    ALLOCATION_401K_ACCESS_AGE,
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    SOURCING_MODE_BUCKETS,
    SOURCING_MODE_SINGLE,
    AssetClass,
    Bucket,
    BucketConfig,
    Scenario,
    ScenarioValidationError,
    activity_multiplier,
    allocation_at_age,
    blended_real_return,
    blended_real_return_at_age,
    cash_target,
    default_bucket_config,
    default_scenario,
    gap_for_age,
    gi_at_age,
    glidepath_boundaries,
    income_target,
    phase_index_for_age,
    spending_for_age,
    validate_scenario,
)
from firebridgelab.simulate import Trajectory, YearPoint, simulate
from firebridgelab.solver import SolverResult, solve_max_spend
from firebridgelab.stress import STRESS_SCENARIOS, effective_spending_rails, simulate_stress
from firebridgelab.spending import (
    AdjustmentEvent,
    AmortizationFrontierStrategy,
    GuardrailStrategy,
    PureAgeStrategy,
    SpendingStrategy,
    make_strategy,
)
from firebridgelab.tax_location import (
    TAX_LOCATION_ACCESS_AGE,
    DepletionDistribution,
    TaxLocationPath,
    TaxLocationRisk,
    aggregate_tax_location_risk,
    has_tax_split,
    resolve_split,
    simulate_path_decomposition,
    simulate_tax_location_deterministic,
)

__all__ = [
    "__version__",
    "data_dir",
    "Trajectory",
    "YearPoint",
    "ALLOCATION_401K_ACCESS_AGE",
    "ALLOCATION_MODE_CONSTANT",
    "ALLOCATION_MODE_GLIDEPATH",
    "EQUITY_CLASS_IDS",
    "SOURCING_MODE_BUCKETS",
    "SOURCING_MODE_SINGLE",
    "STRESS_SCENARIOS",
    "TAX_LOCATION_ACCESS_AGE",
    "AdjustmentEvent",
    "AmortizationFrontierStrategy",
    "AssetClass",
    "Bucket",
    "BucketConfig",
    "DepletionDistribution",
    "FederalTaxOverlayAggregate",
    "FederalTaxPath",
    "FederalTaxYear",
    "GuardrailStrategy",
    "HistoricalResult",
    "MonteCarloResult",
    "PureAgeStrategy",
    "Scenario",
    "ScenarioValidationError",
    "SolverResult",
    "SpendingStrategy",
    "TaxLocationPath",
    "TaxLocationRisk",
    "aggregate_tax_location_risk",
    "aggregate_tax_overlay",
    "compute_federal_tax",
    "compute_tax_overlay",
    "has_tax_split",
    "resolve_split",
    "simulate_path_decomposition",
    "simulate_tax_location_deterministic",
    "activity_multiplier",
    "allocation_at_age",
    "blended_real_return",
    "blended_real_return_at_age",
    "cash_target",
    "default_bucket_config",
    "effective_equity_glidepath",
    "default_scenario",
    "gap_for_age",
    "gi_at_age",
    "glidepath_boundaries",
    "income_target",
    "effective_spending_rails",
    "make_strategy",
    "phase_index_for_age",
    "simulate",
    "simulate_historical",
    "simulate_monte_carlo",
    "simulate_stress",
    "solve_max_spend",
    "spending_for_age",
    "validate_scenario",
]
