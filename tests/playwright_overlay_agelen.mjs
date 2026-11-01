// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Local Playwright check — overlay age-length limitation.
//
// Bug: in the deterministic comparison OVERLAY chart (one Chart.js instance,
// two lines A & B), the x-axis age labels were built from only ONE scenario's
// age range. When start_age/end_age are UNLINKED and Scenario B has a LONGER
// horizon (B.end_age > A.end_age), B's trailing years fell off the overlay
// x-axis — clipped — even though the summary + drill-down carried the full
// horizon. Fix: the overlay x-axis now spans the UNION of both scenarios'
// ages (unionAges/alignPointsByAge in web/app.js, used by renderOverlayChart),
// and each line is aligned BY AGE onto that shared axis.
//
//
// HOW THE SCENARIO IS SET UP (the fix for the previously-broken gate):
//   The End-age field row — its per-field `.link-toggle` button AND the B input
//   `#end_age-b` — is JS-rendered INSIDE two nested collapsed <details>
//   (#horizon-fields → #advanced → #strategy-group), so the toggle button is
//   not clickable in the page's default state (the old gate clicked a HIDDEN
//   element and timed out). Instead we drive the app's OWN public globals (app.js
//   is a classic script, so its top-level function declarations are window
//   properties):
//     - window.toggleLink("end_age")  -> flips end_age via the real
//       ComparisonState transition; the subscriber chain synchronously re-renders
//       the field row (creating #end_age-b) and the chrome, then kicks off a
//       background rerender. This is the EXACT path the per-field toggle button's
//       click handler invokes — no hidden-element clicking, no <details> juggling.
//     - set #end_age (A) and #end_age-b (B) values + dispatch input events.
//     - window.switchView("overlay") -> selects the overlay view (it is the
//       default compare view; this is an explicit no-op-safe assert).
//     - await window.rerender() -> the same async render onRecompute awaits;
//       fetches both scenarios and (re)builds the overlay chart. Awaiting it (vs a
//       fixed timeout) makes the read deterministic and reads the just-set B value.
//
// Asserts, at viewport 1280x1000, Deterministic mode (default), Overlay view:
//   1. Unlink the End age field, set A.end_age=85 and B.end_age=100, recompute.
//   2. The overlay chart renders with both lines.
//   3. The overlay x-axis labels (ages) extend to B's max age (100) — i.e. B's
//      trailing years (86..100) are PRESENT, not clipped. B carries a non-null
//      value at age 100; A is null beyond its own horizon (drawn as a gap, not
//      stretched) and carries a real value at its own final age 85.

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const OUT = process.env.SHOT_DIR || "/tmp/overlay_agelen_shots";

import { mkdirSync } from "node:fs";
mkdirSync(OUT, { recursive: true });

const A_END = 85;
const B_END = 100;

const errors = [];

const browser = await chromium.launch({
  executablePath: undefined,
  headless: true,
});
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await ctx.newPage();

page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
});
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

function fail(msg) {
  console.log(`FAIL: ${msg}`);
  process.exitCode = 1;
}

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });

// The app exposes its top-level functions as window globals (classic script).
// Wait for the inputs to be built + the comparison wiring to be live before we
// drive it.
await page.waitForFunction(
  () =>
    typeof window.toggleLink === "function" &&
    typeof window.switchView === "function" &&
    typeof window.rerender === "function" &&
    document.getElementById("end_age") !== null,
  { timeout: 15000 },
);

// --- 1. Enter comparison by UNLINKING end_age, then set A=85 / B=100 ---
// Drive the app's own state machine + render path (NOT hidden DOM clicks):
//   toggleLink("end_age") -> ComparisonState.setFieldLink(end_age,false) -> the
//   subscriber synchronously re-renders the row (creating #end_age-b) and chrome.
// Then write both inputs and run the SAME async render onRecompute awaits.
const setup = await page.evaluate(
  async ({ aEnd, bEnd }) => {
    // Unlink end_age via the real toggle handler (idempotent: only flip if linked).
    // We don't have compareState on window (it's a const), so probe link state by
    // the presence of the B input after toggling.
    if (!document.getElementById("end_age-b")) {
      window.toggleLink("end_age");
    }
    const a = document.getElementById("end_age");
    const b = document.getElementById("end_age-b");
    if (!a || !b) return { ok: false, reason: "end_age / end_age-b inputs missing after unlink" };

    a.value = String(aEnd);
    a.dispatchEvent(new Event("input", { bubbles: true }));
    b.value = String(bEnd);
    b.dispatchEvent(new Event("input", { bubbles: true }));

    // Select the overlay view (default compare view; safe no-op if already overlay).
    window.switchView("overlay");

    // Run the canonical render path and await it (same path onRecompute awaits).
    // Returns { payloadA, payloadB } or null on a validation bail.
    const result = await window.rerender();
    return {
      ok: true,
      a: a.value,
      b: b.value,
      rerenderNull: result === null,
    };
  },
  { aEnd: A_END, bEnd: B_END },
);
console.log(`setup:`, JSON.stringify(setup));
if (!setup || !setup.ok) {
  fail(`scenario setup failed: ${(setup && setup.reason) || "unknown"}`);
}
if (setup && setup.rerenderNull) {
  fail("rerender() returned null — a validation guard bailed before the overlay render");
}

