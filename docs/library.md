# Using firebridgelab as a library

Install from git and fetch the data once (about 10 MB):

```bash
pip install "git+https://github.com/madisonicus/firebridgelab"
firebridgelab-fetch-data          # writes to ~/.cache/firebridgelab
```

The data directory is `$FIREBRIDGELAB_DATA` if set, else `data/cache/`
in a source checkout, else `~/.cache/firebridgelab`. Set the variable
before importing the package.

## Quickstart

```python
from dataclasses import replace
import firebridgelab as rm

plan = rm.default_scenario()                 # a fictional couple, ages 50 → 95
plan = replace(plan, initial_portfolio=1_500_000.0, annual_spending=95_000.0)

det = rm.simulate(plan)                       # one constant real return
print(f"final ${det.final_portfolio:,.0f}, depleted at {det.depleted_age}")

hist = rm.simulate_historical(plan)           # every Shiller cohort since 1871
print(f"{hist.depletion_count} of {hist.cohort_count} historical cohorts deplete")

mc = rm.simulate_monte_carlo(plan, n_trials=2_000, seed=7)
print(f"{mc.depletion_count} of {mc.trial_count} Monte Carlo trials deplete")

best = rm.solve_max_spend(plan, threshold=0.95, mode="historical")
print(f"max spend surviving 95% of cohorts: ${best.solved_spend:,.0f}")

crash = rm.simulate_stress(plan, "stagflation_1970s")   # ids: rm.STRESS_SCENARIOS
print(f"1970s stress: depleted at {crash.depleted_age}")
```

All dollars are real (plan-start purchasing power). `Scenario` is a frozen
dataclass: build variants with `dataclasses.replace`, and read its field
comments in `firebridgelab/scenario.py` for every knob (spending modes,
guardrail rails, pensions and COLA options, survivor case, tax-location split,
fees, Monte Carlo distribution). `validate_scenario(s)` raises
`ScenarioValidationError` on an inconsistent plan.

The names exported from `firebridgelab/__init__.py` are the supported
surface. Everything else is internal and may change.

The notebooks in [`notebooks/`](../notebooks/) are worked examples.
