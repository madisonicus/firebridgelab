// scenario_store.js — pure capture / diff for the Saved Scenarios feature.
//
// THE SEAM: the Saved Scenarios store is the modeler's writable-state feature.
// Three functions in app.js fused DOM access with data shaping:
//   - captureScenario(name)     FORWARD : live form -> a save blob.
//   - applyScenarioToForm(blob) INVERSE : blob -> live form (DOM mutator, async).
//   - diffScenarioForCompare()  PURE    : two blobs -> a compare-diff record.
// The documented acceptance invariant (app.js Saved Scenarios header): a
// captureScenario() AFTER applyScenarioToForm(blob) reproduces the blob. It was
// tested NOWHERE. This module pulls the PURE pieces out so the store's data
// shaping is unit-testable independently of the browser.
//
// This module owns the two PURE jobs:
//   1. ScenarioStore.diff(savedBlob, currentA, fieldNames)
//        VERBATIM from app.js diffScenarioForCompare (+ its allocationPercentMap
//        helper). Two {view,inputs} blobs -> the compare decision record. No DOM,
//        no mutation. The safe, immediate win.
//   2. ScenarioStore.capture(captureState) -> the save blob.
//        The PURE blob-SHAPING half of captureScenario(). app.js keeps the thin
//        DOM/global reads (it resolves the focused side's params, the glidepath
//        weight map, the buckets body, the guardrails, the MC vols, the tax split,
//        + the four mode globals into a plain `captureState`), and this function
//        shapes them into the EXACT blob captureScenario() emitted before — same
//        keys, same nesting, same conditional inclusion, same values. The only
//        change is value-sourcing: values arrive on captureState instead of being
//        read inline off the DOM / globals.
//
// The async DOM-MUTATING applyScenarioToForm() is NOT moved (its DOM writes +
// ASSET_CLASSES mutation are irreducibly browser-bound). The round-trip invariant
// is exercised through the document-stub harness (tests/test_scenario_store.mjs +
// the existing tests/test_saved_scenarios.mjs): load app.js against a fake DOM,
// applyScenarioToForm(blob), then captureScenario(), assert the client-owned
// fields reproduce the blob — now backed by ScenarioStore.capture's pure shaping.
//
// SHAPE (window-global IIFE, no ES modules — matches chart_view.js /
// comparison_state.js / payload_assembler.js):
//
//   ScenarioStore.capture(captureState) -> { schema_version, name, view, inputs }
//     captureState = {
//       schemaVersion                 // the SCENARIO_SCHEMA_VERSION constant
//       name                          // the save name
//       fieldNames: [name, …]         // FIELDS order
//       side: { <field>: val,         // the focused side's resolved params …
//               allocationBody,       //   ([{id,weight(frac),real_return,vol}])
//               taxable_balance,      //   the tax-location balances (when active)
//               tax_deferred_balance,
//               roth_balance }
//       view: { sim_mode, spending_mode, allocation_mode, sourcing_mode }
//       glidepathPhaseWeights         // {"0":[w…], …} or null — incl ONLY when
//                                     //   view.allocation_mode === 'glidepath'
//       bucketsBody                   // object or null — incl ONLY when
//                                     //   view.sourcing_mode === 'buckets'
//       guardrails                    // object or null — incl ONLY when supplied
//                                     //   (app.js gates on isDynamicSpending())
//       monteCarlo: { correlation,    // object or null — incl ONLY when supplied
//                     volatilities }  //   (app.js gates on monte_carlo)
//       taxLocation                   // {taxable,tax_deferred,roth} or null —
//                                     //   incl ONLY when supplied (app.js gates
//                                     //   on taxLocationActive())
//       taxableGainsFraction          // number (0..1) or null — incl ONLY when
//                                     //   supplied (app.js gates on the SAME
//                                     //   taxLocationActive(), federal-tax
//                                     //   overlay design ft1)
//       bracketFillTarget             // number or null — incl ONLY when a
//                                     //   number (app.js gates on the SAME
//                                     //   taxLocationActive(); tax-drag td1
//                                     //   DECISION 5 — null means split active
//                                     //   but lever off, still omitted)
//     }
//
//   ScenarioStore.diff(savedBlob, currentA, fieldNames)
//     -> { fieldsToUnlink, allocationDiffers, modeMismatch, mismatchedModes,
//          identical }   (see the inline doc, VERBATIM from app.js).
//
// CRITICAL: byte-identical to the old captureScenario() blob for the same inputs.
// tests/test_scenario_store.mjs pins capture's keys/values + the diff records, and
// the document-stub round-trip is the safety net the invariant never had.

