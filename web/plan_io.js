// plan_io.js — the pure half of plan-file import and export in the web UI.
//
// The server parses and writes TOML (firebridgelab.plan, via /api/plan/import
// and /api/plan/export); this module only reshapes JSON:
//
//   PlanIO.blobFromImport(payload, { classIds, simMode }) -> a saved-scenario
//     blob ({ schema_version, name, view, inputs, warnings }) that app.js loads
//     with applyScenarioToForm, the same path a saved scenario takes. `payload`
//     is what /api/plan/import and /api/examples/<name> return: { name,
//     description, trials, seed, stress, body }, where `body` is an API
//     scenario body with the household explicit. `classIds` is the UI's asset
//     class order (glidepath weights are stored per class position).
//   PlanIO.exportRequest(postBody, meta) -> the /api/plan/export request.
//   PlanIO.fileName(name) -> a safe "<slug>.toml" download name.
//
// Window-global IIFE, no ES modules (ADR 0003).

(function (global) {
  // Body fields the UI has no control for; carried from import to export.
  const PLAN_EXTRA_FIELDS = [
    "slow_go_start_age",
    "no_go_start_age",
    "slow_go_multiplier",
    "no_go_multiplier",
    "spending_real_drift",
    "pension_real_drift",
    "mc_t_df",
  ];

  const SIM_MODES = ["deterministic", "historical", "monte_carlo"];

  function pctWeights(allocation, classIds) {
    const byId = {};
    for (const e of allocation || []) byId[e.id] = e.weight;
    return classIds.map((id) => (typeof byId[id] === "number" ? Math.round(byId[id] * 100) : 0));
  }

  function blobFromImport(payload, opts) {
    const p = payload || {};
    const body = p.body || {};
    const classIds = (opts && opts.classIds) || [];
    const HE = global.HouseholdEditor;
    const warnings = [];

    const household = HE.fromBody(body);
    const { startAge, endAge } = HE.planAges(household);

    const inputs = {
      initial_portfolio: body.initial_portfolio,
      annual_spending: body.annual_spending,
      start_age: startAge,
      end_age: endAge,
      allocation: (body.allocation || []).map((e) => ({ ...e })),
    };
    const vols = {};
    for (const e of body.allocation || []) {
      if (typeof e.volatility === "number") vols[e.id] = e.volatility;
    }
    if (Object.keys(vols).length) inputs.volatilities = vols;
    if (typeof body.correlation === "number") inputs.correlation = body.correlation;
    if (Array.isArray(body.correlations)) {
      warnings.push("The plan's correlation matrix cannot be edited here and was not loaded.");
    }
    const unknown = (body.allocation || []).filter((e) => !classIds.includes(e.id));
    if (classIds.length && unknown.length) {
      warnings.push(
        `Asset classes not available here were dropped: ${unknown.map((e) => e.id).join(", ")}.`,
      );
    }

    if (body.allocation_mode === "glidepath" && Array.isArray(body.phase_allocations)) {
      const map = {};
      body.phase_allocations.forEach((phase, k) => {
        map[String(k)] = pctWeights(phase, classIds);
      });
      inputs.glidepath_phase_weights = map;
    }
    if (body.sourcing_mode === "buckets" && body.buckets) {
      inputs.buckets = JSON.parse(JSON.stringify(body.buckets));
    }

    inputs.guardrails = {
      guard_band: body.guard_band,
      guard_adjust: body.guard_adjust,
      spending_floor: body.spending_floor,
      spending_ceiling: body.spending_ceiling,
      amortization_real_return: body.amortization_real_return,
    };

    const split = [body.taxable_balance, body.tax_deferred_balance, body.roth_balance];
    if (split.some((v) => typeof v === "number")) {
      inputs.tax_location = {
        taxable: body.taxable_balance || 0,
        tax_deferred: body.tax_deferred_balance || 0,
        roth: body.roth_balance || 0,
      };
      if (typeof body.taxable_gains_fraction === "number") {
        inputs.taxable_gains_fraction = body.taxable_gains_fraction;
      }
      if (typeof body.bracket_fill_target === "number") {
        inputs.bracket_fill_target = body.bracket_fill_target;
      }
    }

    const ma = {};
    for (const k of ["annual_fee", "assumed_inflation", "mc_distribution", "guard_clamp_base",
      "survivor_decedent", "survivor_spending_fraction"]) {
      if (body[k] !== undefined && body[k] !== null) ma[k] = body[k];
    }
    if (typeof body.survivor_age === "number") ma.survivor_age = body.survivor_age;
    inputs.model_assumptions = ma;

    inputs.household = household;
    inputs.tax_options = HE.taxStateFromBody(body);
    const extras = {};
    for (const k of PLAN_EXTRA_FIELDS) {
      if (body[k] !== undefined && body[k] !== null) extras[k] = body[k];
    }
    inputs.plan_extras = extras;
    inputs.plan_meta = {
      name: p.name || "",
      description: p.description || "",
      trials: p.trials,
      seed: p.seed,
      stress: Array.isArray(p.stress) ? p.stress.slice() : [],
    };

    const simMode = opts && SIM_MODES.includes(opts.simMode) ? opts.simMode : "deterministic";
    return {
      schema_version: 1,
      name: p.name || "Imported plan",
      view: {
        sim_mode: simMode,
        spending_mode: body.spending_mode || "constant",
        allocation_mode: body.allocation_mode || "constant",
        sourcing_mode: body.sourcing_mode || "single",
      },
      inputs,
      warnings,
    };
  }

  // The /api/plan/export request: the scenario body plus the plan's name and
  // run settings. Request-only keys (mode, stress_id, window_start_year,
  // confidence) are dropped; they describe a run, not a plan.
  function exportRequest(postBody, meta) {
    const scenario = { ...(postBody || {}) };
    for (const k of ["mode", "stress_id", "window_start_year", "confidence"]) delete scenario[k];
    const m = meta || {};
    const req = { scenario, name: m.name || "", description: m.description || "" };
    if (Number.isInteger(m.trials)) req.trials = m.trials;
    if (Number.isInteger(m.seed)) req.seed = m.seed;
    if (Array.isArray(m.stress) && m.stress.length) req.stress = m.stress.slice();
    return req;
  }

  function fileName(name) {
    const slug = String(name || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60);
    return `${slug || "plan"}.toml`;
  }

  global.PlanIO = { PLAN_EXTRA_FIELDS, blobFromImport, exportRequest, fileName };
})(typeof window !== "undefined" ? window : globalThis);
