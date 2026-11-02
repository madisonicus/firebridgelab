# Validation

How the model's numbers compare with published references. Every row below is
reproduced by a test in [`tests/validation/`](../tests/validation/), whose
docstring cites the source and states the tolerance and why. CI runs the suite
on every push (`pytest -m validation` runs it alone, in under a second).

Each reference value was read from the party that published it: the study's
own article, the regulation on eCFR, the IRS publication, the statute and
OPM's own page. Sources were read on 2026-11-02. www.ssa.gov blocks fetches
from the machine this was written on, so Social Security is checked against
the regulation (20 CFR 404) and SSA's Program Operations Manual (POMS, on
secure.ssa.gov) rather than SSA's public pages.

Where the model disagrees with a reference, the table says so and gives the
reason. Six disagreements were bugs and were fixed in 0.2.0 (marked
**fixed**); the rest are differences of data or of scope, listed with their
direction and size.

## Withdrawal-rate studies

[`test_withdrawal_studies.py`](../tests/validation/test_withdrawal_studies.py)

The model replays Robert Shiller's data: S&P Composite real total return and a
10-year Treasury built from the yield with duration 7, read January to
January, with withdrawals at mid-year. The studies used Ibbotson calendar-year
data and different bonds. So the comparison is made on the studies' own start
years, and the test checks the direction and size of the gap rather than
equality. Tolerances are counted in cohorts: one cohort is 2.4 points of
success rate out of 41 periods, and 1.8 points out of 55.

### Trinity, 1998

Cooley, Hubbard and Walz, "Retirement Savings: Choosing a Withdrawal Rate That
Is Sustainable", *AAII Journal* 20(2), February 1998, Table 3
("Inflation-Adjusted Portfolio Success Rates: 1926 to 1995", p. 19). S&P 500
and long-term high-grade corporate bonds, 41 overlapping 30-year periods
(start years 1926–1966).

30-year success rate, %: ours vs theirs (difference), same 41 start years.

| Stocks/bonds | 3% | 4% | 5% | 6% |
| --- | --- | --- | --- | --- |
| 100/0 | 100 vs 100 (0) | 95 vs 95 (0) | 83 vs 85 (−2) | 66 vs 68 (−2) |
| 75/25 | 100 vs 100 (0) | 95 vs 98 (−3) | 80 vs 83 (−3) | 56 vs 68 (−12) |
| 50/50 | 100 vs 100 (0) | 93 vs 95 (−2) | 66 vs 76 (−10) | 39 vs 51 (−12) |
| 25/75 | 100 vs 100 (0) | 61 vs 71 (−10) | 22 vs 27 (−5) | 5 vs 20 (−15) |
| 0/100 | 68 vs 80 (−12) | 15 vs 20 (−5) | 5 vs 17 (−12) | 0 vs 12 (−12) |

Tolerance: we may succeed in at most one cohort more than they do in any cell,
and at most 5 cohorts fewer (12 points) with 50% stocks or more, 7 fewer (17
points) for the bond-heavy mixes. The 4% column with 50% stocks or more must be
within 2 cohorts. All cells pass.

### Trinity, 2011 update

