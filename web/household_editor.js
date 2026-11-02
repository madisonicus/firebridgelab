// household_editor.js — the household editor's pure half: state <-> API body.
//
// The web UI's household editor (one or two people, a list of income streams,
// cash events and required-spending adjustments, plus the M4 tax options) keeps
// its rows in a plain JS state object. app.js owns the DOM: it renders a row per
// state entry and writes edits back into the state. This module owns everything
// that is pure data shaping, so it can be unit-tested without a browser
// (tests/test_household_editor.mjs):
//
//   HouseholdEditor.buildHousehold(state)   -> { people, income_streams,
//                                                cash_events, spending_adjustments }
//     Coerces the form's strings to numbers, drops keys left at their default,
//     and throws an Error naming the row and field on a bad value. The field
//     names are the API's (docs/plan-files.md documents each).
//   HouseholdEditor.buildTaxOptions(taxState) -> the M4 tax keys that differ
//     from the engine defaults ({} when every option is at its default).
//   HouseholdEditor.applyToParams(params, household, taxOptions) -> a NEW params
//     object for one scenario: the legacy start_age / end_age / person_* keys are
//     removed (the people and streams replace them; the API rejects a start_age
//     that disagrees with the people) and the household + tax keys are added.
//   HouseholdEditor.fromLegacy(legacy, assumptions) -> editor state holding the
//     two pensions and two Social Security benefits of the legacy fields as
//     income streams ("convert to income streams"). Mirrors the library's
//     legacy_income_streams (firebridgelab/income/household.py).
//   HouseholdEditor.fromBody(body) / taxStateFromBody(body) -> editor state from
//     an API body (an imported plan file or an example).
//   HouseholdEditor.axisAges(state) -> the stream start/end ages on person A's
//     age axis, for the glidepath phase boundaries (the engine derives phases
//     from the same ages: firebridgelab.engine.allocation.glidepath_boundaries).
//
// Window-global IIFE, no ES modules (ADR 0003).

