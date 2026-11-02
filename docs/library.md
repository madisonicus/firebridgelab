# Using firebridgelab as a library

Install from git and fetch the data once (about 10 MB):

```bash
pip install "git+https://github.com/madisonicus/firebridgelab"
firebridgelab-fetch-data          # writes to ~/.cache/firebridgelab
```

The data directory is `$FIREBRIDGELAB_DATA` if set, else `data/cache/`
in a source checkout, else `~/.cache/firebridgelab`. Set the variable
before importing the package.

## Quickstart

```python
from dataclasses import replace
import firebridgelab as rm

plan = rm.default_scenario()                 # a fictional couple, ages 50 → 95
plan = replace(plan, initial_portfolio=1_500_000.0, annual_spending=95_000.0)

det = rm.simulate(plan)                       # one constant real return
print(f"final ${det.final_portfolio:,.0f}, depleted at {det.depleted_age}")

hist = rm.simulate_historical(plan)           # every Shiller cohort since 1871
print(f"{hist.depletion_count} of {hist.cohort_count} historical cohorts deplete")

mc = rm.simulate_monte_carlo(plan, n_trials=2_000, seed=7)
print(f"{mc.depletion_count} of {mc.trial_count} Monte Carlo trials deplete")

best = rm.solve_max_spend(plan, threshold=0.95, mode="historical")
print(f"max spend surviving 95% of cohorts: ${best.solved_spend:,.0f}")

crash = rm.simulate_stress(plan, "stagflation_1970s")   # ids: rm.STRESS_SCENARIOS
print(f"1970s stress: depleted at {crash.depleted_age}")
```

All dollars are real (plan-start purchasing power). `Scenario` is a frozen
dataclass: build variants with `dataclasses.replace`, and read its field
comments in `firebridgelab/engine/scenario.py` for every knob (spending modes,
guardrail rails, pensions and COLA options, survivor case, tax-location split,
fees, Monte Carlo distribution). `validate_scenario(s)` raises
`ScenarioValidationError` on an inconsistent plan.

The names exported from `firebridgelab/__init__.py` are the supported
surface. Everything else is internal and may change.

## Households and income streams

`default_scenario()` is a fictional same-age couple with exactly two pensions
and two Social Security benefits, held in the `person_a_*` / `person_b_*`
fields. Any other household is built with `with_household`:

```python
from dataclasses import replace
import firebridgelab as rm
from firebridgelab import CashEvent, IncomeStream, Person, SpendingAdjustment

plan = rm.with_household(
    replace(rm.default_scenario(), initial_portfolio=1_400_000.0, annual_spending=70_000.0),
    # Person A is 52 and plans to 95; person B is 47 and plans to 95. The plan
    # runs to the later of the two: B's 95, which is A's 100.
    people=[Person("a", 52, 95), Person("b", 47, 95)],
    income_streams=[
        # Ages are the OWNER's ages; end_age is exclusive (paid through 59).
        IncomeStream("a_consulting", "work", 30_000.0, 52, owner="a", end_age=60),
        IncomeStream("a_pension", "pension", 20_000.0, 60, owner="a",
                     cola="fixed", cola_rate=0.01, survivor_fraction=0.5),
        IncomeStream("b_annuity", "annuity", 8_000.0, 65, owner="b", cola="none"),
        IncomeStream("rental", "rental", 14_000.0, 52, owner="household", end_age=75),
        # Social Security: the FRA amount and the claim age (62-70).
        IncomeStream("a_ss", "social_security", 31_000.0, 67, owner="a"),
        IncomeStream("b_ss", "social_security", 22_000.0, 70, owner="b"),
    ],
    cash_events=[
        # Sell the house at A's 70: half the price is gain.
        CashEvent(70, 450_000.0, tax_treatment="capital_gain", basis_fraction=0.5,
                  id="house"),
        CashEvent(56, -40_000.0, id="car"),
    ],
    spending_adjustments=[
        # Health insurance until each person reaches Medicare.
        SpendingAdjustment(9_000.0, 52, 65, owner="a", id="aca_a"),
        SpendingAdjustment(9_000.0, 47, 65, owner="b", id="aca_b"),
    ],
)
plan = replace(plan, survivor_age=88, survivor_decedent="a")  # A dies at A's 88
rm.validate_scenario(plan)

print(plan.start_age, plan.end_age)          # 52 100 (person A's age axis)
year = rm.household_year(plan, 70)           # every flow for the year A is 70
print(year.gi, year.required_spending, year.cash_event, year.n_65_plus)
print(rm.simulate(plan).final_portfolio)
```

The rules, in full, are in the docstring of `firebridgelab/income/household.py`.
In short:

- **Age axis.** The engine's year axis is person A's age. `start_age` is A's
  age at plan start and `end_age` is the last plan year on A's axis
  (`with_household` sets both). Everything a person owns is entered in their
  own age and converted with the fixed age offset. A planning end age is a
  horizon, not a death.
- **One person.** A one-person household files single every year, takes one
  65+ deduction, and has no survivor case.
