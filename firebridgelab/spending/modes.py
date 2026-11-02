"""spending.modes (split from the flat layout)."""

from __future__ import annotations


# Valid spending_mode values. "constant" (default) reproduces today's behavior
# byte-for-byte; "lifecycle" activates the age-anchored phase curve; "guardrails"
# activates the two-sided Guyton-Klinger dynamic-spending strategy (variable-
# spending design). The guardrail is the first PATH-DEPENDENT spending shape — its
# spend depends on the current portfolio and the prior spend — so it is NOT served
# by the pure ``spending_for_age`` helper; it lives in the stateful
# ``firebridgelab.spending.GuardrailStrategy`` (see that module). constant/lifecycle stay
# byte-for-byte identical because their strategy wraps ``spending_for_age``.
SPENDING_MODE_CONSTANT = "constant"
SPENDING_MODE_LIFECYCLE = "lifecycle"
SPENDING_MODE_GUARDRAILS = "guardrails"
# "fr_smooth" activates the funded-ratio partial-adjustment spending strategy (FR
# feature slice 2). Like guardrails it is PATH-DEPENDENT — this year's spend
# depends on the smoothed-portfolio funded ratio and the carried untapered base —
# so it lives in the stateful ``firebridgelab.spending.FundedRatioSmoothStrategy`` and is
# view-only (the max-spend solver is fenced). FR = 1 means "the smoothed portfolio
# exactly funds the baseline annual_spending stream"; each year the base nudges a
# damped fraction (alpha) of the way toward funding, then composes the SAME
# go-go/slow-go/no-go taper + floor/ceiling clamp guardrails uses.
SPENDING_MODE_FR_SMOOTH = "fr_smooth"
# "fr_guardrail" activates the funded-ratio BAND spending strategy (FR feature
# slice 3) — a close sibling of fr_smooth that swaps the damped partial-adjustment
# for a DISCRETE band trip on the funded ratio: hold while funding stays within ±20%
# of fully-funded, a ±10% nudge to the untapered base past either band. Like
# guardrails / fr_smooth it is PATH-DEPENDENT (the spend depends on the smoothed-
# portfolio funded ratio and the carried base) so it lives in the stateful
# ``firebridgelab.spending.FundedRatioGuardrailStrategy`` and is view-only (the max-spend
# solver is fenced). Bands/step are baked v1 presets, not user inputs.
SPENDING_MODE_FR_GUARDRAIL = "fr_guardrail"
# "fr_tiers" activates the funded-ratio TIER LADDER spending strategy (FR feature
# slice 4 — the capstone): a FAITHFUL port of the legacy ``TierLadderPolicy``
# "christmas tree". A funded-ratio band ladder with hysteresis, a crash brake, and
# a hard floor, where the tier AMOUNTS encode the go-go/slow-go/later phase shape
# ($160k go-go MAX → $85k FLOOR). UNLIKE fr_smooth / fr_guardrail it does NOT
# apply the activity taper and does NOT use the operational spending_floor /
# spending_ceiling rails — its clamps are TIER-based (crash → BELT, hard floor →
# FLOOR), gated by its own portfolio-level floor threshold. Its NPV target is the
# MAX go-go tier amount ($160k), NOT annual_spending — the load-bearing invariant.
# Like the other dynamic modes it is PATH-DEPENDENT and view-only (the max-spend
# solver is fenced). The full ladder is a baked v1 preset (no user knobs); the math
# lives in ``firebridgelab.spending.FundedRatioTierStrategy``.
SPENDING_MODE_FR_TIERS = "fr_tiers"
# "amortization" activates the AMORTIZATION FRONTIER spending strategy (amortization-
# frontier design) — the lone DEPLETE-AWARE strategy (all six above are sustain-and-
# preserve). It is the funded-ratio machinery run BACKWARDS: each year it solves for
# the constant-real total spend ``L`` such that the portfolio exactly funds the
# remaining GI-netted gap stream, discounted at ``amortization_real_return`` over the
# hard plan horizon ``end_age`` (i.e. the target ``L`` where the funded ratio == 1).
# Because future GI is netted INSIDE the solve, ``L`` front-loads consumption into the
# pre-SS bridge years (GI-aware consumption smoothing — NOT myopic VPW). It is the
# upper boundary of the spending envelope ("the most you could responsibly pull each
# year if you amortize to zero by the plan horizon") — a benchmark you push toward,
# never "VPW" in the UI. UNLIKE the six clamped strategies it is DELIBERATELY
# UNCLAMPED (the floor/ceiling rails are chart reference lines only, never applied),
# because clamping destroys the very over-/under-funded diagnostic it exists for.
# Path-dependent (re-solved each year off the actual begin-of-year balance) and
# view-only — the max-spend solver is fenced, since the frontier IS the spend. The
# math lives in ``firebridgelab.spending.AmortizationFrontierStrategy``.
SPENDING_MODE_AMORTIZATION = "amortization"
