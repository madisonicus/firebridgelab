// test_chart_view.mjs — first JS tests in this codebase.
//
// Slice 7 adds ChartView.histogram (foundation for mc-final-portfolio-
// histogram). These tests pin the histogram method's contract WITHOUT loading
// the browser or Chart.js itself: we run chart_view.js inside a Node `vm`
// sandbox with a stub Chart constructor that captures the config it was
// instantiated with, so we can assert on what would have been rendered.
//
// Run:
//   (from the repo root) node --test tests/test_chart_view.mjs
//
// Requires Node >= 18 (uses built-in node:test + node:assert/strict).
// Zero npm deps; no package.json change.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makeGetComputedStyle } from "./_css_tokens.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CHART_VIEW_PATH = path.resolve(__dirname, "../web/chart_view.js");

// Build a fresh sandbox per call so tests don't share Chart-stub instances.
function loadChartView() {
  const source = fs.readFileSync(CHART_VIEW_PATH, "utf8");
  // cssVar() reads chart colors off :root at render time; back it with the real
  // tokens parsed from styles.css so the sandbox resolves the same values a
  // browser would.
  const ctx = {
    window: {},
    console,
    document: { documentElement: {} },
    getComputedStyle: makeGetComputedStyle(),
  };
  // Stub Chart — capture every instantiation so tests can introspect.
  ctx.Chart = function (canvasCtx, config) {
    this.ctx = canvasCtx;
    this.config = config;
    // Mirror Chart.js's shape so tooltip filter has `item.chart.data`.
    this.data = config.data;
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.ChartView;
}

// Minimal canvas stub — histogram only needs getContext("2d").
function fakeCanvas() {
  return { getContext: () => ({ _stub: "2d" }) };
}

test("histogram produces a bar chart of the right length", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [
      { x0: 0, x1: 1e6, count: 100 },
      { x0: 1e6, x1: 2e6, count: 200 },
      { x0: 2e6, x1: 3e6, count: 50 },
    ],
    options: {},
  });
  assert.equal(chart.config.type, "bar");
  assert.equal(chart.config.data.labels.length, 3);
  assert.equal(chart.config.data.datasets.length, 1);
  assert.equal(chart.config.data.datasets[0].type, "bar");
  assert.deepEqual(chart.config.data.datasets[0].data, [100, 200, 50]);
});

test("histogram uses the default modeler-palette colour when none provided", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [{ x0: 0, x1: 1, count: 1 }],
  });
  const ds = chart.config.data.datasets[0];
  // Default == LINE_A @ 70% alpha.
  assert.equal(ds.backgroundColor, "rgba(10, 87, 78, 0.7)");
  assert.equal(ds.borderColor, "rgba(10, 87, 78, 0.7)");
});

test("histogram honours a caller-provided bar colour", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [{ x0: 0, x1: 1, count: 1 }],
    options: { color: "rgba(192, 38, 163, 0.6)" },
  });
  const ds = chart.config.data.datasets[0];
  assert.equal(ds.backgroundColor, "rgba(192, 38, 163, 0.6)");
});

test("histogram applies xTickFormatter to bin midpoints for category labels", () => {
  const ChartView = loadChartView();
  const fmt = (v) => `$${(v / 1e6).toFixed(1)}m`;
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [
      { x0: 0, x1: 2e6, count: 10 },     // midpoint 1.0m -> "$1.0m"
      { x0: 2e6, x1: 4e6, count: 20 },   // midpoint 3.0m -> "$3.0m"
    ],
    options: { xTickFormatter: fmt },
  });
  assert.deepEqual(chart.config.data.labels, ["$1.0m", "$3.0m"]);
});

test("histogram axis titles fall through caller overrides", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [{ x0: 0, x1: 1, count: 1 }],
    options: {
      xAxisTitle: "Final portfolio (real $)",
      yAxisTitle: "Number of trials",
    },
  });
  const o = chart.config.options;
  assert.equal(o.scales.x.title.text, "Final portfolio (real $)");
  assert.equal(o.scales.y.title.text, "Number of trials");
  assert.equal(o.scales.y.min, 0);
});

