# Changelog

## Unreleased

### M1: package layout

- Split the package into `returns`, `income`, `spending`, `tax`, `engine`
  and `analysis`. The old flat module paths still import, as thin re-exports
  kept for one release (docs/library.md).

### M2: households

- One- or two-person households with income streams (pension, Social
  Security, annuity, work, rental, other), each with its own COLA, survivor
  fraction and tax treatment; one-off cash events; required spending
  adjustments. The API accepts and echoes them in POST bodies.

### M3: plan files and the command line

- Plan files describe a whole plan in TOML (docs/plan-files.md).
- The `firebridgelab` command runs, compares, sweeps, solves and reports
  plans.

### M4: tax accuracy, every rule cited

- New docs/methodology.md: one section per rule, with its primary source, and
  a list of what is not modelled. Tax tables live in `firebridgelab/tax/data/`
  (`federal_2026.toml`, `medicare_2026.toml`, `states_2026.toml`), and a test
  fails if any table lacks a `source`.
- **Social Security taxation** follows the provisional-income formula of IRC
  86 / IRS Pub. 915 (`tax.ss_taxation = "provisional"`). The old flat 85% is
  kept as `"flat85"`.
- **Unindexed thresholds now shrink in real terms.** The Social Security base
  amounts and the NIIT and additional-Medicare thresholds are fixed in
  nominal dollars by law, so the engine erodes them at `assumed_inflation`.
  Before, the NIIT threshold was held constant in real terms.
- **Payroll tax** (`tax.payroll`, off by default): employee FICA on `work`
  streams up to the 2026 wage base of $184,500 (SSA), or self-employment tax
  for streams marked `self_employed`, with the half-SE-tax deduction, plus the
  0.9% additional Medicare tax.
- **Medicare IRMAA** (`tax.irmaa`, off by default): the 2026 CMS Part B and D
  surcharges from 65, set by the plan's own MAGI two years earlier and added
  as required spending.
- **Roth conversions** (`[tax.roth_conversion]`, off by default): convert
  tax-deferred to Roth each year in an age window, up to the top of a bracket
  or a dollar target, taxed that year. `run` and the API report lifetime tax
  and the terminal after-tax value with and without the policy.
- **State income tax** (`[tax.state]`, off by default): AK, FL, NV, NH, SD,
  TN, TX and WY (no income tax), the flat-rate states GA, IL, IN, IA, LA, MS,
  NC and PA (each sourced from its revenue department), or a `custom` flat
  rate. One state of residence, optionally changing at an age.
- The year tables gain `state_tax`, `payroll_tax`, `irmaa` and
  `roth_conversion` columns when those options are used. The CLI's "lifetime
  federal tax" is now "lifetime tax", because it includes state and payroll
  tax when those are on.

#### Why provisional Social Security taxation is the default

The flat 85% overstates tax whenever provisional income is below the base
amounts. It also ignores the 50% tier. Both are common in the low-income
bridge and early Social Security years this model is built for. The change
moves results only for plans with a tax split: without one the engine
computes no tax.

- The deterministic canary (ADR 0001) uses no tax split and is unchanged; its
  pins are not re-pinned.
- Every hand-anchored tax test calls the fixed point directly with its income
  pinned, so none moved.
- One household test moved, deliberately, because of the NIIT threshold
  erosion: `test_home_sale_capital_gain_is_taxed_in_the_sale_year`. Its
  year-70 bill gains $882.71 of NIIT (200,000 / 1.025^5 = 176,770.86
  threshold), and the hand derivation is in the test.
- In the bundled examples, `part-time-bridge` (deterministic) pays $385,831
  of lifetime tax instead of $392,594 and ends $36,286 richer.
  `deferred-federal-couple` is unchanged: its income keeps it in the 85% tier.

### M5: the easiest way in

- The web UI edits households: one or two people with their own ages, a list
  of income streams, cash events, required spending and the M4 tax options.
  The legacy two-pension / two-Social-Security fields still work, and turning
  the editor on converts them to streams.
- The web UI loads and downloads plan files and loads the bundled examples
  (`/api/plan/import`, `/api/plan/export`, `/api/examples`).
- Notebook 02 uses households and plan files; notebook 04 runs what-if
  sweeps. The notebooks run in a browser on Binder (`binder/`).
- A documentation site (`mkdocs.yml`) with an examples gallery, a FAQ for
  non-programmers and a glossary; CI builds it with `--strict`.
- The examples are real package data in `firebridgelab/examples/` (they were
  a symlink, which Windows checkouts break).
