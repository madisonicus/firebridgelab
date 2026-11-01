// test_comparison_state.mjs — pin the ComparisonState contract (slice 8).
//
// Loaded the same way as test_chart_view.mjs: comparison_state.js is read off
// disk and run inside a node:vm sandbox with a window stub. Zero npm deps;
// requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_comparison_state.mjs
//
// The central invariant under test is ATOMIC NOTIFY: every successful
// transition fires the subscriber EXACTLY ONCE, and the legacy `skipAfter`
// workaround that lived in app.js no longer has a home. The unlinkAll /
// relinkAll batch transitions are the load-bearing case — pre-slice-8 they
// touched multiple link bools through separate calls, then trailed a single
// afterLinkChange() flush; here they're one mutation = one notify.

import { test } from "node:test";
import assert from "node:assert/strict";

// The comparison_state.js source runs inside a vm sandbox with its OWN realm.
// Arrays/Objects it returns have a different `Array` / `Object` constructor
// than the test realm, so node:assert's strict deepEqual rejects them as
// "values have same structure but are not reference-equal". Coerce to a same-
// realm array before asserting.
function arr(x) { return Array.from(x); }
function obj(o) {
  // Shallow same-realm copy of a plain dict.
  const out = {};
  for (const k of Object.keys(o)) out[k] = o[k];
  return out;
}
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CS_PATH = path.resolve(__dirname, "../web/comparison_state.js");

function loadComparisonState() {
  const source = fs.readFileSync(CS_PATH, "utf8");
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx.window.ComparisonState;
}

const FIELDS = [
  "initial_portfolio",
  "annual_spending",
  "start_age",
  "end_age",
];

function newState(opts) {
  const ComparisonState = loadComparisonState();
  return ComparisonState.create(FIELDS.slice(), opts);
}

test("default state: all linked, not comparing, outputMode=single", () => {
  const s = newState();
  assert.equal(s.isComparing(), false);
  assert.equal(s.allocationLinked(), true);
  assert.equal(s.outputMode(), "single");
  assert.equal(s.view(), "overlay");
  assert.equal(s.focusedScenario(), null);
  for (const f of FIELDS) assert.equal(s.isLinked(f), true);
  assert.deepEqual(arr(s.divergingFields()), []);
});

test("setFieldLink false on one field starts a comparison and switches outputMode to overlay", () => {
  const s = newState();
  s.setFieldLink("initial_portfolio", false);
  assert.equal(s.isComparing(), true);
  assert.equal(s.isLinked("initial_portfolio"), false);
  assert.equal(s.isLinked("annual_spending"), true);
  assert.equal(s.outputMode(), "overlay");
  assert.deepEqual(arr(s.divergingFields()), ["initial_portfolio"]);
});

test("setAllocationLinked false makes isComparing true even with all fields linked", () => {
  const s = newState();
  s.setAllocationLinked(false);
  assert.equal(s.isComparing(), true);
  assert.equal(s.allocationLinked(), false);
  assert.deepEqual(arr(s.divergingFields()), []); // allocation is NOT a field
});

test("ATOMIC INVARIANT: one mutation = one notify (the anti-skipAfter rule)", () => {
  const s = newState();
  const notifications = [];
  s.subscribe((snap) => notifications.push(snap));

  // Each of the following transitions is ONE atomic mutation, so each must
  // fire the subscriber exactly once.
  s.setFieldLink("initial_portfolio", false);
  assert.equal(notifications.length, 1);

  s.setFieldLink("annual_spending", false);
  assert.equal(notifications.length, 2);

  s.setAllocationLinked(false);
  assert.equal(notifications.length, 3);

  s.setView("side");
  assert.equal(notifications.length, 4);

  // No-op transitions (already in the requested state) MUST NOT notify.
  s.setFieldLink("initial_portfolio", false); // already false
  assert.equal(notifications.length, 4);

  s.setView("side"); // already side
  assert.equal(notifications.length, 4);
});

test("unlinkAll fires ONE notify (load-bearing — replaces the skipAfter pattern)", () => {
  const s = newState();
  let count = 0;
  s.subscribe(() => count++);
  s.unlinkAll();
  assert.equal(count, 1);
  // Every field + the allocation are now unlinked.
  assert.equal(s.isComparing(), true);
  assert.equal(s.allocationLinked(), false);
  for (const f of FIELDS) assert.equal(s.isLinked(f), false);
});

