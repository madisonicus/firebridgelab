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
- **Easier to start.** The notebooks run in a browser on Binder. The web app
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
- **Capital-gain tax.** In a narrow income band (taxable income just above
  $98,900 joint or $49,450 single) the tax was up to $57 too high, because the
  model skipped the last line of the IRS capital-gain worksheet.

Neither fix changes the results of the bundled examples.

## Upgrading

- Results change only for plans with a survivor case in which the first to die
  claimed Social Security at 64 or earlier, or died before claiming,
  and for plans with a tax split whose income lands in the band above.
- The old flat module paths (`firebridgelab.scenario`,
  `firebridgelab.historical`, ...) still import in this release and will be
  removed in the next. Import from `firebridgelab` or its subpackages instead.
- The full list of changes is in CHANGELOG.md.

## Known limitations

Not yet modelled: graduated-bracket state taxes; Social Security spousal
benefits, the earnings test, and the reduction of a survivor benefit taken
before full retirement age; the FERS rule of no COLA before 62 within a single
stream (split the stream at 62, as the `mra10-single` example does); accounts
tracked per person; tax on taxable-account interest and dividends. The README
and docs/methodology.md list the rest.

Educational software, not financial advice.
