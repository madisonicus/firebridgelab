// comparison_state.js — single source of truth for the comparison axis.
//
// Slice 8 consolidates the link/Focus/view-mode state machine that was
// previously scattered across app.js (linkState object, allocationLinked
// flag, afterLinkChange, the Focus A/B handlers, viewMode + switchView,
// updateOutputMode, updateHistoricalSubView) into ONE module with ONE
// invariant:
//
//   ATOMIC TRANSITIONS — every mutating call updates state AND fires the
//   subscriber chain EXACTLY ONCE. No skipAfter knob, no batched flushes,
//   no implicit ordering between two mutations and a single late notify.
//
// The seam this kills: the previous code path for "Unlink all" had to
// flip every per-field link bool, then call setAllocationLinked with a
// skipAfter:true to avoid a double afterLinkChange(). With atomic
// transitions, the equivalent operation (`unlinkAll`) is one call —> one
// notify, and the workaround disappears.
//
// SHAPE (window-global IIFE, no ES modules — matches chart_view.js):
//
//   Transitions (each = ONE state mutation + ONE notify):
//     enterComparison()
//     exitComparison({ keptScenario = "A" } = {})
//     setFieldLink(name, linked)
//     setAllocationLinked(linked)
//     focusOn(scenario)          scenario in {"A","B",null}
//     setView(view)              view in {"single","overlay","side"}
//     relinkAll()                — caller owns kept-side DOM copy (see below)
//     unlinkAll()
//
//   Pure queries (no mutation, no notify):
//     isComparing()
//     isLinked(fieldName)
//     allocationLinked()
//     outputMode()               → "single" | "overlay" | "side"
//     view()                     → raw stored view (a UI preference)
//     focusedScenario()          → "A" | "B" | null
//     divergingFields()          → array of field names currently unlinked
//
//   Subscriber:
//     subscribe(fn) → unsubscribe fn
//     The subscriber fires after EVERY transition with the new state object
//     (a frozen snapshot, not the internal mutable). Subscribers MUST NOT
//     call transitions during a notify cycle (it would queue a re-entrant
//     notify, breaking the one-mutation-one-notify invariant). They MAY
//     call queries freely.
//
// LEGAL/ILLEGAL view transitions:
//   - setView("single") while comparing -> throws. "single" is a derived
//     state of NOT comparing; the caller should exitComparison() instead.
//   - setView("side") while focused on A or B -> throws. Side-by-side is
//     two panels visible; a focus collapses that to one.
//   - setView("overlay") is always legal, but only manifests as the active
//     output when comparing. Stored either way so a future enterComparison
//     respects the user's last toggle choice.
//
// DERIVED outputMode rules (the actual rendering dispatch):
//   - not comparing                     -> "single"
//   - comparing + focusedScenario set   -> "single" (one panel for the kept side)
//   - comparing + view === "overlay"     -> "overlay"
//   - comparing + view === "side"        -> "side"
//
// FIELD NAMES vs ALLOCATION:
//   The allocation matrix isn't a scalar field, so it gets its own axis
//   (allocationLinked()/setAllocationLinked()), but it's part of the same
//   "is anything diverging" question (isComparing() is true iff ANY field
//   is unlinked OR the allocation is unlinked). This is why
//   divergingFields() omits allocation — callers that need to know about
//   the allocation axis ask allocationLinked() directly.
//
// LINK-NAMES POLICY:
//   The constructor accepts the list of field names. We do NOT bake in
//   "initial_portfolio"/"annual_spending"/etc. — the future funded-ratio
//   work wants to unlink strategy or spending-mode too, and ComparisonState
//   shouldn't care which fields are scalar inputs vs the spending shape.
//   app.js passes FIELDS.map(f => f.name) at init.
//
// PERSISTENCE: the chosen `view` is a UI preference that persists across
// enterComparison/exitComparison cycles, so the user's last "overlay vs
// side-by-side" choice is restored when they next start a comparison.
// `focusedScenario` resets on exitComparison (a fresh single has no focus
// concept). `keptScenario` on relink/exit is a TRANSITION-LOCAL parameter
// — NOT stored — the focus state is meant for "show only A while still
// comparing" and is independent of the relink-and-keep action.

