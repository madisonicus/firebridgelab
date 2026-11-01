// payload_assembler.js — pure assembly of the scenario / POST body.
//
// THE SEAM: readScenarios() used to fuse two jobs into one DOM-fused, untested
// function — (1) READ the ~12 scalar form fields + the conditional sub-bodies
// (allocation, glidepath phases, buckets, guardrails, MC correlation, tax-
// location split) off the DOM, and (2) ASSEMBLE them into the two API param
// objects { a, b } with all the per-field linking + conditional-inclusion rules.
// Job (1) is irreducibly DOM-bound; job (2) is pure data shaping and was never
// unit-tested. This module owns job (2).
//
// app.js keeps the thin DOM read (readFormState()): every getElementById /
// allocation-table / mode-global read lands in a plain JS `formState` object.
// PayloadAssembler.buildScenario(formState) is then PURE — no `document` access —
// and produces the EXACT same { a, b } readScenarios() produced. The split is
// behavior-preserving: the assembly logic moved VERBATIM, the only change is that
// values arrive via `formState.*` instead of inline `getElementById(...).value`.
//
// SHAPE (window-global IIFE, no ES modules — matches chart_view.js /
// comparison_state.js):
//
//   PayloadAssembler.buildScenario(formState) -> { a, b }
//     formState = {
//       fieldNames: [name, …]            // FIELDS order, the iteration order
//       fields: { name: { aVal, bVal, linked } }
//                                        // aVal always present; bVal present
//                                        // only when NOT linked (B input was
//                                        // read lazily, preserving throw-timing);
//                                        // linked picks aVal vs bVal in assembly
//       allocationBodyA, allocationBodyB // id-native allocation arrays
//       allocationLinked                 // bool — B reuses A's allocation body
//       spendingMode                     // 'constant' | 'lifecycle' | …
//       allocationMode                   // 'constant' | 'glidepath'
//       phaseAllocations                 // only consulted when glidepath
//       sourcingMode                     // 'single' | 'buckets'
//       bucketsBody                      // only consulted when buckets
//       guardrails                       // object or null (null → omit)
//       modelAssumptions                 // object of non-default API keys, or null
//       correlation                      // number
//       taxLocation                      // { active, taxable, taxDeferred, roth }
//       taxableGainsFraction             // number (0..1) or undefined (inactive)
//       bracketFillTarget                // number, null (active but blank), or
//                                        // undefined (inactive) — tax-drag td1
//     }
//
//   PayloadAssembler.buildPostBody(params) -> body
//     The small flat→ScenarioBody transform: `allocationBody` becomes
//     `allocation` (only when a non-empty array), every other key rides through.
//
// CRITICAL: byte-identical to the old DOM-fused output for any given inputs —
// same keys, same nesting, same values, same conditional inclusion. The test
// file (tests/test_payload_assembler.mjs) is the safety net the round-trip
// never had.

