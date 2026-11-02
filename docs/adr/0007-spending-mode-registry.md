# ADR 0007 — Spending-mode API properties live in one registry

## Context

A spending mode's API-presentation properties (valid mode IDs, saved-view eligibility, whether the
mode composes with bucket sourcing) were scattered and hand-duplicated across `api/main.py` — a
`_SPENDING_MODES` tuple, a redundant `_SAVE_VIEW_SPENDING_MODES` set, and a hardcoded 4-tuple of
non-composable modes in `_validate_sourcing`. Adding a mode meant editing all three in sync.

## Decision

`api/spending_modes.py` holds a single `SpendingModeDescriptor` registry, ordered to match the
constants in `firebridgelab.spending.modes`. Each descriptor carries exactly two presentation properties:

- `composable_with_buckets` — `False` → 422 when `sourcing_mode == buckets`.
- `save_eligible` — whether the saved-scenario view accepts the mode.

The API derives `SPENDING_MODE_IDS`, `SAVE_VIEW_SPENDING_MODE_IDS`, and `_SPENDING_MODE_BY_ID` from
the registry. Engine *capabilities* (`is_solvable`, `collects_spends`) are deliberately NOT in the
registry — they stay predicates on the SpendingStrategy instance per ADR 0006, read via
`make_strategy` and the typed `SpendingStrategyNotSolvable` exception.

The engine has its own, separate registry (`firebridgelab.spending.registry`): it maps each mode
to the strategy factory that a module in `firebridgelab/spending/rules/` registers. It holds no API
properties, so a new mode is one rule module plus one descriptor here.

## Status

Accepted.
