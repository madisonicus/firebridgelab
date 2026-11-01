// test_chart_explainers.mjs — pin the chart-explainers engine (web/app.js
// ChartExplainers) for the ctx1 slice.
//
// ChartExplainers is the pure, DOM-free logic behind the "How to read this chart ▾"
// disclosures: it holds the APPROVED how-to-read paragraphs VERBATIM, selects the
// MAIN-chart paragraph (comparing wins over sim-mode; overlay/side-by-side vs
// deterministic/historical/monte_carlo), and formats the humanized key-parameters
// digest (Starting portfolio · Spending · Allocation · Sourcing · Guaranteed income ·
// Horizon · View) for a single scenario and for an A/B pair.
//
// Loaded the same way as test_glide_presets.mjs: app.js is read off disk and run in a
// node:vm sandbox with a `window` stub and NO `document`, so init() is skipped (the
// `typeof document` guard) and only the pure ChartExplainers off `window` is
// exercised. The collapsed-by-default invariant is checked against the index.html
// markup (the <details class="chart-explainer"> must carry no `open` attribute). Zero
// npm deps; Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_chart_explainers.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js", "scenario_store.js", "ss_claim.js"].map((f) =>
  path.join(WEB_DIR, f),
);
const APP_PATH = path.join(WEB_DIR, "app.js");
const INDEX_PATH = path.join(WEB_DIR, "index.html");

// Load app.js into a sandbox with a window stub and no document, so init() is skipped
// and we get the pure ChartExplainers off `window`.
function loadExplainers() {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window.ChartExplainers;
}

const CE = loadExplainers();

// TEST-LOCAL class menu (id/label/is_equity), the way the live ASSET_CLASSES holds
// it (sourced from /api/classes). The default 60/40 pair + a couple of extra
// classes so the multi-class allocation path is exercised too.
const CLASSES = [
  { id: "us_total_market", label: "US Total Stock Market", is_equity: true },
  { id: "us_agg_bonds", label: "US Bonds (10-yr Treasury proxy)", is_equity: false },
  { id: "intl_developed", label: "International Developed", is_equity: true },
  { id: "tips", label: "TIPS", is_equity: false },
];

// A representative single scenario (the default shape): $1.8M / $110k constant,
// 60/40, single sourcing, GI (pensions at 60/65, SS at 67/70), 50→95, deterministic.
function baseScenario(overrides = {}) {
  return Object.assign(
    {
      initial_portfolio: 1_800_000,
      annual_spending: 110_000,
      start_age: 50,
      end_age: 95,
      person_a_pension_amount: 24_000,
      person_a_pension_start_age: 60,
      person_a_ss_amount: 33_000,
      person_a_ss_start_age: 67,
      person_b_pension_amount: 9_000,
      person_b_pension_start_age: 65,
      person_b_ss_amount: 26_000,
      person_b_ss_start_age: 70,
      allocationBody: [
        { id: "us_total_market", weight: 0.6, real_return: 0.07, volatility: 0.174 },
        { id: "us_agg_bonds", weight: 0.4, real_return: 0.02, volatility: 0.088 },
      ],
    },
    overrides,
  );
}

const baseState = (overrides = {}) =>
  Object.assign(
    {
      simMode: "deterministic",
      spendingMode: "constant",
      allocationMode: "constant",
      sourcingMode: "single",
      assetClasses: CLASSES,
    },
    overrides,
  );

// ---------------------------------------------------------------------------
// Paragraph SELECTION — the full matrix (simMode × comparing/outputMode).
// ---------------------------------------------------------------------------

test("verbatim paragraphs are all present", () => {
  for (const key of [
    "deterministic",
    "historical",
    "monte_carlo",
    "overlay",
    "side",
    "buckets",
    "guardrails",
  ]) {
    assert.equal(typeof CE.PARAGRAPHS[key], "string");
    assert.ok(CE.PARAGRAPHS[key].length > 40, `${key} paragraph is non-trivial`);
  }
});

test("not comparing → sim-mode selects the paragraph", () => {
  for (const sim of ["deterministic", "historical", "monte_carlo"]) {
    const key = CE.selectMainParagraphKey({
      comparing: false,
      outputMode: "single",
      simMode: sim,
    });
    assert.equal(key, sim, `simMode ${sim} → ${sim} text`);
  }
});

