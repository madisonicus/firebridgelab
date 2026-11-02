# Plan files

A plan file is a small [TOML](https://toml.io) file that describes one
retirement plan: who is in the household, their income, the portfolio, how it
is invested, how it is spent, and how to simulate it. It is plain text, so it
can be kept in git and diffed like any other file.

```bash
firebridgelab init plan.toml                    # a commented starter plan
firebridgelab init --list                       # the bundled examples
firebridgelab init --example leanfire-single lean.toml
firebridgelab run plan.toml                     # check it
```

From Python, `firebridgelab.plan.load(path)` returns the `Scenario` a file
describes and `firebridgelab.plan.read(path)` returns a `Plan` (the scenario
plus `name`, `description`, `trials`, `seed` and `stress`).
`firebridgelab.plan.dump(scenario_or_plan)` writes plan-file text back out
with every value explicit; reading that text gives back an equal scenario.
A scenario built on the library's legacy `person_*` fields is written with
its household spelled out, and its Social Security comes back as an FRA amount
rounded to the cent.

## Conventions

- **Dollars are real**: today's purchasing power, not future nominal dollars.
  The one exception is a stream with `nominal_until_start = true`.
- **Ages are each person's own age.** A stream, cash event or spending
  adjustment owned by person `b` uses `b`'s age. Results are reported on
  person `a`'s age.
- **Fractions, not percents**: `0.04` is 4%, and allocation weights are
  `0.6`, not `60`.
- **Unknown keys are errors.** A misspelt key is reported with the closest
  known key (`unknown key 'spending.anual'; did you mean 'annual'?`); a
  library field name such as `annual_spending` is pointed at its plan-file
  key (`spending.annual`). A value of the wrong type or outside its choices
  names the key and what was expected. The finished plan then goes through
  the library's `validate_scenario`.
- **Required**: at least one `[[people]]`, `portfolio.balance` (or a tax
  split), and `spending.annual`. Everything else has a default, listed below.

## Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | file name | A label for the plan in output and reports. |
| `description` | string | `""` | One line about the plan, shown in reports. |

## `[[people]]`

One or two tables. The first is person `a`, the second person `b`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | `"a"` or `"b"` | by position | Optional; must match the position. |
| `name` | string | `""` | A display name. |
| `start_age` | integer | required | Age at plan start (normally the first year of retirement). |
| `end_age` | integer | required | Planning horizon: the last age planned for. A horizon, not a death (see `[survivor]`). |

The plan runs from person `a`'s `start_age` to the later of the two people's
end ages, converted to `a`'s age.

## `[[income_streams]]`

One table per stream of guaranteed income. Zero or more.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | required | Unique name; used to address the stream in `--set` and `sweep`. |
| `kind` | string | required | `pension`, `social_security`, `annuity`, `work`, `rental` or `other`. |
| `amount` | number | required | Annual amount in real dollars. For `social_security`, the benefit at full retirement age (67); the claim-age adjustment is applied for you. With `nominal_until_start`, the nominal check at commencement. |
| `start_age` | integer | required | Owner's age at the first payment. For `social_security`, the claim age (62–70). |
| `owner` | string | `"a"` | `a`, `b`, or `household` (household streams use person `a`'s age). |
| `end_age` | integer | for life | Owner's age at which payments stop (exclusive: paid through `end_age - 1`). Not allowed for Social Security. |
| `cola` | string | `"full"` | `full` keeps real value constant; `none` is fixed in nominal dollars and loses `assumptions.inflation` a year; `fixed` adds `cola_rate` a year in nominal terms; `fers_diet` applies the FERS rule (CPI up to 2%, 2% for CPI of 2–3%, CPI − 1% above 3%). Social Security must be `full`. |
| `cola_rate` | number | `0` | The nominal yearly COLA for `cola = "fixed"`. |
| `nominal_until_start` | boolean | `false` | `amount` is the nominal check at `start_age`, eroded by inflation from plan start (a deferred FERS annuity, a frozen pension). |
| `survivor_fraction` | number 0–1 | by kind | Share that continues after the owner dies (survivor case). Default 0 for a person's pension, annuity, work and other income, 1 for rental and household-owned streams. Social Security follows the survivor rule instead. |
| `tax_treatment` | string | by kind | `ordinary` (default), `tax_free`, or `social_security` (85% taxable; Social Security only, and its default). |
| `label` | string | `""` | A display name for reports. |

## `[[cash_events]]`

One-off flows in a single year: an inheritance, a home sale, a car.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `age` | integer | required | Owner's age in the year of the flow. |
| `amount` | number | required | Positive is money in, negative money out. Real dollars. |
| `tax_treatment` | string | `"tax_free"` | `tax_free`, `ordinary` or `capital_gain`. Outflows must be `tax_free`. Taxed only with a tax split. |
| `basis_fraction` | number 0–1 | `0` | For `capital_gain`: the share of `amount` that is basis (not taxed). |
| `owner` | string | `"a"` | `a`, `b` or `household`. |
| `id` | string | `""` | A name, for `--set` and reports. |
| `label` | string | `""` | A display name. |

## `[[spending_adjustments]]`

Required spending on top of the spending rule, over an age range: health
insurance before Medicare, a mortgage, college. The spending rule cannot cut
it; floors and ceilings apply only to the rule's part.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `amount` | number | required | Extra real dollars a year. |
| `start_age` | integer | required | Owner's age in the first year. |
| `end_age` | integer | required | Owner's age at which it stops (exclusive). |
| `owner` | string | `"a"` | `a`, `b` or `household`. |
| `survivor_fraction` | number 0–1 | `1` | Share that continues after the owner dies. |
| `id` | string | `""` | A name, for `--set` and reports. |
| `label` | string | `""` | A display name. |

## `[portfolio]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `balance` | number | sum of the split | Invested assets at plan start. Required unless a tax split is given. |
| `taxable` | number | none | Taxable-account balance. Giving any of the three turns on the tax model; missing ones count as 0, and the three must add up to `balance`. |
| `tax_deferred` | number | none | Traditional 401(k)/TSP/IRA balance. |
| `roth` | number | none | Roth balance. |
| `taxable_gains_fraction` | number 0–1 | `0.5` | Share of each taxable-account withdrawal taxed as a long-term gain (stands in for cost basis). |
| `bracket_fill_target` | number | off | From 60 to 74, move money from tax-deferred to taxable each year until ordinary taxable income reaches this many dollars (shrinks later RMDs). Needs a tax split. |
| `annual_fee` | number 0–0.03 | `0` | All-in yearly cost as a fraction of assets (expense ratios plus advice). |

## `[allocation]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | string | `"constant"` | `constant` holds `weights` throughout; `glidepath` uses `phases`. |
| `sourcing` | string | `"single"` | `single` keeps one balance; `buckets` splits it into cash, income and growth buckets (see `[buckets]`). |
| `weights` | table | `{ us_total_market = 0.6, us_agg_bonds = 0.4 }` | Class id = weight; weights sum to 1. Under `glidepath`, defaults to the first phase. |
| `phases` | list of tables | none | `glidepath` only: one weights table per phase, in order. Phases start at the plan start, at 60, and at every income stream's start and end age (on person `a`'s age) inside the plan; `firebridgelab run` prints the derived start ages. Extra phases are ignored; missing ones repeat the last. |
| `real_return` | table | per class | Class id = assumed yearly real return, overriding the default for that class in deterministic and Monte Carlo runs (historical replay uses the data). |
| `volatility` | table | per class | Class id = yearly standard deviation of real return (Monte Carlo). |
| `correlation` | number | `0.2` | The stock–bond real-return correlation used by Monte Carlo when the plan has at most two classes. |
| `correlations` | list of lists | estimated | An N×N correlation matrix in `weights` order, for more than two classes. Omitted, it is estimated from the data. |

**Class ids** and their defaults:

| Id | Class | Default real return |
| --- | --- | --- |
| `us_total_market` | US total stock market (Shiller, 1871+) | 7% |
| `us_agg_bonds` | US bonds, 10-year Treasury proxy (Shiller, 1871+) | 2% |
| `us_small_value` | US small-cap value (Fama-French) | data mean |
| `lt_treasury` | 10-year Treasuries (Damodaran) | data mean |
| `it_treasury` | Intermediate Treasuries (FRED) | data mean |
| `st_treasury` | Short Treasuries / T-bills | data mean |
| `gold` | Gold (1972+) | data mean |
| `intl_developed`, `emerging_markets`, `us_reit`, `tips`, `commodities` | need optional data (DATA-SOURCES.md) | data mean |

"Data mean" is the class's geometric-mean real return over its data; its
volatility defaults to the data's standard deviation. A short-history class
shortens the historical window: `run` reports how many cohorts were tested and
from which year.

## `[buckets]`

Read only with `allocation.sourcing = "buckets"` (and an error otherwise).
Withdrawals come from cash first, refilled from income and growth in up years.
Bucket sourcing has no tax model and cannot be solved.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `n1` | integer | `2` | Cash bucket size, in years of the gap between spending and guaranteed income. |
| `n2` | integer | `7` | Income bucket size, in years of gap after the cash years. |
| `cash` | table | `{ st_treasury = 1.0 }` | Weights for the cash bucket. |
| `income` | table | `{ us_agg_bonds = 1.0 }` | Weights for the income bucket. |
| `growth` | table | `{ us_total_market = 1.0 }` | Weights for the growth bucket (the remainder). |

## `[spending]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `annual` | number | required | Real dollars a year, before `spending_adjustments`. For `lifecycle`, the early (go-go) level. |
| `rule` | string | `"constant"` | `constant`; `lifecycle` (spend less from `slow_go_start_age` and again from `no_go_start_age`); `guardrails` (Guyton-Klinger style); `fr_smooth`, `fr_guardrail`, `fr_tiers` (funded-ratio rules); `amortization` (spend down to zero by the plan's end). See the README's "Spending strategies". |
| `floor` | number | 80% of `annual` | Dynamic rules never cut the rule's part of spending below this. |
| `ceiling` | number | 150% of `annual` | Dynamic rules never raise it above this. |
| `guard_band` | number | `0.2` | `guardrails`: how far the withdrawal rate may drift (±20%) from its starting level before spending is adjusted. |
| `guard_adjust` | number | `0.1` | `guardrails`: the size of each adjustment (10%). |
| `guard_clamp_base` | boolean | `false` | `guardrails`: keep the rule's remembered spending inside the floor and ceiling. |
| `slow_go_start_age` | integer | `75` | `lifecycle`: first age of the slow-go phase. |
| `no_go_start_age` | integer | `85` | `lifecycle`: first age of the no-go phase. |
| `slow_go_multiplier` | number | `0.85` | `lifecycle`: spending in slow-go as a share of `annual`. |
| `no_go_multiplier` | number | `0.75` | `lifecycle`: spending in no-go as a share of `annual`. |
| `amortization_real_return` | number | `0.01` | `amortization`: the planning real return the rule discounts at. |
| `real_drift` | number | `0` | Spending grows by this real rate each year (cost-of-living drift). |

## `[assumptions]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `inflation` | number 0–0.10 | `0.025` | Assumed inflation, used only to erode nominal streams (`cola = "none"`, `fixed`, `fers_diet`, `nominal_until_start`). |
| `pension_real_drift` | number | `0` | Extra real erosion of every pension a year (a COLA-freeze scenario). |

## `[survivor]`

Optional; two-person households only. Omit the table for no survivor case.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `decedent` | `"a"` or `"b"` | required | Who dies first. |
| `age` | integer | required | The decedent's own age at death. |
| `spending_fraction` | number 0–1 | `0.75` | Survivor's spending (and rails) as a share of the couple's. |

From that year the survivor keeps the larger Social Security benefit, the
decedent's other streams continue at their `survivor_fraction`, and taxes use
single-filer brackets.

## `[simulation]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `trials` | integer | `10000` | Monte Carlo trials (`run --trials` overrides it). |
| `seed` | integer | `12345` | Monte Carlo seed; the same seed gives the same results. |
| `distribution` | string | `"normal"` | `normal` or `student_t` (same mean and volatility, fatter tails). |
| `t_df` | number 2.5–100 | `5` | Degrees of freedom for `student_t`; lower is fatter-tailed. |
| `stress` | list of strings | `[]` | Stress paths that `run --mode all` and `report` add: `stagflation_1970s`, `historical_depression_1929`, `slow_grind_2000`, `gfc_2008`, `valuation_unwind_2025`, `depression_extreme`, `ss_haircut_23`, `cost_of_living_shock`, `government_austerity`, `sustained_cost_drift`. Stress paths need constant allocation and single sourcing. |

## Changing keys from the command line

`run`, `compare`, `solve` and `report` take `--set KEY=VALUE`; `sweep` takes
`--set KEY=V1,V2,...` and runs every combination. Keys are dotted paths:

```bash
--set spending.annual=60000
--set spending.rule=guardrails
--set income_streams.a_ss.start_age=70        # a stream, by its id
--set people.a.start_age=55                   # a person, by a or b
--set cash_events.0.amount=-50000             # an item by position, from 0
--set 'allocation.weights={us_total_market=0.7,us_agg_bonds=0.3}'
--set survivor.age=80
```

Values are TOML: numbers, `true`/`false`, `"strings"`, `[lists]` and
`{ inline tables }`; a bare word is taken as a string. In `sweep`, values are
separated by commas (commas inside brackets, braces or quotes do not split),
or written as a range `start:stop:step` with the stop included
(`spending.annual=40000:60000:5000`). The library's field names
(`annual_spending`, `initial_portfolio`, `spending_mode`, ...) are accepted
and translated to plan keys.

Changing `people.a.start_age` moves the plan start (and so the retirement
age) without changing the balance or any stream; ages of streams are the
owner's own ages and stay put.
