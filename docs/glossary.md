# Glossary

Plain-language definitions of the terms used across FIRE Bridge Lab. Where the model
applies a precise rule, [Methodology](methodology.md) has it with its source.

## Planning terms

FIRE
:   Financial Independence, Retire Early: saving and investing enough to stop depending on a
    paycheck, often years before a standard retirement age.

Bridge years
:   The years between leaving work and the start of guaranteed income (a pension, Social
    Security). The portfolio pays for everything during the bridge, then only the gap the
    guaranteed income leaves. Most of an early retiree's risk sits here.

Guaranteed income
:   Income that does not depend on markets: pensions, Social Security, annuities, and in this
    model any income stream you enter (part-time work, rental).

Income stream
:   One source of guaranteed income in a plan: its kind, owner, amount, start and end age,
    cost-of-living rule, survivor share and tax treatment. See [Plan files](plan-files.md).

Required spending
:   Spending on top of the spending rule over an age range, such as health insurance before
    Medicare or a mortgage. A flexible spending rule cannot cut it.

Cash event
:   A one-off flow in a single year: an inheritance or home sale in, a car or roof out.

Real dollars
:   Dollars of today's purchasing power. Every amount in the model is real unless it is marked
    nominal, so $50,000 at 80 buys what $50,000 buys today.

Nominal dollars
:   The face value of money in the year it is paid. A pension that is "fixed in dollars" is
    fixed in nominal dollars and loses real value to inflation.

## Withdrawals and spending rules

Withdrawal rate
:   A year's withdrawal from the portfolio as a share of the portfolio. The model's guardrails
    use the rate **net of guaranteed income**, which matters in the bridge years.

SWR (safe withdrawal rate)
:   The highest starting withdrawal rate, held constant in real terms, that would have lasted
    through the worst historical period for a given horizon and asset mix. The "4% rule" is
    Bengen's 30-year SWR from US data. It is a historical worst case, not a guarantee, and
    early retirees with longer horizons usually need less than 4%.

Sequence (of returns) risk
:   The risk that bad returns arrive early, while withdrawals are largest relative to the
    portfolio. Two retirees with the same average return can end very differently depending
    on the order of the years. The bridge years are where sequence risk bites.

Guardrails
:   A flexible spending rule (after Guyton and Klinger): when the withdrawal rate drifts too
    far above its starting level, spending is cut (by 10% by default); when it drifts too far
    below, spending rises; always between a floor and a ceiling. It trades occasional cuts for
    a lower chance of running out.

Spending floor and ceiling
:   The lowest and highest discretionary spending a flexible rule may choose.

Funded ratio
:   The portfolio divided by the present value of future spending not covered by guaranteed
    income. Above 1 the plan is "overfunded"; the funded-ratio rules adjust spending toward it.

Amortization frontier (VPW-style)
:   Each year, the constant real spending that would exactly use up the portfolio by the end
    of the plan, given the guaranteed income still to come. A benchmark for the most you could
    spend, not a recommendation; it is built to finish at zero.

Lifecycle ("go-go, slow-go, no-go")
:   A spending shape that starts at your full level and steps down later in life (by default
    to 85% from 75 and 75% from 85).

Glidepath
:   An asset mix that changes over the plan, here at the ages where income starts or stops.

Bucket strategy
:   Holding a cash bucket and an income (bond) bucket for the next few years' needs and a
    growth bucket for the rest, spending from cash first.

## Simulation terms

Cohort
:   One historical start year. A 45-year plan replayed from 1871, 1872 and so on gives one
    cohort per start year with enough data.

Historical backtest
:   Running the plan through each cohort's actual sequence of returns and inflation.

Monte Carlo
:   Running the plan through thousands of randomly drawn return sequences with the historical
    average, volatility and correlation between asset classes.

Fat tails (Student-t)
:   Return distributions where extreme years are more common than a bell curve predicts. The
    Student-t option keeps the same average and volatility but moves weight into the tails.