const formError = await page.textContent("#form-error").catch(() => "");
if ((formError || "").trim() !== "") fail(`recompute surfaced a form error: ${formError}`);

// --- 2 + 3. Read the overlay chart's x-axis labels (ages) + dataset alignment ---
// Chart.js v4 exposes the live instance via Chart.getChart(canvas). Labels are
// the union age axis; each dataset's data is null where its scenario doesn't
// reach that age (Chart.js draws a gap). Coerce labels to numbers (Chart.js may
// store them as numbers or strings depending on the source array).
const chartState = await page.evaluate(() => {
  const canvas = document.getElementById("overlay-chart");
  if (!canvas || typeof Chart === "undefined" || !Chart.getChart) return null;
  const chart = Chart.getChart(canvas);
  if (!chart) return null;
  const labels = chart.data.labels.map((v) => Number(v));
  const ds = chart.data.datasets.map((d) => ({
    label: d.label,
    data: d.data.slice(),
  }));
  return { labels, ds };
});
if (!chartState) {
  fail("overlay chart instance not found (Chart.getChart returned null)");
} else {
  const { labels, ds } = chartState;
  // Robust age->index lookup tolerant of number/string label storage.
  const idxOfAge = (age) => labels.findIndex((l) => Number(l) === Number(age));
  const maxLabel = Math.max(...labels);
  const minLabel = Math.min(...labels);
  console.log(`overlay x-axis labels: min=${minLabel} max=${maxLabel} count=${labels.length}`);

  // (3a) CORE ASSERTION: the union axis must reach B's max age — B's trailing
  // years are present, not clipped to A's shorter horizon.
  if (maxLabel < B_END) {
    fail(`overlay x-axis max age ${maxLabel} < B end_age ${B_END} — B's trailing years are clipped`);
  }

  const aDs = ds.find((d) => /Scenario A/.test(d.label));
  const bDs = ds.find((d) => /Scenario B/.test(d.label));
  if (!aDs || !bDs) {
    fail(`could not identify A/B datasets: ${ds.map((d) => d.label).join(", ")}`);
  } else {
    // (3b) B carries a real (non-null) value at age B_END (its true final year).
    const idxBEnd = idxOfAge(B_END);
    const bAtEnd = idxBEnd >= 0 ? bDs.data[idxBEnd] : null;
    console.log(`Scenario B value at age ${B_END}: ${bAtEnd}`);
    if (idxBEnd < 0 || bAtEnd === null || bAtEnd === undefined) {
      fail(`Scenario B has no data point at its final age ${B_END} (clipped)`);
    }

    // (3c) A is aligned by age, not stretched: A must be null at ages beyond
    // A_END (Chart.js draws a gap), and carry a real value at A_END itself.
    const idxAEnd = idxOfAge(A_END);
    const aAtEnd = idxAEnd >= 0 ? aDs.data[idxAEnd] : null;
    const idxBeyondA = idxOfAge(B_END); // an age > A_END within the union
    const aBeyond = idxBeyondA >= 0 ? aDs.data[idxBeyondA] : "n/a";
    console.log(`Scenario A value at age ${A_END}: ${aAtEnd}; at age ${B_END}: ${aBeyond}`);
    if (idxAEnd < 0 || aAtEnd === null || aAtEnd === undefined) {
      fail(`Scenario A missing a value at its own final age ${A_END}`);
    }
    if (aBeyond !== null && aBeyond !== undefined && aBeyond !== "n/a") {
      fail(`Scenario A was stretched past its horizon: non-null value ${aBeyond} at age ${B_END} (should be a null gap)`);
    }
  }
}

await page.screenshot({ path: `${OUT}/overlay_agelen.png`, fullPage: false });

if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  fail(`${errors.length} console/page error(s)`);
}

console.log(process.exitCode ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