test("comparing + overlay → overlay text, regardless of sim-mode", () => {
  for (const sim of ["deterministic", "historical", "monte_carlo"]) {
    const key = CE.selectMainParagraphKey({
      comparing: true,
      outputMode: "overlay",
      simMode: sim,
    });
    assert.equal(key, "overlay", `comparing+overlay (${sim}) → overlay`);
  }
});

test("comparing + side-by-side → side text, regardless of sim-mode", () => {
  for (const sim of ["deterministic", "historical", "monte_carlo"]) {
    const key = CE.selectMainParagraphKey({
      comparing: true,
      outputMode: "side",
      simMode: sim,
    });
    assert.equal(key, "side", `comparing+side (${sim}) → side`);
  }
});

test("comparing but focus-collapsed to single → falls through to sim-mode text", () => {
  // A focus collapse leaves one panel on screen; the single-scenario read is honest.
  assert.equal(
    CE.selectMainParagraphKey({ comparing: true, outputMode: "single", simMode: "historical" }),
    "historical",
  );
  assert.equal(
    CE.selectMainParagraphKey({ comparing: true, outputMode: "single", simMode: "deterministic" }),
    "deterministic",
  );
});

// Risk-callout: a comparing+buckets state shows the OVERLAY paragraph on the MAIN
// chart AND the BUCKET paragraph on the bucket chart (the bucket explainer keys off
// sourcing independently). The bucket/spending paragraphs are static keys (no
// selection), so they coexist with any main-chart selection without conflict.
test("comparing+buckets: main shows overlay, bucket chart shows the bucket paragraph", () => {
  const mainKey = CE.selectMainParagraphKey({
    comparing: true,
    outputMode: "overlay",
    simMode: "deterministic",
  });
  assert.equal(mainKey, "overlay");
  // The bucket-chart explainer always uses the bucket paragraph (keyed off sourcing,
  // which the live render reflects by only showing #bucket-chart-view in buckets mode).
  assert.equal(CE.PARAGRAPHS.buckets.startsWith("Your portfolio split into three"), true);
});

test("comparing+guardrails: main shows side, spending fan shows the guardrails paragraph", () => {
  const mainKey = CE.selectMainParagraphKey({
    comparing: true,
    outputMode: "side",
    simMode: "monte_carlo",
  });
  assert.equal(mainKey, "side");
  assert.equal(CE.PARAGRAPHS.guardrails.startsWith("How much you actually spend"), true);
});

// ---------------------------------------------------------------------------
// Key-parameters FORMATTER — single scenario.
// ---------------------------------------------------------------------------

function bulletMap(bullets) {
  const m = {};
  for (const b of bullets) m[b.label] = b.value;
  return m;
}

test("single scenario: correct bullet set + order", () => {
  const bullets = CE.formatScenarioParams(baseScenario(), baseState());
  // Re-wrap through the test realm: arrays from the node:vm sandbox carry that
  // realm's Array prototype, so a raw deepEqual sees a different type.
  assert.deepEqual(
    Array.from(bullets).map((b) => b.label),
    [
      "Starting portfolio",
      "Spending",
      "Allocation",
      "Sourcing",
      "Guaranteed income",
      "Horizon",
      "View",
    ],
  );
});

test("single scenario: humanized values (portfolio, spending, allocation, GI, horizon, view)", () => {
  const m = bulletMap(CE.formatScenarioParams(baseScenario(), baseState()));
  assert.equal(m["Starting portfolio"], "$1.8M");
  assert.equal(m["Spending"], "$110k/yr, constant-real");
  // 2-class equity/bond → tightened "US stocks" / "US bonds".
  assert.equal(m["Allocation"], "60% US stocks / 40% US bonds");
  assert.equal(m["Sourcing"], "single portfolio");
  // Total GI = 24k + 9k + 33k + 26k = 92k/yr; fully on at the latest start (70);
  // pensions earliest 60, SS earliest 67.
  assert.equal(m["Guaranteed income"], "~$92k/yr by 70 (pensions at 60, SS at 67)");
  assert.equal(m["Horizon"], "50 → 95");
  assert.equal(m["View"], "Deterministic");
});