test("histogram with verticalLine adds a reference-line bar dataset at the right bin", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [
      { x0: 0, x1: 1e6, count: 100 },
      { x0: 1e6, x1: 2e6, count: 200 },  // median lives here at x=1.5m
      { x0: 2e6, x1: 3e6, count: 50 },
    ],
    options: { verticalLine: { x: 1.5e6, label: "Median", color: "#000" } },
  });
  assert.equal(chart.config.data.datasets.length, 2);
  const ref = chart.config.data.datasets[1];
  assert.equal(ref._isRefLine, true);
  assert.equal(ref.label, "Median");
  // Only the median's bin (index 1) should be populated; others null.
  assert.equal(ref.data[0], null);
  assert.equal(ref.data[2], null);
  // Height should overshoot the tallest bar (200) so the line is visible.
  assert.ok(ref.data[1] > 200);
});

test("histogram with verticalLine outside bin range silently drops the line", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [{ x0: 0, x1: 1e6, count: 10 }],
    options: { verticalLine: { x: 5e6, label: "off-chart", color: "#000" } },
  });
  // No second dataset — the line was outside any bin, so we dropped it
  // rather than push a malformed dataset.
  assert.equal(chart.config.data.datasets.length, 1);
});

test("histogram tooltip filter excludes reference-line datasets", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), {
    bins: [{ x0: 0, x1: 1, count: 5 }],
    options: { verticalLine: { x: 0.5, label: "ref", color: "#000" } },
  });
  const filter = chart.config.options.plugins.tooltip.filter;
  // Build minimal tooltip-item shapes Chart.js would pass us.
  const item0 = { datasetIndex: 0, chart: { data: chart.config.data } };
  const item1 = { datasetIndex: 1, chart: { data: chart.config.data } };
  assert.equal(filter(item0), true);   // data bar — keep
  assert.equal(filter(item1), false);  // reference line — drop
});

test("histogram default tooltipLabel reports the bin count; custom override wins", () => {
  const ChartView = loadChartView();
  const bins = [{ x0: 0, x1: 1e6, count: 42 }];
  // Default formatter
  const chartDefault = ChartView.histogram(fakeCanvas(), { bins });
  const defaultLabel = chartDefault.config.options.plugins.tooltip.callbacks.label;
  assert.equal(
    defaultLabel({ dataIndex: 0, parsed: { y: 42 } }),
    "Count: 42",
  );

  // Custom override receives (item, bin).
  const chartCustom = ChartView.histogram(fakeCanvas(), {
    bins,
    options: {
      tooltipLabel: (item, bin) => `bin=${bin.x0}-${bin.x1} n=${bin.count}`,
    },
  });
  const customLabel = chartCustom.config.options.plugins.tooltip.callbacks.label;
  assert.equal(
    customLabel({ dataIndex: 0, parsed: { y: 42 } }),
    "bin=0-1000000 n=42",
  );
});

test("histogram handles an empty-bins input without throwing", () => {
  const ChartView = loadChartView();
  const chart = ChartView.histogram(fakeCanvas(), { bins: [], options: {} });
  assert.equal(chart.config.type, "bar");
  assert.deepEqual(chart.config.data.labels, []);
  assert.equal(chart.config.data.datasets[0].data.length, 0);
});

test("binIndexForX puts a value on the bin's lower edge into THAT bin", () => {
  const ChartView = loadChartView();
  const bins = [
    { x0: 0, x1: 100, count: 1 },
    { x0: 100, x1: 200, count: 1 },
    { x0: 200, x1: 300, count: 1 },
  ];
  // Exactly on the lower edge of bin 1 → bin 1 (half-open [x0, x1)).
  assert.equal(ChartView._binIndexForX(bins, 100), 1);
  // Exactly on the right edge of the last bin → last bin (inclusive).
  assert.equal(ChartView._binIndexForX(bins, 300), 2);
  // Below the lowest bin → -1.
  assert.equal(ChartView._binIndexForX(bins, -1), -1);
  // Above the highest bin → -1.
  assert.equal(ChartView._binIndexForX(bins, 301), -1);
});