(function (global) {
  // Normalize a saved/allocation list ([{id, weight (fraction)}, ...]) into an
  // id -> integer-percent map, matching how the UI carries weights (integers
  // 0..100). Classes absent from the list are 0. Comparing in integer percent
  // is the same granularity the matrix edits + applyScenarioToForm round-trip in,
  // so we don't flag a phantom diff from float dust (0.6 vs 0.6000001).
  // VERBATIM from app.js allocationPercentMap().
  function allocationPercentMap(allocation) {
    const map = Object.create(null);
    for (const e of allocation || []) {
      if (!e || typeof e.id !== "string") continue;
      const pct = typeof e.weight === "number" ? Math.round(e.weight * 100) : 0;
      if (pct !== 0) map[e.id] = pct;
    }
    return map;
  }

  // PURE diff: given the SAVED blob (candidate B) and the CURRENT scenario A
  // (a {view, inputs} shape produced from the live form), decide which scalar
  // fields to unlink, whether the allocation differs, and whether the global
  // modes mismatch. No DOM, no mutation — this is the testable decision the
  // imperative compareWithScenario() then executes.
  //
  //   savedBlob : the GET /api/scenarios/{id} blob ({ name, view, inputs }).
  //   currentA  : { view: {sim_mode,...}, inputs: { <field>: val, allocation } }
  //   fieldNames: the 12 scalar field names (FIELDS.map(f => f.name)).
  //
  // Returns { fieldsToUnlink: string[], allocationDiffers: bool,
  //           modeMismatch: bool, mismatchedModes: string[], identical: bool }.
  // `identical` is true iff NO scalar field differs AND the allocation matches —
  // i.e. nothing to overlay (the caller shows the identical-no-op note).
  // VERBATIM from app.js diffScenarioForCompare().
  function diff(savedBlob, currentA, fieldNames) {
    const savedInputs = (savedBlob && savedBlob.inputs) || {};
    const aInputs = (currentA && currentA.inputs) || {};
    const fieldsToUnlink = [];
    for (const name of fieldNames) {
      const av = aInputs[name];
      const bv = savedInputs[name];
      // Both defined and unequal -> a real diff. Missing-on-one-side is treated
      // as "no usable diff for that field" (we only have one value to show).
      if (av === undefined || av === null) continue;
      if (bv === undefined || bv === null) continue;
      if (Number(av) !== Number(bv)) fieldsToUnlink.push(name);
    }

    const aAlloc = allocationPercentMap(aInputs.allocation);
    const bAlloc = allocationPercentMap(savedInputs.allocation);
    const allKeys = new Set([...Object.keys(aAlloc), ...Object.keys(bAlloc)]);
    let allocationDiffers = false;
    for (const k of allKeys) {
      if ((aAlloc[k] || 0) !== (bAlloc[k] || 0)) {
        allocationDiffers = true;
        break;
      }
    }

    // Mode mismatch: any of the four GLOBAL modes differ between A and the saved
    // blob. These can't be applied per-side; we keep A's modes and note it.
    const aView = (currentA && currentA.view) || {};
    const bView = (savedBlob && savedBlob.view) || {};
    const MODE_KEYS = [
      ["sim_mode", "engine"],
      ["spending_mode", "spending"],
      ["allocation_mode", "allocation"],
      ["sourcing_mode", "sourcing"],
    ];
    const mismatchedModes = [];
    for (const [key, label] of MODE_KEYS) {
      if (bView[key] !== undefined && bView[key] !== aView[key]) {
        mismatchedModes.push(label);
      }
    }
    const modeMismatch = mismatchedModes.length > 0;

    const identical = fieldsToUnlink.length === 0 && !allocationDiffers;
    return {
      fieldsToUnlink,
      allocationDiffers,
      modeMismatch,
      mismatchedModes,
      identical,
    };
  }

  // Shape the focused side + the active conditional blocks into a save blob. This
  // is the PURE half of app.js captureScenario(): app.js resolves the DOM/global
  // reads into `captureState`, and this function does the byte-identical shaping.
  // The conditional blocks (glidepath / buckets / guardrails / MC / tax-location)
  // are included ONLY when app.js supplies them (it gates each on the active mode
  // exactly as captureScenario() did, leaving them null otherwise), so an off
  // block is omitted — byte-identical to the old DOM-fused blob.
  function capture(captureState) {
    const side = captureState.side || {};

    // The 12 scalar fields + the chosen side's allocation. readScenarios() puts
    // the allocation on `allocationBody` ([{id, weight, real_return, volatility}]).
    const inputs = {};
    for (const name of captureState.fieldNames) inputs[name] = side[name];
    inputs.allocation = (side.allocationBody || []).map((e) => ({ ...e }));

    // Glidepath: store the EXPLICIT per-phase weight map (the integer-% rows the
    // user tuned), keyed by phase index as strings — NOT just the derived bodies,
    // so a reload can re-pin weights by index after the phase count re-derives.
    if (captureState.glidepathPhaseWeights) {
      inputs.glidepath_phase_weights = captureState.glidepathPhaseWeights;
    }
    // Buckets: the full bucket config (n1/n2 + the three per-bucket allocations).
    if (captureState.bucketsBody) {
      inputs.buckets = captureState.bucketsBody;
    }
    // Guardrails: the four editable knobs (already fractions for band/adjust).
    if (captureState.guardrails) {
      inputs.guardrails = captureState.guardrails;
    }
    // Monte Carlo: the cross-class correlation + the per-class vol map (keyed by
    // class id) so a reload reseeds Advanced exactly.
    if (captureState.monteCarlo) {
      inputs.correlation = captureState.monteCarlo.correlation;
      inputs.volatilities = captureState.monteCarlo.volatilities;
    }
    // Tax-location split (tax-buckets design tb1): captured only when ACTIVE (single
    // sourcing + a split supplied), as the three balances (missing → 0, mirroring the
    // API + readScenarios). Omitted when off, so an off save carries NO tax_location
    // block — and an OLD save without it loads as feature-off (applyScenarioToForm
    // treats a missing block as "clear the inputs").
    if (captureState.taxLocation) {
      inputs.tax_location = captureState.taxLocation;
    }
    // Taxable-gains-fraction knob (federal-tax overlay design ft1): captured ONLY
    // when app.js supplies a number (it gates on taxLocationActive(), the SAME
    // condition as taxLocation above). Omitted when off/absent, so an old save
    // (or one made with the split off) loads at the feature default (50%) —
    // applyScenarioToForm's fallback, not this module's concern.
    if (typeof captureState.taxableGainsFraction === "number") {
      inputs.taxable_gains_fraction = captureState.taxableGainsFraction;
    }
    // Bracket-fill lever (tax-drag design td1, DECISION 5): captured ONLY when
    // app.js supplies a number (the split is active AND the lever field isn't
    // blank). Omitted when null (split active, lever off) or absent (split
    // off) — an old save, or a save with the lever off, then loads lever-off
    // (applyScenarioToForm's "absent -> clear to blank" fallback).
    if (typeof captureState.bracketFillTarget === "number") {
      inputs.bracket_fill_target = captureState.bracketFillTarget;
    }
    // Model assumptions (review 2026-10-11): only non-default keys, so an
    // untouched panel adds nothing to the blob; an old save loads at defaults.
    if (captureState.modelAssumptions && Object.keys(captureState.modelAssumptions).length) {
      inputs.model_assumptions = { ...captureState.modelAssumptions };
    }

    return {
      schema_version: captureState.schemaVersion,
      name: captureState.name,
      view: {
        sim_mode: captureState.view.sim_mode,
        spending_mode: captureState.view.spending_mode,
        allocation_mode: captureState.view.allocation_mode,
        sourcing_mode: captureState.view.sourcing_mode,
      },
      inputs,
    };
  }

  global.ScenarioStore = {
    capture,
    diff,
    // Exposed for tests + the imperative compareWithScenario() driver; not a
    // stable public API.
    allocationPercentMap,
  };
})(typeof window !== "undefined" ? window : globalThis);
