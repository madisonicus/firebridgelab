# FIRE Bridge Lab

![FIRE Bridge Lab: stress-test the bridge years between early retirement and pension + Social Security](docs/img/social-preview.png)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

*Stress-test the bridge years: backtests, Monte Carlo and guardrails from early retirement to pension and Social Security.*

A free, open-source lab for people planning **FIRE** (Financial Independence, Retire Early) who want to model and experiment with the risk in their plan. Change the variables (retirement age, spending, asset mix, pensions, Social Security and when you claim it, a spouse dying first, fees, taxes) and see how each one moves your odds across every historical retirement since 1871 and thousands of Monte Carlo scenarios.

It is built for the common FIRE situation where you stop working years before guaranteed income starts: the portfolio has to carry everything during those **bridge years**, then only the gap that pensions and Social Security don't cover.

**What you can vary:**

- **When:** retirement age, plan horizon, pension start ages, Social Security claiming ages (62–70, with the exact SSA adjustment)
- **How much:** starting portfolio, annual spending, a spending floor and ceiling, a lifecycle "go-go / slow-go / no-go" taper
- **Spending rule:** constant, Guyton-Klinger-style guardrails, funded-ratio rules, or an amortization (VPW-style) frontier
- **Household:** one or two people with their own ages and planning horizons; any number of income streams (pensions, Social Security, annuities, part-time work, rental, other) each with its own start and end age, COLA rule (full, none, a fixed percentage, or FERS-style), survivor fraction and tax treatment; one-off cash events such as a home sale or a car; required spending over an age range such as pre-Medicare healthcare or a mortgage; and a survivor case (library, API and plan files; the web UI still edits two pensions and two Social Security benefits)
- **Markets:** asset mix across up to 12 asset classes (five need optional data you download yourself), historical replay or Monte Carlo (Normal or fat-tailed), fees, and authored stress scenarios
- **Taxes:** taxable, tax-deferred and Roth balances, with 2026 US federal brackets, capital gains, RMDs and the pre-59½ penalty

Not yet modeled: state or non-US taxes; payroll tax on work income; Social Security spousal benefits and the earnings test; a person's planning end age as a death (it is a horizon only; deaths come from the survivor case); and per-person retirement accounts (the 59½ and RMD ages follow the first person).

It ships as a command line that runs plans written as small text files, a Python library, worked Jupyter notebooks, and a self-hosted web UI.

### Why another FIRE calculator?

