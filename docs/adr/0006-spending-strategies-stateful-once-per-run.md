# ADR 0006 — Spending strategies are stateful objects constructed once per run

## Context

Constant and Lifecycle spending are path-independent (this year's spend depends only on age).
Guardrails and the Funded-ratio strategies are path-**dependent** — this year's spend depends on the
current portfolio and the prior spend (carried nudge, smoothing window, hysteresis state). The
strategy seam had to serve both without engine-loop surgery for each new path-dependent strategy.

## Decision

`SpendingStrategy` is a class constructed **once per simulation run**, before the year loop, and
called per year as `spend_for_year(scenario, age, begin_year_portfolio, gi) -> float`. Carried
state (smoothing buffers, prior portfolio, prior spend, hysteresis) lives on the instance, scoped
to one run. Path-independent strategies are thin wrappers that ignore the carried state and declare
`is_solvable = True`; path-dependent ones declare `is_solvable = False` so the solver refuses them.

## Consequences

- Adding a path-dependent strategy (the entire Funded-ratio family was added this way) needs **no**
  change to the engine's year loop — just a new subclass and a `make_strategy` branch.
- State is per-run, so re-running a scenario is clean; no global mutable state leaks between runs.
- The solver's max-spend search can only operate on path-independent strategies — this is a
  *property of the strategy*, surfaced through `is_solvable`/`reason_not_solvable`, not a string
  check on the mode name. The API translates `SpendingStrategyNotSolvable` to a 422.

## Status

Accepted.
