# ADR 0002 — One canonical algebraic form for the yearly portfolio step

## Context

The midpoint-withdrawal mechanic — how a portfolio moves through one year — is called by all three
return modes (Deterministic, Historical cohort replay, Monte Carlo trials reuse the cohort loop).
The two natural ways to write it are algebraically identical but **not** byte-identical under
IEEE-754 round-to-nearest:

```
end = start + start*r + gi - spending*(1 + r/2)     # canonical
end = start*(1 + r) + gi - spending*(1 + r/2)        # equivalent, but drifts sub-ULP
```

## Decision

`firebridgelab/engine/step.py` defines `step_year` as the **single** implementation, fixed on the first form
(`start + start*r + ...`). All three modes call it; no mode re-expresses the arithmetic inline.

## Consequences

- The Deterministic canary (`8397280.186644783`, ADR 0001) depends on this exact expression. The
  "obvious" factored form would move it.
- A new return mode or a tempting "simplification" must call `step_year`, not rewrite the step. The
  module docstring documents *why* the equivalent form is rejected, so the next reader doesn't
  "fix" it.

## Status

Accepted.