(function (global) {
  "use strict";

  function ComparisonState(fieldNames, opts) {
    const options = opts || {};
    const initialView = options.initialView || "overlay";
    if (!Array.isArray(fieldNames) || fieldNames.length === 0) {
      throw new Error("ComparisonState: fieldNames must be a non-empty array");
    }
    if (!["overlay", "side"].includes(initialView)) {
      // Note: "single" is NOT a legal STORED view — it's a derived output.
      throw new Error(
        "ComparisonState: initialView must be 'overlay' or 'side' (got " +
          initialView + ")",
      );
    }

    // ---- Internal mutable state ----
    // Every field starts linked.
    const linkState = Object.create(null);
    for (const n of fieldNames) linkState[n] = true;
    let allocLinked = true;
    let focused = null;          // "A" | "B" | null
    let view = initialView;      // user's stored overlay/side preference
    const subscribers = [];
    let notifying = false;

    // ---- Helpers ----
    function anyFieldDiverging() {
      for (const n of fieldNames) {
        if (linkState[n] === false) return true;
      }
      return false;
    }

    function isComparingInternal() {
      return anyFieldDiverging() || !allocLinked;
    }

    function snapshot() {
      // A small, FROZEN view of the public state. Subscribers should treat
      // this as read-only; mutating it doesn't affect the source.
      const links = Object.create(null);
      for (const n of fieldNames) links[n] = linkState[n];
      return Object.freeze({
        comparing: isComparingInternal(),
        outputMode: outputModeInternal(),
        view,
        focusedScenario: focused,
        linkState: Object.freeze(links),
        allocationLinked: allocLinked,
      });
    }

    function notify() {
      if (notifying) {
        // Re-entrant notify means a subscriber mutated state. The invariant
        // is one mutation = one notify, so this is a programming error.
        throw new Error(
          "ComparisonState: subscriber attempted to mutate during notify",
        );
      }
      notifying = true;
      const snap = snapshot();
      try {
        for (const fn of subscribers.slice()) fn(snap);
      } finally {
        notifying = false;
      }
    }

    function outputModeInternal() {
      if (!isComparingInternal()) return "single";
      if (focused === "A" || focused === "B") return "single";
      return view === "side" ? "side" : "overlay";
    }

    // ---- Transitions ----

    function enterComparison() {
      if (isComparingInternal()) return;  // no-op, no notify
      // Default: unlink every field + the allocation. The historical "Unlink
      // all" button (onMasterLink in app.js) was the only entry to this
      // transition, so the semantics match.
      for (const n of fieldNames) linkState[n] = false;
      allocLinked = false;
      focused = null;
      notify();
    }

    function exitComparison(o) {
      if (!isComparingInternal()) return;
      const keptScenario = (o && o.keptScenario) || (focused === "B" ? "B" : "A");
      // The actual "keep B's values" logic (copying the B input DOM values
      // into the A inputs) lives in app.js because it touches the DOM. The
      // state-side concern is: every link goes true, allocation goes true,
      // focus clears. The caller (app.js) is responsible for the value-copy
      // step BEFORE calling exitComparison({ keptScenario: "B" }) so the
      // subscribers' re-render sees the correct A-side values.
      for (const n of fieldNames) linkState[n] = true;
      allocLinked = true;
      focused = null;
      notify();
      // Note: keptScenario is communicated to the caller via the explicit
      // parameter pattern; subscribers can read it from the calling
      // function's closure if they need it. Storing it on the snapshot
      // would imply persistence, which we explicitly don't want.
    }

    function setFieldLink(name, linked) {
      if (!(name in linkState)) {
        throw new Error("ComparisonState: unknown field name " + name);
      }
      const target = !!linked;
      if (linkState[name] === target) return;  // no-op
      const wasComparing = isComparingInternal();
      linkState[name] = target;
      const nowComparing = isComparingInternal();
      // Crossing the all-linked boundary clears any stale focus.
      if (wasComparing && !nowComparing) focused = null;
      notify();
    }

    function setAllocationLinked(linked) {
      const target = !!linked;
      if (allocLinked === target) return;
      const wasComparing = isComparingInternal();
      allocLinked = target;
      const nowComparing = isComparingInternal();
      if (wasComparing && !nowComparing) focused = null;
      notify();
    }

    function focusOn(scenario) {
      if (scenario !== "A" && scenario !== "B" && scenario !== null) {
        throw new Error(
          "ComparisonState: focusOn expects 'A', 'B', or null (got " +
            scenario + ")",
        );
      }
      if (scenario !== null && !isComparingInternal()) {
        // Focusing while not comparing is meaningless (already a single
        // scenario). Throwing surfaces the bug rather than silently no-op'ing.
        throw new Error(
          "ComparisonState: cannot focus '" + scenario +
            "' while not comparing",
        );
      }
      if (focused === scenario) return;
      focused = scenario;
      notify();
    }

    function setView(v) {
      if (v === "single") {
        // "single" is a derived output, not a stored view.
        throw new Error(
          "ComparisonState: setView('single') is invalid — use exitComparison()",
        );
      }
      if (v !== "overlay" && v !== "side") {
        throw new Error("ComparisonState: setView expects 'overlay'|'side' (got " + v + ")");
      }
      if (v === "side" && focused !== null) {
        throw new Error(
          "ComparisonState: cannot set view='side' while focused on '" +
            focused + "' — clear the focus first",
        );
      }
      if (view === v) return;
      view = v;
      notify();
    }

    function relinkAll() {
      // Atomic re-link: every field + the allocation snap to true, focus
      // clears, ONE notify.
      //
      // CONTRACT: the CALLER owns kept-side identity. If the caller is
      // exiting comparison while keeping B (the non-canonical side), the
      // caller MUST copy B's DOM values into the A inputs BEFORE calling
      // relinkAll — once the transition fires, the subscriber re-renders
      // every row from A and the B inputs are discarded. app.js's onFocus()
      // does exactly this via copyBIntoA(). ComparisonState's job is just
      // the atomic state flip; it has no business naming which scenario was
      // kept.
      if (!isComparingInternal()) return;
      for (const n of fieldNames) linkState[n] = true;
      allocLinked = true;
      focused = null;
      notify();
    }

    function unlinkAll() {
      // Opposite of relinkAll — every link goes false. Atomic ONE notify.
      // This is the legacy "Unlink all" master button transition; previously
      // it had to skipAfter:true on the allocation flip to avoid two
      // afterLinkChange() calls.
      let changed = false;
      for (const n of fieldNames) {
        if (linkState[n] !== false) {
          linkState[n] = false;
          changed = true;
        }
      }
      if (allocLinked !== false) {
        allocLinked = false;
        changed = true;
      }
      if (focused !== null) {
        focused = null;
        changed = true;
      }
      if (!changed) return;
      notify();
    }

    // ---- Pure queries ----

    function isComparing() {
      return isComparingInternal();
    }

    function isLinked(name) {
      if (!(name in linkState)) {
        throw new Error("ComparisonState: unknown field name " + name);
      }
      return linkState[name];
    }

    function allocationLinkedQuery() {
      return allocLinked;
    }

    function outputMode() {
      return outputModeInternal();
    }

    function viewQuery() {
      return view;
    }

    function focusedScenarioQuery() {
      return focused;
    }

    function divergingFields() {
      const out = [];
      for (const n of fieldNames) {
        if (linkState[n] === false) out.push(n);
      }
      return out;
    }

    function subscribe(fn) {
      if (typeof fn !== "function") {
        throw new Error("ComparisonState: subscribe expects a function");
      }
      subscribers.push(fn);
      return function unsubscribe() {
        const i = subscribers.indexOf(fn);
        if (i >= 0) subscribers.splice(i, 1);
      };
    }

    return {
      // Transitions
      enterComparison,
      exitComparison,
      setFieldLink,
      setAllocationLinked,
      focusOn,
      setView,
      relinkAll,
      unlinkAll,
      // Queries
      isComparing,
      isLinked,
      allocationLinked: allocationLinkedQuery,
      outputMode,
      view: viewQuery,
      focusedScenario: focusedScenarioQuery,
      divergingFields,
      // Subscriber
      subscribe,
    };
  }

  // Pure classifier: does this prev→next transition change WHAT data we'd
  // fetch from the API, or only how we display the data we already have?
  //
  // View-only deltas (return true): view (overlay↔side), focusedScenario
  // (null↔A/B). Both are pure display choices over the same underlying data.
  //
  // Data-changing deltas (return false): allocationLinked, any field's link
  // bool. Each of those flips changes the scenarios we'd POST to the engine.
  //
  // The function is closed: we enumerate every delta we KNOW is view-only and
  // return false for anything else. A future caller that pairs a focus flip
  // with a link flip in one transition will be classified data-changing (the
  // link flip wins) — the helper never silently caches stale data behind a
  // simultaneous view-and-data change.
  //
  // Exposed under the underscore-prefix convention (cf. ChartView._binIndexForX):
  // not part of the stable public API, but importable by tests.
  function _isViewOnlyTransition(prev, next) {
    if (!prev) return false;
    if (prev.allocationLinked !== next.allocationLinked) return false;
    const allFields = new Set([
      ...Object.keys(prev.linkState || {}),
      ...Object.keys(next.linkState || {}),
    ]);
    for (const name of allFields) {
      if (prev.linkState[name] !== next.linkState[name]) return false;
    }
    // Only view + focusedScenario remain. Both are view-only by design.
    return true;
  }

  global.ComparisonState = { create: ComparisonState, _isViewOnlyTransition };
})(typeof window !== "undefined" ? window : globalThis);
