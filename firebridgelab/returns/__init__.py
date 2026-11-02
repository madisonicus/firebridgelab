"""Historical return data and return models.

- ``paths``: where the fetched data lives.
- ``fetch`` / ``data_build``: download the raw sources and build the staged
  annual series.
- ``shiller`` / ``class_series``: load the annual real-return series.
- ``series``: the class-id → series-id registry and the per-cohort series
  selection helpers.
- ``correlation``: historical correlation estimates.
- ``sampling``: Monte Carlo return samplers and the rebalanced compound return.
"""
