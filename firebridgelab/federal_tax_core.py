"""Deprecated import path: ``firebridgelab.federal_tax_core``.

Its contents moved to:

- ``firebridgelab.tax.core``
- ``firebridgelab.tax.rmd``

This module re-exports the public names it used to define so existing
imports keep working for one release. Import from the new modules.
"""
# Compatibility shim (plain re-export, no DeprecationWarning). Remove after
# one release.

from firebridgelab.tax.core import (
    STANDARD_DEDUCTION_MFJ,
    ADDITIONAL_DEDUCTION_65_PLUS,
    NIIT_RATE,
    NIIT_THRESHOLD_MFJ,
    STANDARD_DEDUCTION_SINGLE,
    ADDITIONAL_DEDUCTION_65_PLUS_SINGLE,
    NIIT_THRESHOLD_SINGLE,
    standard_deduction,
    EARLY_WITHDRAWAL_PENALTY_RATE,
    n_65_plus_at_age,
    FederalTaxYear,
    compute_federal_tax,
    federal_tax_total,
)
from firebridgelab.tax.rmd import RMD_START_AGE, rmd_factor

__all__ = [
    "STANDARD_DEDUCTION_MFJ",
    "ADDITIONAL_DEDUCTION_65_PLUS",
    "NIIT_RATE",
    "NIIT_THRESHOLD_MFJ",
    "STANDARD_DEDUCTION_SINGLE",
    "ADDITIONAL_DEDUCTION_65_PLUS_SINGLE",
    "NIIT_THRESHOLD_SINGLE",
    "standard_deduction",
    "EARLY_WITHDRAWAL_PENALTY_RATE",
    "RMD_START_AGE",
    "n_65_plus_at_age",
    "rmd_factor",
    "FederalTaxYear",
    "compute_federal_tax",
    "federal_tax_total",
]