Percentiles (p10, p50, p90)
:   Out of all paths, the value 10% of paths fall below (a bad outcome), the median, and the
    value only 10% exceed (a good outcome).

Stress test
:   A hand-built adverse scenario: a replay of 1929, the 1970s or 2008, or a change to the plan
    itself such as a 23% Social Security cut.

Expense ratio, fee
:   The yearly cost of investing as a share of assets. In the model it comes off every year's
    return.

## Pensions and Social Security

COLA (cost-of-living adjustment)
:   The yearly increase in a pension or benefit. A **full** COLA keeps its real value; **none**
    leaves it fixed in nominal dollars; a **fixed** COLA adds a set percentage a year.

FERS
:   The US Federal Employees Retirement System. Its basic annuity is a common bridge-years
    pension for federal workers.

FERS diet COLA
:   The FERS rule that trails inflation: the full CPI increase when CPI is 2% or less, 2% when
    CPI is between 2% and 3%, and CPI minus 1% above 3%. Regular retirees get no COLA before
    62, so the annuity is a fixed dollar amount until then. Over a long retirement the annuity
    loses real value.

MRA, MRA+10, deferred annuity
:   FERS terms. The MRA (minimum retirement age) is 55 to 57 depending on birth year. MRA+10
    lets an employee with 10 years of service retire at the MRA with a reduced annuity. A
    deferred annuity is one started after leaving service; it is fixed in dollars until it
    starts (`nominal_until_start` in a plan file).

FRA (full retirement age)
:   The age at which Social Security pays 100% of your benefit: 67 for anyone born in 1960 or
    later. The model takes the benefit at FRA from your SSA statement.

Claim age
:   When you start Social Security, from 62 to 70. Claiming at 62 pays 70% of the FRA benefit;
    each year after FRA adds 8% (124% at 70). The model applies the SSA's exact factors.

Survivor benefit
:   What continues after a spouse dies. The survivor gets the larger of their own Social
    Security benefit and the widow(er)'s benefit on the spouse's record; a pension continues at its elected survivor share.
    The widow(er)'s benefit starts at 60 at the earliest and is reduced if taken before full
    retirement age: to 71.5% at 60.

## Taxes

Tax-location split
:   How the portfolio divides among taxable (brokerage), tax-deferred (traditional 401(k), TSP,
    IRA) and Roth accounts. With a split the model computes tax each year; without one it
    computes none.

Provisional income
:   The IRS measure that decides how much Social Security is taxable: other income plus
    tax-exempt interest plus half of the benefits. Below a base amount none of the benefit is
    taxed; above higher thresholds up to 85% is. The thresholds are not inflation-indexed, so
    more benefit becomes taxable over time.

MAGI
:   Modified adjusted gross income. Several rules key off it, IRMAA among them.

IRMAA
:   The income-related monthly adjustment amount: a Medicare Part B and D surcharge for people
    whose MAGI two years earlier was above a threshold.

RMD (required minimum distribution)
:   The minimum that must be withdrawn from tax-deferred accounts each year from 73 or 75,
    depending on birth year.

59½ penalty
:   The 10% additional tax on withdrawals from tax-deferred accounts before age 59½. The model
    applies and reports it; it does not model the exceptions (72(t) payments, the rule of 55).

Roth conversion
:   Moving money from a tax-deferred account to a Roth account, paying income tax now so that
    later withdrawals (and RMDs) are smaller. Often done in the low-income bridge years.

Bracket fill
:   Withdrawing or converting just enough each year to "fill" a tax bracket up to its top.

Payroll tax, FICA, self-employment tax
:   Social Security and Medicare tax on earned income. Employees pay 7.65% (their half);
    the self-employed pay both halves, 15.3%, and deduct half.

NIIT
:   The 3.8% net investment income tax above $200,000 (single) or $250,000 (joint) of MAGI.

ACA
:   The Affordable Care Act marketplace, where early retirees often buy health insurance until
    Medicare at 65. Enter premiums as required spending.