- **Streams.** `kind` is `pension`, `social_security`, `annuity`, `work`,
  `rental` or `other`; `owner` is `a`, `b` or `household` (household streams
  use A's age). `amount` is real dollars at plan start, or the nominal check at
  commencement with `nominal_until_start=True`. COLA: `full` keeps real value
  constant, `none` loses `assumed_inflation` a year, `fixed` adds `cola_rate`
  nominal, `fers_diet` applies the FERS rule. `tax_treatment` is `ordinary`
  (the default), `tax_free`, or `social_security` (85% included; Social
  Security only).
- **Survivor case.** `survivor_decedent` dies at their own age
  `survivor_age`. The survivor gets the larger of their own Social Security
  benefit and the widow(er)'s benefit on the decedent's record; the
  decedent's other streams continue at their `survivor_fraction` (default 0,
  rental 1.0, household-owned 1.0); spending scales by
  `survivor_spending_fraction` and taxes switch to single filer.
- **Cash events** are mid-year flows the spending rule never sees. Under a
  tax-location split an inflow nets against that year's spending need, the
  rest lands in the taxable account, and its gain (or ordinary income) is taxed
  that year. Outflows must be `tax_free`.
- **Spending adjustments are required spending.** Each rule is handed
  guaranteed income minus required spending (`net_income_at_age`), so the
  guardrail withdrawal rate, the funded-ratio and amortization NPV gaps and the
  bucket gap all count the adjustment as a withdrawal the rule cannot cut. The
  rule moves only the discretionary part, the floor and ceiling bound that
  part, and the engine adds the required amount on top. Recorded spending is
  the total. The alternative, folding the adjustment into the discretionary
  spend, would let a guardrail cut your mortgage payment.

The legacy fields still work. With `income_streams=None` the four `person_*`
fields are converted into the same internal stream list, and
`legacy_income_streams(scenario)` returns them as `IncomeStream` objects (Social
Security as an FRA amount). Passing `income_streams` replaces the legacy
fields, including the scenario-level `pension_cola`,
`pension_nominal_until_start` and `survivor_pension_fraction`.

The web API takes the same household in the POST body of `/api/trajectory`
and `/api/solve` as `people`, `income_streams`, `cash_events` and
`spending_adjustments` (lists of objects with these field names) and echoes
them in `scenario`.

## Package layout

| Subpackage | Contents |
| --- | --- |
| `returns/` | data location (`paths`), download and build (`fetch`, `data_build`), the Shiller and asset-class series, the class → series registry (`series`), correlation estimates, Monte Carlo samplers (`sampling`) |
| `income/` | the household (`household`: people, income streams, cash events, spending adjustments, resolved per age), Social Security claim factors, the FERS COLA rule, survivor timing, and `gi_at_age` / `net_income_at_age`, the sums every engine reads |
| `spending/` | the `SpendingStrategy` interface (`base`), one module per rule in `spending/rules/`, and the `registry` that maps `Scenario.spending_mode` to a rule |
| `tax/` | federal tax (`core`), RMDs (`rmd`), Social Security taxation (`ss_taxation`), payroll tax (`payroll`), Medicare IRMAA (`irmaa`), Roth conversions (`roth`), state income tax (`state`), unindexed thresholds (`thresholds`), the tax-location fixed point (`location`, `location_path`, `location_risk`) and the tax overlay; the yearly constants are in `tax/data/*.toml` |
| `engine/` | `Scenario` and its validation, the yearly step, and the deterministic, historical and Monte Carlo drivers, plus bucket sourcing |
| `analysis/` | the max-spend solver and the stress tests |

To add a spending rule, add a module to `firebridgelab/spending/rules/` that
defines a `SpendingStrategy` and decorates it (or a factory) with
`@register("<mode>")` from `firebridgelab.spending.registry`; the registry
imports every module in that package. The web API also needs a descriptor in
`api/spending_modes.py`.

To update the tax year, edit the files in `firebridgelab/tax/data/`
(`federal_2026.toml`, `medicare_2026.toml`, `states_2026.toml`), or add
`<name>_<year>.toml` files and change `TAX_YEAR` in
`firebridgelab/tax/params.py`. Each table carries a `source` field naming the
publication, notice or statute it comes from; a test fails without one.

## Tax options

With a tax-location split, these `Scenario` fields refine the tax bill (plan
file `[tax]`, docs/plan-files.md; rules and sources, docs/methodology.md):

```python
plan = replace(
    plan,
    ss_taxation="provisional",          # default; "flat85" is the old rule
    payroll_tax=True,                    # FICA / SE tax on work streams
    irmaa=True, irmaa_prior_magi=140_000.0,
    roth_conversion_target="22%",        # or a dollar amount of taxable income
    roth_conversion_start_age=60, roth_conversion_end_age=73,
    state="NC", state_move_age=70, state_after_move="FL",
)
print(rm.compare_roth_conversion(plan).to_dict())
```

A work stream marked `IncomeStream(..., self_employed=True)` pays
self-employment tax instead of FICA.

The old flat module paths (`firebridgelab.scenario`, `firebridgelab.historical`,
`firebridgelab.monte_carlo`, `firebridgelab.tax_location`, ...) still import:
each is a thin re-export of the names it used to define, kept for one release.

The notebooks in [`notebooks/`](../notebooks/) are worked examples.
