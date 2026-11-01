# ADR 0004 — The engine runs in real (inflation-adjusted) dollars

## Context

A retirement tool can model in nominal dollars (with an explicit inflation/CPI series) or in real
dollars (everything already inflation-adjusted, inflation implicit). The first is more general but
adds an axis to every return, every GI stream, and every spending number.

## Decision

The engine is **real-dollar throughout**. Returns are real returns, spending floor/ceiling are real,
GI streams are assumed to grow with inflation (i.e. constant in real terms — full COLA assumed). No
nominal frame, no CPI knob.

## Consequences

- Simpler everywhere: one number per return, no nominal/real toggle, no per-stream escalation.
- **Limitation made explicit:** the model cannot represent inflation *variability*, differential
  asset-class inflation impact, or non-COLA pension erosion. For two streams that are fully COLA'd
  this is fine; it would mislead for a fixed-nominal pension.
- The `cpi-inflation` backlog card is a **deliberate reopening** of this decision, not a missing
  feature — it would add the nominal frame this ADR rejected for v1. Anyone picking it up should
  read this ADR first.

## Status

Accepted, with a known reopening tracked on the board (`cpi-inflation`).
