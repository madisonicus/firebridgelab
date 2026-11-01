// test_annual_spending_gate.mjs — the Amortization Frontier greys + locks the
// "Annual spending ($)" field (web/app.js, updateAnnualSpendingGate, af2).
//
// The Amortization Frontier spending mode IGNORES annual_spending (it derives the
// spend from portfolio + GI + horizon + the amortization return), so the field is
// disabled + visually muted with an inline note in that mode, and restored editable
// in every other mode. Acceptance invariants asserted here:
//   - spendingMode === "amortization": A input (#annual_spending) AND the unlinked B
//     input (#annual_spending-b) are disabled; the row carries the `is-gated` class;
//     exactly one `.annual-spending-gate-note` appears with the agreed copy.
//   - any other mode: both inputs re-enabled; `is-gated` removed; the note removed.
//   - idempotent across mode flips (toggle back and forth → clean state each time,
//     no duplicate notes).
//   - the field's `.value` is still readable while disabled (the posted-value /
//     save-load contract — disabled does NOT drop it from the read path).
//
// Loaded like test_reset_defaults.mjs: app.js runs in a node:vm sandbox with NO
// `document` at load (init() skipped), then a focused fake DOM that ACTUALLY tracks
// classList / disabled / appendChild / querySelector is injected, and the gate is
// driven by poking the module-level `spendingMode` global + calling the function
// across the vm boundary. Zero npm deps; requires Node >= 18.
//
// Run:
//   (from the repo root) node --test tests/test_annual_spending_gate.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "../web");
const DEP_PATHS = ["chart_view.js", "comparison_state.js", "payload_assembler.js", "scenario_store.js", "ss_claim.js"].map(
  (f) => path.join(WEB_DIR, f),
);
const APP_PATH = path.join(WEB_DIR, "app.js");

const EXPECTED_NOTE =
  "Ignored by the Amortization Frontier — spend is derived from your portfolio, " +
  "guaranteed income, and the amortization return.";

// ---------- a small fake DOM that actually tracks the state under test ----------

// Real classList semantics (Set-backed) so toggle/contains reflect the gate.
function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    toggle: (c, force) => {
      const want = force === undefined ? !set.has(c) : !!force;
      if (want) set.add(c);
      else set.delete(c);
      return want;
    },
    contains: (c) => set.has(c),
  };
}

function makeElement(id) {
  const children = [];
  const el = {
    id: id || "",
    tagName: "",
    _value: "",
    disabled: false,
    children,
    dataset: {},
    style: {},
    hidden: false,
    classList: makeClassList(),
    _className: "",
    _attrs: {},
    parentNode: null,
    // className setter syncs classList (real-DOM semantics) so code that assigns
    // `el.className = "foo bar"` is reflected in classList.contains(...).
    set className(v) {
      el._className = String(v);
      el.classList = makeClassList();
      for (const c of el._className.split(/\s+/).filter(Boolean)) el.classList.add(c);
    },
    get className() {
      return el._className;
    },
    get value() {
      return this._value;
    },
    set value(v) {
      this._value = String(v);
    },
    set textContent(v) {
      this._text = String(v);
    },
    get textContent() {
      return this._text || "";
    },
    set innerHTML(_v) {
      children.length = 0;
    },
    get innerHTML() {
      return "";
    },
    appendChild(c) {
      children.push(c);
      c.parentNode = el;
      return c;
    },
    removeChild(c) {
      const i = children.indexOf(c);
      if (i >= 0) children.splice(i, 1);
      c.parentNode = null;
    },
    remove() {
      if (el.parentNode) el.parentNode.removeChild(el);
    },
    setAttribute(k, v) {
      el._attrs[k] = v;
    },
    getAttribute(k) {
      return el._attrs[k];
    },
    removeAttribute(k) {
      delete el._attrs[k];
    },
    addEventListener() {},
    removeEventListener() {},
    // The row's only structured query is for its child note by class.
    querySelector(sel) {
      if (sel === ".annual-spending-gate-note") {
        return children.find((c) => c.classList.contains("annual-spending-gate-note")) || null;
      }
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return false;
    },
    closest() {
      return null;
    },
    focus() {},
  };
  return el;
}

