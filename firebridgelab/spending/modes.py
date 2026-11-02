"""Valid ``Scenario.spending_mode`` values.

Each mode is implemented by one rule module in ``spending.rules``, whose module
docstring describes the rule; ``spending.registry`` maps the mode to it.
"""

from __future__ import annotations


# Pure functions of age (``rules.constant``): "constant" (default) spends
# ``annual_spending`` every year; "lifecycle" applies the go-go/slow-go/no-go curve.
SPENDING_MODE_CONSTANT = "constant"
SPENDING_MODE_LIFECYCLE = "lifecycle"
# Two-sided Guyton-Klinger guardrail on the withdrawal rate (``rules.guardrails``).
SPENDING_MODE_GUARDRAILS = "guardrails"
# Funded-ratio partial adjustment: the base moves a damped fraction toward full
# funding each year (``rules.fr_smooth``).
SPENDING_MODE_FR_SMOOTH = "fr_smooth"
# Funded-ratio band: hold within ±20% of fully funded, ±10% nudge outside
# (``rules.fr_guardrail``).
SPENDING_MODE_FR_GUARDRAIL = "fr_guardrail"
# Funded-ratio tier ladder with hysteresis, crash brake and hard floor; no taper,
# tier-based clamps (``rules.fr_tiers``).
SPENDING_MODE_FR_TIERS = "fr_tiers"
# Amortization frontier: the deplete-aware, unclamped spend that amortizes the
# portfolio to zero by ``end_age`` (``rules.amortization``).
SPENDING_MODE_AMORTIZATION = "amortization"
