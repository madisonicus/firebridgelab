// test_bucket_view.mjs — pin the client bucket constants (web/app.js BucketView)
// against the engine's default bucket config + payload contract.
//
// The bucket TARGETS (cash/income year sizing) are derived ENGINE-side and ride in
// the payload; the client never recomputes them, so there's no target math to pin.
// What CAN silently drift is the client's seed mapping — which 100%-weight class
// each bucket defaults to — and the cash → income → growth id order, both of which
// must match firebridgelab.default_bucket_config (tests/test_buckets.py) and the
// worst-cohort / bucket_balances payload order. This test HARDCODES the engine's
// expected values and asserts the JS BucketView reproduces them.
//
// Loaded the same way as test_glidepath_view.mjs: app.js is read off disk and run
// inside a node:vm sandbox with a `window` stub and NO `document`, so init() is
// skipped and only the pure BucketView (attached to window) is exercised. Zero npm
// deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_bucket_view.mjs

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

function loadAppWindow() {
  const ctx = { console: { ...console } };
  // In a browser `window` IS the global object; mirror that so app.js's bare
  // `ChartView` / `ComparisonState` references + its `window.X = ...` exports work.
  ctx.window = ctx;
  ctx.Chart = function (canvasCtx, config) {
    this.config = config;
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  return ctx.window;
}

function loadBucketView() {
  return loadAppWindow().BucketView;
}

test("BucketView is exposed and init() is skipped without a DOM", () => {
  const BucketView = loadBucketView();
  assert.equal(typeof BucketView, "object");
  assert.ok(Array.isArray(BucketView.ids));
  assert.equal(typeof BucketView.defaultClass, "object");
  assert.equal(typeof BucketView.labels, "object");
});

// The BucketView objects are built inside the vm sandbox, so their Array/Object
// prototypes differ from this realm's — deepStrictEqual's reference-equal prototype
// check then fails on structurally-identical values. Normalize through JSON (a
// plain-data round-trip) before comparing, exactly the cross-realm concern the
// glidepath test sidesteps with Array.from.
const plain = (x) => JSON.parse(JSON.stringify(x));

test("bucket id order is cash → income → growth (the engine + payload order)", () => {
  const BucketView = loadBucketView();
  // Mirrors firebridgelab.scenario.BucketConfig field order + the BucketYear/payload
  // cash/income/growth order the worst-cohort + bucket_balances series carry.
  assert.deepEqual(plain(BucketView.ids), ["cash", "income", "growth"]);
});

test("default 100%-weight class per bucket matches firebridgelab.default_bucket_config", () => {
  const BucketView = loadBucketView();
  // Hardcoded engine expectation (firebridgelab.scenario.default_bucket_config /
  // tests/test_buckets.py): cash 100% st_treasury, income 100% us_agg_bonds,
  // growth 100% us_total_market. A drift here would seed the client matrices to a
  // different portfolio than the engine's documented default.
  assert.deepEqual(plain(BucketView.defaultClass), {
    cash: "st_treasury",
    income: "us_agg_bonds",
    growth: "us_total_market",
  });
});

test("bucket display labels match the engine's bucket names", () => {
  const BucketView = loadBucketView();
  // firebridgelab.scenario.default_bucket_config names: Cash / Income / Growth.
  assert.deepEqual(plain(BucketView.labels), {
    cash: "Cash",
    income: "Income",
    growth: "Growth",
  });
});

// ---------- comparison-window alignment (option 3, WindowAlign) ----------

// A fixed class-id → data_start_year map for the window-alignment unit tests.
// Mirrors the real menu's spans (us_equity/us_bonds 1871+, it_treasury 1954+,
// us_reit 1972+) so the floor math reads like production without loading data.
const STARTS = {
  us_total_market: 1871,
  us_agg_bonds: 1871,
  it_treasury: 1954,
  us_reit: 1972,
};

test("scenarioClassIds collects ids across single / glidepath / buckets shapes", () => {
  const { WindowAlign } = loadAppWindow();
  // plain() normalizes the cross-realm array (sandbox prototype) before compare.
  // single
  assert.deepEqual(
    plain(WindowAlign.scenarioClassIds({
      allocationBody: [{ id: "us_total_market" }, { id: "us_agg_bonds" }],
    })).sort(),
    ["us_agg_bonds", "us_total_market"],
  );
  // buckets — union across the three bucket allocations
  assert.deepEqual(
    plain(WindowAlign.scenarioClassIds({
      sourcing_mode: "buckets",
      buckets: {
        cash: { allocation: [{ id: "us_agg_bonds" }] },
        income: { allocation: [{ id: "it_treasury" }] },
        growth: { allocation: [{ id: "us_total_market" }] },
      },
    })).sort(),
    ["it_treasury", "us_agg_bonds", "us_total_market"],
  );
});

test("scenarioTestableSince = the latest data_start_year across used classes", () => {
  const { WindowAlign } = loadAppWindow();
  // Default 60/40 (both 1871) → 1871.
  assert.equal(
    WindowAlign.scenarioTestableSince(
      { allocationBody: [{ id: "us_total_market" }, { id: "us_agg_bonds" }] },
      STARTS,
    ),
    1871,
  );
  // it_treasury (1954) drags the window forward → 1954.
  assert.equal(
    WindowAlign.scenarioTestableSince(
      { allocationBody: [{ id: "us_total_market" }, { id: "it_treasury" }] },
      STARTS,
    ),
    1954,
  );
  // No known class data → null (caller leaves the comparison unclamped).
  assert.equal(
    WindowAlign.scenarioTestableSince({ allocationBody: [{ id: "mystery" }] }, STARTS),
    null,
  );
});

test("commonComparisonFloor: max of the two native windows when they differ", () => {
  const { WindowAlign } = loadAppWindow();
  const a = { allocationBody: [{ id: "us_total_market" }, { id: "us_agg_bonds" }] }; // 1871
  const b = { allocationBody: [{ id: "us_total_market" }, { id: "it_treasury" }] }; // 1954
  // A (1871) vs B (1954) → clamp BOTH to 1954.
  assert.equal(WindowAlign.commonComparisonFloor(a, b, STARTS), 1954);
  assert.equal(WindowAlign.commonComparisonFloor(b, a, STARTS), 1954);
});

test("commonComparisonFloor: null (no clamp) when windows match or are unknown", () => {
  const { WindowAlign } = loadAppWindow();
  const a = { allocationBody: [{ id: "us_total_market" }, { id: "us_agg_bonds" }] }; // 1871
  // Same native window on both → no clamp needed.
  assert.equal(WindowAlign.commonComparisonFloor(a, a, STARTS), null);
  // One side's window unknown → don't guess, leave native.
  const unknown = { allocationBody: [{ id: "mystery" }] };
  assert.equal(WindowAlign.commonComparisonFloor(a, unknown, STARTS), null);
});