Cooley, Hubbard and Walz, "Portfolio Success Rates: Where to Draw the Line",
*Journal of Financial Planning* 24(4), April 2011, Table 2
([FPA](https://www.financialplanningassociation.org/article/journal/APR11-portfolio-success-rates-where-draw-line)).
Monthly S&P 500 and high-grade corporate returns, monthly rebalancing and
withdrawals, 55 overlapping 30-year periods (start years 1926–1980). The table
is an image in the article; the values were read from it.

| Stocks/bonds | 3% | 4% | 5% | 6% |
| --- | --- | --- | --- | --- |
| 100/0 | 100 vs 100 (0) | 95 vs 98 (−3) | 75 vs 80 (−5) | 60 vs 62 (−2) |
| 75/25 | 100 vs 100 (0) | 93 vs 100 (−7) | 73 vs 82 (−9) | 53 vs 60 (−7) |
| 50/50 | 100 vs 100 (0) | 89 vs 96 (−7) | 60 vs 67 (−7) | 36 vs 51 (−15) |
| 25/75 | 100 vs 100 (0) | 62 vs 80 (−18) | 27 vs 31 (−4) | 7 vs 22 (−15) |
| 0/100 | 76 vs 84 (−8) | 22 vs 35 (−13) | 7 vs 22 (−15) | 2 vs 11 (−9) |

Tolerance: at most one cohort more than theirs; at most 8 cohorts fewer (15
points) with 50% stocks or more and 11 fewer (20 points) for the bond-heavy
mixes; the 4% column with 50% stocks or more within 4 cohorts. All cells pass.

### Bengen, 1994

Bengen, "Determining Withdrawal Rates Using Historical Data", *Journal of
Financial Planning* 7(4), October 1994. Ibbotson large-company stocks and
intermediate-term Treasuries, start years 1926–1976, withdrawals at year end.

| Claim | Theirs | Ours | Tolerance | Reason for the gap |
| --- | --- | --- | --- | --- |
| 3%, 50/50: shortest portfolio life | never under 50 years | never under 50 years | exact | — |
| 4%, 50/50: worst start year | 1966 | 1966 | exact | — |
| 4%: worst 30-year safe rate, 50/50 | in [4.00%, 4.25%) | 3.58% | ≥ 3.50%, < 4.25% | bond series and data timing, see below |
| 4%: worst 30-year safe rate, 75/25 | in [4.00%, 4.25%) | 3.74% | ≥ 3.50%, < 4.25% | as above |
| 4%, 50/50: shortest portfolio life | 33 years (1966) | 24 years (1966) | 20 to 32 years | the safe-rate gap, magnified: near the boundary a small rate change moves the life by years |
| 6%, 50/50: starts that fail a 30-year client | 31 of 51 | 34 of 51 | ± 4 | his data ended in 1992, so his starts after 1962 had fewer than 30 actual years |

Bengen's worst-case bounds come from his text: at 4% "in no past case has it
caused a portfolio to be exhausted before 33 years", and "a 4.25-percent
first-year withdrawal could exhaust a portfolio in as little as 28 years".

**Where the gap comes from.** For the 1966 start at 50/50, the worst case:

| Change from the default replay | 1966 safe rate |
| --- | --- |
| Shiller stocks, duration-7 10-year Treasury proxy, mid-year withdrawals (the model) | 3.58% |
| the same, with year-end withdrawals (Bengen's timing) | 3.62% |
| FRED intermediate Treasuries (`it_treasury`) in place of the proxy | 3.76% |
| Damodaran calendar-year 10-year Treasury (`lt_treasury`) in place of the proxy | 3.64% |

The bond series accounts for about 0.2 point and the withdrawal timing for
0.04. The rest is the stock series: Shiller's monthly-average January prices
against Ibbotson's calendar-year returns. The model is more conservative than
all three studies in every cell; the gap grows with the bond share, because
long corporates (Trinity) and intermediate Treasuries (Bengen) held up better
than a 10-year Treasury proxy through 1966–1981.

## Social Security

[`test_social_security.py`](../tests/validation/test_social_security.py)

Claim-age factors for full retirement age 67 (born 1960 or later,
[20 CFR 404.409](https://www.ecfr.gov/current/title-20/part-404/section-404.409)).
Reduction 5/9 of 1% a month for 36 months and 5/12 of 1% after
([404.410(a)](https://www.ecfr.gov/current/title-20/part-404/section-404.410));
credits 2/3 of 1% a month to 70
([404.313(b)(2)](https://www.ecfr.gov/current/title-20/part-404/section-404.313)).

| Claim age | 62 | 63 | 64 | 65 | 66 | 67 | 68 | 69 | 70 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Regulation | 70% | 75% | 80% | 86.67% | 93.33% | 100% | 108% | 116% | 124% |
| Ours | 70% | 75% | 80% | 86.67% | 93.33% | 100% | 108% | 116% | 124% |

Tolerance 1e-12; all match.

Survivor benefit, for a survivor at or past full retirement age. The survivor
gets the larger of their own benefit and the widow(er)'s benefit
([404.407(a)](https://www.ecfr.gov/current/title-20/part-404/section-404.407)),
which is the decedent's PIA plus their delayed credits, or, if the decedent
claimed early, the larger of the decedent's reduced benefit and 82.5% of the
PIA ([404.338](https://www.ecfr.gov/current/title-20/part-404/section-404.338)).
Decedent's PIA $30,000, survivor's own PIA $10,000 unless stated.

| Case | Regulation | Ours | |
| --- | --- | --- | --- |
| Both claimed at 67 | $30,000 | $30,000 | |
| Decedent claimed at 70 | $37,200 | $37,200 | |
| Decedent claimed at 65 (86.67%) | $26,000 | $26,000 | |
| Decedent claimed at 64 (80%) | $24,750 (82.5% floor) | $24,750 | **fixed**: was $24,000 |
| Decedent claimed at 62 (70%) | $24,750 (82.5% floor) | $24,750 | **fixed**: was $21,000 |
| Survivor's own benefit larger ($30,000 vs $10,000) | $30,000 | $30,000 | |
| Decedent planned to claim at 70, died at 68 | $32,400 from 68 | $32,400 from 68 | **fixed**: was the survivor's own $10,000 until 70, then $37,200 |
| Decedent planned to claim at 70, died at 67 | $30,000 from 67 | $30,000 from 67 | **fixed**, as above |

Tolerance $0.000001; all match.

Survivor below full retirement age (**fixed**). A widow(er)'s benefit based on
age is payable from 60
([404.335](https://www.ecfr.gov/current/title-20/part-404/section-404.335))
and, taken before full retirement age, is reduced by PIA × 0.285 × (months
before FRA) / (months from 60 to the month before FRA)
([404.410(c)(1)](https://www.ecfr.gov/current/title-20/part-404/section-404.410));
with FRA 67 the period is 84 months. That is 0.285/84 of the PIA a month, not
a fixed 19/40 of 1% (which is the same thing only for FRA 65, a 60-month
period). The reduction is permanent; the model takes the benefit as soon as it
is payable. Decedent's PIA $30,000, claimed at 67; survivor the same age, own
PIA $10,000 claimed at 67:

| Survivor's age at the death | Regulation | Ours | Before the fix |
| --- | --- | --- | --- |
| 58 | nothing at 58–59; $21,450 (71.5%) from 60 | the same | $30,000 from 58 |
| 60 | $21,450 (71.5%) | $21,450 | $30,000 |
| 62 | $23,892.86 (79.64%) | $23,892.86 | $30,000 |
| 64 | $26,335.71 (87.79%) | $26,335.71 | $30,000 |
| 67 (FRA) | $30,000 | $30,000 | $30,000 |
| 62, own PIA $36,000 claimed at 62 ($25,200) | $25,200, the own benefit (404.407(a)) | $25,200 | $30,000 |
| 60, decedent claimed at 62 ($21,000) | $21,450: the reduced 71.5% is under the 82.5% limit | $21,450 | $24,750 |
| 66, decedent claimed at 62 | $24,750: the reduced 95.93% is capped at 82.5% (404.338(c), applied after the age reduction per [POMS RS 00615.320](https://secure.ssa.gov/poms.nsf/lnx/0300615320)) | $24,750 | $24,750 |

Tolerance $0.000001; all match.

Simplified, and not matched:

- **The order of the age reduction and delayed credits.** 404.410(c)(1)
  reduces the PIA, so the model leaves the decedent's delayed credits
  unreduced. Neither the regulation nor the POMS section above states the order
  for that case explicitly.
- **When to take the survivor benefit.** The model takes it as soon as it is
  payable. Waiting (for example to full retirement age, for the unreduced
  benefit) is not an option it models.
- **Spousal benefits** (404.333) and the **earnings test** are not modelled, so
  a low earner married to a high earner is understated while both are alive.
- Months: the model works in whole years of age.

## Federal tax

[`test_irs_examples.py`](../tests/validation/test_irs_examples.py)

| Reference | Source | Ours | Theirs | Difference | Tolerance |
| --- | --- | --- | --- | --- | --- |
| 2026 tax rate schedules, joint and single, at 16 incomes from $5,000 to $1,000,000 including every bracket edge | [Rev. Proc. 2025-32](https://www.irs.gov/pub/irs-drop/rp-25-32.pdf) sec. 4.01, Tables 1 and 3 | e.g. $35,932.00 at $211,400 joint | the schedule's base amount plus rate on the excess | 0 | $0.005 |
| Standard deduction and aged amount | Rev. Proc. 2025-32 sec. 4.14 | $32,200 / $16,100; +$1,650 / +$2,050 | same | 0 | exact |
| Taxable Social Security, Pub. 915 Example 1 (single) | [Pub. 915 (2025)](https://www.irs.gov/publications/p915), Worksheet 1 | $2,990 | $2,990 | 0 | $1 |
| Pub. 915 Example 2 (joint, IRA deduction) | same | $0 | $0 | 0 | $1 |
| Pub. 915 Example 3 (joint, line 8 over $44,000) | same | $6,275 | $6,275 | 0 | $1 |
| Gains stacked on ordinary income, 12 cases × 2 filing statuses | [Form 1040 instructions (2025)](https://www.irs.gov/pub/irs-pdf/i1040gi.pdf), Qualified Dividends and Capital Gain Tax Worksheet, with the 2026 amounts of Rev. Proc. 2025-32 sec. 4.03 | equal to line 25 in every case, e.g. $11,492.00 at $98,900 ordinary + $1,000 gains, joint | the worksheet's line 25 | 0 (**fixed**, see below) | $0.005 |
| Net investment income tax, 5 cases | [Form 8960 instructions (2025)](https://www.irs.gov/pub/irs-pdf/i8960.pdf), "Application to Individuals" | e.g. $1,900 on MAGI $300,000 joint with $80,000 of gains | 3.8% × min(NII, MAGI − $250,000) | 0 | $0.005 |
| Uniform Lifetime Table, ages 75–120 | [Treas. Reg. 1.401(a)(9)-9(c)](https://www.ecfr.gov/current/title-26/part-1/section-1.401(a)(9)-9) | every divisor | — | 0 | exact |
| RMD worked example: $100,000, turns 75 in 2026 | [Pub. 590-B (2025)](https://www.irs.gov/pub/irs-pdf/p590b.pdf), ch. 1 | $4,065.04 | $4,065 | 0 | $1 |
| 10% additional tax on an early distribution (Tom Jones, 35, $3,000) | Pub. 590-B (2025), ch. 1 | $300 on the $3,000, plus 10% of the draw that pays its tax | $300 | 0 | $0.01 |

**Fixed: the capital-gain worksheet's line 25.** The IRS takes the smaller of
the stacked computation and the regular tax on all taxable income. The model
skipped that comparison. It matters only where the 15% gains band starts below
the top of the 12% bracket: taxable income from $98,900 to $100,800 joint
($49,450 to $50,400 single). There the model overstated tax by up to $57 joint
($28.50 single). With $98,900 of ordinary taxable income and $1,000 of gains,
joint, it charged $11,522.00 against the worksheet's $11,492.00.

Simplified, and not matched:

- Below $100,000 of taxable income the IRS Tax Table rounds to $50 bands; the
  model uses the exact schedule (the Tax Computation Worksheet), so it can
  differ from a return by a few dollars.
- Net investment income in the model is the year's long-term gains. Interest
  and dividends in the taxable account are not taxed at all.
- Married filing separately and head of household are not modelled (Pub. 915
  Example 4 is a separate return and is left out).
- The model treats ages 59 and below as before 59½, so a person who turns 59½
  during the year pays the 10% on the whole year's draws.

## FERS

[`test_fers.py`](../tests/validation/test_fers.py)

[5 U.S.C. 8462](https://www.law.cornell.edu/uscode/text/5/8462)(b)(1) and
[OPM](https://www.opm.gov/frequently-asked-questions/retire-faq/post-retirement/how-is-the-cost-of-living-adjustment-cola-determined/):
CPI up to 2%, 2% for CPI of 2–3%, CPI − 1% above 3%.

| CPI | 1.5% | 2.0% | 2.5% | 3.0% | 4.0% |
| --- | --- | --- | --- | --- | --- |
| Statute | 1.5% | 2.0% | 2.0% | 2.0% | 3.0% |
| Ours (`fers_diet`) | 1.5% | 2.0% | 2.0% | 2.0% | 3.0% |

No COLA before 62 (8462(c)(3)(A); OPM: COLAs "are not provided until age
62"). Checked against a hand-built schedule (the nominal check fixed until 62,
then the 8462 COLA each year, deflated at CPI) at every age, at each of the
five CPI rates:

| Case | Ours vs the schedule | Tolerance |
| --- | --- | --- |
| MRA+10 retiree at 57, one `fers_diet` stream of $22,500 from 57 (the bundled `mra10-single` plan) | equal at every age 57–95 | $0.000001 |
| The same stream, owned by a person three years younger than person A (the rule reads the owner's own 62) | equal at every age | $0.000001 |
| The same annuity as the old workaround, a `cola = "none"` stream to 62 and `nominal_until_start` + `fers_diet` from 62 | equal at every age | $0.000000001 |
| Deferred annuity: separated at 50, $20,000 payable from 62, one stream with `nominal_until_start` + `fers_diet` | equal at every age 50–95 | $0.000001 |
| Special-provision or disability retiree at 57, `fers_cola_before_62 = true` (schedule with the COLA from 57) | equal at every age 57–95 | $0.000001 |

**Fixed: no COLA before 62.** The statute's rule is 8462(c)(3)(A): an
increase is not effective for an annuitant under 62. It covers annuitants under
8412, 8413 and 8414; it does not cover special-provision law enforcement
officers, firefighters and air traffic controllers (8412(d)(1), (e)) or
disability retirees (8462(c)(3)(B)), and survivor annuities are outside it.
Before the fix, a single `fers_diet` stream got the COLA from its start age,
which overstated a regular retiree's annuity by 1.02⁵ − 1 = 10.4% from 62 for a
$22,500 annuity from 57 at 2.5% CPI ($21,956.55 at 62 against the statute's
$19,886.72). The exempt annuitants set `fers_cola_before_62 = true`.

The proration of the first COLA (8462(c)(1)) is a within-year detail the
yearly model does not carry.

## Closed form

[`test_closed_form.py`](../tests/validation/test_closed_form.py)

On a constant real return with no income, the engine's yearly step is
B<sub>k</sub> = B<sub>k−1</sub>(1 + r) − s(1 + r/2): growth, then a mid-year
withdrawal.

| Identity | Ours vs the formula | Tolerance |
| --- | --- | --- |
| Final balance = P(1+r)ⁿ − s(1 + r/2)((1+r)ⁿ − 1)/r, r = 0–5% | equal | relative 1e-9 |
| The constant real withdrawal with present value P, s* = P r (1+r)ⁿ / ((1 + r/2)((1+r)ⁿ − 1)), runs the portfolio to zero, and the solver reports it rounded down to $1,000 | equal | $0.000001; exact |
| Amortization rule: each year's spend is the level mid-year withdrawal that runs the current balance B to zero over the remaining m years, B r / ((1 + r/2)(1 − (1+r)⁻ᵐ)); on a flat return equal to the planning rate (0%, 1%, 3%) that is s* every year and the portfolio ends at zero | equal, every year; final balance 0 | relative 1e-9; $0.000001 |

**Fixed: the amortization rule's timing.** The rule planned with
start-of-year payments (annuity due) while the engine withdraws at mid-year,
so each withdrawal cost (1 + r/2)/(1 + r) of what the rule assumed. It now
plans with the engine's timing, for guaranteed income and required spending
as well as the spend. $1,000,000 over 30 years on a flat 3% return equal to
the planning rate:

| | First-year spend | Last-year spend | Left at the end |
| --- | --- | --- | --- |
| Before the fix | $49,533.26 | $52,868.68 (+6.7%) | $793.03 |
| After | $50,265.28 | $50,265.28 | $0.00 |

When guaranteed income exceeds the spend in a later year, the rule does not
borrow against the surplus (a gap is never negative), so a plan in which income
eventually covers the spend ends with that later surplus in the portfolio.

## Owl cross-check

[`test_owl_crosscheck.py`](../tests/validation/test_owl_crosscheck.py), with
[notebook 03](../notebooks/03-owl-cross-check.ipynb) as the evidence.

The default fictional household (60/40 stocks/10-year Treasuries, 25/60/15
taxable/tax-deferred/Roth, indexed pensions, flat spending, no bequest)
through [Owl](https://github.com/mdlacasse/Owl) 2026.10.28 and this library
for every start year from 1928 to 1980, the last with a full 46-year window:
the highest flat after-tax spend that lasts to 95. Run on 2026-11-02.

| Measure | Owl | Ours | Difference |
| --- | --- | --- | --- |
| Worst cohort | $91,845 (1973) | $90,991 (1973) | −0.9% |
| Median cohort | $115,803 | $117,499 | +1.5% |
| Per cohort | | | −3.7% to +6.3%, median +1.6% |
| Rank correlation across cohorts | | | 0.99 |
| Four worst starts | 1973, 1969, 1968, 1966 | 1973, 1969, 1966, 1968 | |
| Ours on Owl's return table, per cohort | | | +1.0% to +4.3%, median +2.5% |

The test re-derives our side for three cohorts and pins it: 1929 $109,131 (Owl
$104,502, +4.4%), 1966 $93,658 (Owl $93,237, +0.5%) and 1973 $90,991 (Owl
$91,845, −0.9%). Tolerance: $20 on the reference Shiller file (the bisection
stops within $10), 0.5% on any later revision of it; and the gap to Owl must
stay inside the −3.7% to +6.3% range above.

Run on the same returns, this library finds slightly more spendable money than
Owl in every cohort: it does not tax interest and dividends in the taxable
account, which Owl does, and the tools differ in within-year timing; Owl also
optimizes the withdrawal order, which pushes the other way. With each tool on
its own data, most of the scatter comes from the return series.

## Comparisons we could not make

| Comparison | Why |
| --- | --- |
| SSA's own pages (the benefit-reduction table, survivor planner) | www.ssa.gov blocks fetches from the machine this was written on; the regulation on eCFR and the POMS were used instead |
| Bengen's Figures 2–4 (portfolio life by allocation and rate) | the values exist only as bar charts; only figures stated in his text are compared |
| Bengen's appendix (full assumptions) | the available scan of the appendix is unreadable |
| Trinity 1998 withdrawal timing | the article does not state it |
| A numeric NIIT worked example | the Form 8960 instructions state the rule but give no individual example with numbers; the test applies the stated rule |
| OPM CSRS/FERS Handbook, COLA chapter | not used; the statute and OPM's own FAQ state the rule the tests check |
| Pub. 915 Example 4, and any married-filing-separately or head-of-household case | the model has no such filing status |
| Spousal benefits, the earnings test | not modelled (see Social Security above) |
| State income tax | not attempted in this release; each state's rules are cited in [methodology](methodology.md) and pinned by the unit tests, but no state's own worked example is reproduced |
