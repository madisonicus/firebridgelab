# Methodology

How firebridgelab computes each part of a projection, where every rule and
number comes from, and what is left out. Every value is taken from the body
that sets it (the IRS, SSA, CMS, OPM, or a state's revenue department) and
the yearly tax tables live in `firebridgelab/tax/data/*.toml` with a `source`
on every table (`tests/test_tax_m4.py` fails if a table has none). Pages were
read on 2026-11-01.

All dollars are real: plan-start purchasing power (ADR 0004). The 2026 tax
tables are treated as plan-start values. The law indexes most of them to
inflation, so they stay constant in real terms. The few that the law fixes in
nominal dollars shrink every year at `assumptions.inflation`
(`firebridgelab/tax/thresholds.py`).

## The yearly step

Each year the starting balance grows by that year's real return, net of
`annual_fee`. Spending and guaranteed income are mid-year flows (each
`× (1 + r/2)`). With a tax-location split (taxable, tax-deferred and Roth
balances), tax is a year-end bill paid from the accounts. It is solved as a
fixed point because the draws that pay it are themselves taxed
(`firebridgelab/tax/location.py`). Without a split the engine computes no
tax.

## Federal income tax

- Brackets, standard deduction, the 65+ additional deduction and the
  long-term capital-gain breakpoints for 2026: IRS
  [Rev. Proc. 2025-32](https://www.irs.gov/pub/irs-drop/rp-25-32.pdf),
  section 4 (IRC 1(j), 63(c), 63(f)). They are indexed, so they are held
  constant in real terms.
- Filing status is married filing jointly for a couple and single for one
  person or after the first death (the survivor year). This is conservative by
  the one year a surviving spouse may still file jointly.
- Long-term gains stack on top of ordinary taxable income through the 0/15/20%
  breakpoints. A flat `taxable_gains_fraction` of each taxable-account
  withdrawal is gain; the model does not track cost basis.
- Net investment income tax: 3.8% of the smaller of the year's gains and MAGI
  over $250,000 joint or $200,000 single (IRC 1411). The thresholds are not
  indexed, so they shrink in real terms.
- The temporary 2025–2028 senior deduction is not modelled.

## Social Security taxation

`tax.ss_taxation = "provisional"` (the default) applies IRC 86 as worked in
[IRS Publication 915](https://www.irs.gov/publications/p915), Worksheet 1.
Provisional income is the year's other AGI (ordinary income including account
draws, conversions and the RMD, plus long-term gains) plus half the benefits.

- Below the base amount ($25,000 single, $32,000 joint) none of the benefits
  are taxable.
- Up to the adjusted base amount ($34,000 / $44,000), up to 50% is taxable.
- Above it, up to 85% is taxable.

These amounts are fixed in law and shrink in real terms. Because more
withdrawal income makes more of the benefits taxable, the tax on a marginal
dollar can reach 1.85 times the bracket rate. The fixed point resolves this,
and the bracket-fill lever and the Roth conversion fill both account for it
(`tax/ss_taxation.py: fill_amount`). `"flat85"` taxes 85% of every benefit
whatever the income, which was the behaviour before this option. CHANGELOG.md
explains why provisional is the default.

## Payroll tax

`tax.payroll = true` charges payroll tax on `work` streams
(`tax/payroll.py`):

- **Employee FICA** (IRC 3101; [IRS Topic 751](https://www.irs.gov/taxtopics/tc751)):
  6.2% Social Security up to the 2026 wage base of $184,500, and 1.45%
  Medicare. The wage base comes from SSA's
  [Cost-of-Living Increase and Other Determinations for 2026](https://www.federalregister.gov/documents/2025/11/03/2025-19763/cost-of-living-increase-and-other-determinations-for-2026)
  (Federal Register doc. 2025-19763). It is indexed to average wages and held
  constant in real terms. Only the employee's share is charged.
- **Self-employment tax** for streams with `self_employed = true` (IRC 1401,
  1402; [IRS Topic 554](https://www.irs.gov/taxtopics/tc554)): 12.4% + 2.9% on
  92.35% of the income. Nothing is due under $400 of net earnings. The Social
  Security part stops at the wage base less the person's wages. Half the tax
  is deducted from income (IRC 164(f)).
- **Additional Medicare tax** (IRC 3101(b)(2), 1401(b)(2);
  [IRS Topic 560](https://www.irs.gov/taxtopics/tc560)): 0.9% over $250,000
  joint or $200,000 single, combined per Form 8959. The threshold is not
  indexed, so it shrinks in real terms.

## Medicare IRMAA

`tax.irmaa = true` adds the Part B and Part D income-related monthly
adjustments for every living person aged 65 or more. They are required
spending: the spending rule cannot cut them (`tax/irmaa.py`).

- The 2026 tiers and amounts come from the CMS fact sheet
  [2026 Medicare Parts A & B Premiums and Deductibles](https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles).
- The tier is set by MAGI from two years earlier
  ([20 CFR 418.1135(a)](https://www.ecfr.gov/current/title-20/part-418/section-418.1135))
  and that year's filing status. MAGI is the model's own AGI for that year
  along the same path.
- The first two plan years use `tax.irmaa_prior_magi`. If it is not given,
  there is no surcharge in those years.
- The ranges are CPI-indexed
  ([20 CFR 418.1115(e)](https://www.ecfr.gov/current/title-20/part-418/section-418.1115)),
  except the $500,000 / $750,000 bounds, which stay frozen in nominal dollars
  through 2027 and so lose one year of inflation in the model.
- The standard Part B premium is ordinary spending and is not added.

## Roth conversions

`[tax.roth_conversion]` converts tax-deferred money to Roth each year in an
age window (`tax/roth.py`).

- The conversion fills ordinary taxable income up to the top of a named
  bracket or a dollar target.
- It is ordinary income that year (IRC 408A(d)(3)) and carries no 10% penalty.
  Tax-funding draws it forces from tax-deferred before 59½ do carry the
  penalty.
- In RMD years the RMD comes first, because an RMD cannot be converted.
- `run` and the API report lifetime tax and the terminal after-tax value, with
  and without the policy. The terminal value is taxable + Roth + tax-deferred
  × (1 − heir rate). Taxable assets get a stepped-up basis at death (IRC 1014).
  A tax-deferred balance is income in respect of a decedent (IRC 691), taxed at
  `heir_tax_rate`, an assumption the user sets.

The bracket-fill lever is different: it moves money to the taxable account,
not to Roth, at ages 60–74 (`portfolio.bracket_fill_target`).

## State income tax

`[tax.state]` adds one state of residence, optionally changing at an age (a
move). The state bill is paid from the accounts inside the same fixed point as
the federal bill (`tax/state.py`, data in `tax/data/states_2026.toml`).

Covered, each from the state's own revenue department or legislature (the data
file names every page):

| State | 2026 rate | Social Security | Pensions | IRA / 401(k) withdrawals | Deductions |
| --- | --- | --- | --- | --- | --- |
| AK, FL, NV, NH, SD, TN, TX, WY | none | — | — | — | — |
| Georgia | 4.99% | exempt | per-person exclusion $35,000 at 62–64, $65,000 at 65+, also covering interest, dividends, rent and gains | same exclusion | $15,000 / $30,000 |
| Illinois | 4.95% | exempt | exempt | exempt | $2,925 per person, $1,000 per person 65+, lost above AGI $250,000 / $500,000 |
| Indiana | 2.95% | exempt | taxable | taxable | $1,000 per person, $1,000 per person 65+, $500 more each if AGI < $40,000 |
| Iowa | 3.8% | exempt | exempt from 55 | exempt from 55 | starts from federal taxable income |
| Louisiana | 3% | exempt | $12,324 per person from 65 | same exclusion | $12,838 / $25,676 |
| Mississippi | 4% above the first $10,000 | exempt | exempt | exempt from 60 (early distributions taxable) | $8,300 / $16,600, $1,500 per person 65+ |
| North Carolina | 3.99% | exempt | taxable | taxable | $12,750 / $25,500 |
| Pennsylvania | 3.07% | exempt | exempt | exempt from 60 (59½) | none |

Georgia's exclusion amounts come from its 2025 booklet; everything else is the
2026 figure. `custom` taxes federal AGI at a flat `rate`, with toggles that
exempt Social Security, pensions and IRA withdrawals.

Not covered: every graduated-bracket state, and these others:

- **Arizona**: its 2026 standard deduction and 65+ exemption are not yet
  published.
- **Colorado**: the 2026 rate depends on a TABOR trigger.
- **Idaho**: the 2026 rate and retirement deduction are not yet published.
- **Kentucky**: the joint-return treatment of its deduction could not be read
  from the state.
- **Michigan, Ohio, Utah**: credits and phase-ins beyond a flat-rate model.
- **Massachusetts**: a 4% surtax and pension rules by plan type.
- **Washington**: no wage tax, but a capital-gains tax whose 2026 deduction is
  unpublished, and a new 2026 tax not yet verified.

Simplifications:

- Accounts are household-wide. An age-gated exemption of IRA withdrawals or
  pensions therefore applies to the share of living people who meet the age.
- State deductions are held constant in real terms.
- These are not modelled: local and county income taxes (Indiana counties,
  Pennsylvania local earned-income taxes), state credits, the special
  treatment of particular public pensions (Bailey in North Carolina, fully
  exempt Louisiana and federal systems), and Mississippi's per-spouse
  computation of its zero-rate band.

## Social Security benefits

The FRA benefit and a claim age from 62 to 70 set the benefit. Early claiming
is reduced by 5/9 of 1% a month for the first 36 months and 5/12 of 1% after
that ([20 CFR 404.410](https://www.ecfr.gov/current/title-20/part-404/section-404.410)).
Delayed credits add 2/3 of 1% a month up to 70
([20 CFR 404.313](https://www.ecfr.gov/current/title-20/part-404/section-404.313)).
FRA is 67 (born 1960 or later). Benefits are constant in real terms
(COLA-indexed). Earnings-test withholding before FRA is not modelled.

## Pensions, FERS COLA and deferral

A pension is a real amount from its start age, or with `nominal_until_start`
the nominal check at commencement deflated to plan-start dollars. This is how a
FERS deferred annuity behaves: fixed from separation until it starts (5 U.S.C.
8413).

COLA options:

- `full` keeps the real value.
- `none` loses inflation every year.
- `fixed` adds a stated nominal rate.
- `fers_diet` applies [5 U.S.C. 8462](https://uscode.house.gov/us/usc/119-124/t5/s8462/)
  ([OPM summary](https://www.opm.gov/frequently-asked-questions/retire-faq/post-retirement/how-is-the-cost-of-living-adjustment-cola-determined/)):
  CPI up to 2%, 2% for CPI of 2–3%, CPI − 1% above 3%, evaluated at
  `assumptions.inflation` (`income/pension.py`).

## Survivor case

When the first person dies (`[survivor]`):

- The survivor keeps the larger of the two Social Security benefits in payment
  (SSA widow(er)'s benefit and dual-entitlement rules; the survivor's own
  early-claim limit is ignored).
- Each of the decedent's other streams continues at its `survivor_fraction`.
  A FERS full survivor annuity is 50% (5 U.S.C. 8442).
- Spending and the guardrail rails scale by `spending_fraction`.
- Taxes switch to single filing from the death year.

## Required minimum distributions and penalties

RMDs start at 75 for people born 1960 or later (IRC 401(a)(9)(C)(v), as
amended by SECURE 2.0 section 107). The divisor is the Uniform Lifetime Table
(Treas. Reg. 1.401(a)(9)-9(c);
[IRS Pub. 590-B](https://www.irs.gov/publications/p590b), Appendix B,
Table III), applied to the prior year-end tax-deferred balance. Any shortfall
of the year's actual draws is moved to taxable and taxed. Ages and RMDs follow
person A.

A tax-deferred draw before 59½ (modelled as age 59 or younger) pays the
additional 10% tax of IRC 72(t)
([IRS: tax on early distributions](https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-tax-on-early-distributions)).
Penalty-free 72(t)/SEPP access, the rule of 55 and other exceptions are not
modelled.

## Returns data

Historical mode replays every Shiller cohort since 1871 (US stocks and a
10-year Treasury proxy with duration 7). Other classes come from Damodaran,
Fama-French, FRED and optionally Nareit and testfol.io. Sources, terms and
citations are in [DATA-SOURCES.md](../DATA-SOURCES.md).

Shiller prices are monthly averages, which mute crash years. Overlapping
cohorts are counts, not probabilities: about three independent 46-year
histories exist.

## Monte Carlo

Each trial draws yearly real returns per class from a Normal distribution, or
a multivariate Student-t rescaled to the same mean and volatility. Classes are
correlated through a Cholesky factor of the nearest positive-definite
correlation matrix (`returns/sampling.py`). Simple returns are floored at −99%.

There is no autocorrelation, valuation conditioning or parameter uncertainty.
A fixed seed makes runs reproducible, and success rates carry about ±0.5 point
of sampling noise at 10,000 trials.

## Spending rules

- **Guardrails** (`spending.rule = "guardrails"`) follow Guyton, J. T. and
  Klinger, W. J. (2006), "Decision Rules and Maximum Initial Withdrawal Rates,"
  *Journal of Financial Planning* 19(3). When the portfolio-funded withdrawal
  rate drifts more than `guard_band` from its starting value, spending is cut
  or raised by `guard_adjust`, within the floor and ceiling. The model runs in
  real dollars, so G&K's nominal inflation rule is not modelled
  (`spending/rules/guardrails.py`).
- **Funded-ratio rules** (`fr_smooth`, `fr_guardrail`, `fr_tiers`) compare the
  portfolio with the present value of the remaining gap between spending and
  guaranteed income, discounted at 1% real.
- **Amortization** (`amortization`) solves each year for the constant real
  spend that draws the portfolio to zero at the plan's end, net of future
  guaranteed income and discounted at `amortization_real_return`. This is
  annuity (PMT) arithmetic, the same principle as the Bogleheads
  [variable percentage withdrawal](https://www.bogleheads.org/wiki/Variable_percentage_withdrawal).
  A test pins it to the closed-form annuity.

## What is not modelled

- Tax with no tax-location split. The pre-tax engine charges none.
- Graduated-bracket states and the flat-rate states listed above as not
  covered, local and county income taxes, state credits, and foreign tax.
- The temporary 2025–2028 senior deduction, itemized deductions, and tax
  credits.
- Tracked cost basis. A flat gains fraction is used, and taxable-account
  dividends and interest are not taxed.
- ACA premium credits. Enter premiums before 65 as required spending.
- The standard Medicare premium. It is ordinary spending; only IRMAA is
  added.
- The Social Security earnings test, spousal benefits, and the survivor's own
  early-claim limit.
- The FERS rule that regular retirees get no COLA before 62. `fers_diet`
  applies from the start age.
- Roth five-year rules, 72(t)/SEPP, the rule of 55, and other exceptions to
  the 10% penalty.
- Accounts per person (access ages and RMDs follow person A), and per-person
  ownership of IRA withdrawals for state rules.
- Mortality-weighted horizons, long-term-care shocks as explicit lines, and
  regime-switching or valuation-conditioned returns.