(function (global) {
  const STREAM_KINDS = ["pension", "social_security", "annuity", "work", "rental", "other"];
  const STREAM_COLAS = ["full", "none", "fixed", "fers_diet"];
  const STREAM_TAX = ["ordinary", "tax_free", "social_security"];
  const EVENT_TAX = ["tax_free", "ordinary", "capital_gain"];
  const SS_TAXATION = ["provisional", "flat85"];
  const ROTH_BRACKETS = ["10%", "12%", "22%", "24%", "32%", "35%"];
  const STATE_CODES = [
    "AK", "FL", "NV", "NH", "SD", "TN", "TX", "WY",
    "GA", "IL", "IN", "IA", "LA", "MS", "NC", "PA", "custom",
  ];

  const KIND_LABELS = {
    pension: "Pension",
    social_security: "Social Security",
    annuity: "Annuity",
    work: "Work income",
    rental: "Rental",
    other: "Other",
  };

  const TAX_DEFAULTS = {
    ss_taxation: "provisional",
    payroll_tax: false,
    irmaa: false,
    irmaa_prior_magi: null,
    roth_conversion_target: null,
    roth_conversion_start_age: null,
    roth_conversion_end_age: null,
    roth_heir_tax_rate: 0.22,
    state: null,
    state_move_age: null,
    state_after_move: null,
    state_custom_rate: 0,
    state_custom_exempt_ss: false,
    state_custom_exempt_pensions: false,
    state_custom_exempt_ira: false,
  };

  // ---------- value coercion ----------

  function blank(v) {
    return v === null || v === undefined || (typeof v === "string" && v.trim() === "");
  }

  // A required number. `where` names the row and field for the error message.
  function num(v, where) {
    if (blank(v)) throw new Error(`${where} is required`);
    const n = typeof v === "number" ? v : Number(String(v).trim());
    if (!Number.isFinite(n)) throw new Error(`${where} must be a number`);
    return n;
  }

  function int(v, where) {
    const n = num(v, where);
    if (!Number.isInteger(n)) throw new Error(`${where} must be a whole number`);
    return n;
  }

  // An optional number: blank -> null.
  function optNum(v, where) {
    return blank(v) ? null : num(v, where);
  }

  function optInt(v, where) {
    return blank(v) ? null : int(v, where);
  }

  function fraction(v, where) {
    const n = num(v, where);
    if (n < 0 || n > 1) throw new Error(`${where} must be between 0 and 1`);
    return n;
  }

  function choice(v, allowed, where) {
    if (!allowed.includes(v)) {
      throw new Error(`${where} must be one of ${allowed.join(", ")} (got ${JSON.stringify(v)})`);
    }
    return v;
  }

  function str(v) {
    return blank(v) ? "" : String(v).trim();
  }

  // ---------- ids ----------

  function uniqueId(base, taken) {
    const used = new Set(taken);
    const root = String(base || "item").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "item";
    if (!used.has(root)) return root;
    for (let k = 2; ; k++) {
      const id = `${root}_${k}`;
      if (!used.has(id)) return id;
    }
  }

  // ---------- defaults for new rows ----------

  function defaultState(startAge, endAge) {
    const s = Number.isInteger(startAge) ? startAge : 50;
    const e = Number.isInteger(endAge) ? endAge : 95;
    return {
      enabled: false,
      people: [
        { id: "a", name: "", start_age: s, end_age: e },
        { id: "b", name: "", start_age: s, end_age: e },
      ],
      streams: [],
      cashEvents: [],
      adjustments: [],
    };
  }

  function newStream(state, kind, owner) {
    const k = STREAM_KINDS.includes(kind) ? kind : "pension";
    const o = owner || "a";
    const person = (state.people || []).find((p) => p.id === o) || (state.people || [])[0];
    const startAge = k === "social_security" ? 67 : person ? person.start_age : 60;
    const taken = (state.streams || []).map((st) => st.id);
    return {
      id: uniqueId(`${o === "household" ? "hh" : o}_${k === "social_security" ? "ss" : k}`, taken),
      kind: k,
      owner: o,
      amount: k === "social_security" ? 24000 : 12000,
      start_age: startAge,
      end_age: k === "work" && person ? person.start_age + 5 : null,
      cola: "full",
      cola_rate: 0,
      nominal_until_start: false,
      fers_cola_before_62: false,
      survivor_fraction: null,
      tax_treatment: null,
      self_employed: false,
      label: "",
    };
  }

  function newCashEvent(state, owner) {
    const o = owner || "a";
    const person = (state.people || []).find((p) => p.id === o) || (state.people || [])[0];
    const taken = (state.cashEvents || []).map((ev) => ev.id);
    return {
      id: uniqueId("event", taken),
      owner: o,
      age: person ? person.start_age + 5 : 55,
      amount: -20000,
      tax_treatment: "tax_free",
      basis_fraction: 0,
      label: "",
    };
  }

  function newAdjustment(state, owner) {
    const o = owner || "a";
    const person = (state.people || []).find((p) => p.id === o) || (state.people || [])[0];
    const taken = (state.adjustments || []).map((adj) => adj.id);
    const start = person ? person.start_age : 50;
    return {
      id: uniqueId("required", taken),
      owner: o,
      amount: 8000,
      start_age: start,
      end_age: Math.max(start + 1, 65),
      survivor_fraction: 1,
      label: "",
    };
  }

  // ---------- state -> API body ----------

  function owners(state) {
    return state.people.length === 2 ? ["a", "b", "household"] : ["a", "household"];
  }

  function buildPeople(state) {
    const people = state.people || [];
    if (people.length < 1 || people.length > 2) {
      throw new Error("A household has one or two people");
    }
    return people.map((p, i) => {
      const who = i === 0 ? "Person A" : "Person B";
      const out = {
        id: i === 0 ? "a" : "b",
        start_age: int(p.start_age, `${who} age now`),
        end_age: int(p.end_age, `${who} plans to age`),
      };
      if (out.end_age <= out.start_age) {
        throw new Error(`${who}: the planning age must be after the age now`);
      }
      const name = str(p.name);
      if (name) out.name = name;
      return out;
    });
  }

  function buildStream(st, i, allowedOwners) {
    const where = (field) => `Income stream ${i + 1} (${str(st.id) || "no id"}): ${field}`;
    const id = str(st.id);
    if (!id) throw new Error(`Income stream ${i + 1}: an id is required`);
    const kind = choice(st.kind, STREAM_KINDS, where("kind"));
    const out = {
      id,
      kind,
      owner: choice(st.owner || "a", allowedOwners, where("owner")),
      amount: num(st.amount, where("amount")),
      start_age: int(st.start_age, where(kind === "social_security" ? "claim age" : "start age")),
    };
    if (out.amount < 0) throw new Error(`${where("amount")} cannot be negative`);
    if (kind === "social_security") {
      if (out.start_age < 62 || out.start_age > 70) {
        throw new Error(`${where("claim age")} must be between 62 and 70`);
      }
    } else {
      const end = optInt(st.end_age, where("end age"));
      if (end !== null) {
        if (end <= out.start_age) throw new Error(`${where("end age")} must be after the start age`);
        out.end_age = end;
      }
    }
    const cola = kind === "social_security" ? "full" : choice(st.cola || "full", STREAM_COLAS, where("COLA"));
    if (cola !== "full") out.cola = cola;
    if (cola === "fixed") out.cola_rate = num(st.cola_rate, where("COLA rate"));
    if (kind !== "social_security" && st.nominal_until_start) out.nominal_until_start = true;
    if (cola === "fers_diet" && st.fers_cola_before_62) out.fers_cola_before_62 = true;
    if (kind !== "social_security" && !blank(st.survivor_fraction)) {
      out.survivor_fraction = fraction(st.survivor_fraction, where("survivor share"));
    }
    if (!blank(st.tax_treatment)) {
      const tt = choice(st.tax_treatment, STREAM_TAX, where("tax treatment"));
      if (tt === "social_security" && kind !== "social_security") {
        throw new Error(`${where("tax treatment")}: only Social Security is taxed as Social Security`);
      }
      out.tax_treatment = tt;
    }
    if (kind === "work" && st.self_employed) out.self_employed = true;
    const label = str(st.label);
    if (label) out.label = label;
    return out;
  }

  function buildCashEvent(ev, i, allowedOwners) {
    const where = (field) => `Cash event ${i + 1}: ${field}`;
    const out = {
      age: int(ev.age, where("age")),
      amount: num(ev.amount, where("amount")),
      owner: choice(ev.owner || "a", allowedOwners, where("owner")),
      tax_treatment: choice(ev.tax_treatment || "tax_free", EVENT_TAX, where("tax treatment")),
    };
    if (out.amount < 0 && out.tax_treatment !== "tax_free") {
      throw new Error(`${where("tax treatment")}: money going out must be tax-free`);
    }
    if (out.tax_treatment === "capital_gain" && !blank(ev.basis_fraction)) {
      const b = fraction(ev.basis_fraction, where("basis share"));
      if (b) out.basis_fraction = b;
    }
    const id = str(ev.id);
    if (id) out.id = id;
    const label = str(ev.label);
    if (label) out.label = label;
    return out;
  }

  function buildAdjustment(adj, i, allowedOwners) {
    const where = (field) => `Required spending ${i + 1}: ${field}`;
    const out = {
      amount: num(adj.amount, where("amount")),
      start_age: int(adj.start_age, where("start age")),
      end_age: int(adj.end_age, where("end age")),
      owner: choice(adj.owner || "a", allowedOwners, where("owner")),
    };
    if (out.amount < 0) throw new Error(`${where("amount")} cannot be negative`);
    if (out.end_age <= out.start_age) throw new Error(`${where("end age")} must be after the start age`);
    if (!blank(adj.survivor_fraction)) {
      const f = fraction(adj.survivor_fraction, where("survivor share"));
      if (f !== 1) out.survivor_fraction = f;
    }
    const id = str(adj.id);
    if (id) out.id = id;
    const label = str(adj.label);
    if (label) out.label = label;
    return out;
  }

  function buildHousehold(state) {
    const people = buildPeople(state);
    const allowed = owners({ people });
    const ids = new Set();
    const income_streams = (state.streams || []).map((st, i) => {
      const out = buildStream(st, i, allowed);
      if (ids.has(out.id)) throw new Error(`Income stream id "${out.id}" is used twice`);
      ids.add(out.id);
      return out;
    });
    return {
      people,
      income_streams,
      cash_events: (state.cashEvents || []).map((ev, i) => buildCashEvent(ev, i, allowed)),
      spending_adjustments: (state.adjustments || []).map((adj, i) => buildAdjustment(adj, i, allowed)),
    };
  }

  // ---------- tax options ----------

  function defaultTaxState() {
    return { ...TAX_DEFAULTS };
  }

  function buildTaxOptions(t) {
    const tax = { ...TAX_DEFAULTS, ...(t || {}) };
    const out = {};
    const ss = choice(tax.ss_taxation, SS_TAXATION, "Social Security taxation");
    if (ss !== TAX_DEFAULTS.ss_taxation) out.ss_taxation = ss;
    if (tax.payroll_tax) out.payroll_tax = true;
    if (tax.irmaa) {
      out.irmaa = true;
      const prior = optNum(tax.irmaa_prior_magi, "IRMAA income before the plan");
      if (prior !== null) out.irmaa_prior_magi = prior;
    }
    if (!blank(tax.roth_conversion_target)) {
      const raw = tax.roth_conversion_target;
      if (ROTH_BRACKETS.includes(raw)) {
        out.roth_conversion_target = raw;
      } else {
        const dollars = num(raw, "Roth conversion target");
        if (dollars < 0) throw new Error("Roth conversion target cannot be negative");
        out.roth_conversion_target = dollars;
      }
      const start = optInt(tax.roth_conversion_start_age, "Roth conversion first age");
      const end = optInt(tax.roth_conversion_end_age, "Roth conversion stop age");
      if (start !== null) out.roth_conversion_start_age = start;
      if (end !== null) out.roth_conversion_end_age = end;
      const heir = blank(tax.roth_heir_tax_rate)
        ? TAX_DEFAULTS.roth_heir_tax_rate
        : fraction(tax.roth_heir_tax_rate, "Heirs' tax rate");
      if (heir !== TAX_DEFAULTS.roth_heir_tax_rate) out.roth_heir_tax_rate = heir;
    }
    if (!blank(tax.state)) {
      out.state = choice(tax.state, STATE_CODES, "State");
      if (!blank(tax.state_after_move)) {
        out.state_after_move = choice(tax.state_after_move, STATE_CODES, "State after the move");
        out.state_move_age = int(tax.state_move_age, "Age at the move");
      }
      if (out.state === "custom" || out.state_after_move === "custom") {
        const rate = num(tax.state_custom_rate, "Custom state rate");
        if (rate < 0 || rate > 0.2) throw new Error("Custom state rate must be between 0 and 0.2");
        out.state_custom_rate = rate;
        if (tax.state_custom_exempt_ss) out.state_custom_exempt_ss = true;
        if (tax.state_custom_exempt_pensions) out.state_custom_exempt_pensions = true;
        if (tax.state_custom_exempt_ira) out.state_custom_exempt_ira = true;
      }
    }
    return out;
  }

  // ---------- params ----------

  const LEGACY_KEYS = [
    "start_age",
    "end_age",
    "person_a_pension_amount",
    "person_a_pension_start_age",
    "person_b_pension_amount",
    "person_b_pension_start_age",
    "person_a_ss_fra_amount",
    "person_a_ss_claim_age",
    "person_b_ss_fra_amount",
    "person_b_ss_claim_age",
    // Legacy-only pension knobs: the streams carry their own COLA, nominal
    // flag and survivor share.
    "pension_cola",
    "pension_nominal_until_start",
    "survivor_pension_fraction",
  ];

  function applyToParams(params, household, taxOptions) {
    const out = { ...(params || {}) };
    if (household) {
      for (const k of LEGACY_KEYS) delete out[k];
      out.people = household.people;
      out.income_streams = household.income_streams;
      out.cash_events = household.cash_events;
      out.spending_adjustments = household.spending_adjustments;
    }
    if (taxOptions) Object.assign(out, taxOptions);
    return out;
  }

  // ---------- legacy fields -> streams ----------

  function fromLegacy(legacy, assumptions) {
    const L = legacy || {};
    const A = assumptions || {};
    const start = Number(L.start_age);
    const end = Number(L.end_age);
    const state = defaultState(start, end);
    state.enabled = true;
    const pension = (who) => ({
      id: `person_${who}_pension`,
      kind: "pension",
      owner: who,
      amount: Number(L[`person_${who}_pension_amount`]) || 0,
      start_age: Number(L[`person_${who}_pension_start_age`]),
      end_age: null,
      cola: A.pension_cola || "full",
      cola_rate: 0,
      nominal_until_start: !!A.pension_nominal_until_start,
      survivor_fraction:
        typeof A.survivor_pension_fraction === "number" ? A.survivor_pension_fraction : 0.5,
      tax_treatment: null,
      self_employed: false,
      label: "",
    });
    const ss = (who) => ({
      id: `person_${who}_ss`,
      kind: "social_security",
      owner: who,
      amount: Number(L[`person_${who}_ss_fra_amount`]) || 0,
      start_age: Number(L[`person_${who}_ss_claim_age`]),
      end_age: null,
      cola: "full",
      cola_rate: 0,
      nominal_until_start: false,
      survivor_fraction: null,
      tax_treatment: null,
      self_employed: false,
      label: "",
    });
    state.streams = [pension("a"), pension("b"), ss("a"), ss("b")];
    return state;
  }

  // ---------- API body -> state ----------

  function fromBody(body) {
    const b = body || {};
    const people = (b.people || []).map((p) => ({
      id: p.id,
      name: p.name || "",
      start_age: p.start_age,
      end_age: p.end_age,
    }));
    const state = defaultState(people[0] ? people[0].start_age : 50, people[0] ? people[0].end_age : 95);
    state.enabled = true;
    if (people.length) state.people = people;
    state.streams = (b.income_streams || []).map((st) => ({
      id: st.id,
      kind: st.kind,
      owner: st.owner || "a",
      amount: st.amount,
      start_age: st.start_age,
      end_age: st.end_age == null ? null : st.end_age,
      cola: st.cola || "full",
      cola_rate: st.cola_rate || 0,
      nominal_until_start: !!st.nominal_until_start,
      fers_cola_before_62: !!st.fers_cola_before_62,
      survivor_fraction: st.survivor_fraction == null ? null : st.survivor_fraction,
      tax_treatment: st.tax_treatment == null ? null : st.tax_treatment,
      self_employed: !!st.self_employed,
      label: st.label || "",
    }));
    state.cashEvents = (b.cash_events || []).map((ev) => ({
      id: ev.id || "",
      owner: ev.owner || "a",
      age: ev.age,
      amount: ev.amount,
      tax_treatment: ev.tax_treatment || "tax_free",
      basis_fraction: ev.basis_fraction || 0,
      label: ev.label || "",
    }));
    state.adjustments = (b.spending_adjustments || []).map((adj) => ({
      id: adj.id || "",
      owner: adj.owner || "a",
      amount: adj.amount,
      start_age: adj.start_age,
      end_age: adj.end_age,
      survivor_fraction: adj.survivor_fraction == null ? 1 : adj.survivor_fraction,
      label: adj.label || "",
    }));
    return state;
  }

  function taxStateFromBody(body) {
    const t = defaultTaxState();
    for (const k of Object.keys(TAX_DEFAULTS)) {
      if (body && body[k] !== undefined && body[k] !== null) t[k] = body[k];
    }
    return t;
  }

  // ---------- derived ages ----------

  // Person A's age when `owner` is `age` (household streams use A's age).
  function toAxis(state, owner, age) {
    if (owner !== "b" || !state.people || state.people.length < 2) return age;
    return age + (Number(state.people[0].start_age) - Number(state.people[1].start_age));
  }

  // Plan start and end on person A's axis (the end is the later of the people's).
  function planAges(state) {
    const people = state.people || [];
    const aStart = Number(people[0] && people[0].start_age);
    let end = Number(people[0] && people[0].end_age);
    for (const p of people.slice(1)) {
      end = Math.max(end, Number(p.end_age) - (Number(p.start_age) - aStart));
    }
    return { startAge: aStart, endAge: end };
  }

  // [{age, kind}] for every stream's start and end, on person A's axis.
  function axisAges(state) {
    const out = [];
    for (const st of state.streams || []) {
      const start = Number(st.start_age);
      if (Number.isFinite(start)) out.push({ age: toAxis(state, st.owner, start), kind: st.kind });
      if (!blank(st.end_age) && st.kind !== "social_security") {
        const end = Number(st.end_age);
        if (Number.isFinite(end)) out.push({ age: toAxis(state, st.owner, end), kind: "end" });
      }
    }
    return out;
  }

  global.HouseholdEditor = {
    STREAM_KINDS,
    STREAM_COLAS,
    STREAM_TAX,
    EVENT_TAX,
    SS_TAXATION,
    ROTH_BRACKETS,
    STATE_CODES,
    KIND_LABELS,
    TAX_DEFAULTS,
    defaultState,
    defaultTaxState,
    newStream,
    newCashEvent,
    newAdjustment,
    buildHousehold,
    buildTaxOptions,
    applyToParams,
    fromLegacy,
    fromBody,
    taxStateFromBody,
    planAges,
    axisAges,
    uniqueId,
  };
})(typeof window !== "undefined" ? window : globalThis);