test("bucket-balance tooltipLabel returns one row PER bucket, not all three per call", () => {
  // Regression pin for the bucket-balance chart tooltip bug: with
  // interaction.mode "index", Chart.js calls tooltipLabel once per dataset at
  // the hovered index. The 3-bucket line chart (cash/income/growth) must
  // return only ITS OWN row each call (keyed by item.datasetIndex), giving 3
  // distinct rows total — not the full 3-row set on every call (which produced
  // 3x3 = 9 rows). Mirrors app.js renderBucketBalanceChart's tooltipLabel.
  const ChartView = loadChartView();
  const series = {
    ages: [60, 61, 62],
    cash: [100000, 95000, 80000],
    income: [500000, 480000, 460000],
    growth: [1000000, 1050000, 1100000],
  };
  const fmtMoney = (v) => `$${v.toLocaleString("en-US")}`;
  const tooltipLabel = (item) => {
    const i = item.dataIndex;
    const rows = ChartView.tooltipRows([
      ["Cash", fmtMoney(series.cash[i])],
      ["Income", fmtMoney(series.income[i])],
      ["Growth", fmtMoney(series.growth[i])],
    ]);
    return rows[item.datasetIndex];
  };
  const chart = ChartView.lineChart(fakeCanvas(), {
    labels: series.ages,
    datasets: [
      { label: "Cash bucket", data: series.cash },
      { label: "Income bucket", data: series.income },
      { label: "Growth bucket", data: series.growth },
    ],
    options: { tooltipLabel },
  });
  const label = chart.config.options.plugins.tooltip.callbacks.label;

  // Hover at age index 1: Chart.js fires the callback once per dataset.
  const dataIndex = 1;
  const lines = [0, 1, 2].map(
    (datasetIndex) => label({ dataIndex, datasetIndex }),
  );

  // Three rows total (one per bucket), each a single string — NOT 9.
  assert.equal(lines.length, 3);
  lines.forEach((row) => assert.equal(typeof row, "string"));
  // Each call surfaced exactly its own bucket, in dataset order.
  assert.ok(lines[0].startsWith("Cash"));
  assert.ok(lines[1].startsWith("Income"));
  assert.ok(lines[2].startsWith("Growth"));
  // Values are the hovered age's (index 1) figures, one per row.
  assert.ok(lines[0].includes("$95,000"));
  assert.ok(lines[1].includes("$480,000"));
  assert.ok(lines[2].includes("$1,050,000"));
  // The full rendered tooltip body is 3 lines, not 9.
  const fullBody = lines.flat();
  assert.equal(fullBody.length, 3);
});

test("lineChart threads tooltipFooter + tooltipTitle through to the callbacks", () => {
  // The bucket-balance chart shows the hovered age's Spend + GI as a tooltip
  // FOOTER (once per tooltip), since interaction.mode "index" leaves no extra
  // dataset row to carry them. Pin that lineChart passes a tooltipFooter (and the
  // symmetric tooltipTitle) override straight onto tooltip.callbacks, the same way
  // it threads tooltipLabel. Without an override, the default title stays and
  // there is no footer.
  const ChartView = loadChartView();

  // Default: title is the built-in `Age {label}`, footer is undefined.
  const dflt = ChartView.lineChart(fakeCanvas(), {
    labels: [60, 61],
    datasets: [{ label: "Cash", data: [1, 2] }],
    options: {},
  });
  const dfltCb = dflt.config.options.plugins.tooltip.callbacks;
  assert.equal(typeof dfltCb.title, "function");
  assert.equal(dfltCb.title([{ label: "61" }]), "Age 61");
  assert.equal(dfltCb.footer, undefined);

  // Override: both callbacks are the supplied fns and fire once per tooltip.
  const chart = ChartView.lineChart(fakeCanvas(), {
    labels: [60, 61],
    datasets: [{ label: "Cash", data: [1, 2] }],
    options: {
      tooltipTitle: (items) => `Age ${items[0].label} (custom)`,
      tooltipFooter: (items) => `Spend $100,000 · GI $40,000 @ ${items[0].dataIndex}`,
    },
  });
  const cb = chart.config.options.plugins.tooltip.callbacks;
  assert.equal(cb.title([{ label: "61" }]), "Age 61 (custom)");
  assert.equal(
    cb.footer([{ dataIndex: 1 }]),
    "Spend $100,000 · GI $40,000 @ 1",
  );
});