The popular free tools (cFIREsim, FIRECalc, FI Calc, ERN's SWR Toolbox) are excellent at constant-income backtests. FIRE Bridge Lab focuses on what they leave out or approximate:

- **Bridge years:** spending rules that know a pension at 60 and Social Security at 67 are coming, instead of treating them as a flat adjustment.
- **Taxes:** taxable, tax-deferred and Roth accounts, 2026 federal brackets, capital gains stacking, NIIT, RMDs and the pre-59½ penalty, solved each year.
- **Pensions as they really work:** amounts fixed in dollars until they start, FERS-style partial COLAs, survivor benefits, single-filer brackets after a first death.
- **Validated:** Trinity/Bengen backtests reproduced, and a per-cohort cross-check against the independent optimizer [Owl](https://github.com/mdlacasse/Owl) (see the notebooks).

**Educational software, not financial advice. Consult a qualified advisor before acting.**

## Quick start with a plan file

A plan is a short [TOML](https://toml.io) file you can edit in any text editor
and keep in git. [docs/plan-files.md](docs/plan-files.md) lists every key.

```bash
pip install "firebridgelab[report] @ git+https://github.com/madisonicus/firebridgelab"
firebridgelab-fetch-data                 # the public return data, once

firebridgelab init plan.toml             # a commented starter plan; edit it
firebridgelab run plan.toml              # deterministic, historical, Monte Carlo
firebridgelab sweep plan.toml --set spending.annual=60000:90000:10000 \
                              --set income_streams.a_ss.start_age=62,67,70
firebridgelab solve plan.toml --confidence 0.9     # max sustainable spending
firebridgelab compare plan.toml plan-retire-55.toml
firebridgelab report plan.toml -o report.html      # one offline HTML file
```

`run` prints success and depletion counts, the earliest depletion age, how
often a dynamic rule hits its spending floor, spending and ending-portfolio
percentiles, lifetime tax when you give a tax split, and a
year-by-year table; add `--format json` or `--format csv` for scripts. `sweep`
runs every combination of the values you give it. Any key can be changed for
one run with `--set`, e.g. `--set people.a.start_age=55`.
`firebridgelab init --list` shows the [examples](firebridgelab/examples/), all fictional:
a deferred federal couple, an MRA+10 single retiree, a leanfire single, a
Social Security-only couple with an age gap, a part-time bridge with ACA
premiums, and rental income with a pension. `firebridgelab ui` starts the web
UI from a checkout.

## The example household

The built-in default household is fictional: two people aged 50 who plan to
age 95, a $1.8M portfolio, $110k a year of real spending, pensions of $24k
(from 60) and $9k (from 65), and Social Security of $33k and $26k at full
retirement age, claimed at 67 and 70.

## Three ways to use it

### 1. Library

```bash
pip install "git+https://github.com/madisonicus/firebridgelab"
firebridgelab-fetch-data      # downloads the public data (a few MB)
python -c "import firebridgelab as rm; print(rm.simulate(rm.default_scenario()).final_portfolio)"
```

Data goes to `$FIREBRIDGELAB_DATA` if set, else `data/cache/` in a source
checkout, else `~/.cache/firebridgelab`. The public API is `Scenario`,
`default_scenario`, `simulate`, `simulate_historical`, `simulate_monte_carlo`,
`solve_max_spend` and `simulate_stress`; [docs/library.md](docs/library.md) has
a quickstart.

### 2. Notebooks

```bash
git clone https://github.com/madisonicus/firebridgelab
cd firebridgelab
python -m venv .venv && . .venv/bin/activate
pip install -e ".[notebooks]"
firebridgelab-fetch-data
jupyter lab notebooks/
```

| Notebook | What it shows |
| --- | --- |
| [01-historical-backtest](notebooks/01-historical-backtest.ipynb) | Trinity/Bengen-style checks: 30-year 4% withdrawals on four stock/bond mixes, success rates, failing start years, worst-cohort safe withdrawal rates, and the limits of the Shiller data. |
| [02-pension-bridge-guardrails](notebooks/02-pension-bridge-guardrails.ipynb) | A fictional early-retiree couple bridging to pensions and Social Security: constant spending vs guardrails vs the amortization frontier, FERS-style pension options, a survivor case, fees, and Student-t Monte Carlo. |
| [03-owl-cross-check](notebooks/03-owl-cross-check.ipynb) | The same household through [Owl](https://github.com/mdlacasse/Owl), an independent optimizer, for every historical start year. |

The notebooks are committed with their outputs, so they read on GitHub without
running anything.

### 3. Self-hosted web UI

The browser UI compares two scenarios side by side, and the same JSON API
serves scripts. With Docker or Podman:

```bash
git clone https://github.com/madisonicus/firebridgelab
cd firebridgelab
docker compose up -d
# or: podman build -t firebridgelab . && podman run -d -p 8000:8000 firebridgelab
```

Then open <http://127.0.0.1:8000/>. The image downloads the data at build time.
With compose, saved scenarios live in the `scenarios` volume.

Or from a checkout, without a container:

```bash
pip install -r requirements.txt -e ".[web]"
firebridgelab-fetch-data
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

The API describes itself at `/docs`. The main endpoints are `GET`/`POST
/api/trajectory` (all three modes and stress tests), `/api/solve` (max
sustainable spend), `/api/classes` and `/api/presets`. The page loads its
assets and calls the API by relative path, so it also works behind a reverse
proxy under a path prefix.

## Return modes

| Mode | Function | What it does |
| --- | --- | --- |
| Deterministic | `simulate` | One constant real return, compounded year by year. The rate is the allocation's *rebalanced* compound return: the weighted arithmetic mean less half the portfolio variance, computed from the same volatilities and correlations that Monte Carlo samples. |
| Historical | `simulate_historical` | Replays every historical cohort in the 1871–2025 Shiller data. Each cohort is a window as long as the plan (46 years for the default, ages 50–95, which gives 110 cohorts) that starts in a real calendar year. The result is a percentile fan. Multi-class allocations use the window that every selected class covers. |
| Monte Carlo | `simulate_monte_carlo` | Draws per-class returns from `Normal(arithmetic_mean, vol)`, or from a fat-tailed multivariate Student-t with the same mean, volatility and correlation. Simple returns are floored at −99%. Draws are Cholesky-correlated through a correlation matrix estimated pairwise over the longest overlap of each pair, then projected to the nearest positive semi-definite matrix. By default, 10,000 seeded trials go through the same cohort loop as Historical. |

The mean used for sampling is `compound + vol²/2`, the standard variance-drag
adjustment, so the Monte Carlo median terminal value sits close to the
deterministic one: within a few percent, and slightly below once withdrawals
add sequence risk.

**Stress tests** (`simulate_stress`) run the plan through authored adverse
return paths (1929, the 1970s, 2000–02, 2008, a valuation unwind, a
no-recovery depression) and through scenario perturbations: a 23% Social
Security cut, government-income austerity (the SS cut plus a frozen pension
COLA), a one-time +20% cost-of-living shock, and a sustained +1.5%/yr real
cost-of-living drift.

## Spending strategies

| Strategy | What it does |
| --- | --- |
| Constant | Flat real spending at every age. |
| Lifecycle | Age-anchored taper off the input level: ×1.00 to 74, ×0.85 from 75 to 84, ×0.75 from 85. |
| Guardrails | Guyton-Klinger-style capital-preservation and prosperity rules on the withdrawal rate net of guaranteed income, times the lifecycle taper, clamped to a floor and ceiling. Band, adjustment, floor and ceiling are editable. Stateful. |
| Funded ratio (smooth) | Partial adjustment toward the funded ratio (portfolio ÷ NPV of future spending gaps), α = 0.5. |
| Funded ratio (bands) | ±10% steps triggered at funded ratios of 1.20 and 0.80. |
| Funded ratio (tiers) | A fixed preset tier ladder (FLOOR $85k up to MAX $160k), with the funded ratio measured against the MAX tier. |
| Amortization frontier | Each year, the constant real spend that exactly amortizes the portfolio over the remaining horizon against the gaps left after guaranteed income. It is a maximum-consumption benchmark: unclamped, with terminal value driven toward zero. |

Constant and lifecycle are path-independent, so the max-sustainable-spend
solver works on them. The other strategies depend on the path and are not
solvable. Withdrawals can come from a single portfolio or from a three-bucket
cash / income / growth structure. The allocation can also follow a glidepath
that changes at guaranteed-income events.

## Methodology and limitations

All dollars are real, in plan-start purchasing power. Each year, the starting
balance grows by that year's real return net of `annual_fee`. Spending and
guaranteed income are both mid-year flows (each `× (1 + r/2)`). When a
tax-location split is supplied, federal tax is a year-end outflow solved by a
fixed point.

**Data.** US stocks and the 10-year Treasury proxy come from Robert Shiller's
monthly file, January to January. Its prices are monthly averages, which mute
crash years by 2–5 points against calendar-year data (2022 is −17.4% against
−23.0%). The bond class is built from the 10-year yield with a fixed duration
of 7, not from an aggregate-bond index. Other classes come from Damodaran,
Fama-French, FRED, and (optionally) Nareit and testfol.io simulated series; see
[DATA-SOURCES.md](DATA-SOURCES.md). Historical cohorts overlap heavily: 110
cohorts of 46 years amount to about three independent histories, so "% of
cohorts" is a count, not a probability.

**Pensions and Social Security.** Amounts are entered for the start age. With
`pension_nominal_until_start`, a pension is read as the nominal check at
commencement and deflated at `assumed_inflation`. This fits, for example, a US
federal (FERS-style) deferred annuity, which is fixed from separation until it
starts. `pension_cola="fers_diet"` then applies the FERS COLA rule each year it
is paid. Social Security is entered as the full-retirement-age benefit plus a
claim age (62–70). The API applies the SSA early-reduction and delayed-credit
factors, and the benefit is then constant in real terms. The survivor case
(`survivor_age`) keeps the larger SS benefit, continues
`survivor_pension_fraction` of the decedent's pension, scales spending (and the
guardrail rails) by `survivor_spending_fraction`, and files single from the
death year.

**Tax.** With a tax split the accounts pay a yearly bill solved as a fixed
point. Federal brackets and deductions are the 2026 IRS tables, held constant
in real dollars; amounts the law does not index (the Social Security base
amounts, the NIIT and additional-Medicare thresholds) shrink at the assumed
inflation rate. Social Security is taxed by the provisional-income formula by
default (`tax.ss_taxation`). Options add payroll tax on work income, Medicare
IRMAA surcharges, a Roth conversion policy, and state income tax for the
no-tax and flat-rate states whose rules could be read from the state (AK, FL,
NV, NH, SD, TN, TX, WY, GA, IL, IN, IA, LA, MS, NC, PA) or a custom flat rate.
A flat taxable-gains fraction stands in for cost basis. RMDs are modelled, and
a bracket-fill lever moves extra money from tax-deferred to taxable between
ages 60 and 74. Every rule, its source and what is left out are in
[docs/methodology.md](docs/methodology.md).

**What the model does not do:** tax without a tax split, graduated-bracket
states and the flat-rate states not listed above, local income taxes, state
credits, foreign tax, automatic ACA or standard Medicare premiums (enter them
as required spending), long-term-care shocks as explicit lines, tracked cost
basis, tax on taxable-account dividends and interest, Roth five-year rules,
72(t)/SEPP access before 59½ (the penalty is applied and reported instead),
the FERS no-COLA-before-62 rule, accounts tracked per person (access ages and
RMDs follow person A), mortality-weighted horizons, and valuation-conditioned
or regime-switching returns. Monte Carlo has no autocorrelation or parameter
uncertainty. Its fixed seed means success rates carry about ±0.5 point of
sampling noise.

## Validation

- **Trinity / Bengen** ([notebook 01](notebooks/01-historical-backtest.ipynb)).
  30-year 4% constant-real withdrawals over the 126 Shiller cohorts starting
  1871–1996 succeed in 81.7% (25/75), 95.2% (50/50), 96.8% (75/25) and 97.6%
  (100/0) of cohorts. At 50/50 the failures are the 1964–69 starts. The
  worst-cohort safe withdrawal rate is 3.34%, 3.58%, 3.74% and 3.83% for the
  four mixes; 1966 is the worst start for the first three and 1929 for 100/0.
  These are in line with Shiller-based tools such as cFIREsim and FIRECalc.
- **Closed form.** Zero-volatility paths match annuity math to 1e-9.
- **IRS constants.** The 2026 federal brackets, standard deduction and
  capital-gains thresholds match IRS Rev. Proc. 2025-32.
- **Owl cross-check** ([notebook 03](notebooks/03-owl-cross-check.ipynb)).
  The default fictional household (60/40 stocks/10-year Treasuries, 25/60/15
  taxable/tax-deferred/Roth, indexed pensions, flat spending, no bequest) went
  through Owl 2026.10.28 for every start year from 1928 to 1980, the last with a
  full 46-year window. The maximum flat after-tax spend per cohort compares as
  follows:
  - worst cohort: both tools pick 1973; Owl gives $91,845 and this library
    $90,991 (−0.9%);
  - median cohort: Owl $115,803, this library $117,499 (+1.5%);
  - per cohort, this library ranges from −3.7% to +6.3% of Owl (median +1.6%),
    with a rank correlation of 0.99;
  - both name the same four worst starts: 1973, 1969, 1966 and 1968 here, with
    the last two swapped in Owl.

  Run on Owl's own return table, this library comes out 0.9% to 4.3% above Owl
  in every cohort. So most of the scatter comes from the two tools' return
  data, and the steady gap comes from modelling differences; for example, this
  library does not tax taxable-account interest and dividends.

## Data sources

The library reads Robert Shiller's *Irrational Exuberance* data, Aswath
Damodaran's historical returns, the Kenneth R. French Data Library and FRED.
`firebridgelab-fetch-data` downloads them. They are not in this
repository and must not be committed, and the optional Nareit and testfol.io
series cannot be redistributed at all. [DATA-SOURCES.md](DATA-SOURCES.md) has
the terms, the citations and how to add the optional classes.

## Tests

```bash
pip install -r requirements.txt -e ".[dev]"
firebridgelab-fetch-data
python -m pytest -q tests          # library + API
node --test tests/test_*.mjs       # web modules (Node 22, no npm dependencies)
```

The default scenario's deterministic output is pinned as a canary
([ADR 0001](docs/adr/0001-byte-stable-canary-discipline.md)). Shiller revises
recent months, so the pin is exact only on the data revision recorded in
`tests/_canary.py` and holds to a relative 1e-4 on any other. The
`tests/playwright_*.mjs` scripts are optional browser checks, not part of the
suite. The settled design decisions are in [docs/adr/](docs/adr/README.md).

## Layout

```
firebridgelab/  the library (pure Python, real dollars), the data fetcher and the CLI (cli/)
firebridgelab/examples/  fictional plan files (package data, so `firebridgelab init --example` works anywhere)
api/                 FastAPI app: parameter parsing, validation, serialization
web/                 zero-build vanilla-JS frontend
notebooks/           worked examples, committed with outputs
tests/               pytest and node:test suites, Playwright browser checks
docs/                library quickstart and architecture decision records
```

## Contributing and support

This project is provided as-is. Issues and pull requests are welcome, but no
support is promised. Tax tables are refreshed yearly while the project is
maintained. Please keep test fixtures and examples fictional.

## License

MIT; see [LICENSE](LICENSE). Data sources have their own terms; see
[DATA-SOURCES.md](DATA-SOURCES.md).