test("relinkAll fires ONE notify and clears focus", () => {
  const s = newState();
  s.unlinkAll();
  s.focusOn("A");
  assert.equal(s.focusedScenario(), "A");
  let count = 0;
  s.subscribe(() => count++);
  // relinkAll takes no arguments — kept-side identity is the caller's concern.
  s.relinkAll();
  assert.equal(count, 1);
  assert.equal(s.isComparing(), false);
  assert.equal(s.allocationLinked(), true);
  assert.equal(s.focusedScenario(), null);
});

test("enterComparison / exitComparison are atomic and idempotent", () => {
  const s = newState();
  const events = [];
  s.subscribe(() => events.push("notify"));
  s.enterComparison();
  assert.equal(events.length, 1);
  assert.equal(s.isComparing(), true);
  // Idempotent: enterComparison while already comparing is a no-op.
  s.enterComparison();
  assert.equal(events.length, 1);
  s.exitComparison();
  assert.equal(events.length, 2);
  assert.equal(s.isComparing(), false);
  // Idempotent: exitComparison while not comparing is a no-op.
  s.exitComparison();
  assert.equal(events.length, 2);
});

test("focusOn drives outputMode to 'single' while still comparing", () => {
  const s = newState();
  s.setFieldLink("initial_portfolio", false);
  assert.equal(s.outputMode(), "overlay");
  s.focusOn("A");
  assert.equal(s.outputMode(), "single");
  assert.equal(s.focusedScenario(), "A");
  assert.equal(s.isComparing(), true); // we're still comparing, just focused
  s.focusOn(null);
  assert.equal(s.outputMode(), "overlay");
});

test("setView('single') is illegal (use exitComparison instead)", () => {
  const s = newState();
  assert.throws(() => s.setView("single"), /setView\('single'\)/);
});

test("setView('side') while focused on A throws", () => {
  const s = newState();
  s.setFieldLink("initial_portfolio", false);
  s.focusOn("A");
  assert.throws(() => s.setView("side"), /focused/);
  // Clear focus, then it's legal.
  s.focusOn(null);
  s.setView("side");
  assert.equal(s.view(), "side");
  assert.equal(s.outputMode(), "side");
});

test("focusOn while not comparing throws (catches the bug rather than silently no-op'ing)", () => {
  const s = newState();
  assert.throws(() => s.focusOn("A"), /while not comparing/);
});

test("crossing the all-linked boundary clears stale focus", () => {
  const s = newState();
  s.setFieldLink("initial_portfolio", false);
  s.focusOn("B");
  assert.equal(s.focusedScenario(), "B");
  // Re-link the only diverging field => not comparing => focus must clear.
  s.setFieldLink("initial_portfolio", true);
  assert.equal(s.isComparing(), false);
  assert.equal(s.focusedScenario(), null);
});

test("divergingFields lists only unlinked fields, in original order", () => {
  const s = newState();
  s.setFieldLink("annual_spending", false);
  s.setFieldLink("initial_portfolio", false);
  // Original FIELDS order is preserved, regardless of the order they were unlinked.
  assert.deepEqual(arr(s.divergingFields()), ["initial_portfolio", "annual_spending"]);
});

test("subscriber receives a frozen snapshot of public state", () => {
  const s = newState();
  let lastSnap = null;
  s.subscribe((snap) => { lastSnap = snap; });
  s.setFieldLink("initial_portfolio", false);
  assert.ok(Object.isFrozen(lastSnap));
  assert.equal(lastSnap.comparing, true);
  assert.equal(lastSnap.linkState.initial_portfolio, false);
  assert.equal(lastSnap.linkState.annual_spending, true);
  assert.equal(lastSnap.outputMode, "overlay");
  // Mutating the snapshot doesn't affect the source.
  assert.throws(() => { lastSnap.comparing = false; }, TypeError);
});

