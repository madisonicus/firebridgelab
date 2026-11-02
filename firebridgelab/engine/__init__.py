"""The projection engine.

- ``scenario`` / ``allocation`` / ``validation``: the plan inputs.
- ``step``: the one canonical yearly portfolio step.
- ``deterministic``, ``historical``, ``monte_carlo``: the three drivers.
- ``sim_core``, ``cohort_windows``, ``cohort_aggregate``: per-cohort simulation,
  window selection and aggregation shared by the historical and Monte Carlo
  drivers.
- ``buckets``: the cash / income / growth bucket sourcing strategy.
"""
