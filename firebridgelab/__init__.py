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
- ``data_dir()``: where the fetched data lives (see ``firebridgelab.returns.paths``).

Everything else exported here is supported but more specialised. The
subpackages (``returns``, ``income``, ``spending``, ``tax``, ``engine``,
``analysis``) are importable but internal. See docs/library.md for a
quickstart and the package layout.
"""

__version__ = "0.1.0"

from firebridgelab.analysis.solver import SolverResult, solve_max_spend
from firebridgelab.analysis.stress import (
    STRESS_SCENARIOS,
    effective_spending_rails,
    simulate_stress,
)
from firebridgelab.engine.allocation import (
    ALLOCATION_401K_ACCESS_AGE,
    ALLOCATION_MODE_CONSTANT,
    ALLOCATION_MODE_GLIDEPATH,
    AssetClass,
    allocation_at_age,
    blended_real_return,
    blended_real_return_at_age,
    glidepath_boundaries,
    phase_index_for_age,
)
from firebridgelab.engine.buckets import (
    EQUITY_CLASS_IDS,
    cash_target,
    effective_equity_glidepath,
    gap_for_age,
    income_target,
)
from firebridgelab.engine.cohort_aggregate import HistoricalResult
from firebridgelab.engine.deterministic import Trajectory, YearPoint, simulate
from firebridgelab.engine.historical import simulate_historical
from firebridgelab.engine.monte_carlo import MonteCarloResult, simulate_monte_carlo
from firebridgelab.engine.scenario import (
    SOURCING_MODE_BUCKETS,
    SOURCING_MODE_SINGLE,
    Bucket,
    BucketConfig,
    Scenario,
    default_bucket_config,
    default_scenario,
)
from firebridgelab.engine.validation import ScenarioValidationError, validate_scenario
from firebridgelab.income.guaranteed import gi_at_age
from firebridgelab.returns.paths import data_dir
from firebridgelab.returns.series import renormalized_weights
from firebridgelab.spending.base import AdjustmentEvent, SpendingStrategy
from firebridgelab.spending.lifecycle import activity_multiplier, spending_for_age
from firebridgelab.spending.registry import make_strategy
from firebridgelab.spending.rules.amortization import AmortizationFrontierStrategy
from firebridgelab.spending.rules.constant import PureAgeStrategy
from firebridgelab.spending.rules.guardrails import GuardrailStrategy
from firebridgelab.tax.core import FederalTaxYear, compute_federal_tax
from firebridgelab.tax.location import TAX_LOCATION_ACCESS_AGE, has_tax_split, resolve_split
from firebridgelab.tax.location_path import (
    TaxLocationPath,
    simulate_path_decomposition,
    simulate_tax_location_deterministic,
)
from firebridgelab.tax.location_risk import (
    DepletionDistribution,
    TaxLocationRisk,
    aggregate_tax_location_risk,
)
from firebridgelab.tax.overlay import (
    FederalTaxOverlayAggregate,
    FederalTaxPath,
    aggregate_tax_overlay,
    compute_tax_overlay,
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
