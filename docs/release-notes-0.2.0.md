# FIRE Bridge Lab 0.2.0

FIRE Bridge Lab models the bridge years between early retirement and pensions
and Social Security: historical backtests since 1871, Monte Carlo, dynamic
spending rules and US federal tax. This release makes it model your own
household, run from plain-text plan files, compute a more complete tax bill,
and show how its numbers compare with published references.

## Highlights

- **Your household, not a template.** One or two people with their own ages
  and horizons; any number of income streams (pension, Social Security,
  annuity, part-time work, rental, other), each with its own start and end
  age, COLA rule and survivor share; one-off cash events such as a home sale;
  and required spending such as health insurance before Medicare.
- **Plan files and a command line.** Describe a whole plan in a small TOML
  file and run it with `firebridgelab run`, `compare`, `sweep`, `solve` or
  `report` (one offline HTML file). `firebridgelab init --list` shows the
  bundled examples.
- **A fuller tax bill, every rule cited.** Social Security is taxed by the
  provisional-income formula (IRS Pub. 915). Thresholds that the law does not
  index shrink with inflation. Optional payroll tax, Medicare IRMAA, a Roth
  conversion policy, and state income tax for the no-tax and flat-rate states.
  docs/methodology.md gives the source of each rule and what is left out.
- **Easier to start.** The notebooks are set up to run in a browser on Binder. The web app
  edits households, loads the examples and loads and saves plan files. A
  documentation site has an examples gallery, a FAQ for non-programmers and a
  glossary.
- **Validation.** docs/validation.md compares the model with the Trinity
  (1998, 2011) and Bengen (1994) studies, the Social Security regulations, IRS
  worked examples, the FERS COLA statute, closed-form annuity math and the
  independent optimizer Owl. Each comparison cites its source, states its
  tolerance and explains any gap, and each is a test that CI runs.

## Fixes found by the validation work

- **Survivor Social Security.** When the first spouse to die had claimed
  early, the survivor now gets at least 82.5% of that spouse's full benefit,
  as the regulation requires; before, the survivor got the reduced benefit
  (70% for a claim at 62). When the first spouse died before claiming, the
  survivor now gets the full benefit plus any delayed credits earned up to the
  death, starting the year of the death; before, nothing was paid on that
  record until the planned claim age.
- **Survivor below full retirement age.** A widow(er)'s benefit taken before
  full retirement age is now reduced for the survivor's age, as the
  regulation requires: to 71.5% of the decedent's full benefit at 60, 79.6% at
  62, 87.8% at 64. It is not paid before the survivor's 60; someone widowed
  younger gets it from 60. Before, the full benefit was paid at any age.
- **FERS COLA before 62.** Regular FERS retirees get no cost-of-living
  increase before 62. A `fers_diet` pension that starts before 62 is now a
  fixed dollar amount until its owner is 62; before, it got increases from
  the start, which overstated it by about 10% from 62 for a retirement at 57.
  Disability and special-provision retirees, who do get increases before 62,
  set `fers_cola_before_62 = true` on the stream.
- **Amortization spending rule.** The rule now plans with the same mid-year
  withdrawals the simulation uses. On a steady return equal to the planning
  rate its spending is level and the portfolio ends at zero; before, spending
  started about 1.5% low (at 3%) and crept up over the plan.
- **Capital-gain tax.** In a narrow income band (taxable income just above
  $98,900 joint or $49,450 single) the tax was up to $57 too high, because the
  model skipped the last line of the IRS capital-gain worksheet.

None of these fixes changes the results of the bundled examples.

## Upgrading

- Results change for plans with a survivor case in which the first to die
  claimed Social Security at 64 or earlier or died before claiming, or in
  which the survivor is younger than 67 at the death; for plans with a
  `fers_diet` pension that starts before its owner's 62; for plans using the
  `amortization` spending rule; and for plans with a tax split whose income
  lands in the band above.
- **Breaking:** the `firebridgelab.simulate` module is removed. Importing it
  replaced the `firebridgelab.simulate()` function with the module. Use
  `firebridgelab.simulate` (the function) or import `simulate`, `Trajectory`
  and `YearPoint` from `firebridgelab.engine.deterministic`.
- The other old flat module paths (`firebridgelab.scenario`,
  `firebridgelab.historical`, ...) still import in this release and will be
  removed in the next. Import from `firebridgelab` or its subpackages instead.
- The full list of changes is in CHANGELOG.md.

## Known limitations

Not yet modelled: graduated-bracket state taxes; Social Security spousal
benefits and the earnings test; accounts tracked per person; tax on
taxable-account interest and dividends. The README
and docs/methodology.md list the rest.

Educational software, not financial advice.