(function (global) {
  // Attach the three tax-location balances to a param object when a split is
  // active. A supplied split sends ALL THREE (missing → 0), matching the API's
  // "if any is set, all are set" rule. No-op when off (the param object stays
  // byte-identical to today). VERBATIM from app.js attachTaxLocation, except the
  // split + active flag now arrive on formState.taxLocation instead of being read
  // from the DOM via readTaxLocation()/taxLocationActive().
  function attachTaxLocation(params, taxLocation) {
    if (!taxLocation || !taxLocation.active) return;
    const { taxable, taxDeferred, roth } = taxLocation;
    params.taxable_balance = Number.isFinite(taxable) ? taxable : 0;
    params.tax_deferred_balance = Number.isFinite(taxDeferred) ? taxDeferred : 0;
    params.roth_balance = Number.isFinite(roth) ? roth : 0;
  }

  // Attach the taxable-gains-fraction knob (federal-tax overlay design ft1,
  // DECISION 4) to a param object. `fraction` is undefined when the tax-location
  // split is inactive (app.js's readFormState only reads the live knob while
  // active, the SAME gate attachTaxLocation uses above) — a no-op then, so an
  // inactive request stays byte-identical to pre-ft1. Otherwise sends the
  // fraction (0..1) verbatim.
  function attachTaxableGainsFraction(params, fraction) {
    if (fraction === undefined) return;
    params.taxable_gains_fraction = fraction;
  }

  // Attach the bracket-fill lever (tax-drag design td1, DECISION 5) to a param
  // object. `target` is undefined when the tax-location split is inactive, and
  // null when the split is active but the lever field is blank ("empty = off"
  // — app.js's readFormState only reads the live field while active, the SAME
  // gate attachTaxLocation uses above). Either way this is a no-op — only a
  // real number rides — so an off-lever request stays byte-identical to
  // pre-td1 (no bracket_fill_target key at all).
  function attachBracketFillTarget(params, target) {
    if (typeof target !== "number") return;
    params.bracket_fill_target = target;
  }

  // Build the two API param objects from a plain formState. For each field:
  //   scenario A → the A (shared) input
  //   scenario B → the B input when unlinked, else the shared input
  // This is the VERBATIM body of readScenarios(), with value-sourcing swapped
  // from DOM reads to formState lookups.
  function buildScenario(formState) {
    const a = {};
    const b = {};
    for (const name of formState.fieldNames) {
      const f = formState.fields[name];
      const aVal = f.aVal;
      a[name] = aVal;
      if (f.linked) {
        b[name] = aVal;
      } else {
        b[name] = f.bVal;
      }
    }
    // Allocation can diverge. Scenario A always uses the A weight column; B uses
    // A's allocation when linked (identical) or the B column when unlinked.
    a.allocationBody = formState.allocationBodyA;
    b.allocationBody = formState.allocationLinked
      ? a.allocationBody
      : formState.allocationBodyB;
    // Lifecycle spending: the shape toggle is GLOBAL in v1, so both scenarios get
    // the same spending_mode on every call. 'constant' (default) leaves today's
    // behavior untouched.
    a.spending_mode = formState.spendingMode;
    b.spending_mode = formState.spendingMode;
    // Glidepath: the allocation-shape toggle is GLOBAL in v1, so both scenarios
    // get the same allocation_mode + phase_allocations. 'constant' (default) omits
    // phase_allocations. The phase set is GLOBAL, applied to both A and B.
    a.allocation_mode = formState.allocationMode;
    b.allocation_mode = formState.allocationMode;
    if (formState.allocationMode === "glidepath") {
      const phases = formState.phaseAllocations;
      a.phase_allocations = phases;
      b.phase_allocations = phases;
    }
    // Buckets sourcing: the GLOBAL sourcing toggle, sent on BOTH scenarios.
    // 'single' (default) is omitted-equivalent. Under buckets the SAME config
    // rides on both A and B (GLOBAL in v1).
    a.sourcing_mode = formState.sourcingMode;
    b.sourcing_mode = formState.sourcingMode;
    if (formState.sourcingMode === "buckets") {
      const bucketsBody = formState.bucketsBody;
      a.buckets = bucketsBody;
      b.buckets = bucketsBody;
    }
    // Guardrail knobs: a single shared set, sent on BOTH scenarios but ONLY under
    // guardrails — formState.guardrails is null otherwise, so constant/lifecycle
    // requests omit them and the API uses the scenario defaults.
    const guardrails = formState.guardrails;
    if (guardrails) {
      Object.assign(a, guardrails);
      Object.assign(b, guardrails);
    }
    // Model assumptions (review 2026-10-11): one shared set — fees, inflation,
    // pension treatment, MC shape, survivor case, guardrail clamp — sent on BOTH
    // scenarios. Only non-default keys are present, so a default form sends
    // nothing extra (byte-identical to before).
    const modelAssumptions = formState.modelAssumptions;
    if (modelAssumptions) {
      Object.assign(a, modelAssumptions);
      Object.assign(b, modelAssumptions);
    }
    // Monte Carlo correlation: a single shared cross-class knob, sent on BOTH
    // scenarios. Only the MC engine reads it; Deterministic + Historical ignore it.
    const correlation = formState.correlation;
    a.correlation = correlation;
    b.correlation = correlation;
    // Tax-location split: the three OPTIONAL balances, sent on BOTH scenarios ONLY
    // when a split is supplied AND sourcing is single (the gate already baked into
    // formState.taxLocation.active). Off → the params stay byte-identical to today.
    attachTaxLocation(a, formState.taxLocation);
    attachTaxLocation(b, formState.taxLocation);
    // Taxable-gains-fraction knob (federal-tax overlay design ft1): a single
    // shared knob (like correlation), sent on BOTH scenarios ONLY when the
    // tax-location split is active — undefined otherwise, so the params object
    // stays byte-identical to pre-ft1 when the split is off.
    attachTaxableGainsFraction(a, formState.taxableGainsFraction);
    attachTaxableGainsFraction(b, formState.taxableGainsFraction);
    // Bracket-fill lever (tax-drag design td1, DECISION 5): a single shared
    // dollar knob (like the gains fraction), sent on BOTH scenarios ONLY when
    // it resolves to a real number (the split is active AND the field isn't
    // blank) — omitted otherwise, so the params object stays byte-identical to
    // pre-td1 when the lever is off.
    attachBracketFillTarget(a, formState.bracketFillTarget);
    attachBracketFillTarget(b, formState.bracketFillTarget);
    return { a, b };
  }

  // Reshape a flat `params` object into the ScenarioBody the API expects:
  // `allocationBody` array → `allocation` (only when non-empty); every other key
  // rides through verbatim. VERBATIM from app.js buildPostBody().
  function buildPostBody(params) {
    if (!params) return {};
    const body = {};
    for (const [k, v] of Object.entries(params)) {
      if (k === "allocationBody") {
        if (Array.isArray(v) && v.length) body.allocation = v;
      } else {
        body[k] = v;
      }
    }
    return body;
  }

  // Stress overlay (stress-test design slice 1): tag a scenario's request params
  // for a Family-A synthetic-cohort stress. A stress is an OVERLAY — it leaves the
  // allocation / spend / GI params untouched and only adds the request flavor
  // (`mode: "stress"` + the chosen `stress_id`), so the SAME deterministic
  // single-line render path draws the stressed trajectory. Returns a NEW object
  // (never mutates `params`) so the caller's base scenario params stay clean for
  // the non-stress modes. A falsy `stressId` is a programming error (the caller
  // gates on stress mode), but we still return the base params unchanged rather
  // than emit a half-formed stress request.
  function buildStressParams(params, stressId) {
    const base = params ? { ...params } : {};
    if (!stressId) return base;
    return { ...base, mode: "stress", stress_id: stressId };
  }

  global.PayloadAssembler = {
    buildScenario,
    buildPostBody,
    buildStressParams,
    // Exposed for tests; not a stable public API.
    _attachTaxLocation: attachTaxLocation,
    _attachTaxableGainsFraction: attachTaxableGainsFraction,
    _attachBracketFillTarget: attachBracketFillTarget,
  };
})(typeof window !== "undefined" ? window : globalThis);
