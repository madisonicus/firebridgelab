# Data sources

The library reads two files from its data directory at runtime
(`$FIREBRIDGELAB_DATA`, else `data/cache/` in a source checkout, else
`~/.cache/firebridgelab`):

- `ie_data.xls` (Shiller), parsed by `firebridgelab/returns/shiller.py` into the US stock and
  10-year Treasury series;
- `asset_class_series.csv`, built by `firebridgelab/returns/data_build.py`
  from the other raw files below and read by `firebridgelab/returns/class_series.py`.

None of these files are in the repository. `firebridgelab-fetch-data`
downloads the freely available ones and runs the builder. `data/cache/` is
gitignored. Do not commit it, because some sources may not be redistributed.

| File | Source | Feeds | Terms |
| --- | --- | --- | --- |
| `ie_data.xls` | Robert J. Shiller, *Irrational Exuberance* online data (shillerdata.com) | `us_total_market` (S&P 500 real total return) and `us_agg_bonds` (10-year Treasury real total return, duration 7), 1871 onward. These drive Historical mode and the default 60/40. | Free to use with attribution. |
| `histretSP.xls` | Aswath Damodaran, "Historical Returns on Stocks, Bonds and Bills" (NYU Stern) | `st_treasury` (3-month T-bill), `lt_treasury` (10-year T-bond), `gold`, and the annual CPI used to deflate every built series. | Free to use with attribution. |
| `6_Portfolios_2x3_CSV.zip` | Kenneth R. French Data Library (Dartmouth Tuck), 6 portfolios formed on size and book-to-market | `us_small_value` (small / high-B/M, value-weighted). | Free to use with attribution. |
| `fred_gs5_gs10.csv` | FRED, Federal Reserve Bank of St. Louis: series GS5 and GS10 (Board of Governors H.15) | `it_treasury` (5-year constant-maturity yield, duration-constructed total return). | Public domain (US government data). Cite FRED and the series. |
| `nareit_monthly.xls` | FTSE Nareit All Equity REITs total-return index (Nareit) | `us_reit` (optional). | **Not redistributable.** Download it yourself. |
| `testfol_raw_{EFASIM,VWOSIM,GSGSIM,TIPSIM}.json` | testfol.io simulated total-return series | `intl_developed`, `emerging_markets`, `commodities`, `tips` (optional). | **Not redistributable.** Check testfol.io's terms and fetch it yourself. |

## Optional classes

The five classes built from Nareit and testfol.io data are optional. Without
their raw files the builder skips them, and the engine runs with the other
seven classes. The missing classes drop out of the class menu
(`/api/classes`), out of any preset that needs them (`/api/presets`), and out
of the Historical and Monte Carlo registries. Tests that need them carry the
`requires_optional_classes` marker and are skipped. `firebridgelab-fetch-data`
prints instructions for getting the files. The exact testfol.io request is
`_TESTFOL_FETCH_CMD` in `firebridgelab/returns/data_build.py`.

## Citations

- Shiller, Robert J. *Irrational Exuberance* (3rd ed., Princeton University
  Press, 2015), and the accompanying online data, "U.S. Stock Markets 1871–Present
  and CAPE Ratio," https://shillerdata.com/ (formerly
  http://www.econ.yale.edu/~shiller/data.htm, which stopped updating in 2023).
- Damodaran, Aswath. "Historical Returns on Stocks, Bonds and Bills: 1928–present,"
  NYU Stern School of Business,
  https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histret.html.
- French, Kenneth R. Data Library, "6 Portfolios Formed on Size and
  Book-to-Market (2 x 3)," Tuck School of Business at Dartmouth,
  https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html.
- Board of Governors of the Federal Reserve System (US), "Market Yield on U.S.
  Treasury Securities at 5-Year [GS5] and 10-Year [GS10] Constant Maturity,"
  retrieved from FRED, Federal Reserve Bank of St. Louis,
  https://fred.stlouisfed.org/series/GS5 and https://fred.stlouisfed.org/series/GS10.
- (Optional) FTSE Nareit U.S. Real Estate Index Series, Nareit, https://www.reit.com.
- (Optional) testfol.io simulated index series, https://testfol.io.

Data hosts move files from time to time. If a download in
`firebridgelab-fetch-data` fails, get the file from the source page above
and save it in the data directory under the name in the table. The fetcher
reads the current Shiller link from shillerdata.com and warns if the file it
got looks stale.

## Tax and benefit tables

The tax rules read small TOML tables shipped with the package in
`firebridgelab/tax/data/` (no download needed). Every table carries a `source`
naming the publication, notice or statute it was read from, and
`tests/test_tax_m4.py` fails if one does not.

| File | What | Source |
| --- | --- | --- |
| `federal_2026.toml` | 2026 brackets, standard deduction, capital-gain breakpoints, NIIT, Social Security taxation base amounts, payroll tax rates and the 2026 wage base, the 10% early-withdrawal tax, RMD start age and Uniform Lifetime Table | IRS Rev. Proc. 2025-32; IRC 1, 63, 86, 1401, 1402, 1411, 3101, 72(t), 401(a)(9); IRS Pub. 915 and 590-B; IRS Tax Topics 554, 560, 751; SSA, "Cost-of-Living Increase and Other Determinations for 2026" (Federal Register doc. 2025-19763) |
| `medicare_2026.toml` | 2026 Part B and Part D income-related monthly adjustment amounts and MAGI tiers | CMS fact sheet "2026 Medicare Parts A & B Premiums and Deductibles"; 20 CFR 418.1115 and 418.1135 |
| `states_2026.toml` | State income tax rates, deductions and retirement-income rules for the covered states | Each state's revenue department, legislature or form instructions; one `source` per state |

To refresh a tax year, edit these files (or add `<name>_<year>.toml` and change
`TAX_YEAR` in `firebridgelab/tax/params.py`) and update each `source`. Values
that a body has not yet published for the year should be left out, not
estimated (docs/methodology.md lists what was left out for 2026).
