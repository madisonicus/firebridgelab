# ADR 0008 — Social Security claim-age slider: UI/API derives the benefit; engine stays amount-driven

## Context

[ADR 0005](0005-gi-streams-supplied-not-derived.md) made each GI stream's amount a raw user
input *for the chosen start age*, and explicitly named the actuarial SS-claiming calculator as
"a separate future feature… [that] would feed amounts *into* these same four fields." That feature
is now built.

ADR 0005's design had a real footgun in practice: moving an SS stream's **start age** while leaving
its **amount** unchanged silently overstated an early claim (e.g. claiming at 62 still paid the
full-retirement-age amount unless the user hand-computed the 30% reduction). Social Security is the
single largest discretionary lever on plan viability, so the burden of the reduction/credit math
sitting on the user was the wrong place for it.

## Decision

Per person, the UI now takes a **full-retirement-age (FRA) benefit amount** + a **claim-age slider
(62–70, yearly)**. The **API derives** the effective amount — `effective = FRA × ss_factor(claim_age)`
— and feeds the *unchanged* amount-driven engine (`ss_amount` = derived effective, `ss_start_age` =
claim age).

`ss_factor` is the exact SSA rule (`firebridgelab/income/social_security.py`, verified against 20 CFR §404.410, §404.313,
SSA Handbook §724): early reduction = **5/9 of 1%/mo for the first 36 months** before FRA then
**5/12 of 1%/mo beyond 36**; delayed credit = **2/3 of 1%/mo (8%/yr)**, capped at 70. For FRA 67 this
reproduces 62→0.70, 67→1.00, 70→1.24 — matching SSA's published factors.

**FRA age is an internal constant (67, for anyone born 1960 or later), not a UI knob.** The FRA *amount* is the
estimate the user enters from their SSA statement (an ordinary input; a field can be frozen as a
hidden input carrying the default, still posted/saved/reset, with a one-line `hidden: true`). The two
claim-age sliders are surfaced into the always-visible primary inputs (SS is a primary lever, not a
deep-tuning variable); pension start ages stay ordinary age inputs (no slider).

## Consequences

- **ADR 0005's engine contract is preserved**, not overturned: the engine still receives a dollar
  amount + a start age and never derives anything. The factor math lives in one place
  (`firebridgelab/income/social_security.py`), exercised by the API; the browser mirrors it (`web/ss_claim.js`) only for
  the live readout, with a node parity test (`test_ss_claim_parity.mjs`) pinning JS == Python at all
  nine ages to guard drift. Glidepath phase boundaries already derive from `ss_start_age`, so the
  slider shifts the glide for free.
- **ADR 0005's *product* stance is superseded**: the tool now *does* compute the actuarial SS
  adjustment. The transparency ADR 0005 valued is retained differently — the readout shows the
  derived `$/yr · % of FRA` live, so the adjustment is visible, not hidden.
- Saved scenarios round-trip both FRA amount + claim age per person (reproducible even if a default
  later changes). No back-compat migration was needed (zero pre-existing saved scenarios).

## Status

Accepted (2026-06-07). Supersedes the *product* stance of [ADR 0005](0005-gi-streams-supplied-not-derived.md)
(SS is now derived); ADR 0005's *engine* contract (amounts supplied to the engine) remains in force.
