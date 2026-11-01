# ADR 0003 — Frontend has zero build step; window-global IIFE modules, no ES modules

## Context

The web UI is plain HTML + vanilla JS + Chart.js, served as static files behind uvicorn/nginx.
A modern frontend would reach for ES modules and a bundler. We deliberately did not.

## Decision

No build step, no bundler, no npm runtime dependency. JS "modules" are window-global IIFEs
(`window.ChartView`, `window.ComparisonState`, …). Tests use Node's built-in `node:test` and load
modules by sandboxing the script and reaching the `window.X` export — no transpilation.

## Consequences

- **Pro:** editing a file and reloading the browser is the entire dev loop; nothing to install,
  build, or keep in sync; the server needs no toolchain. Matches the "boring infrastructure" rule.
- **Con:** no tree-shaking, no import graph, no type-checking; module boundaries are a convention
  (an IIFE assigning to `window`), not enforced by the loader. Extracting logic from `web/app.js`
  means following this convention, not introducing `import`/`export`.
- A new shared module follows the established IIFE pattern; do not introduce a bundler to add one.

## Status

Accepted.
