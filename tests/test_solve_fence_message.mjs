// test_solve_fence_message.mjs — pin that a typed engine fence (BucketsNotSolvable
// today; the tax-drag design td1 MC+split solve fence — firebridgelab/solver.py's
// "max-spend solve is not yet available with tax drag in Monte Carlo mode" —
// tomorrow) surfaces through the web layer as a normal user-facing notice, never
// a raw HTTP/error dump.
//
// Two pieces, both pre-existing (not new to td-d) and both exercised here for
// the first time:
//   - postJson(url, params): on a non-2xx response, extracts the API's
//     friendly `detail` string from the JSON body and throws an Error carrying
//     it VERBATIM — never "HTTP 422" or a stack trace — so long as the body
//     parses and carries `detail`.
//   - setSolveReadout(text, { isError }): writes that message into
//     #solve-readout with the error style, and (isError) hides the "About this
//     number" caption — the SAME rendering path BucketsNotSolvable's message
//     uses today, so a future MC+split fence 422 renders identically, with no
//     new code needed in onSolve's existing catch block.
//
// onSolve() itself is NOT reachable from the UI for the MC+split fence
// specifically (the Solve button is disabled in Monte Carlo mode regardless of
// split — SOLVE_MC_TITLE — so that particular 422 can only come from a direct
// API call, never this button). This test isolates the two mechanisms onSolve's
// existing `catch (err) { setSolveReadout(err.message, { isError: true }); }`
// already composes, using the fence's own copy as the payload, rather than
// reconstructing onSolve's full form-reading dependency graph (which has no
// pre-existing test harness — out of scope to build from scratch here).
//
// Run:
//   (from the repo root) node --test tests/test_solve_fence_message.mjs

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

const FENCE_DETAIL = "max-spend solve is not yet available with tax drag in Monte Carlo mode";

function makeElement() {
  return { hidden: false, textContent: "", classList: { toggle() {} } };
}

// Load app.js with NO document (init() skipped), a stubbed `fetch` returning a
// 422 with the fence's detail, then attach a minimal document (readout +
// caption elements) afterward.
function loadCtx({ status = 422, body = { detail: FENCE_DETAIL } } = {}) {
  const ctx = { console: { ...console, warn() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) {
    vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  }
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  const readout = makeElement();
  const caption = makeElement();
  const byId = { "solve-readout": readout, "solve-caption": caption };
  ctx.document = {
    documentElement: {},
    getElementById: (id) => byId[id] || null,
  };
  return { ctx, readout, caption };
}

test("postJson: a 422 with a fence detail throws an Error carrying that detail VERBATIM", async () => {
  const { ctx } = loadCtx();
  ctx.URL_UNDER_TEST = "/api/solve";
  let caught = null;
  try {
    await vm.runInContext('postJson(URL_UNDER_TEST, { mode: "monte_carlo" })', ctx);
  } catch (err) {
    caught = err;
  }
  assert.ok(caught, "postJson must throw on a non-2xx response");
  assert.equal(caught.message, FENCE_DETAIL);
  // Never a raw HTTP dump when a friendly detail is available.
  assert.doesNotMatch(caught.message, /HTTP 422/);
});

test("postJson: a non-2xx response WITHOUT a parseable detail falls back to a generic HTTP message (never silently empty)", async () => {
  const { ctx } = loadCtx({ status: 500, body: {} });
  ctx.URL_UNDER_TEST = "/api/solve";
  let caught = null;
  try {
    await vm.runInContext('postJson(URL_UNDER_TEST, {})', ctx);
  } catch (err) {
    caught = err;
  }
  assert.ok(caught);
  assert.equal(caught.message, "HTTP 500");
});

test("setSolveReadout: renders the fence detail as a plain, styled-error notice — not a raw dump", () => {
  const { ctx, readout, caption } = loadCtx();
  vm.runInContext(`setSolveReadout(${JSON.stringify(FENCE_DETAIL)}, { isError: true })`, ctx);
  assert.equal(readout.textContent, FENCE_DETAIL);
  assert.equal(readout.hidden, false, "the notice is shown, not swallowed");
  // The "About this number" caption only makes sense alongside a successful
  // solve — hidden on the error path (matches BucketsNotSolvable's presentation).
  assert.equal(caption.hidden, true);
});

test("setSolveReadout -> postJson end-to-end: the SAME idiom onSolve's catch block already uses", async () => {
  // Mirrors onSolve()'s existing `catch (err) { setSolveReadout(err.message,
  // { isError: true }); }` — composing the two pieces above is exactly what
  // happens when any typed engine fence (BucketsNotSolvable today, the td1
  // MC+split fence tomorrow) reaches the API as a 422.
  const { ctx, readout, caption } = loadCtx();
  ctx.URL_UNDER_TEST = "/api/solve";
  await vm.runInContext(`
    (async () => {
      try {
        await postJson(URL_UNDER_TEST, { mode: "monte_carlo" });
      } catch (err) {
        setSolveReadout(err.message, { isError: true });
      }
    })()
  `, ctx);
  assert.equal(readout.textContent, FENCE_DETAIL);
  assert.equal(caption.hidden, true);
});