test("subscriber attempting to mutate state during notify throws (reentrancy guard)", () => {
  const s = newState();
  s.subscribe(() => {
    // Trying to mutate from inside a notify cycle is a programming error.
    s.setFieldLink("annual_spending", false);
  });
  assert.throws(() => s.setFieldLink("initial_portfolio", false), /subscriber attempted to mutate/);
});

test("unknown field name throws on setFieldLink and isLinked", () => {
  const s = newState();
  assert.throws(() => s.setFieldLink("does_not_exist", false), /unknown field/);
  assert.throws(() => s.isLinked("does_not_exist"), /unknown field/);
});

// ---------------------------------------------------------------------------
// _isViewOnlyTransition (slice 8 fix-up): the routing classifier app.js uses
// to decide renderFromCache (cheap) vs rerender (re-fetch). The risk model is
// a future caller that wires focus to the UI AND simultaneously flips a link
// bool — the old loop-and-return-true shape would classify focus-only deltas
// as view-only and could miss the link flip if the loop short-circuited
// wrong. The tightened classifier enumerates legal view-only deltas and
// returns false for any data-changing delta even when paired with a focus
// flip.
// ---------------------------------------------------------------------------

test("_isViewOnlyTransition: pure view (overlay → side) is view-only", () => {
  const ComparisonState = loadComparisonState();
  const prev = {
    allocationLinked: true,
    linkState: { initial_portfolio: true, annual_spending: true, start_age: true, end_age: true },
    view: "overlay",
    focusedScenario: null,
  };
  const next = { ...prev, view: "side" };
  assert.equal(ComparisonState._isViewOnlyTransition(prev, next), true);
});

test("_isViewOnlyTransition: focus null → 'B' is view-only", () => {
  const ComparisonState = loadComparisonState();
  const prev = {
    allocationLinked: true,
    linkState: { initial_portfolio: false, annual_spending: true, start_age: true, end_age: true },
    view: "overlay",
    focusedScenario: null,
  };
  const next = { ...prev, focusedScenario: "B" };
  assert.equal(ComparisonState._isViewOnlyTransition(prev, next), true);
});

test("_isViewOnlyTransition: any field link flip is data-changing", () => {
  const ComparisonState = loadComparisonState();
  const prev = {
    allocationLinked: true,
    linkState: { initial_portfolio: true, annual_spending: true, start_age: true, end_age: true },
    view: "overlay",
    focusedScenario: null,
  };
  const next = {
    ...prev,
    linkState: { ...prev.linkState, initial_portfolio: false },
  };
  assert.equal(ComparisonState._isViewOnlyTransition(prev, next), false);
});

test("_isViewOnlyTransition: allocationLinked flip is data-changing", () => {
  const ComparisonState = loadComparisonState();
  const prev = {
    allocationLinked: true,
    linkState: { initial_portfolio: true, annual_spending: true, start_age: true, end_age: true },
    view: "overlay",
    focusedScenario: null,
  };
  const next = { ...prev, allocationLinked: false };
  assert.equal(ComparisonState._isViewOnlyTransition(prev, next), false);
});

test("_isViewOnlyTransition: SIMULTANEOUS focus flip + link flip is data-changing (the race)", () => {
  // The motivating case for the tightened classifier. A future caller wires
  // focusOn to a UI element and, in the same transition cycle, a link bool
  // also flips (e.g., a strategy A/B toggle that re-evaluates link state).
  // The cache must NOT serve this — the underlying data changed.
  const ComparisonState = loadComparisonState();
  const prev = {
    allocationLinked: true,
    linkState: { initial_portfolio: false, annual_spending: true, start_age: true, end_age: true },
    view: "overlay",
    focusedScenario: null,
  };
  const next = {
    allocationLinked: true,
    // BOTH change at once: a previously-linked field unlinks AND focus moves.
    linkState: { ...prev.linkState, annual_spending: false },
    view: "overlay",
    focusedScenario: "B",
  };
  assert.equal(
    ComparisonState._isViewOnlyTransition(prev, next),
    false,
    "link flip wins — must re-fetch even though a focus flip rode along",
  );
});

test("_isViewOnlyTransition: missing prev (initial state) is data-changing", () => {
  const ComparisonState = loadComparisonState();
  assert.equal(ComparisonState._isViewOnlyTransition(null, { allocationLinked: true, linkState: {} }), false);
});
