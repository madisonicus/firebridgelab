# Architecture Decision Records

Settled decisions that are hard to reverse, surprising without context, and the result of a real
trade-off. Read these before "fixing" something that looks odd — it may be deliberate.

| ADR | Decision |
| --- | --- |
| [0001](0001-byte-stable-canary-discipline.md) | Byte-stable canary discipline — refactors preserve default output to the float |
| [0002](0002-step-year-canonical-form.md) | One canonical algebraic form for the yearly portfolio step |
| [0003](0003-zero-build-step-frontend.md) | Frontend has zero build step; window-global IIFE modules, no ES modules |
| [0004](0004-real-dollar-engine.md) | The engine runs in real (inflation-adjusted) dollars |
| [0005](0005-gi-streams-supplied-not-derived.md) | Guaranteed-income amounts are user-supplied, not actuarially derived |
| [0006](0006-spending-strategies-stateful-once-per-run.md) | Spending strategies are stateful objects constructed once per run |
| [0007](0007-spending-mode-registry.md) | Spending-mode API properties live in one registry |
| [0008](0008-ss-claim-age-ui-derivation.md) | Social Security claim-age slider: the UI/API derives the benefit; the engine stays amount-driven |

File paths in the ADRs follow the package layout in [docs/library.md](../library.md#package-layout);
the old flat module paths still import through compatibility shims.

New ADRs: increment the highest number; one decision per file; a paragraph is enough.