// A document whose getElementById serves the A/B inputs and whose
// querySelector(".field-row[data-field=...]") serves the annual-spending row.
function makeFakeDocument() {
  const byId = new Map();
  const aInput = makeElement("annual_spending");
  aInput.value = "110000";
  const bInput = makeElement("annual_spending-b");
  bInput.value = "110000";
  byId.set("annual_spending", aInput);
  byId.set("annual_spending-b", bInput);

  const row = makeElement("");
  row.dataset.field = "annual_spending";

  return {
    _row: row,
    _aInput: aInput,
    _bInput: bInput,
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, makeElement(id));
      return byId.get(id);
    },
    querySelector(sel) {
      if (sel === '.field-row[data-field="annual_spending"]') return row;
      return null;
    },
    querySelectorAll() {
      return [];
    },
    createElement(tag) {
      const el = makeElement("");
      el.tagName = String(tag).toUpperCase();
      return el;
    },
    addEventListener() {},
    body: makeElement("body"),
  };
}

function loadApp() {
  const ctx = { console: { ...console, warn() {}, error() {} } };
  ctx.window = ctx;
  ctx.Chart = function () {};
  ctx.fetch = async () => {
    throw new Error("fetch disabled in gate unit test");
  };
  vm.createContext(ctx);
  for (const dep of DEP_PATHS) vm.runInContext(fs.readFileSync(dep, "utf8"), ctx);
  vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), ctx);
  ctx.document = makeFakeDocument();
  return ctx;
}

function setMode(ctx, mode) {
  vm.runInContext(`spendingMode = ${JSON.stringify(mode)};`, ctx);
  vm.runInContext("updateAnnualSpendingGate();", ctx);
}

function notes(ctx) {
  return ctx.document._row.children.filter((c) => c.classList.contains("annual-spending-gate-note"));
}

test("amortization mode: both inputs disabled, row gated, one note with the agreed copy", () => {
  const ctx = loadApp();
  setMode(ctx, "amortization");

  assert.equal(ctx.document._aInput.disabled, true, "A input disabled");
  assert.equal(ctx.document._bInput.disabled, true, "B input disabled");
  assert.equal(ctx.document._row.classList.contains("is-gated"), true, "row is-gated");

  const n = notes(ctx);
  assert.equal(n.length, 1, "exactly one gate note");
  assert.equal(n[0].textContent, EXPECTED_NOTE, "note copy matches");
});

test("non-amortization modes: inputs re-enabled, ungated, note removed", () => {
  for (const mode of ["constant", "guardrails", "fr_smooth", "lifecycle"]) {
    const ctx = loadApp();
    setMode(ctx, "amortization"); // gate first
    setMode(ctx, mode); // then leave amortization

    assert.equal(ctx.document._aInput.disabled, false, `A input enabled in ${mode}`);
    assert.equal(ctx.document._bInput.disabled, false, `B input enabled in ${mode}`);
    assert.equal(ctx.document._row.classList.contains("is-gated"), false, `row ungated in ${mode}`);
    assert.equal(notes(ctx).length, 0, `note removed in ${mode}`);
  }
});

test("idempotent across repeated flips — no duplicate notes, clean disabled state", () => {
  const ctx = loadApp();
  // Several amortization passes must not stack notes.
  setMode(ctx, "amortization");
  setMode(ctx, "amortization");
  setMode(ctx, "amortization");
  assert.equal(notes(ctx).length, 1, "no duplicate notes after repeated gating");
  assert.equal(ctx.document._aInput.disabled, true);

  // Toggle back and forth; each end-state must be clean.
  setMode(ctx, "constant");
  assert.equal(notes(ctx).length, 0);
  assert.equal(ctx.document._aInput.disabled, false);
  setMode(ctx, "amortization");
  assert.equal(notes(ctx).length, 1);
  assert.equal(ctx.document._aInput.disabled, true);
  setMode(ctx, "guardrails");
  assert.equal(notes(ctx).length, 0);
  assert.equal(ctx.document._aInput.disabled, false);
});

test("disabled field's .value is still readable (posted-value / save-load contract)", () => {
  const ctx = loadApp();
  setMode(ctx, "amortization");
  // The read path is input.value (readFieldValue), independent of `disabled`.
  assert.equal(ctx.document._aInput.disabled, true, "input is disabled");
  assert.equal(ctx.document._aInput.value, "110000", "value still readable while disabled");
});
