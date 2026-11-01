// chart_view.js — shared factory for the line + fan chart families.
//
// Slice 5 extracted the line family (renderChart, renderOverlayChart).
// Slice 6 extends ChartView to absorb the fan family — single + overlay +
// side-by-side portfolio fans, plus the variable-spending fan. The 4
// fan renderers in app.js now build their datasets via ChartView.fanDatasets,
// choose their axis options via ChartView.fanChartOptions, and instantiate the
// Chart via ChartView.fanChart (or the line-family entry point lineChart).
//
// Design (line family, slice 5):
//   ChartView.lineChart(canvas, { labels, datasets, options })
//   ChartView.tooltipRows(rows)
//
// Design (fan family, slice 6):
//   ChartView.fanDatasets(bands, { rgb, border, bandAlpha, includeInner,
//                                  medianLabel })
//     Build the percentile-band Chart.js datasets. Lower bands have fill:'-1'
//     (fill to the dataset above) so Chart.js stacks the translucent fills
//     into the fan's signature look. Returns { datasets, medianIndex } —
//     the median index is what the tooltip filter keys on (one row at hover,
//     not a row per band).
//
//     ** Visual-stability constraint: the fill chain is load-bearing. **
//     - p10 has fill:false (chain head — nothing to fill TO)
//     - p90 has fill:'-1' (fills DOWN to p10)
//     - if includeInner:
//         p25 has fill:false (a NEW chain head — must NOT continue the p10/p90
//                             chain or Chart.js would fill weirdly)
//         p75 has fill:'-1' (fills DOWN to p25)
//     - median has fill:false (drawn line, no fill)
//     Reference lines appended AFTER this set get fill:false too (built by
//     refLineDataset) AND are SKIPPED from the chain by virtue of also being
//     fill:false, never '-1'.
//
//   ChartView.refLineDataset(label, value, count, color, dash)
//     One flat horizontal reference line at `value` across `count` ages, in the
//     supplied colour with optional dash. Always fill:false, pointRadius:0 — by
//     design it does not participate in the band-fill chain.
//
//   ChartView.fanChartOptions({ yMax, tooltipFilter, tooltipLabel,
//                               legend, yAxisTitle, yTickFormatter, yMaxScale })
//     Fan-specific Chart.js options. The tooltip filter is the load-bearing
//     bit — only the median dataset(s) surface a tooltip row, otherwise every
//     band edge would too and the tooltip becomes noise. `legend` accepts
//     either a boolean or an object { display, labels } so the spending fan
//     can show the median + reference-line legend while hiding band-edge
//     entries (filter on borderWidth > 0).
//
//   ChartView.fanChart(canvas, { labels, datasets, options })
//     Convenience instantiation — same shape as lineChart but passes options
//     through fanChartOptions. Returns the Chart instance; the caller owns
//     lifecycle bookkeeping (destroy-on-redraw).
//
//   ChartView.alignMoneyRows(rows, fmtMoney)
//     Render [[label, numericValue], …] into monospace-aligned tooltip rows
//     using the supplied `fmtMoney` formatter. Labels right-padded; values
//     left-padded. Used by the fan tooltips (portfolio + spending) so all
//     row sets line up identically.
//
// IMPORTANT — visual stability constraint:
//   Every Chart.js option here was verified identical between the four fan
//   renderers (slice 6) and across renderChart / renderOverlayChart (slice 5)
//   before being hoisted. Options that DIFFER are parameters, not constants.
//   Adding "obvious cleanups" here is a visual regression risk; reach for the
//   param hatch instead.
(function (global) {
  "use strict";

  // Read a CSS custom property off :root. Color is owned by the stylesheet
  // (a <canvas> can't be CSS-styled), so chart code READS the token and hands
  // the string to Chart.js. MUST be called at render time (inside the chart
  // builders), never cached at module load: styles may not be applied yet at
  // load, and a future theme toggle needs each render to re-read the live value.
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // ============================================================
  // Line family (slice 5)
  // ============================================================

  // The shared option skeleton (line family). Caller fills in
  // legend/tooltip overrides via buildOptions().
  function baseLineOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "index",
        intersect: false,
        axis: "x",
      },
      plugins: {
        legend: { display: false, labels: { color: cssVar("--chart-legend-text") } },
        tooltip: {
          mode: "index",
          intersect: false,
          axis: "x",
          backgroundColor: cssVar("--chart-tooltip-bg"),
          titleColor: cssVar("--chart-tooltip-text"),
          bodyColor: cssVar("--chart-tooltip-text"),
          borderColor: cssVar("--chart-tooltip-border"),
          borderWidth: 1,
          bodyFont: { family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
          callbacks: {
            title: (items) => `Age ${items[0].label}`,
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: "Age", color: cssVar("--chart-axis-title") },
          grid: { color: cssVar("--chart-grid") },
          ticks: { color: cssVar("--chart-tick") },
        },
        y: {
          min: 0,
          title: { display: true, text: "Portfolio (real $)", color: cssVar("--chart-axis-title") },
          grid: { color: cssVar("--chart-grid") },
          ticks: { color: cssVar("--chart-tick") },
        },
      },
    };
  }

  // Build the Chart.js options for a LINE chart from caller overrides.
  //
  // opts shape (all optional):
  //   legend:        { display, position }     — overrides plugins.legend
  //   tooltipLabel:  fn(item) => string|string[]  — tooltip.callbacks.label
  //   tooltipTitle:  fn(items) => string|string[] — tooltip.callbacks.title
  //                  (overrides the default `Age {age}` title block)
  //   tooltipFooter: fn(items) => string|string[] — tooltip.callbacks.footer
  //                  (a once-per-tooltip line BELOW the per-dataset rows — the
  //                  hook the bucket chart uses for spend/GI, which have no own
  //                  dataset row to ride on under interaction.mode "index")
  //   tooltipFilter: fn(item) => bool          — tooltip.filter. Under
  //                  interaction.mode "index" the body callback fires ONCE PER
  //                  active dataset at the hovered x; a filter restricts WHICH
  //                  datasets surface a row (e.g. "only datasetIndex 0" so flat
  //                  guide lines contribute nothing). Mirrors fanChartOptions'
  //                  filter wiring — without this the line family silently
  //                  dropped a caller-supplied filter, duplicating rows once per
  //                  guide line.
  //   tooltipDisplayColors: bool               — tooltip.displayColors
  //   suggestedYMax: number                    — y.suggestedMax (auto if omitted)
  //   yTickFormatter: fn(v) => string          — y.ticks.callback
  //   secondaryYAxis: {                         — OPTIONAL right-hand y-axis
  //                    id, title, suggestedMax, tickFormatter }
  //                  When present, a SECOND scale (keyed by `id`, default
  //                  "yFlows") is added on the right. It has grid.drawOnChartArea
  //                  false so it never paints gridlines over the primary axis's —
  //                  the right axis is a reading ruler for a co-plotted series that
  //                  lives on a wildly different magnitude (spending/GI in the
  //                  $0–260k band vs the portfolio in the millions). Datasets that
  //                  want it carry `yAxisID: id`. When OMITTED the options are
  //                  byte-identical to the single-axis default — existing callers
  //                  are untouched.
  //
  // Anything not in opts uses the line-family default.
  function buildLineOptions(opts) {
    const o = baseLineOptions();
    const u = opts || {};

    if (u.legend) {
      o.plugins.legend = Object.assign({}, o.plugins.legend, u.legend);
    }
    if (typeof u.tooltipLabel === "function") {
      o.plugins.tooltip.callbacks.label = u.tooltipLabel;
    }
    if (typeof u.tooltipTitle === "function") {
      o.plugins.tooltip.callbacks.title = u.tooltipTitle;
    }
    if (typeof u.tooltipFooter === "function") {
      o.plugins.tooltip.callbacks.footer = u.tooltipFooter;
    }
    if (typeof u.tooltipFilter === "function") {
      o.plugins.tooltip.filter = u.tooltipFilter;
    }
    if (typeof u.tooltipDisplayColors === "boolean") {
      o.plugins.tooltip.displayColors = u.tooltipDisplayColors;
    }
    if (u.suggestedYMax != null) {
      o.scales.y.suggestedMax = u.suggestedYMax;
    }
    if (typeof u.yTickFormatter === "function") {
      o.scales.y.ticks.callback = u.yTickFormatter;
    }
    if (u.secondaryYAxis) {
      const s = u.secondaryYAxis;
      const id = s.id || "yFlows";
      o.scales[id] = {
        position: "right",
        beginAtZero: true,
        // drawOnChartArea:false — the secondary ruler must NOT paint its own
        // gridlines across the plot, or two non-aligned grids fight for the eye.
        // Only the primary (portfolio) axis owns the chart-area grid.
        grid: { drawOnChartArea: false, color: cssVar("--chart-grid") },
        title: {
          display: true,
          text: s.title || "",
          color: cssVar("--chart-axis-title"),
        },
        ticks: {
          callback: typeof s.tickFormatter === "function" ? s.tickFormatter : undefined,
          color: cssVar("--chart-tick"),
        },
      };
      if (s.suggestedMax != null) {
        o.scales[id].suggestedMax = s.suggestedMax;
      }
    }
    return o;
  }

  // Public: build a Chart.js LINE chart on `canvas` with the supplied datasets
  // and option overrides. Returns the Chart instance so the caller can track
  // / destroy it (the caller already owns chart-lifecycle bookkeeping).
  function lineChart(canvas, config) {
    const ctx = canvas.getContext("2d");
    const { labels, datasets, options } = config;
    return new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: buildLineOptions(options),
    });
  }

  // Public: render an array of [label, value] pairs as monospace-aligned
  // tooltip rows. `value` is already a string. Labels right-padded to the
  // longest label width; values left-padded to the longest value width.
  function tooltipRows(rows) {
    const labelStrings = rows.map(([label]) => label + ":");
    const valueStrings = rows.map(([, value]) => value);
    const labelW = Math.max(...labelStrings.map((s) => s.length));
    const valueW = Math.max(...valueStrings.map((s) => s.length));
    return rows.map(
      (_, i) => labelStrings[i].padEnd(labelW) + valueStrings[i].padStart(valueW),
    );
  }

  // ============================================================
  // Fan family (slice 6)
  // ============================================================

  // Public: build the percentile-band datasets for a fan chart. The fill chain
  // is THE load-bearing thing — see the file header constraint section.
  //
  // `bands` is an array of {age, p10, p25, median, p75, p90}.
  // opts:
  //   rgb:         "R, G, B" string (Chart.js consumes as rgba(rgb, alpha))
  //   border:      median line colour (hex / css)
  //   bandAlpha:   outer (10–90) band alpha; inner (25–75) gets +0.03
  //   includeInner: true => p25 + p75 datasets drawn; false => only p10/p90
  //   medianLabel: legend label for the median line
  //   lineWidth:   median line stroke width (default 4 — matches the line
  //                family's LINE_WIDTH so single + fan medians read the same)
  //
  // Returns { datasets, medianIndex }. medianIndex tells the tooltip filter
  // which dataset is "the line"; band datasets are filtered OUT of the tooltip.
  function fanDatasets(bands, { rgb, border, bandAlpha, includeInner, medianLabel, lineWidth }) {
    const col = (key) => bands.map((p) => p[key]);
    const clear = `rgba(${rgb}, 0)`;
    const lw = lineWidth != null ? lineWidth : 4;
    const datasets = [
      {
        label: "p10",
        data: col("p10"),
        borderColor: clear,
        backgroundColor: clear,
        fill: false,             // chain head; nothing to fill TO
        tension: 0.15,
        pointRadius: 0,
        borderWidth: 0,
      },
      {
        label: "p90",
        data: col("p90"),
        borderColor: clear,
        backgroundColor: `rgba(${rgb}, ${bandAlpha})`,
        fill: "-1",              // fill DOWN to p10 — the outer band
        tension: 0.15,
        pointRadius: 0,
        borderWidth: 0,
      },
    ];
    if (includeInner) {
      datasets.push(
        {
          label: "p25",
          data: col("p25"),
          borderColor: clear,
          backgroundColor: clear,
          fill: false,           // NEW chain head — do NOT continue the previous chain
          tension: 0.15,
          pointRadius: 0,
          borderWidth: 0,
        },
        {
          label: "p75",
          data: col("p75"),
          borderColor: clear,
          backgroundColor: `rgba(${rgb}, ${bandAlpha + 0.03})`,
          fill: "-1",            // fill DOWN to p25 — the inner band
          tension: 0.15,
          pointRadius: 0,
          borderWidth: 0,
        },
      );
    }
    const medianIndex = datasets.length;
    datasets.push({
      label: medianLabel,
      data: col("median"),
      borderColor: border,
      backgroundColor: clear,
      fill: false,               // line only, no fill
      tension: 0.15,
      pointRadius: 0,
      borderWidth: lw,
    });
    return { datasets, medianIndex };
  }

  // Public: a flat horizontal reference-line dataset at `value` across `count`
  // ages. Dashed, point-less, in a muted colour — Chart.js's "annotation"
  // without the annotation plugin. ALWAYS fill:false so it never participates
  // in the band-fill chain (an absolute-floor line that filled to the median
  // line would be a serious visual regression).
  function refLineDataset(label, value, count, color, dash) {
    return {
      label,
      data: new Array(count).fill(value),
      borderColor: color,
      backgroundColor: color,
      borderDash: dash,
      borderWidth: 1.5,
      fill: false,
      tension: 0,
      pointRadius: 0,
    };
  }

  // Public: build Chart.js options for a FAN chart.
  //
  // opts:
  //   yMax:           shared comparison max; suggestedMax = yMax * yMaxScale
  //                   (default yMaxScale = 1.05, i.e. 5% headroom)
  //   yMaxScale:      override the 1.05 default
  //   tooltipFilter:  fn(item) => bool — usually "only the median dataset(s)"
  //   tooltipLabel:   fn(item) => string|string[]
  //   legend:         bool OR { display, labels } — pass the object form to
  //                   provide a labels.filter (the spending fan uses this to
  //                   hide the invisible band-edge datasets from the legend
  //                   while keeping the median + reference lines visible).
  //   yAxisTitle:     y-axis title text (default "Portfolio (real $)")
  //   yTickFormatter: fn(v) => string for y ticks (default millions, e.g.
  //                   "$2.5m"; the spending fan passes a $-thousands fn).
  //   secondaryYAxis: {                         — OPTIONAL right-hand y-axis
  //                    id, title, suggestedMax, tickFormatter }
  //                  Identical to buildLineOptions' secondaryYAxis: a SECOND
  //                  scale (keyed by `id`, default "yFlows") added on the right,
  //                  with grid.drawOnChartArea false so it never paints gridlines
  //                  over the primary fan axis's — the right axis is a reading
  //                  ruler for a co-plotted series on a wildly different magnitude
  //                  (spending/GI in the $0–260k band vs the portfolio fan in the
  //                  millions). Datasets that want it carry `yAxisID: id`. The
  //                  flow-overlay on the Historical / MC fans uses it.
  //                  When OMITTED the options are byte-identical to the
  //                  single-axis default — existing fan callers are untouched.
  function fanChartOptions(opts) {
    const u = opts || {};
    const scale = u.yMaxScale != null ? u.yMaxScale : 1.05;

    // Default y-tick formatter mirrors the portfolio fan's pre-refactor
    // behaviour ("$2.50m"). Callers (spending fan) override for $-thousands.
    const defaultYTicks = (v) => `$${(v / 1e6).toFixed(2)}m`;

    // Dark-mode slice 2: legend label color tracks the theme. The caller's
    // legend object (when object-form) is spread so its display/position/labels
    // win, but a label color is injected when the caller didn't set one.
    const legendLabelColor = cssVar("--chart-legend-text");
    let legend;
    if (u.legend && typeof u.legend === "object") {
      legend = Object.assign({}, u.legend);
      legend.labels = Object.assign({ color: legendLabelColor }, u.legend.labels);
    } else {
      legend = { display: !!u.legend, labels: { color: legendLabelColor } };
    }

    const o = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: {
        legend,
        tooltip: {
          mode: "index",
          intersect: false,
          axis: "x",
          displayColors: false,
          filter: u.tooltipFilter,
          backgroundColor: cssVar("--chart-tooltip-bg"),
          titleColor: cssVar("--chart-tooltip-text"),
          bodyColor: cssVar("--chart-tooltip-text"),
          borderColor: cssVar("--chart-tooltip-border"),
          borderWidth: 1,
          bodyFont: { family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
          callbacks: {
            title: (items) => `Age ${items[0].label}`,
            label: u.tooltipLabel,
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: "Age", color: cssVar("--chart-axis-title") },
          grid: { color: cssVar("--chart-grid") },
          ticks: { color: cssVar("--chart-tick") },
        },
        y: {
          min: 0,
          suggestedMax: u.yMax != null ? u.yMax * scale : undefined,
          title: {
            display: true,
            text: u.yAxisTitle != null ? u.yAxisTitle : "Portfolio (real $)",
            color: cssVar("--chart-axis-title"),
          },
          grid: { color: cssVar("--chart-grid") },
          ticks: { callback: u.yTickFormatter || defaultYTicks, color: cssVar("--chart-tick") },
        },
      },
    };
    if (u.secondaryYAxis) {
      const s = u.secondaryYAxis;
      const id = s.id || "yFlows";
      o.scales[id] = {
        position: "right",
        beginAtZero: true,
        // drawOnChartArea:false — the secondary ruler must NOT paint its own
        // gridlines across the plot, or two non-aligned grids fight for the eye.
        // Only the primary (portfolio) axis owns the chart-area grid.
        grid: { drawOnChartArea: false, color: cssVar("--chart-grid") },
        title: {
          display: true,
          text: s.title || "",
          color: cssVar("--chart-axis-title"),
        },
        ticks: {
          callback: typeof s.tickFormatter === "function" ? s.tickFormatter : undefined,
          color: cssVar("--chart-tick"),
        },
      };
      if (s.suggestedMax != null) {
        o.scales[id].suggestedMax = s.suggestedMax;
      }
    }
    return o;
  }

  // Public: instantiate a Chart.js LINE chart configured for the fan family
  // (options run through fanChartOptions). Returns the Chart instance.
  function fanChart(canvas, config) {
    const ctx = canvas.getContext("2d");
    const { labels, datasets, options } = config;
    return new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: fanChartOptions(options),
    });
  }

  // Public: monospace-align [[label, moneyNumber], …] into padded rows using
  // the caller-supplied `fmtMoney` formatter (so app.js owns the formatter,
  // ChartView owns the alignment math). Used by the portfolio + spending fan
  // tooltips so all row sets line up identically.
  function alignMoneyRows(rows, fmtMoney) {
    const labelStrings = rows.map(([label]) => label + ":");
    const moneyStrings = rows.map(([, value]) => fmtMoney(value));
    const labelW = Math.max(...labelStrings.map((s) => s.length));
    const moneyW = Math.max(...moneyStrings.map((s) => s.length));
    return rows.map(
      (_, i) => labelStrings[i].padEnd(labelW) + moneyStrings[i].padStart(moneyW),
    );
  }

  // ============================================================
  // Histogram family (slice 7) — foundation for mc-final-portfolio-histogram.
  //
  // Why bar (not line): histograms have discrete bin extents. Chart.js's
  // `bar` type renders exactly the rectangles we want.
  //
  // Bin format: { x0, x1, count } per bin. This is the more flexible shape —
  // it supports irregular bin widths (e.g., log-spaced bins for a long-tailed
  // final-portfolio distribution). The simpler `[count, count, …]` form was
  // rejected because the MC final-portfolio histogram may want log bins; we'd
  // rather decide that at call-site than re-shape the API later.
  //
  // X-axis: a CATEGORY axis with one label per bin (formatted by the caller's
  // xTickFormatter, applied to the bin midpoint). Categories keep the bars
  // aligned and let us trivially map a vertical reference-line x-value to a
  // bin index.
  //
  // Vertical reference line: Chart.js mixed-type trick. We add a second
  // dataset of `type: 'line'` with TWO points — one at y=0 and one at y=ymax
  // — both at the SAME category index (the bin containing the caller's x).
  // No plugin; just a line dataset that draws straight up at one tick. This
  // is the simpler path vs a custom plugin; document it here so the next
  // editor doesn't "clean it up" into a broken plugin.
  // ============================================================

  // Default bar colour — matches LINE_A (#1b4f8a == rgb(27,79,138)) with ~70%
  // alpha so it reads as the modeler's primary blue. Callers can override.
  // Built at render time from the --chart-hist-rgb token (the "R, G, B" triplet)
  // so the 0.7 alpha stays in JS and the base colour is theme-addressable.
  const defaultHistColor = () => `rgba(${cssVar("--chart-hist-rgb")}, 0.7)`;

  // Locate the bin index that contains value `x`. Returns -1 if x is outside
  // the histogram's x-extent. Bins are treated as [x0, x1) except the last
  // bin, which includes its right edge so a value sitting exactly on the
  // last x1 doesn't fall off the chart.
  function binIndexForX(bins, x) {
    if (!bins || bins.length === 0) return -1;
    for (let i = 0; i < bins.length; i++) {
      const b = bins[i];
      const isLast = i === bins.length - 1;
      if (x >= b.x0 && (isLast ? x <= b.x1 : x < b.x1)) return i;
    }
    return -1;
  }

  // Public: build a Chart.js histogram (bar chart) on `canvas`.
  //
  // config:
  //   bins:    [{ x0, x1, count }, ...]
  //   options: {
  //     yAxisTitle,     // default "Number of trials"
  //     xAxisTitle,     // default "Value"
  //     color,          // bar colour (any css colour string); default defaultHistColor()
  //     xTickFormatter, // fn(midpointValue) => string; default String
  //     tooltipLabel,   // fn(item, bin) => string|string[]; default shows the bin range + count
  //     verticalLine,   // optional { x, label, color } reference line at data-x
  //   }
  //
  // Returns the Chart instance (caller owns lifecycle).
  function histogram(canvas, config) {
    const ctx = canvas.getContext("2d");
    const bins = (config && config.bins) || [];
    const u = (config && config.options) || {};
    const color = u.color || defaultHistColor();
    const xTick = typeof u.xTickFormatter === "function" ? u.xTickFormatter : String;

    // Category labels — formatted bin midpoints. Empty-bins case yields an
    // empty category axis, which Chart.js handles gracefully.
    const labels = bins.map((b) => xTick((b.x0 + b.x1) / 2));
    const counts = bins.map((b) => b.count);

    const datasets = [
      {
        label: "Count",
        type: "bar",
        data: counts,
        backgroundColor: color,
        borderColor: color,
        borderWidth: 0,
        // Tight bars — categoryPercentage * barPercentage controls the gap.
        // 1.0 * 1.0 would butt them edge-to-edge; the small gap reads as
        // discrete bins without looking like a sparse chart.
        categoryPercentage: 0.98,
        barPercentage: 1.0,
      },
    ];

    // Vertical reference-line dataset (the category-axis trick).
    //
    // On a CATEGORY x-axis, an arbitrary-x vertical line is awkward — Chart.js
    // line datasets index by category, not by data-x, so a true `{x, y}`-pair
    // line doesn't work. The clean alternative: a second BAR dataset, one tall
    // bar at the target bin, with barPercentage tuned tiny so the bar reads as
    // a thin vertical rule. Height = max bin count * 1.05 so the line clearly
    // overshoots the tallest bar. Tagged `_isRefLine: true` so the tooltip
    // filter skips it (the line is annotation, not data).
    //
    // If a future caller needs the line at a data-x that doesn't fall in any
    // bin (e.g., $0 depletion threshold when all bins are positive), we
    // silently drop it (binIndexForX returns -1). Callers can clamp upstream.
    if (u.verticalLine && bins.length > 0) {
      const v = u.verticalLine;
      const idx = binIndexForX(bins, v.x);
      if (idx >= 0) {
        const yMax = Math.max(...counts, 0);
        const refColor = v.color || `rgba(${cssVar("--chart-refline-rgb")}, 0.85)`;
        const refData = new Array(bins.length).fill(null);
        refData[idx] = yMax * 1.05;
        datasets.push({
          label: v.label || "ref",
          type: "bar",
          data: refData,
          backgroundColor: refColor,
          borderColor: refColor,
          borderWidth: 0,
          categoryPercentage: 1.0,
          // Thin bar so it reads as a vertical line. 0.04 ≈ 4% of cat width.
          barPercentage: 0.04,
          _isRefLine: true,
        });
      }
    }

    return new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: histogramOptions(bins, u),
    });
  }

  // Public: build the Chart.js options for a histogram. Exposed so tests +
  // future callers can introspect / reuse without instantiating a Chart.
  function histogramOptions(bins, opts) {
    const u = opts || {};
    const xTick = typeof u.xTickFormatter === "function" ? u.xTickFormatter : String;

    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: {
        legend: { display: false, labels: { color: cssVar("--chart-legend-text") } },
        tooltip: {
          mode: "index",
          intersect: false,
          axis: "x",
          displayColors: false,
          backgroundColor: cssVar("--chart-tooltip-bg"),
          titleColor: cssVar("--chart-tooltip-text"),
          bodyColor: cssVar("--chart-tooltip-text"),
          borderColor: cssVar("--chart-tooltip-border"),
          borderWidth: 1,
          bodyFont: { family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
          // Filter out the reference-line dataset so the tooltip doesn't show
          // a phantom "ref" row.
          filter: (item) => {
            const ds = item.chart.data.datasets[item.datasetIndex];
            return !(ds && ds._isRefLine);
          },
          callbacks: {
            title: (items) => {
              if (!items || items.length === 0) return "";
              const i = items[0].dataIndex;
              const b = bins[i];
              if (!b) return items[0].label;
              return `${xTick(b.x0)} – ${xTick(b.x1)}`;
            },
            label: typeof u.tooltipLabel === "function"
              ? (item) => u.tooltipLabel(item, bins[item.dataIndex])
              : (item) => {
                  const b = bins[item.dataIndex];
                  const c = b ? b.count : item.parsed.y;
                  return `Count: ${c}`;
                },
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: u.xAxisTitle != null ? u.xAxisTitle : "Value", color: cssVar("--chart-axis-title") },
          grid: { display: false },
          ticks: { color: cssVar("--chart-tick") },
        },
        y: {
          min: 0,
          title: { display: true, text: u.yAxisTitle != null ? u.yAxisTitle : "Number of trials", color: cssVar("--chart-axis-title") },
          grid: { color: cssVar("--chart-grid") },
          ticks: { color: cssVar("--chart-tick") },
        },
      },
    };
  }

  global.ChartView = {
    // Line family (slice 5)
    lineChart,
    tooltipRows,
    // Fan family (slice 6)
    fanDatasets,
    refLineDataset,
    fanChartOptions,
    fanChart,
    alignMoneyRows,
    // Histogram family (slice 7)
    histogram,
    histogramOptions,
    // Exposed for tests; not a stable public API.
    _binIndexForX: binIndexForX,
  };
})(window);
