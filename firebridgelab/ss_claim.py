"""Social Security claim-age actuarial adjustment (pure helper).

Computes the SSA actuarial factor by which a Social Security benefit is
reduced (claiming before Full Retirement Age) or increased (delaying past
FRA), so a caller can DERIVE the effective benefit from the FRA ("primary
insurance amount", PIA) benefit and a chosen claim age:

    effective_benefit = fra_benefit * ss_factor(claim_age)

This fixes an amount-driven footgun: the engine pays a fixed dollar amount
from a start age and does NOT actuarially adjust for claim age, so moving a
start age earlier without lowering the amount silently overstates an early
claim. The API computes the adjustment HERE and feeds the engine the already-
adjusted effective amount; the engine stays amount-driven and unchanged.

EXACT SSA rules (no per-age lookup table — the per-MONTH formula is the
source of truth so it generalizes to any FRA):

- Early retirement (claim before FRA) — reduction (20 CFR §404.410):
    * 5/9 of 1% per month for the first 36 months before FRA, PLUS
    * 5/12 of 1% per month for each additional month beyond 36.
  So a full 36 months early is a 20% reduction; months 37+ cut a further
  5/12 of 1% each.
- Delayed retirement credits (claim after FRA) — increase (20 CFR §404.313):
    * 2/3 of 1% per month after FRA (= 8% per year), and
    * credits STOP accruing at age 70 (no benefit to delaying past 70).
  (The 2/3-of-1%/month rate is the rate for workers born 1943 or later; the
  model assumes both people are born 1960+, so it applies.)
- At FRA the factor is exactly 1.0.

References: 20 CFR §404.410 (reduction for early retirement), 20 CFR §404.313
(delayed retirement credits), SSA Handbook §724 (delayed retirement credit
rates). The factor table this reproduces for FRA 67 matches SSA's published
factors (62 → 0.70, 67 → 1.00, 70 → 1.24).

Pure module: no imports from ``api`` or ``web``, and no engine imports
needed — it stands alone.
"""

# The model assumes both people are born 1960 or later, so Full Retirement Age
# is 67. This is an INTERNAL constant — the model fixes FRA at 67 and exposes the
# CLAIM age as the user knob, never FRA itself. ``ss_factor`` keeps ``fra_age``
# parameterized (so the per-month formula generalizes and is unit-testable at
# other FRAs), but the API only ever passes this default.
DEFAULT_FRA_AGE = 67

# The Social Security claim-age window: a worker may claim as early as 62 and
# gains nothing by delaying past 70 (delayed credits stop accruing at 70). A
# claim age outside [62, 70] is a user error, not a clamp — ``ss_factor``
# raises ``ValueError`` so the API can translate it to a 422.
MIN_CLAIM_AGE = 62
MAX_CLAIM_AGE = 70

# Per-month SSA adjustment rates, as fractions of the FRA benefit (NOT percent).
# Stated here once so the formula reads as the regulation does.
_EARLY_RATE_FIRST_36 = (5.0 / 9.0) / 100.0   # 5/9 of 1% per month, first 36 mo
_EARLY_RATE_BEYOND_36 = (5.0 / 12.0) / 100.0  # 5/12 of 1% per month, months 37+
_DELAYED_RATE_PER_MONTH = (2.0 / 3.0) / 100.0  # 2/3 of 1% per month after FRA
_EARLY_FIRST_TIER_MONTHS = 36                  # boundary between the two early tiers


def ss_factor(claim_age: int, fra_age: int = DEFAULT_FRA_AGE) -> float:
    """Actuarial factor: effective benefit = FRA benefit * ss_factor(claim_age).

    Derived from the per-MONTH SSA rules (see module docstring), not a per-age
    lookup table, so it generalizes to any ``fra_age``. ``months`` is the signed
    count of months between the claim age and FRA: negative = claiming early
    (reduction), positive = delaying (credit), zero = exactly at FRA → 1.0.

    Raises ``ValueError`` if ``claim_age`` is outside [``MIN_CLAIM_AGE``,
    ``MAX_CLAIM_AGE``]; the message is user-facing (the API turns it into a 422).
    """
    if not (MIN_CLAIM_AGE <= claim_age <= MAX_CLAIM_AGE):
        raise ValueError(
            f"claim_age must be between {MIN_CLAIM_AGE} and {MAX_CLAIM_AGE} "
            f"(got {claim_age})"
        )

    months = (claim_age - fra_age) * 12

    if months == 0:
        # Exactly at FRA: no adjustment.
        return 1.0

    if months < 0:
        # Early: reduce. The first 36 months before FRA reduce at 5/9 of 1%/mo;
        # any months beyond 36 reduce at the gentler 5/12 of 1%/mo.
        months_early = -months
        first_tier = min(months_early, _EARLY_FIRST_TIER_MONTHS)
        beyond_tier = max(0, months_early - _EARLY_FIRST_TIER_MONTHS)
        reduction = (
            first_tier * _EARLY_RATE_FIRST_36
            + beyond_tier * _EARLY_RATE_BEYOND_36
        )
        return 1.0 - reduction

    # Delayed: increase at 2/3 of 1%/mo. Credits stop accruing at 70; with the
    # [62, 70] guard above, ``claim_age`` never exceeds 70, so ``months`` is
    # already capped at the FRA→70 span and no extra clamp is needed.
    return 1.0 + months * _DELAYED_RATE_PER_MONTH


def effective_ss_amount(
    fra_amount: float, claim_age: int, fra_age: int = DEFAULT_FRA_AGE
) -> float:
    """The effective annual SS benefit for claiming at ``claim_age``.

    ``fra_amount`` is the benefit if claimed exactly at FRA (the PIA). The
    effective benefit is ``fra_amount * ss_factor(claim_age, fra_age)``;
    propagates the ``ValueError`` from ``ss_factor`` for an out-of-range claim
    age. At FRA (factor 1.0) this returns ``fra_amount`` unchanged.
    """
    return fra_amount * ss_factor(claim_age, fra_age)
