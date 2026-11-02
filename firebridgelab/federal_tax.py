"""Deprecated import path: ``firebridgelab.federal_tax``.

Its contents moved to:

- ``firebridgelab.tax.core``
- ``firebridgelab.tax.overlay``
- ``firebridgelab.tax.rmd``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.tax.core import (
    ADDITIONAL_DEDUCTION_65_PLUS,
    EARLY_WITHDRAWAL_PENALTY_RATE,
    NIIT_RATE,
    NIIT_THRESHOLD_MFJ,
    STANDARD_DEDUCTION_MFJ,
    FederalTaxYear,
    compute_federal_tax,
    n_65_plus_at_age,
)
from firebridgelab.tax.overlay import (
    FederalTaxPath,
    compute_tax_overlay,
    FederalTaxOverlayAggregate,
    aggregate_tax_overlay,
)
from firebridgelab.tax.rmd import RMD_START_AGE, rmd_factor

__all__ = [
    "ADDITIONAL_DEDUCTION_65_PLUS",
    "EARLY_WITHDRAWAL_PENALTY_RATE",
    "NIIT_RATE",
    "NIIT_THRESHOLD_MFJ",
    "RMD_START_AGE",
    "STANDARD_DEDUCTION_MFJ",
    "FederalTaxYear",
    "FederalTaxOverlayAggregate",
    "FederalTaxPath",
    "aggregate_tax_overlay",
    "compute_federal_tax",
    "compute_tax_overlay",
    "n_65_plus_at_age",
    "rmd_factor",
]
