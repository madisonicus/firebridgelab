# ADR 0005 — Guaranteed-income amounts are user-supplied, not actuarially derived

## Context

Guaranteed income is four independent streams (Person A/Person B × pension/Social-Security), each with its
own amount and start age. Social Security in reality is *not* a free amount: claiming early reduces
it and delaying past full retirement age grows it ~8%/yr (delayed retirement credits). A tool could
either (a) take the amount as an input for the chosen age, or (b) take a claiming age and compute
the actuarially-adjusted amount.

## Decision

Each GI stream's amount is a **user input for the chosen start age** (option a). The model sums the
streams active at an age (`age >= start_age`); it does not gross-up or haircut for claiming timing.

## Consequences

- The user controls the assumption directly — "Person B claims at 70" means entering Person B's age-70
  benefit *and* start age 70, not the model deriving it. Honest and transparent: no hidden actuarial
  model the user can't see.
- The burden of the claiming-age tradeoff math sits with the user. Modeling that tradeoff *inside*
  the tool (an SS-claiming calculator that derives the amount from the age) is a separate future
  feature, not a bug in the GI model. If built, it would feed amounts *into* these same four fields.
- Per-person independence is real and load-bearing: the two SS streams have independent amounts and
  ages. Canonicalizing the GI sum (one `gi_at_age` instead of three copies) protects this
  independence — see the deepening work on `firebridgelab/scenario.py:gi_at_age`.

## Status

Accepted. **Product stance superseded by [ADR 0008](0008-ss-claim-age-ui-derivation.md)** (2026-06-07):
the tool now derives the actuarial Social-Security adjustment from a claim-age slider. The *engine*
contract below — amounts supplied to the engine, not derived inside it — remains in force; ADR 0008's
derivation happens in the UI/API layer and feeds these same four fields, exactly as anticipated here.