// ---------------------------------------------------------------------------
// Tax-location "Accounts" bullet + the federal-tax-overlay bullet (federal-tax
// overlay design ft1, item 6) that rides alongside it. Both are gated on the
// SAME signal (a split on the scenario) and are absent together when off.
// ---------------------------------------------------------------------------

test("no tax-location split: neither Accounts nor Federal tax overlay appear", () => {
  const m = bulletMap(CE.formatScenarioParams(baseScenario(), baseState()));
  assert.equal("Accounts" in m, false);
  assert.equal("Federal tax overlay" in m, false);
});

test("tax-location split active: Accounts + Federal tax overlay both appear, in order, after Sourcing", () => {
  const scen = baseScenario({
    taxable_balance: 1_500_000,
    tax_deferred_balance: 700_000,
    roth_balance: 300_000,
    taxable_gains_fraction: 0.5,
  });
  const bullets = CE.formatScenarioParams(scen, baseState());
  const labels = Array.from(bullets).map((b) => b.label);
  assert.deepEqual(labels, [
    "Starting portfolio",
    "Spending",
    "Allocation",
    "Sourcing",
    "Accounts",
    "Federal tax overlay",
    "Guaranteed income",
    "Horizon",
    "View",
  ]);
  const m = bulletMap(bullets);
  assert.equal(m["Accounts"], "$1.5M taxable / $700k tax-deferred / $300k Roth");
  assert.equal(m["Federal tax overlay"], "50% taxable-gains fraction — federal overlay on");
});