test("lineChart adds a secondary right-hand y-axis only when requested", () => {
  const ChartView = loadChartView();

  // No secondaryYAxis → single-axis default: only x + y scales, byte-identical
  // to the pre-overlay line chart.
  const single = ChartView.lineChart(fakeCanvas(), {
    labels: [60, 61],
    datasets: [{ label: "Portfolio", data: [1, 2] }],
    options: { yTickFormatter: (v) => `$${v}` },
  });
  const sScales = single.config.options.scales;
  assert.deepEqual(Object.keys(sScales).sort(), ["x", "y"]);
  assert.equal(sScales.yFlows, undefined);

  // With secondaryYAxis → a right-side "yFlows" scale appears, with its title,
  // a beginAtZero baseline, gridlines OFF the chart area, suggestedMax, and the
  // $Nk tick formatter. The primary axis is untouched.
  const dual = ChartView.lineChart(fakeCanvas(), {
    labels: [60, 61],
    datasets: [
      { label: "Portfolio", data: [1e6, 2e6] },
      { label: "Annual spending", data: [110000, 110000], yAxisID: "yFlows" },
    ],
    options: {
      yTickFormatter: (v) => `$${v}`,
      secondaryYAxis: {
        id: "yFlows",
        title: "Annual flows ($/yr)",
        suggestedMax: 132000,
        tickFormatter: (v) => `$${Math.round(v / 1000)}k`,
      },
    },
  });
  const f = dual.config.options.scales.yFlows;
  assert.ok(f, "yFlows scale exists");
  assert.equal(f.position, "right");
  assert.equal(f.beginAtZero, true);
  assert.equal(f.grid.drawOnChartArea, false);
  assert.equal(f.title.text, "Annual flows ($/yr)");
  assert.equal(f.suggestedMax, 132000);
  assert.equal(f.ticks.callback(40500), "$41k");
  // Primary axis untouched (still keyed "y", $-formatter intact).
  assert.equal(dual.config.options.scales.y.ticks.callback(5), "$5");
});

test("fanChartOptions adds a secondary right-hand y-axis only when requested", () => {
  const ChartView = loadChartView();

  // No secondaryYAxis → single-axis default: only x + y scales, byte-identical to
  // the pre-fan-overlay fan options. (Canary: the comparison / side-by-side fans never
  // pass secondaryYAxis, so they must stay single-axis.)
  const single = ChartView.fanChartOptions({ yMax: 5e6 });
  assert.deepEqual(Object.keys(single.scales).sort(), ["x", "y"]);
  assert.equal(single.scales.yFlows, undefined);

  // With secondaryYAxis → a right-side "yFlows" scale appears, mirroring the
  // line-family (buildLineOptions) handling: position right, beginAtZero, grid OFF
  // the chart area, title + suggestedMax + $Nk tick formatter. Primary axis intact.
  const dual = ChartView.fanChartOptions({
    yMax: 5e6,
    secondaryYAxis: {
      id: "yFlows",
      title: "Annual flows ($/yr)",
      suggestedMax: 132000,
      tickFormatter: (v) => `$${Math.round(v / 1000)}k`,
    },
  });
  const f = dual.scales.yFlows;
  assert.ok(f, "yFlows scale exists");
  assert.equal(f.position, "right");
  assert.equal(f.beginAtZero, true);
  assert.equal(f.grid.drawOnChartArea, false);
  assert.equal(f.title.text, "Annual flows ($/yr)");
  assert.equal(f.suggestedMax, 132000);
  assert.equal(f.ticks.callback(40500), "$41k");
  // Primary fan axis untouched (still keyed "y", millions formatter intact).
  assert.equal(dual.scales.y.ticks.callback(2.5e6), "$2.50m");
});

test("ChartView still exposes slices 5 + 6 surfaces (no regression)", () => {
  const ChartView = loadChartView();
  // Slice 5 surface
  assert.equal(typeof ChartView.lineChart, "function");
  assert.equal(typeof ChartView.tooltipRows, "function");
  // Slice 6 surface
  assert.equal(typeof ChartView.fanDatasets, "function");
  assert.equal(typeof ChartView.refLineDataset, "function");
  assert.equal(typeof ChartView.fanChartOptions, "function");
  assert.equal(typeof ChartView.fanChart, "function");
  assert.equal(typeof ChartView.alignMoneyRows, "function");
  // Slice 7 surface
  assert.equal(typeof ChartView.histogram, "function");
  assert.equal(typeof ChartView.histogramOptions, "function");
});
