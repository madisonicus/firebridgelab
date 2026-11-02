# FAQ for non-programmers

FIRE Bridge Lab is free, open-source software for people planning **FIRE** (Financial
Independence, Retire Early). You do not need to know Python to use it. This page answers the
questions people ask first.

!!! warning "Not financial advice"
    This is educational software. It shows how a plan behaved across past markets and
    simulated ones under the assumptions you give it. It does not know your full situation,
    it cannot predict markets, and it is not a substitute for a qualified advisor.

## How do I try it without installing anything?

Open the notebooks in your browser with Binder:
[![Launch Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/madisonicus/firebridgelab/main?labpath=notebooks%2F04-what-if-experiments.ipynb)

Binder is a free public service that builds the project in the cloud and opens it in
JupyterLab. The first start can take a few minutes while it downloads the return data. Each
notebook is a document with text, tables and charts; to run it yourself choose
**Run → Run All Cells**. To try your own numbers, change the values in a cell (for example the
plan text at the top of notebook 04) and run it again. Nothing you change is saved after you
close the tab, so download anything you want to keep.

The notebooks also read on GitHub without running anything: they are committed with their
results.

## How do I run the web app on my own computer?

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Podman), then
run this one line in a terminal:

```bash
docker build -t firebridgelab https://github.com/madisonicus/firebridgelab.git#main && docker run --rm -p 127.0.0.1:8000:8000 firebridgelab
```

Open <http://127.0.0.1:8000/> in your browser. The first build takes a few minutes (it
downloads the return data); later runs start in seconds with
`docker run --rm -p 127.0.0.1:8000:8000 firebridgelab`. Everything runs on your computer;
nothing is sent anywhere.

In the web app:

- **Examples…** loads one of the [example households](examples.md).
- **Household & income streams** describes your household: one or two people with their own
  ages, and any number of pensions, Social Security benefits, annuities, part-time work or
  rental income, plus one-off events (a home sale) and required spending (health insurance
  before Medicare).
- **Recompute** runs the plan; the tabs at the top switch between a single average path,
  every historical start year, Monte Carlo, and stress tests.
- **Download plan** saves your scenario as a small text file (a plan file).
  **Load plan** opens one again. The same file works on the command line.

## I'd rather use the command line

With Python 3.11 or newer installed:

```bash
pipx install "git+https://github.com/madisonicus/firebridgelab"   # or: pip install ...
firebridgelab-fetch-data          # downloads the public return data once
firebridgelab init my-plan.toml   # writes a starter plan with comments
firebridgelab run my-plan.toml
```

Edit `my-plan.toml` in any text editor. [Plan files](plan-files.md) lists every setting.
`firebridgelab sweep` tries several values of a setting at once, for example
`--set spending.annual=60000:90000:10000`.

## What do the numbers mean? {#what-do-the-numbers-mean}

**All dollars are "real"**: today's purchasing power. $60,000 at age 80 means what $60,000
buys today, not a future nominal amount.

The model runs your plan three ways:

- **Deterministic**: one path where markets return their long-run average every year. Useful
  for reading the year-by-year flows, useless for judging risk: real markets never do this.
- **Historical**: your plan replayed starting in every year since 1871 for which there is
  enough data to cover your whole plan (for a 45-year plan, about 110 start years). This shows
  how your plan would have done through 1929, the 1970s inflation, 2000 and 2008.
- **Monte Carlo**: thousands of made-up return sequences with the same average and volatility
  as history.

The headline numbers:

| Number | Meaning |
| --- | --- |
| **success** | The share of paths in which the portfolio never ran out before the end of the plan. |
| **depleted paths** | How many paths ran out. After that, spending falls to whatever pensions and Social Security pay. |
| **earliest depletion age** | The youngest age (person A's) at which any path ran out. |
| **ever forced to spending floor** | For flexible spending rules: the share of paths where spending was cut all the way to your floor at least once. |
| **spending at 60 (p10/p50/p90)** | Spending at that age in a bad path (10th percentile), a typical one (median) and a good one (90th percentile). |
| **ending portfolio** | What is left at the end of the plan, in the same three percentiles. |
| **lifetime tax** | With a tax-account split: total tax paid over the plan. |

Read "96% of historical cohorts succeeded" as "the plan survived 96 of 100 past start years",
not as "a 96% chance". The start years overlap heavily, so they are far fewer independent
histories than they look, and the future may be worse than any of them. Monte Carlo has its
own blind spot: it has no long runs of bad years like 1966–1982. Use both as a range.

The [glossary](glossary.md) explains the terms, and [methodology](methodology.md) gives every
rule and its source.

## Is my data private?

The web app and the command line run entirely on your computer. Binder runs on a shared public
server: don't type real account numbers or anything sensitive there. None of the tools need
your name, accounts or anything beyond the numbers in your plan.

## What doesn't it model?

Among other things: graduated-bracket state taxes, spousal Social Security benefits, the
earnings test, long-term-care shocks as explicit lines, and accounts tracked per person. The
README's "Methodology and limitations" section has the full list.

## Who maintains it, and can I get help?

It is provided as-is by one person. Issues and pull requests on GitHub are welcome, but no
support is promised.
