"""US federal tax.

- ``params`` loads the yearly constants from ``data/federal_<year>.toml``.
- ``core``: the pure year-income → tax computation.
- ``rmd``: required minimum distributions.
- ``location``: the per-year tax-drag fixed point over taxable / tax-deferred /
  Roth accounts; ``location_path`` runs it over a path; ``location_risk``
  aggregates paths.
- ``overlay``: the federal-tax overlay on a tax-location path.
"""