test("federal tax overlay bullet reflects a non-default gains fraction", () => {
  const scen = baseScenario({
    taxable_balance: 1_000_000,
    tax_deferred_balance: 500_000,
    roth_balance: 0,
    taxable_gains_fraction: 0.65,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(m["Federal tax overlay"], "65% taxable-gains fraction — federal overlay on");
});

test("federal tax overlay bullet falls back to 50% when the fraction is missing (split active)", () => {
  // taxable_gains_fraction omitted -> the digest still shows the overlay is on,
  // defaulting the displayed knob to 50% (mirrors the engine/API default).
  const scen = baseScenario({
    taxable_balance: 1_000_000,
    tax_deferred_balance: 500_000,
    roth_balance: 0,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(m["Federal tax overlay"], "50% taxable-gains fraction — federal overlay on");
});

test("summarizeFederalTaxOverlay: null when the split is off, even with a stray gains fraction", () => {
  // taxable_gains_fraction always rides the params (per readFormState's gate,
  // it's only ATTACHED when active) — but if it somehow arrived without a
  // split, the bullet must still be gated on the split, not the fraction.
  const scen = baseScenario({ taxable_gains_fraction: 0.7 });
  assert.equal(CE.summarizeFederalTaxOverlay(scen), null);
});

test("federal tax overlay bullet gains the lever clause when bracket_fill_target is active (tax-drag td1)", () => {
  const scen = baseScenario({
    taxable_balance: 1_500_000,
    tax_deferred_balance: 700_000,
    roth_balance: 300_000,
    taxable_gains_fraction: 0.5,
    bracket_fill_target: 100_800,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(
    m["Federal tax overlay"],
    "50% taxable-gains fraction — federal overlay on, bracket-fill lever to $100.8k",
  );
});

test("federal tax overlay bullet omits the lever clause when bracket_fill_target is absent (lever off)", () => {
  const scen = baseScenario({
    taxable_balance: 1_500_000,
    tax_deferred_balance: 700_000,
    roth_balance: 300_000,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(m["Federal tax overlay"], "50% taxable-gains fraction — federal overlay on");
  assert.doesNotMatch(m["Federal tax overlay"], /bracket-fill/);
});

test("spending strategy labels: lifecycle + guardrails (with floor/ceiling)", () => {
  const life = bulletMap(
    CE.formatScenarioParams(baseScenario(), baseState({ spendingMode: "lifecycle" })),
  );
  assert.equal(life["Spending"], "$110k/yr, lifecycle");

  const guard = bulletMap(
    CE.formatScenarioParams(
      baseScenario({ spending_floor: 85_000, spending_ceiling: 150_000 }),
      baseState({ spendingMode: "guardrails" }),
    ),
  );
  assert.equal(guard["Spending"], "$110k/yr, guardrails (floor $85k, ceiling $150k)");

  // fr_smooth (FR slice 2): the funded-ratio smooth label, with floor/ceiling.
  const frSmooth = bulletMap(
    CE.formatScenarioParams(
      baseScenario({ spending_floor: 85_000, spending_ceiling: 150_000 }),
      baseState({ spendingMode: "fr_smooth" }),
    ),
  );
  assert.equal(
    frSmooth["Spending"],
    "$110k/yr, funded ratio – smooth (floor $85k, ceiling $150k)",
  );
});

test("View label tracks simMode", () => {
  for (const [sim, label] of [
    ["deterministic", "Deterministic"],
    ["historical", "Historical"],
    ["monte_carlo", "Monte Carlo"],
  ]) {
    const m = bulletMap(CE.formatScenarioParams(baseScenario(), baseState({ simMode: sim })));
    assert.equal(m["View"], label);
  }
});

test("allocation: glidepath summarized as ~start→end% equity", () => {
  const scen = baseScenario({
    phase_allocations: [
      // phase 0: 30% equity (us_total_market) / 70% bonds
      [
        { id: "us_total_market", weight: 0.3 },
        { id: "us_agg_bonds", weight: 0.7 },
      ],
      // phase 1: 50/50
      [
        { id: "us_total_market", weight: 0.5 },
        { id: "us_agg_bonds", weight: 0.5 },
      ],
      // phase 2: 70% equity / 30% bonds
      [
        { id: "us_total_market", weight: 0.7 },
        { id: "us_agg_bonds", weight: 0.3 },
      ],
    ],
  });
  const m = bulletMap(
    CE.formatScenarioParams(scen, baseState({ allocationMode: "glidepath" })),
  );
  assert.equal(m["Allocation"], "Glidepath, ~30→70% equity across phases");
});

test("allocation: multi-class (non 2-class) lists the non-zero classes by label", () => {
  const scen = baseScenario({
    allocationBody: [
      { id: "us_total_market", weight: 0.5 },
      { id: "intl_developed", weight: 0.2 },
      { id: "us_agg_bonds", weight: 0.3 },
    ],
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(
    m["Allocation"],
    "50% US Total Stock Market / 20% International Developed / 30% US Bonds (10-yr Treasury proxy)",
  );
});

test("sourcing: buckets summarized with cash/income years", () => {
  const scen = baseScenario({ buckets: { n1: 2, n2: 7 } });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState({ sourcingMode: "buckets" })));
  assert.equal(m["Sourcing"], "buckets (cash 2y / income 7y)");
});

test("GI: none when no streams are set", () => {
  const scen = baseScenario({
    person_a_pension_amount: 0,
    person_a_ss_amount: 0,
    person_b_pension_amount: 0,
    person_b_ss_amount: 0,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(m["Guaranteed income"], "none");
});

test("GI: pensions-only omits the SS clause", () => {
  const scen = baseScenario({
    person_a_pension_amount: 0,
    person_a_ss_amount: 0,
    person_b_pension_amount: 45_000,
    person_b_pension_start_age: 65,
    person_b_ss_amount: 0,
  });
  const m = bulletMap(CE.formatScenarioParams(scen, baseState()));
  assert.equal(m["Guaranteed income"], "~$45k/yr by 65 (pensions at 65)");
});

// ---------------------------------------------------------------------------
// Key-parameters FORMATTER — A/B pair (each list complete + independent).
// ---------------------------------------------------------------------------

test("A/B pair: each side formatted independently + completely", () => {
  const a = baseScenario(); // $1.8M, 60/40
  const b = baseScenario({
    initial_portfolio: 3_000_000,
    allocationBody: [
      { id: "us_total_market", weight: 0.8 },
      { id: "us_agg_bonds", weight: 0.2 },
    ],
  });
  const aB = bulletMap(CE.formatScenarioParams(a, baseState()));
  const bB = bulletMap(CE.formatScenarioParams(b, baseState()));

  assert.equal(aB["Starting portfolio"], "$1.8M");
  assert.equal(aB["Allocation"], "60% US stocks / 40% US bonds");

  assert.equal(bB["Starting portfolio"], "$3M");
  assert.equal(bB["Allocation"], "80% US stocks / 20% US bonds");

  // Shared fields stay identical across the two complete lists.
  assert.equal(aB["Horizon"], bB["Horizon"]);
  assert.equal(aB["Guaranteed income"], bB["Guaranteed income"]);
});

// ---------------------------------------------------------------------------
// Portfolio formatting edge cases.
// ---------------------------------------------------------------------------

test("portfolio formatter: M / k / plain dollar thresholds", () => {
  assert.equal(CE.fmtPortfolio(1_800_000), "$1.8M");
  assert.equal(CE.fmtPortfolio(3_000_000), "$3M");
  assert.equal(CE.fmtPortfolio(950_000), "$950k");
  assert.equal(CE.fmtPortfolio(1_250_000), "$1.25M");
  assert.equal(CE.fmtPortfolio(0), "$0");
});

// ---------------------------------------------------------------------------
// Collapsed-by-default — markup invariant.
// ---------------------------------------------------------------------------

test("all chart-explainer disclosures are collapsed by default (no `open`)", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  // Every <details class="chart-explainer" ...> must NOT carry the `open` attribute.
  // tb1 added a fourth chart-explainer (the tax-location chart's); dsc1 added a fifth
  // (the deterministic guardrails spending line's); ft1 adds a sixth (the federal-tax
  // overlay's own "how to read"), so there are now six: main, bucket, spending,
  // tax-location, det-spending, federal-tax.
  const re = /<details\b[^>]*class="chart-explainer"[^>]*>/g;
  const matches = html.match(re) || [];
  assert.equal(matches.length, 6, "exactly six chart-explainer disclosures");
  for (const tag of matches) {
    assert.ok(!/\bopen\b/.test(tag), `expander is collapsed by default: ${tag}`);
  }
});

test("the explainer text targets exist in the markup", () => {
  const html = fs.readFileSync(INDEX_PATH, "utf8");
  for (const id of [
    "explainer-main-text",
    "explainer-bucket-text",
    "explainer-spending-text",
    "explainer-tax-location-text", // tb1
    "explainer-det-spending-text", // dsc1
    "explainer-federal-tax-text", // ft1
  ]) {
    assert.ok(html.includes(`id="${id}"`), `${id} present`);
  }
  assert.ok(html.includes('id="explainer-main-params"'), "main params container present");
});

// ctx1b regression: the initial-load path must populate the explainer. init() seeds
// the first chart via renderSingle()/update* WITHOUT routing through rerender(), so
// renderChartExplainers() (wired into rerender() + renderFromCache()) is only invoked
// on first paint if init() also calls it. The full init→DOM seam is DOM-integration
// (covered by Playwright, not this DOM-free vm sandbox), so this is a STATIC check:
// the init() body in the source must contain a renderChartExplainers() call. Pins the
// wiring against the exact regression that left #explainer-main empty until the user's
// first mode change.
test("renderChartExplainers() is wired into init() (fresh-load population)", () => {
  const src = fs.readFileSync(APP_PATH, "utf8");
  const start = src.indexOf("async function init()");
  assert.ok(start !== -1, "init() found in app.js");
  // Bound the init() body by the NEXT top-level function declaration. (Char-by-char
  // brace matching is unreliable here because braces appear inside comments/strings
  // in the body, which would overshoot into rerender()/renderFromCache() — both of
  // which also call renderChartExplainers() and would mask a missing init wiring.)
  // The init() body ends just before `function prefetchScenarioList()`.
  const rest = src.slice(start + "async function init()".length);
  const nextFn = rest.search(/\n(?:async )?function \w+\s*\(/);
  assert.ok(nextFn !== -1, "next function after init() found");
  // Strip line comments so a mention of the call in a comment can't satisfy the
  // assertion — only an actual statement counts.
  const body = rest
    .slice(0, nextFn)
    .split("\n")
    .map((line) => line.replace(/\/\/.*$/, ""))
    .join("\n");
  assert.ok(
    /\brenderChartExplainers\s*\(\s*\)\s*;/.test(body),
    "init() calls renderChartExplainers() so #explainer-main populates on fresh load",
  );
});
