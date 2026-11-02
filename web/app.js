// Slice 7 frontend: ONE link-aware inputs section + two pure-output panels.
//
// Reorg of slice 4-6 (two full forms). The controls are now separated from the
// comparison view: a single inputs section at the top drives BOTH scenarios,
// and the two panels below are output-only (summary + chart + year-by-year).
//
// Link model (carried forward conceptually from slice 6's bLinkState):
//   - Each of the 13 fields is link-toggleable. LINKED (default) renders ONE
//     input whose value applies to both scenarios. UNLINKED renders TWO inputs
//     side by side (A | B); B starts equal to A, then is independently editable.
//   - "One direction of truth": relinking DISCARDS B's value and the field
//     reverts to the single shared (A-side) value. Unlinking initializes B = A.
//   - A master control flips every field at once: "Unlink all" (when all linked)
//     starts a comparison; "Focus A / Focus B" (when comparing) collapses it back
//     to a single scenario, keeping the chosen side's values.
//
// Recompute (button-driven, no auto-recompute) builds two scenarios from the
// input state — for each field, scenario A = the shared/A input, scenario B =
// the B input when unlinked else the shared input — and fires two parallel
// GET /api/trajectory calls, rendering each into its panel.
//
// Engine + API are UNCHANGED: still one scenario's params per call; we call
// it twice. Rendering is keyed by panelId ("a" / "b").

// Read a CSS custom property off :root. CSS owns every color (a <canvas> can't
// be CSS-styled), so chart code READS the token at render time and hands the
// string to Chart.js. MUST be called inside the render functions, never cached
// at module load: styles may not be applied yet at load, and a future theme
// toggle needs each render to re-read the live value.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const API_URL = "api/trajectory";
// Spending solver endpoint (slice 2). Mirrors API_URL's prefix; same scenario
// params as /api/trajectory plus `mode` and (historical) `confidence`.
const SOLVE_URL = "api/solve";
// Slice 4b (asset-class design): the class-menu + preset endpoints. The matrix
// rows + per-class CMA defaults come from /api/classes; the preset picker from
// /api/presets. Both are read once on init.
const CLASSES_URL = "api/classes";
const PRESETS_URL = "api/presets";

// The editable fields. `pct` flags fields stored as percent in the form but
// real-units in the API. (Slice 11a removed real_return — the portfolio real
// return now derives from a fixed default asset-class allocation sent on every
// fetch; the editable allocation matrix arrives in slice 11b.)
// `label` is the human-facing field label. `group` routes the field row into
// the right container (primary / horizon / per-person GI fieldset). `min`,
// `max`, `step` mirror the slice-3 HTML5 input bounds.
const FIELDS = [
  { name: "initial_portfolio", label: "Starting portfolio ($)", type: "float", pct: false, group: "primary",  min: 0,  max: null, step: 1000 },
  { name: "annual_spending",   label: "Annual spending ($)",    type: "float", pct: false, group: "primarySpending",  min: 0,  max: null, step: 1000 },
  { name: "start_age",         label: "Start age",              type: "int",   pct: false, group: "horizon",  min: 18, max: 110,  step: 1 },
  { name: "end_age",           label: "End age",                type: "int",   pct: false, group: "horizon",  min: 18, max: 110,  step: 1 },
  { name: "person_a_pension_amount",      label: "Pension annual ($)",          type: "float", pct: false, group: "person_a",   min: 0,  max: null, step: 1000 },
  { name: "person_a_pension_start_age",   label: "Pension start age",           type: "int",   pct: false, group: "person_a",   min: 18, max: 110,  step: 1 },
  // SS claim-age slider (slice 2): SS is specified per person as an FRA ("primary
  // insurance amount") benefit + a CLAIM age (62–70). The API derives the engine's
  // effective amount = fra_amount × ss_factor(claim_age) server-side. The FRA
  // amount is a normal dollar input; the claim age renders as a SLIDER with a live
  // effective-benefit readout (control: "slider"). FRA itself (67) is NOT a control
  // — it's the constant in web/ss_claim.js. `defaultFrom` maps the field's load-time
  // seed onto the echoed default scenario, which carries the DERIVED effective
  // person_*_ss_amount / person_*_ss_start_age. For the FRA-amount fields,
  // `defaultClaimAgeFrom` names the echoed claim age, and the seed divides the
  // effective amount by ssFactor(claim age) to recover the FRA amount, so a default
  // claim age other than 67 seeds correctly. The engine echo is the single source
  // of truth — do NOT hard-code a duplicate here. Adding `hidden: true` to a field
  // renders it as a hidden input that still flows into the payload, save/load and Reset.
  { name: "person_a_ss_fra_amount",       label: "Social Security at full retirement age (67) ($)", type: "float", pct: false, group: "person_a",   min: 0,  max: null, step: 1000, defaultFrom: "person_a_ss_amount", defaultClaimAgeFrom: "person_a_ss_start_age" },
  { name: "person_a_ss_claim_age",        label: "Person A — claim age",        type: "int",   pct: false, group: "ss",    min: 62, max: 70,   step: 1, control: "slider", fraField: "person_a_ss_fra_amount", defaultFrom: "person_a_ss_start_age" },
  { name: "person_b_pension_amount",    label: "Pension annual ($)",          type: "float", pct: false, group: "person_b", min: 0,  max: null, step: 1000 },
  { name: "person_b_pension_start_age", label: "Pension start age",           type: "int",   pct: false, group: "person_b", min: 18, max: 110,  step: 1 },
  { name: "person_b_ss_fra_amount",     label: "Social Security at full retirement age (67) ($)", type: "float", pct: false, group: "person_b", min: 0,  max: null, step: 1000, defaultFrom: "person_b_ss_amount", defaultClaimAgeFrom: "person_b_ss_start_age" },
  { name: "person_b_ss_claim_age",      label: "Person B — claim age",        type: "int",   pct: false, group: "ss",    min: 62, max: 70,   step: 1, control: "slider", fraField: "person_b_ss_fra_amount", defaultFrom: "person_b_ss_start_age" },
];

const GROUP_CONTAINER = {
  primary: "primary-fields-portfolio",
  primarySpending: "primary-fields-spending",
  ss: "primary-fields-ss",
  horizon: "horizon-fields",
  person_a: "person-a-gi-fields",
  person_b: "person-b-gi-fields",
};

// The editable asset-class allocation — the SINGLE source of truth for the
// matrix rows AND the Advanced CMA inputs (both render by iterating it). Slice 4b
// (asset-class design): this is now the canonical 12-class menu, ID-NATIVE. Each
// entry carries a stable class `id` (the allocation/menu/data-binding key — what
// the POST body sends), a display `label`, the integer percent `weight` (0–100),
// the `real_return` in PERCENT, the `volatility` (annual real-return stdev, a
// FRACTION), and `data_start_year` (the earliest year of real data — the
// "testable since YYYY" hint shown on the row).
//
// Populated at init from GET /api/classes (loadClassMenu): all 12 classes render
// as rows, the two legacy classes (us_total_market / us_agg_bonds) default to the
// 60/40 canary weights (7.0% / 2.0% return from the echoed default scenario), the
// other 10 default to 0% (addable via a preset or by typing a weight). The seed
// below is the two-class fallback used only if /api/classes is unreachable, so a
// degraded load still produces the canary default. `id`s must be the engine's
// stable class ids — they bind to a series in the engine, never the display
// label.
//
// `volatility` is the per-class Monte Carlo knob (USER-EDITABLE in Advanced, MC
// mode only — shown as a PERCENT 17.4, stored as a fraction 0.174). The POST body
// emits id/weight/real_return/volatility per class so the MC sampler uses the
// live values; Deterministic/Historical ignore volatility.
// `is_equity` is SOURCED from /api/classes (firebridgelab.engine.buckets.EQUITY_CLASS_IDS, the
// single source of truth) via loadClassMenu; the fallback seed below carries the
// two legacy classes' flags so a degraded load (API unreachable) still classifies
// equity correctly for the glide presets.
let ASSET_CLASSES = [
  { id: "us_total_market", label: "US Total Stock Market", weight: 60, real_return: 7.0, volatility: 0.174, data_start_year: 1871, is_equity: true },
  { id: "us_agg_bonds",    label: "US Bonds (10-yr Treasury proxy)",    weight: 40, real_return: 2.0, volatility: 0.088, data_start_year: 1871, is_equity: false },
];

// Monte Carlo equity–bond return correlation, the single cross-class knob for
// v1's two classes. Stored as a number in [-0.99, +0.99] (the API 422s outside
// that — exactly ±1 is rank-deficient for the Cholesky factor). User-editable in
// Advanced (Build 2); mode-gated to Monte Carlo. Default +0.20 (full-history
// Shiller figure). Seeded from the init-scenario echo (seedCorrelationFromScenario)
// then re-read live on every edit; sent to /api/trajectory + /api/solve as the
// `correlation` param ONLY in Monte Carlo mode (the other engines ignore it).
let mcCorrelation = 0.2;

// The valid correlation bound, mirrored from the engine/API (CORRELATION_LIMIT =
// 0.99). Used for the client-side input min/max + the hint, so the form can't
// submit a value the API would 422. Exactly ±1 is excluded.
const CORRELATION_LIMIT = 0.99;

// Overwrite each ASSET_CLASSES entry's `volatility` from the API-echoed init
// scenario's per-class `volatility` (a FRACTION, e.g. 0.088 for Bonds). This sets
// the INITIAL value the editable per-class vol input renders with; the POST body
// then emits the live value per class.
//
// MATCHED ON CLASS `id` (asset-class design carry-forward #10): the API echo's
// allocation entries carry `id`/`label` (not the removed `name` key) since the
// slice-0 re-baseline. Matching on `a.name === cls.name` was DEAD (both undefined
// → no match on most engines, accidental match only when both happened to be
// undefined), so per-class vol seeding silently desynced now that classes carry
// differing vols. Keying on the stable `id` repairs it. A class with no matching
// echo entry keeps its menu/seeded default.
function seedVolatilitiesFromScenario(scenario) {
  if (!scenario || !Array.isArray(scenario.allocation)) return;
  for (const cls of ASSET_CLASSES) {
    const echoed = scenario.allocation.find((a) => a.id === cls.id);
    if (echoed && typeof echoed.volatility === "number") {
      cls.volatility = echoed.volatility;
    }
  }
}

// Seed the equity–bond correlation from the API-echoed init scenario (a number,
// e.g. 0.2). The echo is authoritative; falls back to the seeded default if the
// field is absent. Build 2 makes correlation editable, so this is the initial
// value the Advanced correlation input renders with.
function seedCorrelationFromScenario(scenario) {
  if (scenario && typeof scenario.correlation === "number") {
    mcCorrelation = scenario.correlation;
  }
}

// Slice 4b (asset-class design deliverable 4): build the full 12-class ASSET_CLASSES
// menu from GET /api/classes, preserving the canary default. Each class becomes a
// matrix row; the two LEGACY classes (us_total_market / us_agg_bonds) keep the
// 60/40 canary weights (60/40) + their default-scenario returns (7.0% / 2.0%), so
// the default view is byte-stable; the other 10 classes default to 0% weight (added
// via a preset or by typing a weight) and pre-fill their return/volatility from the
// menu's data-derived defaults. `data_start_year` rides along for the "since YYYY"
// row hint + the Historical window intuition.
//
// The `defaultScenario` echo (from the first POST) is the source of truth for the
// two legacy classes' weights + returns, so the canary holds exactly; /api/classes
// supplies the menu metadata (labels, the other 10 classes, data start years, the
// MC defaults). If /api/classes is unreachable the seeded 2-class fallback stands
// and the UI degrades to the legacy 2-class view.
async function loadClassMenu(defaultScenario) {
  let menu;
  try {
    const res = await fetch(CLASSES_URL);
    if (!res.ok) return; // keep the 2-class fallback
    menu = (await res.json()).classes;
  } catch (_) {
    return; // network error → keep the 2-class fallback
  }
  if (!Array.isArray(menu) || !menu.length) return;

  // The echoed default scenario's allocation (the canary 60/40), keyed by id, so
  // a class in the default allocation keeps its scenario weight + return.
  const echoed = {};
  if (defaultScenario && Array.isArray(defaultScenario.allocation)) {
    for (const a of defaultScenario.allocation) echoed[a.id] = a;
  }

  ASSET_CLASSES = menu.map((m) => {
    const inDefault = echoed[m.id];
    return {
      id: m.id,
      label: m.label,
      // Default-scenario classes keep their canary weight (60/40); others 0%.
      weight: inDefault ? Math.round(inDefault.weight * 100) : 0,
      // Returns: the default classes keep the scenario return (7.0/2.0); others
      // pre-fill from the menu's geometric-mean default (percent).
      real_return: inDefault
        ? Math.round(inDefault.real_return * 1000) / 10
        : Math.round(m.real_return * 1000) / 10,
      // Volatility: prefer the scenario echo (a fraction) for the default classes,
      // else the menu default (also a fraction).
      volatility: inDefault && typeof inDefault.volatility === "number"
        ? inDefault.volatility
        : m.volatility,
      data_start_year: m.data_start_year,
      // Equity/non-equity classification, SOURCED from the API (firebridgelab.buckets.
      // EQUITY_CLASS_IDS is the single source of truth; /api/classes publishes it).
      // The glide-preset mapping reads this instead of a hand-kept JS replica.
      is_equity: Boolean(m.is_equity),
    };
  });
}

// The equity classification as a Set of class ids, derived from the loaded class
// menu's `is_equity` flags (which the API sources from firebridgelab.buckets.
// EQUITY_CLASS_IDS — the single source of truth). Injectable for testing; defaults
// to the live ASSET_CLASSES. Passed INTO GlidePresets so the preset module stays
// classification-agnostic (no embedded equity list). If /api/classes is unreachable
// the 2-class fallback seed carries its own is_equity flags (us_total_market=true,
// us_agg_bonds=false), so this still classifies correctly in the degraded path.
function equityClassIds(menu = ASSET_CLASSES) {
  const ids = new Set();
  for (const c of menu) {
    if (c && c.is_equity) ids.add(c.id);
  }
  return ids;
}

// Slice 4b (deliverable 5): the named portfolio presets, fetched once on init.
// Each is { name, faithful, weights: {class_id: percent} }. Cached so the picker
// can populate the matrix without re-fetching.
let PRESETS = [];

async function loadPresets() {
  try {
    const res = await fetch(PRESETS_URL);
    if (!res.ok) return;
    PRESETS = (await res.json()).presets || [];
  } catch (_) {
    PRESETS = [];
  }
}

// Allocation matrix input element ids, keyed by asset-class index so the
// renderers and readers agree on a stable id even if names change. Slice 11b-2
// makes the matrix unlinkable into an A│B comparison, so weight inputs are now
// keyed by COLUMN ("a" / "b") as well as index; the B column only exists when
// the allocation is unlinked. Returns stay SHARED (no column), keyed by index.
function weightId(col, i) { return `alloc-weight-${col}-${i}`; }
function returnId(i) { return `alloc-return-${i}`; }
// Per-class Monte Carlo volatility input id (Build 2 — editable vol). Returns
// stay shared (no column), so vol matches: keyed by class index only.
function volId(i) { return `alloc-vol-${i}`; }
// The single equity–bond correlation input id (Build 2 — editable correlation).
const CORRELATION_INPUT_ID = "mc-correlation";

// Editable guardrail-knob input ids (variable-spending design slice 3). The band +
// nudge are entered as PERCENT (20, 10) and converted to fractions on the API
// path; the floor + ceiling are real dollars. Guardrails-only — the inputs exist
// only when the guardrail fieldset is rendered, and are read only under guardrails.
const GUARD_BAND_INPUT_ID = "guard-band";
const GUARD_ADJUST_INPUT_ID = "guard-adjust";
const SPENDING_FLOOR_INPUT_ID = "spending-floor";
const SPENDING_CEILING_INPUT_ID = "spending-ceiling";

// Amortization-frontier knob (amortization-frontier design decision 4). Entered as
// PERCENT (1.0) and converted to a fraction on the API path; read only under the
// amortization spending mode.
const AMORTIZATION_RETURN_INPUT_ID = "amortization-return";
// Default planning real return (1% real) — mirrors the engine default
// scenario.amortization_real_return for the initial slider value.
const AMORTIZATION_RETURN_DEFAULT = 0.01;

// Per-panel render state. chart is the Chart.js instance, kept so we can
// destroy/redraw on recompute. defaultScenario is captured from the first
// successful fetch (the API echoes the resolved scenario == defaults when no
// query params are sent); used to populate the initial input values.
const panels = {
  a: { chart: null },
  b: { chart: null },
};

// Slice 10b: the overlay chart is a separate Chart.js instance (one chart, two
// lines) drawn into its own canvas. Tracked separately from the per-panel
// charts so switching output modes can destroy exactly the instances that are
// going away, with no leaks. (The per-panel side-by-side charts live in
// `panels`; this is the comparison-overlay chart.)
const overlay = { chart: null };

// Slice 8: the view preference (overlay vs side-by-side) now lives on
// compareState. The legacy `viewMode` global is gone; reads route through
// compareState.view() and writes through compareState.setView(). The stored
// view is "overlay" | "side" (NOT "sidebyside" anymore — ComparisonState
// uses the shorter "side"); the data-view DOM attribute kept the historical
// "sidebyside" spelling, so wireUp normalises at the read site.

// Return Variability (Build 1): simulation MODE. 'deterministic' (default) is
// today's constant-real-return projection with the full A│B comparison surface.
// 'historical' replays every historical cohort; 'monte_carlo' samples correlated
// per-class Normal returns. Both stochastic modes draw the SAME fan surface
// (median + 10–90 band + descriptive depletion readout) and reuse the historical
// fan renderer — the API returns the identical payload shape for each. The mode
// toggle at the top of the inputs flips this; setMode() swaps which output region
// is visible and re-renders. Monte Carlo is single-scenario at defaults in Build
// 1 (editable vols + the solver land in Build 2).
let simMode = "deterministic";

// Stress Test (stress-test design slice 1): the active synthetic-cohort stress id
// when simMode === "stress" (the sub-selector's value). A stress is an OVERLAY on
// the current plan — it reuses the DETERMINISTIC single-line render path, only
// swapping the return environment via mode=stress + this id on the fetch. Slice 1
// has a single scenario; the sub-selector defaults to it.
let stressId = "stagflation_1970s";

// The two stochastic ("fan") modes share the historical fan view + renderer; the
// deterministic mode is the only line/comparison mode. isStochasticMode() gates
// every "show the fan view instead of the deterministic region" branch so the
// two read identically without duplicating the historical render path.
function isStochasticMode() {
  return simMode === "historical" || simMode === "monte_carlo";
}

// Stress mode reuses the deterministic single-line region (a stress produces ONE
// trajectory). isStressMode() gates the stress sub-selector visibility and the
// stress-param injection on the A-side fetch.
function isStressMode() {
  return simMode === "stress";
}

// Flow-overlay gate (slice 1). The annual-spending + GI-floor overlay
// on #trajectory-chart-a is scoped to the DETERMINISTIC SINGLE-scenario view
// ONLY: one trajectory, on the line chart (not the Historical/MC fan — that's a
// later slice), and not while comparing (the comparison panels stay single-axis).
// Stress mode (simMode === "stress") is deliberately excluded here — it keeps the
// existing separate det-spending chart; this slice doesn't touch it. Returns true
// only for plain deterministic, single-scenario runs, across ALL spending
// strategies (constant / lifecycle / guardrails / FR / amortization).
function shouldOverlayFlows() {
  return simMode === "deterministic" && !isComparing();
}

// Fan flow-overlay gate (slice 2). The twin of shouldOverlayFlows for
// the Historical / Monte-Carlo SINGLE-scenario fan: overlay the deterministic
// flows (GI floor always; spending line only in constant/lifecycle — gated at the
// caller) on the fan's secondary right-hand axis. Scoped to the stochastic fan
// view, single-scenario only — the comparison overlay / side-by-side fans stay
// untouched (byte-identical), exactly as shouldOverlayFlows fences the
// deterministic comparison panels.
function shouldOverlayFanFlows() {
  return isStochasticMode() && !isComparing();
}

// Lifecycle spending: the GLOBAL spending-shape mode. 'constant' (default) spends
// the annual-spending input flat in real terms forever — today's behavior,
// unchanged. 'lifecycle' treats that input as the GO-GO (peak) level and tapers
// later phases by their multipliers (slow-go ×0.85, no-go ×0.75). The toggle
// (setSpendingMode) flips this and sets spending_mode on BOTH scenarios' API
// calls, in BOTH simulation modes. v1: the phases are fixed constants (no UI),
// mirrored here from the engine defaults purely to render the read-only phase
// breakdown — the engine is the source of truth for the actual math.
let spendingMode = "constant";

// Dynamic (path-dependent) spending predicate. True for the spending modes whose
// withdrawn spend VARIES across market paths — guardrails (Guyton-Klinger),
// fr_smooth (funded-ratio partial-adjustment, FR slice 2), and fr_guardrail
// (funded-ratio band, FR slice 3). These are the modes the
// API returns a spending fan + adjustment readout for, are view-only (the solver is
// fenced), and draw the deterministic spending line. constant/lifecycle are pure
// functions of age → no fan, no adjustments, solver available. Used at the render
// gates so a new dynamic mode lights up the fan/line/panel without per-mode edits.
function isDynamicSpending(mode = spendingMode) {
  return (
    mode === "guardrails" ||
    mode === "fr_smooth" ||
    mode === "fr_guardrail" ||
    mode === "fr_tiers" ||
    mode === "amortization"
  );
}

// Glidepath allocation (glidepath design): the GLOBAL allocation-shape mode.
// 'constant' (default) holds the single allocation matrix across the whole horizon
// — today's behavior, byte-for-byte unchanged. 'glidepath' carries one allocation
// per DERIVED phase (a bond tent): the phase boundaries are derived from the plan's
// 401(k) access age (a fixed IRS constant, 60) plus the four GI start-age inputs.
// The toggle (setAllocationMode) flips this and sets allocation_mode +
// phase_allocations on BOTH scenarios' API calls. v1 scope: the phase allocations
// are GLOBAL (one set applied to both A and B); the constant matrix stays the A│B
// comparison surface, so the comparison path is unaffected.
let allocationMode = "constant";

// Withdrawal sourcing (bucket-strategy design): the GLOBAL sourcing-shape mode.
// 'single' (default) holds the single blended balance — today's behavior, byte-for-
// byte unchanged. 'buckets' routes to the 3-bucket cash/income/growth sourcing. The
// toggle (setSourcingMode) flips this and sets sourcing_mode on BOTH scenarios' API
// calls. Slice 1a is THIN: buckets renders the EXISTING portfolio fan + a text
// buffer-survival readout; it sends NO custom bucket config, so the API uses the
// default bucket config (the editable bucket matrices are slice 1b). Buckets +
// guardrails is fenced (the API 422s), so flipping to buckets while guardrails is
// active surfaces the API's friendly error on the readout line.
let sourcingMode = "single";

// The 401(k) penalty-free access age — the one fixed boundary the glidepath adds
// (IRS 59½ rule, modeled at the first full year of access on the integer age axis).
// Mirrored from the engine constant ALLOCATION_401K_ACCESS_AGE purely to derive the
// phase labels client-side; the engine is the source of truth for the math.
const ALLOCATION_401K_ACCESS_AGE = 60;

// Per-phase weight state for glidepath, keyed by phase index → array of integer
// percents (one per ASSET_CLASS, parallel to ASSET_CLASSES). Seeded from the single
// allocation when glidepath is first enabled (so toggling on then off is a no-op),
// then edited per phase. Preserved across phase re-derivation (a GI-age edit) by
// index, so editing an SS age doesn't wipe the phases the user already tuned.
let glidepathPhaseWeights = {};

// Bucket sourcing state (bucket-strategy design slice 1b). N1/N2 are the forward-gap
// year knobs (cash / income years); bucketWeights is one integer-% array per bucket
// (parallel to ASSET_CLASSES), the editable per-bucket allocation matrices. Seeded
// from the default bucket config (cash 100% st_treasury, income 100% us_agg_bonds,
// growth 100% us_total_market) the first time Buckets is enabled, then user-edited.
// The bucket config is GLOBAL in v1 (one set applied to BOTH A and B scenarios —
// the single/glidepath constant matrix stays the A│B surface), so it isn't keyed by
// column. BUCKET_IDS pins the cash → income → growth order the engine + payload use.
const BUCKET_IDS = ["cash", "income", "growth"];
const BUCKET_LABELS = { cash: "Cash", income: "Income", growth: "Growth" };
// The default 100%-weight class per bucket (mirrors firebridgelab.default_bucket_config).
const BUCKET_DEFAULT_CLASS = {
  cash: "st_treasury",
  income: "us_agg_bonds",
  growth: "us_total_market",
};
let bucketN1 = 2;
let bucketN2 = 7;
let bucketWeights = {}; // { cash: int[], income: int[], growth: int[] } parallel to ASSET_CLASSES
let bucketWeightsSeeded = false; // seed once on first Buckets enable (then preserve edits)

// Tax-location split (tax-buckets design tb1). Three OPTIONAL additive balances —
// taxable brokerage / tax-deferred (Trad 401k/IRA) / Roth — that must sum to
// initial_portfolio within $1. The feature is OFF when all three inputs are blank
// (the canary single-pool behavior is preserved). The state lives in the DOM
// inputs (#tax-taxable / #tax-deferred / #tax-roth); these constants name them so
// the read/write/reset/round-trip helpers share one source. tb1 is single-sourcing
// only — under buckets sourcing the inputs are disabled + the split is no-op'd
// server-side, so we don't send it. The 401(k) penalty-free door opens at 59½,
// modeled on the integer-age axis as "the last pre-door age is 59" (PRE_595_MAX_AGE
// in the engine); the readout copy says "59½".
const TAX_TAXABLE_INPUT_ID = "tax-taxable";
const TAX_DEFERRED_INPUT_ID = "tax-deferred";
const TAX_ROTH_INPUT_ID = "tax-roth";
// The "Model accounts by tax location" checkbox (tb1b). It is NOT a separate state
// flag — taxLocationActive() stays driven by the inputs (hasSplit). The box is a
// fill/clear convenience control AND a live INDICATOR of "is a split present":
// checking pre-fills the three inputs from the starting portfolio at the default
// split below; unchecking clears them; and it auto-syncs to the inputs (manual
// typing checks it, clearing unchecks it, a saved split loads it checked).
const TAX_LOCATION_ENABLE_ID = "tax-location-enable";
// An illustrative default split (taxable + tax-deferred + roth = 1.0). Used only to pre-fill the inputs on CHECK; Roth is
// the exact-sum balancer at fill time (roth = P − taxable − tax_deferred) so the
// three always sum to EXACTLY the starting portfolio (no $1 rounding drift that
// would trip the sum-validation). The inputs stay editable after the pre-fill.
const TAX_DEFAULT_TAXABLE_FRAC = 0.25;
const TAX_DEFAULT_DEFERRED_FRAC = 0.6;
const TAX_DEFAULT_ROTH_FRAC = 0.15; // documentary; Roth is computed as the balancer
// The integer age that is still BEFORE the 59½ penalty door (mirrors the engine's
// PRE_595_MAX_AGE). Used only to phrase the deterministic "spare at 59" readout.
const TAX_BRIDGE_LAST_PRE_DOOR_AGE = 59;

// Taxable-gains-fraction knob (federal-tax overlay design ft1, DECISION 4). The flat
// LTCG-taxable share of each taxable-account withdrawal — a single user-set knob,
// entered as a PERCENT (0..100) in #tax-gains-fraction, sent to the API as a 0..1
// fraction. Default 50%. It rides the SAME reveal/gate as the three tax-location
// balances (shown/enabled only when the split is on, single sourcing), but is a
// separate axis from them: it isn't part of the running-sum check and isn't cleared
// when the split is unchecked (readTaxableGainsFraction just isn't ATTACHED to the
// request while inactive — see attachTaxableGainsFraction's gate — so the value the
// user set is preserved if they re-enable the split later).
const TAX_GAINS_FRACTION_INPUT_ID = "tax-gains-fraction";
const TAX_GAINS_FRACTION_DEFAULT = 0.5; // fraction (50%) — mirrors the engine default

// Voluntary bracket-fill drawdown lever (tax-drag design td1, DECISION 5). One
// optional dollar field — each year age 60-74, draw enough extra from
// tax-deferred to fill ordinary taxable income up to this target, moving the
// excess to taxable before RMDs take over at 75. Empty = off (the default,
// mirrored server-side by Scenario.bracket_fill_target = None). Rides the SAME
// reveal/gate as the tax-location balances + gains-fraction knob (shown only
// when the split is on, disabled under buckets sourcing), but like the gains
// knob it's a separate axis: not part of the running-sum check, and the API
// only ever reads it from the dragged decomposition (single sourcing).
const BRACKET_FILL_TARGET_INPUT_ID = "bracket-fill-target";

// Spending solver (slice 2): the selected confidence dial stop, as a PERCENT
// integer (80 / 90 / 95 / 100). Default 80% (the honest default per the brief —
// a static success rate assumes no mid-retirement adjustment, so chasing
// 95–100% is artificial scarcity). Sent to /api/solve as a fraction (÷100) and
// ONLY in Historical mode — Deterministic is a single path, so the dial is
// hidden there and the solve uses the implicit "the one path must survive".
let solveConfidence = 80;

// Fixed lifecycle phase config (research defaults, v1 — NOT user-editable yet).
// Boundaries are the FIRST age of each later phase; multipliers scale DOWN from
// the go-go (peak) input. Kept in lock-step with firebridgelab.scenario's defaults; if
// those become editable later, this readout switches to reading them from the
// API-echoed scenario instead of these constants.
const SPENDING_PHASES = {
  slowGoStartAge: 75,
  noGoStartAge: 85,
  slowGoMultiplier: 0.85,
  noGoMultiplier: 0.75,
};

// Guyton-Klinger guardrail research defaults (variable-spending design slice 1),
// mirroring the engine Scenario defaults. Used ONLY for the read-only note under
// the spending toggle; the knobs are not yet UI-editable (slice 3 makes them so).
const GUARDRAIL_DEFAULTS = {
  guardBand: 0.20,
  guardAdjust: 0.10,
  floor: 85000,
  ceiling: 150000,
};

// Chart.js instances for Historical mode, tracked so each can be destroyed
// cleanly when leaving Historical mode or switching historical sub-view (mirrors
// `overlay.chart` / `panels`). Build 1–2 used only `single`; Build 3 adds the
// two-scenario comparison fans: `overlay` (one chart, two fans) and the
// side-by-side pair `sideA`/`sideB` (two fan charts, shared Y axis).
const historical = {
  single: null,
  overlay: null,
  sideA: null,
  sideB: null,
  // Variable-spending design slice 1: the single-scenario SPENDING fan (guardrails
  // mode only). Tracked alongside the portfolio fans so it's torn down the same
  // way when leaving Historical mode / switching sub-view / switching spending mode.
  spendingFan: null,
};

// Bucket-balance chart instance (bucket-strategy design slice 1b). ONE Chart.js
// line chart (3 datasets: cash/income/growth over age), tracked here so each
// redraw destroys the prior instance (no leak) — same lifecycle discipline as the
// per-panel + historical charts. null when no chart is drawn (single sourcing, MC,
// or before the first buckets render).
const bucket = { chart: null };

// Tax-location balance chart instance (tax-buckets design tb1). ONE Chart.js chart,
// tracked here so each redraw destroys the prior instance (no leak) — same
// lifecycle discipline as the bucket + per-panel + historical charts. Deterministic
// draws 3 account lines; Historical/MC draws the taxable percentile fan. null when
// no chart is drawn (no split, comparing, or before the first split render).
const taxLocation = { chart: null };

// Deterministic guardrails spending-line chart instance (dsc1 sub-slice B). ONE
// Chart.js line chart, tracked here so each redraw destroys the prior instance (no
// leak) — same lifecycle discipline as the tax-location/bucket/per-panel charts.
// Shown ONLY in Deterministic mode + guardrails spending + not comparing; null
// whenever the chart is hidden/torn down (any other mode/spending, or comparing).
const detSpending = { chart: null };

// Slice 10c: cache of the last-fetched scenario payloads ({ payloadA, payloadB })
// so flipping the view toggle (overlay <-> side-by-side) re-renders from data
// already in hand instead of re-hitting the API. Every render path updates this
// after its fetch resolves; switchView() reads it. Cleared when inputs change in
// a way that could stale it (a Recompute / link change re-fetches and refills
// it). null means "no fresh data yet — fetch on next render".
let lastPayloads = null;

// Recompute busy/re-entrancy guard. A Monte-Carlo + buckets run is ~16s
// server-side, so without a busy affordance Recompute looks frozen and a user
// may double-click (firing a second long run). recomputeInFlight is true for the
// duration of an in-flight Recompute; onRecompute ignores clicks while it's set
// (so a double-click can't queue a second run), and setRecomputeBusy() drives the
// button's disabled / aria-busy / label state. General to any slow render path —
// not buckets-specific.
let recomputeInFlight = false;

// Toggle the Recompute button's busy affordance: disabled + aria-busy + a
// "Computing…" label while a run is in flight, restored on completion (success
// OR error, via the finally in onRecompute). No-op if the button isn't present.
function setRecomputeBusy(busy) {
  const btn = document.getElementById("recompute-btn");
  if (!btn) return;
  btn.disabled = busy;
  if (busy) {
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Computing…";
  } else {
    btn.removeAttribute("aria-busy");
    btn.textContent = "Recompute";
  }
}

// ---------- Working indicator (tax-drag design td1 — timing acceptance) ----------
//
// Requirement: a long run is acceptable as long as the UI shows it is working,
// not hanging. A split-active Monte Carlo run rides
// the dragged fixed point across 10,000 trials (~15-25s measured post-memoization
// process-parallel trials + hot-loop micro-opts; was ~45-60s pre-memoization) — an
// indeterminate spinner alone doesn't satisfy the requirement; it needs a
// LIVE elapsed-seconds counter plus expected-duration copy. RunProgressView is
// the pure, DOM-free copy/formatting logic (unit-testable in a node:vm sandbox,
// same idiom as TaxLocationView/FederalTaxView); startRunProgress/
// stopRunProgress are the DOM binding + the setInterval ticker.

const RunProgressView = {
  // Appear only after this much elapsed time — fast runs (the overwhelming
  // majority: no split, or a no-split MC) never see it. TICK_MS is how often
  // the ticker re-checks/re-paints; sub-second so the visible transition at
  // APPEAR_DELAY_MS feels immediate once crossed, and the displayed seconds
  // count feels live rather than laggy.
  APPEAR_DELAY_MS: 2000,
  TICK_MS: 250,

  // Expected-duration copy, mode/split aware. Prominent + specific for
  // split-active Monte Carlo (the ~15-25s case measured post-memoization; the pre-memoization
  // timing gate measured ~45-60s);
  // split-active Historical is a few seconds (the solver's historical path is
  // ~3s per the design's timing gate); everything else (no split, or a fast
  // deterministic/stress run that nonetheless crossed 2s) gets a generic line.
  copyFor({ splitActive, mode }) {
    if (splitActive && mode === "monte_carlo") {
      return (
        "Computing taxes year-by-year across 10,000 trials — typically " +
        "15–25 seconds."
      );
    }
    if (splitActive && mode === "historical") {
      return "Computing taxes year-by-year across historical cohorts — typically a few seconds.";
    }
    return "Still working…";
  },

  // "12s elapsed" from a millisecond count. Floors (never rounds up past the
  // actual elapsed second), clamped at 0 so a clock skew can't show negative.
  elapsedLabel(elapsedMs) {
    const s = Math.max(0, Math.floor(elapsedMs / 1000));
    return `${s}s elapsed`;
  },
};
// Exposed for the node:vm test (tests/test_run_progress.mjs); not a stable API.
window.RunProgressView = RunProgressView;

// The live ticker's interval handle + start time. null when no run is in
// flight (the indicator is hidden/cleared). Module-level like recomputeInFlight
// — at most one run is ever tracked at a time (rerender()/onSolve() are always
// awaited sequentially, never overlapped).
let runProgressTimer = null;
let runProgressStartedAt = null;

// Start tracking a run: seed the copy, hide the indicator (it only reveals
// itself past APPEAR_DELAY_MS), and tick a live elapsed-seconds counter.
// `{ splitActive, mode }` picks the expected-duration copy (RunProgressView.
// copyFor). Safe to call with no #run-progress element in the DOM (the reduced
// test DOM) — every step no-ops on a missing element. Always paired with
// stopRunProgress in a finally, so the indicator can never linger stale.
function startRunProgress({ splitActive, mode }) {
  stopRunProgress(); // defensive: clear any stale prior run before starting a new one
  const wrap = document.getElementById("run-progress");
  if (!wrap) return;
  // setInterval is a host API, not core JS — absent in the node:vm sandboxes
  // the JS test suite runs app.js in (no browser, no timers stubbed). Same
  // defensive idiom as this file's sessionStorage/localStorage try/catches: a
  // missing timer degrades to "no ticking indicator," never a thrown error
  // that would break every test reaching rerender()/applyScenarioToForm.
  if (typeof setInterval !== "function") return;
  const elapsedEl = document.getElementById("run-progress-elapsed");
  const copyEl = document.getElementById("run-progress-copy");
  runProgressStartedAt = Date.now();
  wrap.hidden = true; // stays hidden until the first tick past APPEAR_DELAY_MS
  if (copyEl) copyEl.textContent = RunProgressView.copyFor({ splitActive, mode });
  if (elapsedEl) elapsedEl.textContent = "";
  runProgressTimer = setInterval(() => {
    const elapsed = Date.now() - runProgressStartedAt;
    if (elapsed >= RunProgressView.APPEAR_DELAY_MS) {
      wrap.hidden = false;
      if (elapsedEl) elapsedEl.textContent = RunProgressView.elapsedLabel(elapsed);
    }
  }, RunProgressView.TICK_MS);
}

// Stop tracking the current run: clear the ticker (so it never fires again),
// hide the indicator, and blank its text — called on BOTH success and error
// (every caller wraps its fetch in try/finally) so a failed run never leaves a
// stale "N seconds elapsed" notice on screen. Idempotent — safe to call when
// nothing is in flight (the defensive call at the top of startRunProgress).
function stopRunProgress() {
  if (runProgressTimer !== null && typeof clearInterval === "function") {
    clearInterval(runProgressTimer);
    runProgressTimer = null;
  }
  runProgressStartedAt = null;
  const wrap = document.getElementById("run-progress");
  if (wrap) wrap.hidden = true;
  const elapsedEl = document.getElementById("run-progress-elapsed");
  if (elapsedEl) elapsedEl.textContent = "";
  const copyEl = document.getElementById("run-progress-copy");
  if (copyEl) copyEl.textContent = "";
}

// Build 3: the same view-toggle cache, but for Historical mode's two-scenario
// fan payloads ({ payloadA, payloadB }, each a ?mode=historical response). Kept
// separate from lastPayloads so flipping overlay <-> side-by-side in Historical
// re-renders the fans from data in hand without re-running both simulations.
// null means "no fresh historical comparison data yet — fetch on next render".
let historicalLastPayloads = null;

// Scenario line colours. A keeps the existing blue; B is magenta. The magenta
// is chosen so that, under translucent overlap with A's blue, the two fan bands
// blend to a clean purple (Build 3's two-fan comparison) — and it stays distinct
// from A and avoids a red/green pairing. Reused for the overlay datasets, the
// historical fans, and the color-coded summary lines + A/B chips (kept in sync
// with the CSS values for .summary-line-b / .ab-tag-b in styles.css).
// Tokenized (dark-mode slice 1): these now READ their value from the CSS token
// table at render time via cssVar(), instead of being baked hex/rgb literals.
// Each is a FUNCTION (called at the chart-build site) so the value is re-read on
// every render — never cached at module load, where styles may not yet apply.
const COLOR_A = () => cssVar("--chart-band-a"); // blue (existing accent) — BAND FILL base only
const COLOR_B = () => cssVar("--chart-band-b"); // magenta (blends with blue → purple under overlap) — BAND FILL base only
// RGB triples for the two scenario colours, used to build translucent rgba()
// band fills at render time (so the alpha can vary without duplicating hexes).
const RGB_A = () => cssVar("--chart-band-a-rgb");  // COLOR_A triplet
const RGB_B = () => cssVar("--chart-band-b-rgb");  // COLOR_B triplet

// LINE-STROKE colours — deliberately DARKER / more saturated than the band
// fills above, so a scenario's median/scenario line reads as a distinct opaque
// stroke over its own (lighter, translucent) band AND over the bluish-purple
// band-overlap region. A is a deep blue; B is shifted toward crimson/berry so
// it separates from the purple overlap (where magenta + blue blend) instead of
// disappearing into it. Used for line strokes ONLY — never for band fills or
// the A/B chip colours (those stay COLOR_A/COLOR_B).
const LINE_A = () => cssVar("--chart-line-a"); // deep blue
const LINE_B = () => cssVar("--chart-line-b"); // bright crimson-rose (brighter than #9e1457 for line contrast)

// Standard stroke width for the comparison scenario / median lines. Bumped from
// 2 so the lines read clearly on top of the translucent bands (the user could
// not distinguish A from B where B crossed the purple overlap).
const LINE_WIDTH = 4;

// Captured engine defaults (from the first /api/trajectory load). Used to seed
// every field's initial value.
let defaultScenario = null;

// Slice 8: comparison axis state lives in ComparisonState (see
// web/comparison_state.js). Per-field link bools, allocation link, view
// preference, focused scenario, and the derived outputMode all read/write
// through this single seam. The atomic-transitions invariant (one mutation =
// one notify) replaces the previous scattered `linkState` object, the
// `allocationLinked` flag, `viewMode` global, and the `skipAfter` workaround
// on the old setAllocationLinked. app.js subscribes its chrome + render
// dispatch to compareState and stops poking link bools directly.
const compareState = ComparisonState.create(FIELDS.map((f) => f.name), {
  initialView: "overlay",
});

const LINK_GLYPH = "\u{1F517}"; // 🔗 linked (closed chain)
const UNLINK_GLYPH = "\u{1F513}"; // 🔓 unlinked (open lock)

// ---------- formatting helpers ----------

function fmtMoney(n) {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function fmtMillions(n) {
  return `$${(n / 1_000_000).toFixed(2)}m`;
}

// Compact "$85k" form for the lifecycle phase breakdown. Drops a trailing ".0"
// so a round $85k reads "$85k" (not "$85.0k"); a $102k taper reads "$102k".
function fmtK(n) {
  const k = n / 1_000;
  const s = Number.isInteger(k) ? String(k) : k.toFixed(1).replace(/\.0$/, "");
  return `$${s}k`;
}

function summaryLine(traj, scenario) {
  if (traj.depleted_age !== null && traj.depleted_age !== undefined) {
    return `Portfolio depletes at age ${traj.depleted_age}.`;
  }
  return `Portfolio ends at age ${scenario.end_age} with ${fmtMillions(traj.final_portfolio)}.`;
}

// ---------- guaranteed income by age (client-side) ----------

// The four [amount, start_age] GI stream pairs off an echoed scenario — the ONE
// place the stream field names live on the client, shared by the per-age summation
// below and the worst-cohort depletion clause.
function giStreams(scenario) {
  return [
    [scenario.person_a_pension_amount, scenario.person_a_pension_start_age],
    [scenario.person_b_pension_amount, scenario.person_b_pension_start_age],
    [scenario.person_a_ss_amount, scenario.person_a_ss_start_age],
    [scenario.person_b_ss_amount, scenario.person_b_ss_start_age],
  ];
}

// Guaranteed income at a given age = sum of the four per-person streams active at
// that age (a stream is active once age >= its start). Deterministic — the SAME
// summation the engine does per year (firebridgelab.engine.deterministic / firebridgelab.income.guaranteed.gi_at_age)
// — so the front-end can show GI at a hovered age in the fan tooltips without the
// API adding a `gi_by_age` array to the payload. Reads the streams off the echoed
// `scenario` the fan payload already carries (amounts + start ages). Returns 0 for
// a missing/empty scenario so a tooltip never throws.
function guaranteedIncomeAtAge(scenario, age) {
  if (!scenario) return 0;
  let gi = 0;
  for (const [amount, start] of giStreams(scenario)) {
    if (typeof amount === "number" && typeof start === "number" && age >= start) {
      gi += amount;
    }
  }
  return gi;
}

// The activity-taper multiplier at an age, mirroring firebridgelab.spending.lifecycle.activity_
// multiplier: go-go (< slow_go_start_age) ×1, slow-go ×slow_go_multiplier, no-go
// ×no_go_multiplier. Reads the (echoed) phase fields off the scenario, falling
// back to the v1 research defaults when a field is absent. Used to show
// constant/lifecycle SPENDING per age in the portfolio-fan tooltip (those modes
// have no spending fan — spending is a deterministic function of age).
function activityMultiplierAtAge(scenario, age) {
  const noGo = (scenario && scenario.no_go_start_age) || SPENDING_PHASES.noGoStartAge;
  const slowGo = (scenario && scenario.slow_go_start_age) || SPENDING_PHASES.slowGoStartAge;
  if (age >= noGo) {
    return (scenario && scenario.no_go_multiplier) || SPENDING_PHASES.noGoMultiplier;
  }
  if (age >= slowGo) {
    return (scenario && scenario.slow_go_multiplier) || SPENDING_PHASES.slowGoMultiplier;
  }
  return 1.0;
}

// Spending at an age for the constant/lifecycle modes (a pure function of age),
// mirroring firebridgelab.spending.lifecycle.spending_for_age. Guardrails has its own per-age
// spending fan, so this is only the fallback the portfolio-fan tooltip uses when
// no spending fan rides along (constant/lifecycle). Returns null for a missing
// scenario.
function deterministicSpendAtAge(scenario, age) {
  if (!scenario || typeof scenario.annual_spending !== "number") return null;
  if (scenario.spending_mode === "lifecycle") {
    return scenario.annual_spending * activityMultiplierAtAge(scenario, age);
  }
  return scenario.annual_spending;
}

// ---------- comparison-window alignment (client-side) ----------

// Map of class id → its earliest year of real data (data_start_year), built once
// from the loaded class menu (ASSET_CLASSES). A class's Historical cohort window
// can start no earlier than this year. Returns {} before the menu loads (the
// fallback then leaves the comparison unclamped, which is the prior behavior).
function classDataStartYears() {
  const out = {};
  for (const c of ASSET_CLASSES) {
    if (typeof c.data_start_year === "number") out[c.id] = c.data_start_year;
  }
  return out;
}

// The set of class ids a scenario's params actually use (weight > 0), across its
// sourcing/allocation shape:
//   - single (constant)  -> the ids in `allocationBody`.
//   - glidepath          -> the union of ids across every phase allocation.
//   - buckets            -> the union of ids across the three bucket allocations.
// Only emitted (weight > 0) classes are present in those bodies (buildAllocationBody
// drops zeros), so this is the SELECTED set — the same classes the engine's window
// policy binds on. Returns a Set of ids.
function scenarioClassIds(params) {
  const ids = new Set();
  const addAlloc = (alloc) => {
    if (Array.isArray(alloc)) for (const e of alloc) if (e && e.id) ids.add(e.id);
  };
  if (params && params.sourcing_mode === "buckets" && params.buckets) {
    for (const bid of BUCKET_IDS) {
      const b = params.buckets[bid];
      if (b) addAlloc(b.allocation);
    }
    return ids;
  }
  if (params && params.allocation_mode === "glidepath" && Array.isArray(params.phase_allocations)) {
    for (const phase of params.phase_allocations) addAlloc(phase);
    return ids;
  }
  addAlloc(params && params.allocationBody);
  return ids;
}

// A scenario's native Historical testable_since = the LATEST data_start_year
// across the classes it uses (the common-overlap window can't start before the
// shortest-history class's data begins). Mirrors the engine's window policy
// (max start across selected series); the engine remains the source of truth and
// echoes the realized `testable_since` on each payload — this client estimate is
// used ONLY to pick the comparison floor to REQUEST, never to report a result.
// Returns null when no class has a known data start (menu not loaded), so the
// caller leaves the comparison unclamped. `starts` (a class-id → data_start_year
// map) defaults to the live class menu; it's injectable so the helper is pure +
// unit-testable without a DOM.
function scenarioTestableSince(params, starts = classDataStartYears()) {
  let latest = null;
  for (const id of scenarioClassIds(params)) {
    const y = starts[id];
    if (typeof y === "number" && (latest === null || y > latest)) latest = y;
  }
  return latest;
}

// The common Historical comparison floor for an A/B pair = max(testable_since A,
// testable_since B) — the latest start year BOTH scenarios can be scored from. The
// web layer sends this as `window_start_year` to BOTH so the head-to-head measures
// them on the SAME cohort set (a buckets scenario testable since 1954 vs a 60/40
// since 1871 otherwise scores buckets only on the brutal post-1954 cohorts).
// Returns null when the two share the same native window (no clamp needed) or when
// either side's window is unknown (leave it native — don't guess). `starts` is
// injectable (defaults to the live menu) for pure unit testing.
function commonComparisonFloor(paramsA, paramsB, starts = classDataStartYears()) {
  const a = scenarioTestableSince(paramsA, starts);
  const b = scenarioTestableSince(paramsB, starts);
  if (a === null || b === null) return null;
  const floor = Math.max(a, b);
  return a === b ? null : floor;
}

// ---------- value formatting for inputs ----------

// Format a raw scenario value for display in an input (percent fields are
// shown as e.g. 5.0, stored as 0.05 in the scenario).
function fmtFieldValue(field, raw) {
  return field.pct ? (raw * 100).toFixed(1) : String(raw);
}

// The load-time default for a field, read off an echoed default scenario. Most
// fields key on their own name; the SS claim-age slider + FRA-amount fields use
// `defaultFrom` to read the DERIVED person_*_ss_amount / person_*_ss_start_age the
// API echoes. An FRA-amount field divides the effective amount by the claim-age
// factor (`defaultClaimAgeFrom`) to recover the FRA amount. Returns undefined
// when the echo lacks the key.
function defaultValueFromEcho(f, echo) {
  if (!echo || typeof echo !== "object") return undefined;
  const v = echo[f.defaultFrom || f.name];
  if (typeof v !== "number") return undefined;
  if (f.defaultClaimAgeFrom && typeof echo[f.defaultClaimAgeFrom] === "number") {
    const g = typeof window !== "undefined" ? window : globalThis;
    const factor = g.SsClaim ? g.SsClaim.ssFactor(echo[f.defaultClaimAgeFrom]) : 1;
    return Math.round((v / factor) * 100) / 100;
  }
  return v;
}

// The load-time default for a field. Falls back to 0 before the default
// scenario is known.
function defaultFieldValue(f) {
  const v = defaultValueFromEcho(f, defaultScenario);
  return typeof v === "number" ? v : 0;
}

// ---------- input element ids ----------

// The A-side (shared, authoritative) input keeps the bare field name as its id.
// The B-side input (only present when unlinked) gets a "-b" suffix. This keeps
// ids unique and lets readForm pick the right source per scenario.
function aId(name) { return name; }
function bId(name) { return `${name}-b`; }

function aInputEl(name) { return document.getElementById(aId(name)); }
function bInputEl(name) { return document.getElementById(bId(name)); }

// ---------- link-aware input rendering ----------

// Build the inputs section once on load: one .field-row per field, each
// initially in its linked (single-input) form, seeded with the default value.
function buildInputs() {
  for (const f of FIELDS) {
    const container = document.getElementById(GROUP_CONTAINER[f.group]);
    const row = document.createElement("div");
    row.className = "field-row";
    row.dataset.field = f.name;
    container.appendChild(row);
  }
  for (const f of FIELDS) renderFieldRow(f.name);
}

// (Re)render a single field row to match its current link state. Preserves the
// current A value across re-renders; on unlink, seeds B = A; on relink, B is
// dropped (its element is removed, so its value is discarded — one direction
// of truth).
function renderFieldRow(name) {
  const f = FIELDS.find((x) => x.name === name);
  const row = document.querySelector(`.field-row[data-field="${name}"]`);
  const linked = compareState.isLinked(name);

  // Capture the current A value before tearing down (first render falls back to
  // the default scenario value).
  const existingA = aInputEl(name);
  const aValue = existingA
    ? existingA.value
    : fmtFieldValue(f, defaultFieldValue(f));
  // Capture B's value only if it currently exists AND we're staying unlinked
  // (i.e. a re-render that isn't a relink). On a relink we intentionally drop it.
  const existingB = bInputEl(name);
  const bValue = existingB ? existingB.value : aValue;

  row.innerHTML = "";
  row.classList.toggle("is-unlinked", !linked);

  // Hidden (frozen) field: render the input(s) as type="hidden" and hide the row
  // entirely — no label, no link toggle, no readout. The element(s) keep the same
  // id + value as a visible row, so the payload assembler, save/load, Reset, and the
  // slider readout (all of which read by getElementById) work unchanged. We still
  // honor link state so an unlinked B value round-trips. (FRA-amount fields — see the
  // FROZEN note in FIELDS.)
  if (f.hidden) {
    row.style.display = "none";
    row.appendChild(makeHiddenInput(aId(name), aValue));
    if (!linked) row.appendChild(makeHiddenInput(bId(name), bValue));
    return;
  }

  // Label + toggle header.
  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = f.label;
  // Point the label at the A input for accessibility.
  label.htmlFor = aId(name);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-toggle";
  btn.dataset.field = name;
  btn.textContent = linked ? LINK_GLYPH : UNLINK_GLYPH;
  btn.classList.toggle("is-linked", linked);
  btn.setAttribute("aria-pressed", linked ? "false" : "true");
  const aria = linked
    ? "Linked — one value for both scenarios. Click to unlink."
    : "Unlinked — separate A and B values. Click to relink.";
  btn.setAttribute("aria-label", aria);
  btn.title = aria;
  btn.addEventListener("click", () => toggleLink(name));
  head.appendChild(label);
  head.appendChild(btn);
  row.appendChild(head);

  // Inputs.
  const inputsWrap = document.createElement("div");
  inputsWrap.className = linked ? "field-inputs linked" : "field-inputs unlinked";

  inputsWrap.appendChild(makeInput(f, aId(name), aValue, linked ? null : "A"));
  if (!linked) {
    inputsWrap.appendChild(makeInput(f, bId(name), bValue, "B"));
  }
  row.appendChild(inputsWrap);

  // Re-apply the amortization gate after a re-render rebuilds this row's input(s),
  // so the greyed/locked state + note survive a link/unlink toggle (idempotent).
  if (name === ANNUAL_SPENDING_FIELD) updateAnnualSpendingGate();
}

// Build a bare type="hidden" input carrying a frozen field's id + value. Used for
// fields flagged `hidden: true` (the FRA amounts) so they stay in the DOM/payload
// without a visible, editable row. Mirrors the id/name/value contract of makeInput's
// number box so every getElementById(...).value reader keeps working.
function makeHiddenInput(id, value) {
  const input = document.createElement("input");
  input.type = "hidden";
  input.id = id;
  input.name = id;
  input.value = value;
  return input;
}

// Build one <input> (optionally wrapped with an A/B column tag for the
// unlinked layout). A field with control:"slider" (the SS claim age) renders a
// range slider + a live effective-benefit readout instead of a number box.
function makeInput(f, id, value, abTag) {
  const wrap = document.createElement("div");
  wrap.className = "input-cell";
  if (abTag) {
    const tag = document.createElement("span");
    tag.className = `ab-tag ab-tag-${abTag.toLowerCase()}`;
    tag.textContent = abTag;
    tag.setAttribute("aria-hidden", "true");
    wrap.appendChild(tag);
  }
  if (f.control === "slider") {
    wrap.classList.add("input-cell-slider");
    return makeSliderInput(f, id, value, abTag, wrap);
  }
  const input = document.createElement("input");
  input.type = "number";
  input.id = id;
  input.name = id;
  input.value = value;
  if (f.min !== null && f.min !== undefined) input.min = String(f.min);
  if (f.max !== null && f.max !== undefined) input.max = String(f.max);
  input.step = String(f.step);
  input.required = true;
  wrap.appendChild(input);
  return wrap;
}

// Build the SS claim-age slider cell: a range <input> (id = the field id, so the
// FIELDS read/write/link machinery treats it exactly like a number input) plus a
// live readout (selected age · derived annual benefit · % of FRA). The slider's
// `input` event refreshes its readout live; the FRA-amount field's `input` event
// also refreshes it (the amount feeds the derivation) — both wired in renderFieldRow
// via a delegated form-level listener (see wireSsReadouts). `abTag` ("A"/"B"/null)
// picks which FRA-amount input is the derivation mate for an unlinked B slider.
function makeSliderInput(f, id, value, abTag, wrap) {
  const slider = document.createElement("input");
  slider.type = "range";
  slider.id = id;
  slider.name = id;
  slider.value = value;
  slider.min = String(f.min);
  slider.max = String(f.max);
  slider.step = String(f.step);
  // The FRA-amount input this slider derives against. Linked / A → the A input;
  // an unlinked B slider derives against the B FRA input. Stored on the dataset so
  // the live readout updater (ssReadoutText) can find its amount mate.
  slider.dataset.fraField = abTag === "B" ? bId(f.fraField) : aId(f.fraField);
  slider.dataset.ssSlider = "1";
  wrap.appendChild(slider);

  const readout = document.createElement("span");
  readout.className = "ss-readout";
  readout.id = `${id}-readout`;
  readout.setAttribute("aria-live", "polite");
  wrap.appendChild(readout);
  // Seed the readout to match the initial slider/FRA state.
  refreshSsReadout(slider);
  return wrap;
}

// Format the live SS readout for a claim-age slider, e.g.
// "Claim at 64 · $31,872/yr · 80% of FRA". Reads the slider's current age + its
// mate FRA-amount input (via slider.dataset.fraField), derives the effective amount
// with SsClaim.effectiveSsAmount (the same math the API applies server-side), and
// the % of FRA from SsClaim.ssFactor. A blank/invalid FRA amount shows just the age
// + factor (no dollar figure) rather than "$NaN".
function ssReadoutText(slider) {
  const age = parseInt(slider.value, 10);
  if (!Number.isFinite(age)) return "";
  let factorPct = "";
  try {
    factorPct = `${Math.round(SsClaim.ssFactor(age) * 100)}% of FRA`;
  } catch (_err) {
    return `Claim at ${age}`;
  }
  const fraEl = document.getElementById(slider.dataset.fraField);
  const fra = fraEl ? parseFloat(String(fraEl.value).trim()) : NaN;
  if (!Number.isFinite(fra)) {
    return `Claim at ${age} · ${factorPct}`;
  }
  const eff = SsClaim.effectiveSsAmount(fra, age);
  return `Claim at ${age} · ${fmtMoney(Math.round(eff))}/yr · ${factorPct}`;
}

// Write the live readout text for one claim-age slider into its sibling readout
// span. No-op if the readout element isn't present (e.g. a slider not yet rendered).
function refreshSsReadout(slider) {
  if (!slider) return;
  const readout = document.getElementById(`${slider.id}-readout`);
  if (readout) readout.textContent = ssReadoutText(slider);
}

// Refresh every claim-age slider's readout. Cheap (≤4 sliders); called when an FRA
// amount edits (the dollar figure depends on it) without needing to know which
// slider a given FRA input feeds.
function refreshAllSsReadouts() {
  for (const slider of document.querySelectorAll('input[data-ss-slider="1"]')) {
    refreshSsReadout(slider);
  }
}

function toggleLink(name) {
  // Slice 8: setFieldLink fires the atomic transition. The subscriber chain
  // (registered in init) re-renders this row AND the dependent chrome/output
  // in ONE pass — no manual renderFieldRow/afterLinkChange sequencing here.
  compareState.setFieldLink(name, !compareState.isLinked(name));
}

// Slice 8: both queries delegate to ComparisonState. masterAllLinked is the
// negation of isComparing (one less seam to keep consistent); isComparing is
// the canonical question — kept as a local wrapper so the dozens of legacy
// call sites read identically.
function masterAllLinked() {
  return !compareState.isComparing();
}

function isComparing() {
  return compareState.isComparing();
}

// Toggle the right-region layout + panel titles to match the current
// comparison state. SINGLE (nothing unlinked): .panels gets .is-single (one
// column, panel B hidden via CSS) and panel A's title becomes the neutral
// "Projection". COMPARING: .is-single is removed (panel B reappears, two
// columns) and titles revert to "Scenario A" / "Scenario B". Driven by the
// per-panel data-title-* attributes so the labels live in the HTML, not here.
function updatePanelChrome() {
  const comparing = isComparing();
  const panelsEl = document.querySelector(".panels");
  if (panelsEl) panelsEl.classList.toggle("is-single", !comparing);
  for (const panelId of ["a", "b"]) {
    const section = document.querySelector(`.panel[data-panel="${panelId}"]`);
    if (!section) continue;
    const title = section.querySelector(".panel-title");
    if (!title) continue;
    const text = comparing
      ? section.dataset.titleCompare
      : (section.dataset.titleSingle || section.dataset.titleCompare);
    if (text) title.textContent = text;
  }
}

// Slice 10b: which output container is visible, given comparison state +
// viewMode. Three mutually-exclusive states:
//   - not comparing            -> .panels (single, panel B hidden by .is-single)
//   - comparing + overlay      -> .overlay-view  (DEFAULT)
//   - comparing + sidebyside   -> .panels (two columns)
// Hiding a container also tears down the chart instance(s) it owns, so leaving
// a mode never leaks a Chart.js instance (and a destroyed chart frees its
// canvas for re-init on the way back).
function currentOutputMode() {
  // Slice 8: ComparisonState.outputMode() returns "single" | "overlay" | "side".
  // Legacy callers (the deterministic dispatch + updateOutputMode) still expect
  // "sidebyside" for the two-column state, so we map "side" -> "sidebyside" at
  // this single read site rather than changing every legacy call site.
  const mode = compareState.outputMode();
  return mode === "side" ? "sidebyside" : mode;
}

function destroyPanelCharts() {
  for (const id of ["a", "b"]) {
    if (panels[id].chart) {
      panels[id].chart.destroy();
      panels[id].chart = null;
    }
  }
}

function destroyPanelChart(id) {
  if (panels[id].chart) {
    panels[id].chart.destroy();
    panels[id].chart = null;
  }
}

function destroyOverlayChart() {
  if (overlay.chart) {
    overlay.chart.destroy();
    overlay.chart = null;
  }
}

function updateOutputMode() {
  const mode = currentOutputMode();
  const overlayView = document.querySelector(".overlay-view");
  const panelsEl = document.querySelector(".panels");
  const showOverlay = mode === "overlay";
  // .panels carries both single + side-by-side; show it for either, hide for
  // overlay. updatePanelChrome() (single vs compare titles + .is-single) still
  // runs for those two states.
  if (overlayView) overlayView.hidden = !showOverlay;
  if (panelsEl) panelsEl.hidden = showOverlay;

  // Destroy the charts that just went off-screen so they don't leak.
  if (showOverlay) {
    destroyPanelCharts();
  } else {
    destroyOverlayChart();
    // 10c makes side-by-side reachable, so collapsing to single can arrive with
    // panel B's chart still alive (it's the comparison-only panel, hidden by
    // .is-single in the single state). Tear it down so single keeps exactly one
    // live chart instance. Panel A's chart stays — renderSingle redraws it, so
    // destroying it here would only cause a flash.
    if (mode === "single") destroyPanelChart("b");
  }
}

// Slice 10c: show the view toggle ONLY when comparing. In the single-scenario
// state there is no Scenario B, so "Overlay vs Side-by-side" is meaningless —
// the control hides. Bound to the same comparison state as the rest of the
// chrome (called from afterLinkChange + init), so it appears the moment a field
// is unlinked and disappears when everything is relinked.
// NOTE: updateViewToggle() is defined later (Return Variability, Build 1) — it
// adds the "hidden in Historical mode" rule on top of the isComparing() check.

// Slice 11b-2: the deterministic-model limitation caption. Shown ONLY when the
// allocation is unlinked (an allocation comparison is active) — that's when the
// "higher equity always wins" artefact is on screen and needs the caveat. It
// sits just under the view toggle, above whichever comparison container is
// active, so it reads in BOTH overlay and side-by-side views. Hidden when
// allocation is linked (varying only non-allocation fields doesn't trigger it).
function updateAllocationCaption() {
  const cap = document.getElementById("allocation-caption");
  // Build 3: the "higher equity always wins" caveat is a DETERMINISTIC-mode
  // artefact. Historical mode IS the answer to it (it shows the volatility /
  // sequence-of-returns penalty), so the caption would be misleading there —
  // hide it in Historical even when the allocation is unlinked.
  if (cap) cap.hidden = compareState.allocationLinked() || simMode !== "deterministic";
}

// Reflect the current view in the toggle buttons (selected styling +
// aria-pressed). Kept separate from updateViewToggle (visibility) so each does
// one thing. Slice 8: the view lives on compareState; the data-view DOM
// attribute keeps the historical "sidebyside" spelling, so we normalise on
// the read.
function updateViewToggleSelection() {
  const v = compareState.view(); // "overlay" | "side"
  const domView = v === "side" ? "sidebyside" : v;
  for (const btn of document.querySelectorAll(".view-toggle-btn")) {
    const selected = btn.dataset.view === domView;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// Slice 10c / Slice 8: flip the stored view and re-render the comparison
// output. The compareState.setView() transition fires ONE notify that the
// subscriber translates into chrome refresh + a full rerender (which already
// includes updateOutputMode / updateHistoricalSubView). That full rerender
// path uses the API; for the toggle-without-recompute idiom we still want
// renderFromCache (no new fetch), so after setting the view we explicitly
// invoke it — the subscriber's rerender() will still run too, but it short-
// circuits when isComparing is true and renderFromCache has already drawn
// from the cached payloads.
//
// NB: we accept either the legacy "sidebyside" (DOM attribute spelling) or
// the new "side" (ComparisonState spelling) so wireUp doesn't need to know.
function switchView(mode) {
  let normalized;
  if (mode === "overlay") normalized = "overlay";
  else if (mode === "sidebyside" || mode === "side") normalized = "side";
  else return;
  if (!compareState.isComparing()) return;
  if (compareState.view() === normalized) return;
  compareState.setView(normalized);
  // The subscriber chain already re-rendered. We do NOT call renderFromCache
  // here separately — the subscriber's rerender() picks the right path from
  // the (still-fresh) lastPayloads cache automatically via renderFromCache.
}

// The master collapse control swaps between two shapes:
//   - all linked  -> the single "Unlink all" button (starts a comparison)
//   - comparing    -> the "Focus A | Focus B" segmented control (collapses the
//                     comparison, keeping the chosen scenario as the baseline)
// We toggle [hidden] on each rather than rewriting one button's text, because
// the comparing state is now two controls, not one.
function updateMasterButton() {
  const btn = document.getElementById("link-master");
  const focus = document.getElementById("focus-control");
  const allLinked = masterAllLinked();
  if (btn) {
    btn.hidden = !allLinked;
    btn.textContent = "Unlink all";
    btn.dataset.action = "unlink";
  }
  if (focus) focus.hidden = allLinked;
}

// Show the A/B column legend whenever at least one field is unlinked.
function updateLegend() {
  const legend = document.getElementById("ab-legend");
  if (legend) legend.hidden = masterAllLinked();
}

// Slice 8: the post-link-change refresh is now a ComparisonState subscriber
// (registered in init), fired ONCE per atomic transition. The body is exactly
// the chrome + render fan-out that was here pre-slice-8, plus a single sync
// step that re-renders any field row / the allocation matrix whose DOM state
// no longer matches its link bool. The OLD "manually call renderFieldRow then
// afterLinkChange" idiom is dead — call sites just invoke a transition on
// compareState and the subscriber handles the rest.
//
// One subtlety from pre-slice-8 preserved here: a VIEW-ONLY transition (the
// Overlay|Side-by-side toggle) used to call renderFromCache() — no new fetch
// — while every link-state change rerender()'d (which fetches). The subscriber
// discriminates by diffing the previous snapshot's link/allocation/focus axes
// against the new ones. If those match, only `view` flipped, so we skip the
// fetch and draw from the cache.
let _lastComparisonSnapshot = null;

// Set true for the duration of applyScenarioToForm(). Collapsing a comparison
// there notifies this subscriber BEFORE the new form values are written; without
// this guard the subscriber's rerender() would read (and fetch) the STALE
// scenario, and a second rerender() at the end of apply would race it. While the
// guard is set we keep the cheap chrome sync but skip the recompute; apply() runs
// exactly one rerender() after clearing the guard.
let applyingScenario = false;

function onComparisonStateChanged(snap) {
  const prev = _lastComparisonSnapshot;
  _lastComparisonSnapshot = snap;
  syncFieldRowsToState();
  // syncFieldRowsToState may have just built (unlink → new B rows) or rebuilt
  // (relink → fresh A rows) one or more SS claim-age sliders. makeSliderInput's
  // own seed call is a no-op for those because the readout span isn't in the
  // document yet when it runs; reseed every SS readout now that the rows are
  // attached. Selector covers BOTH panels (A and -b), so it fixes the empty
  // B-panel readouts on unlink and is safe/idempotent on relink and no-op
  // transitions (≤4 sliders).
  refreshAllSsReadouts();
  syncAllocationToState();
  updateMasterButton();
  updateLegend();
  updatePanelChrome();
  // Output-region sync: deterministic + stochastic each have their own
  // container family that needs reshowing/hiding when comparison state or
  // view changes. The active mode wins; the inactive mode's containers stay
  // hidden via updateModeRegions (independent of this subscriber).
  updateOutputMode();
  if (isStochasticMode()) {
    // Mirror updateOutputMode's job for the historical fan containers
    // (single / overlay / side-by-side). pre-slice-8 switchView() called
    // this directly when in a stochastic mode; the subscriber now owns it.
    updateHistoricalSubView();
  }
  updateViewToggle();          // show/hide the Overlay|Side-by-side toggle (10c)
  updateViewToggleSelection(); // keep the toggle's selected button in sync
  updateAllocationCaption();   // show/hide the deterministic-model caveat (11b-2)
  updateSolveControl();        // enable/grey the solver cluster for the comparison state (slice 2)
  // sv2: a compare overlay's note only makes sense while comparing. When the
  // comparison collapses to a single scenario (relink-all / focus-collapse),
  // drop the stale chart-adjacent note. (Cheap; runs even mid-apply, which is
  // fine — apply() also clears it explicitly.)
  if (!snap.comparing) setCompareNote("");
  // Mid-apply: applyScenarioToForm collapses the comparison BEFORE writing the
  // new form values, so any render here would read the stale scenario. Skip it;
  // apply() fires exactly one rerender() with the new values after it finishes.
  if (applyingScenario) return;
  if (_isViewOnlyTransition(prev, snap)) {
    // Cheap toggle path — no API call, no flash. Mirrors pre-slice-8
    // switchView()'s renderFromCache() use.
    renderFromCache();
  } else {
    rerender();
  }
}

// Routing decision: view-only transitions (focus null↔A/B, view overlay↔side)
// can satisfy from cache; everything else (any link bool, allocation bool)
// needs a fresh API roundtrip. The classifier itself lives in comparison_state
// where it's exercised by node tests; we only forward here.
function _isViewOnlyTransition(prev, next) {
  return ComparisonState._isViewOnlyTransition(prev, next);
}

// Re-render any field row whose .is-unlinked DOM class disagrees with the new
// link state. Cheap (12 rows max); preserves typing UX (no re-render unless
// the linked-ness flipped). renderFieldRow's "seed B = A on unlink, discard B
// on relink" logic still applies — it's the SAME function the old toggleLink
// invoked directly.
function syncFieldRowsToState() {
  for (const f of FIELDS) {
    const row = document.querySelector(`.field-row[data-field="${f.name}"]`);
    if (!row) continue;
    const domUnlinked = row.classList.contains("is-unlinked");
    const stateLinked = compareState.isLinked(f.name);
    // What the DOM SHOULD show: the negation of the linked bit. If the live
    // DOM disagrees, re-render that row. Written as `!==` against the
    // expected-unlinked bit (rather than the older `===` against the linked
    // bit) so the intent reads directly — and so a future "this looks
    // backwards, let me flip the operator" edit can't silently break sync.
    const expectedUnlinked = !stateLinked;
    if (domUnlinked !== expectedUnlinked) {
      renderFieldRow(f.name);
    }
  }
}

function syncAllocationToState() {
  const btn = document.getElementById("allocation-link");
  if (!btn) return;
  const domLinked = btn.classList.contains("is-linked");
  if (domLinked === compareState.allocationLinked()) return;
  renderAllocationToggle();
  renderAllocationRows();
  refreshAllocation();
}

// "Unlink all": ONE atomic transition that unlinks every field AND the
// allocation. The subscriber re-renders all rows (seeding B = A everywhere
// via renderFieldRow), refreshes the chrome, and re-runs the render — all in
// ONE pass. Pre-slice-8 this was a manual loop + a skipAfter:true on
// setAllocationLinked to avoid a double afterLinkChange(); the workaround
// is gone.
function onMasterLink() {
  compareState.unlinkAll();
}

// Focus collapse: stop the comparison and keep ONE scenario as the single
// baseline. Focus B copies B's values (fields + allocation) into the A inputs
// FIRST, so the relinkAll the subscriber sees has the kept-side values in the
// canonical A inputs (renderFieldRow/renderAllocationRows preserve those across
// the relink and discard the B inputs — one direction of truth). Focus A just
// relinks (A's values are already canonical).
// The DOM-value copy is a pre-state-change step (it reads the live B inputs
// before they're torn down by the re-render that the relink transition
// triggers). After the copy, compareState.relinkAll() fires ONE atomic
// transition; the subscriber runs the full chrome + render fan-out.
function copyBIntoA() {
  for (const f of FIELDS) {
    if (compareState.isLinked(f.name)) continue; // linked fields already share
    const bEl = bInputEl(f.name);
    const aEl = aInputEl(f.name);
    if (bEl && aEl) aEl.value = bEl.value;
  }
  if (!compareState.allocationLinked()) {
    ASSET_CLASSES.forEach((_, i) => {
      const bEl = document.getElementById(weightId("b", i));
      const aEl = document.getElementById(weightId("a", i));
      if (bEl && aEl) aEl.value = bEl.value;
    });
  }
}

function onFocus(which) {
  if (!compareState.isComparing()) return;
  // Kept-side identity is the CALLER's responsibility: if B is the survivor,
  // we copy B's DOM values into the A inputs BEFORE we fire the relink, so
  // the subscriber's full re-render reads canonical-A values that already
  // reflect the kept side. ComparisonState.relinkAll() doesn't need to know.
  if (which === "b") copyBIntoA();
  // Atomic exit: all link bools flip true, allocation flips true, focus
  // clears, the subscriber re-renders once.
  compareState.relinkAll();
}

// ---------- Slice 11b-2: unlinkable A│B allocation matrix ----------
//
// LINKED (default): one weight column (A) drives both scenarios; returns shared.
// UNLINKED: two weight columns (A│B), each scenario's weights independent; the
// shared returns still apply to both. Weights are INTEGERS (0–100); returns stay
// decimal. The matrix has its own link toggle + dedicated allocationLinked
// state, wired into the SAME afterLinkChange()/isComparing() machinery the
// fields use so the adaptive render reacts live. Per-COLUMN Total + blended
// lines (A always; B only when unlinked), each with its own ✓/✗ state.
//
// Two renderers (matrix rows in the sidebar; return inputs in Advanced) both
// iterate ASSET_CLASSES, so the matrix is N-class-ready (12 classes in slice 4b).
// buildAllocationBody takes a column selector so readScenarios can build A's and
// B's id-native POST allocation independently.

const ALLOC_COLS = ["a", "b"];

// Which columns are currently active: always A; B too when unlinked.
function activeAllocCols() {
  return compareState.allocationLinked() ? ["a"] : ["a", "b"];
}

// True when at least one allocation-weight input actually exists in the DOM. The
// matrix is always rendered in the real app, but pre-render / unit-test sandboxes
// have NO weight inputs at all. follow-up: distinguish "the matrix DOM
// doesn't exist" (every getElementById(weightId) is null → skip the strategy
// guard so it can't false-positive a missing matrix as invalid) from "inputs are
// present but sum to ≠100%" (the real all-zero bug, which MUST still bail). This
// only reports PRESENCE — a present-but-zeroed input still counts as present, so
// the all-zero column check downstream is untouched.
function allocationWeightInputsPresent() {
  for (const col of activeAllocCols()) {
    for (let i = 0; i < ASSET_CLASSES.length; i++) {
      if (document.getElementById(weightId(col, i))) return true;
    }
  }
  return false;
}

// Read a weight input's INTEGER percent for the given column ("a"/"b"). Empty /
// NaN → 0 so the live Total + validity reflect a half-typed value as "off 100"
// rather than throwing. Slice 11b-2: weights are integers — Math.round guards
// against any stray decimal a user pastes in.
function readWeight(col, i) {
  const el = document.getElementById(weightId(col, i));
  if (!el) return ASSET_CLASSES[i].weight;
  const n = parseInt(el.value.trim(), 10);
  return Number.isNaN(n) ? 0 : n;
}

// Read a return input's numeric value (percent). Empty / NaN → 0. Returns are
// shared across A and B, so there's no column dimension here.
function readReturn(i) {
  const el = document.getElementById(returnId(i));
  if (!el) return ASSET_CLASSES[i].real_return;
  const n = parseFloat(el.value.trim());
  return Number.isNaN(n) ? 0 : n;
}

// Read a class's volatility input (shown as a PERCENT, e.g. 17.4) back to a
// FRACTION (0.174) for the API. Falls back to the model value (ASSET_CLASSES[i]
// .volatility, already a fraction) when the input is absent (the vol inputs only
// render in Monte Carlo mode) or empty/NaN, so non-MC requests still emit the
// seeded default 4th field. Shared across A and B (no column).
function readVolFraction(i) {
  const el = document.getElementById(volId(i));
  if (!el) return ASSET_CLASSES[i].volatility;
  const raw = el.value.trim();
  if (raw === "") return ASSET_CLASSES[i].volatility;
  const n = parseFloat(raw);
  return Number.isNaN(n) ? ASSET_CLASSES[i].volatility : n / 100;
}

// Read the equity–bond correlation input. Falls back to mcCorrelation (the
// seeded/last-known value) when the input is absent (non-MC modes don't render
// it) or empty/NaN, so a request always carries a sane value. Clamped to the
// valid [-LIMIT, +LIMIT] range so a half-typed "1" can't 422 a live recompute.
function readCorrelation() {
  const el = document.getElementById(CORRELATION_INPUT_ID);
  if (!el) return mcCorrelation;
  const raw = el.value.trim();
  if (raw === "") return mcCorrelation;
  const n = parseFloat(raw);
  if (Number.isNaN(n)) return mcCorrelation;
  return Math.max(-CORRELATION_LIMIT, Math.min(CORRELATION_LIMIT, n));
}

// Read the taxable-gains-fraction knob (federal-tax overlay design ft1, DECISION 4)
// as a FRACTION (0..1) off the percent input. Blank/invalid -> the knob default
// (50%). Clamped to [0, 1] so a stray "150" can't 422 a live recompute (mirrors
// readCorrelation's clamp-not-throw discipline). The input is static markup (like
// the three tax-location balances), so — unlike readCorrelation, whose input is
// built only in MC mode — there's no "torn down" case; a missing element only
// happens in the reduced test DOM, where the default is the right fallback too.
function readTaxableGainsFraction() {
  const el = document.getElementById(TAX_GAINS_FRACTION_INPUT_ID);
  if (!el) return TAX_GAINS_FRACTION_DEFAULT;
  const raw = el.value.trim();
  if (raw === "") return TAX_GAINS_FRACTION_DEFAULT;
  const n = parseFloat(raw);
  if (Number.isNaN(n)) return TAX_GAINS_FRACTION_DEFAULT;
  return Math.max(0, Math.min(1, n / 100));
}

// Write the taxable-gains-fraction knob from a FRACTION (0..1) -> the percent input
// (mirrors setInputValueById's value*scale convention the guardrail knobs use).
// Used by applyScenarioToForm (restore from a save / the reset default).
function setTaxGainsFractionInput(fraction) {
  setInputValueById(TAX_GAINS_FRACTION_INPUT_ID, fraction, 100);
}

// Read the bracket-fill lever (tax-drag design td1, DECISION 5) as a dollar
// amount, or null when blank/absent/invalid — "empty = off" is the feature
// gate itself (mirrors readTaxBalance's blank-is-null discipline, NOT
// readTaxableGainsFraction's blank-is-default discipline, since the lever has
// no default value to fall back to — off IS the default).
function readBracketFillTarget() {
  const el = document.getElementById(BRACKET_FILL_TARGET_INPUT_ID);
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (raw === "") return null;
  const num = parseFloat(raw);
  return Number.isFinite(num) ? num : null;
}

// Write the bracket-fill lever from a dollar amount (or null/non-number to
// clear it to blank — off). Used by applyScenarioToForm (restore from a save /
// the reset default). Mirrors setTaxLocationInputs' null-clears discipline
// (not setTaxGainsFractionInput's percent-scale write, since this is a raw
// dollar value with no scale factor).
function setBracketFillTargetInput(value) {
  const el = document.getElementById(BRACKET_FILL_TARGET_INPUT_ID);
  if (!el) return;
  el.value = Number.isFinite(value) ? String(Math.round(value)) : "";
}

function allocationWeightSum(col) {
  return ASSET_CLASSES.reduce((sum, _, i) => sum + readWeight(col, i), 0);
}

// Single source of truth for validity, the displayed Total, and the emitted
// allocation weights: each weight rounded to a 4-decimal FRACTION (percent/100),
// exactly as buildAllocationBody emits it. With INTEGER weights summing to 100 the
// fractions are exact (the 11b-1 rounding edge case is now impossible), but we
// keep the rounded-fraction logic intact so validity is still checked on the SAME
// fractions the API receives.
function allocationFractions(col) {
  return ASSET_CLASSES.map((_, i) => Math.round((readWeight(col, i) / 100) * 1e4) / 1e4);
}

// True when the rounded fractions for a column sum to 1 within the API's 1e-6
// tolerance.
function allocationTotalValid(col) {
  const sum = allocationFractions(col).reduce((s, x) => s + x, 0);
  return Math.abs(sum - 1) < 1e-6;
}

// The displayed Total (percent) for a column, from the SAME rounded fractions
// used for validity — so it never reads "100%" while the allocation is invalid.
function allocationTotalPct(col) {
  const sum = allocationFractions(col).reduce((s, x) => s + x, 0);
  return sum * 100;
}

// Blended real return (percent) for a column = Σ (weight/100 × real_return),
// using the shared returns.
function blendedRealReturn(col) {
  return ASSET_CLASSES.reduce(
    (sum, _, i) => sum + (readWeight(col, i) / 100) * readReturn(i),
    0,
  );
}

// The id-native POST allocation body for a column (asset-class design B4): a list
// of {id, weight, real_return, volatility} entries, one per SELECTED class (weight
// > 0), in engine units (weight + real_return as fractions, volatility already a
// fraction). This is the multi-class transport — it carries the stable class id,
// so the engine binds each class to its series directly (no name→id alias). Sent
// as the JSON body to POST /api/trajectory + /api/solve.
//
// Only classes with a NON-ZERO weight are emitted — a 12-class menu mostly sits at
// 0%, and sending 0%-weight classes would (a) bloat the body and (b) for
// Historical, a 0%-weight-but-mapped class does NOT bind the window on the engine
// side anyway, but a 0%-weight class with a SHORTER history would still be
// "mapped" and could confuse the read; dropping zeros keeps the request to exactly
// what the user allocated. The weights of the emitted subset still sum to 100%
// (validated before send), so renormalization is a no-op.
//
// real_return is read live from the shared per-class return input (percent →
// fraction); volatility live from the per-class vol input when present (MC mode),
// else the seeded/menu default. Deterministic + Historical ignore volatility.
//
// EXPLICIT CORRELATION MATRIX (asset-class design carry-forward #11): this UI does
// NOT send an explicit N×N `correlations` matrix for multi-class MC. For N>2 the
// engine builds the per-pair/max-overlap ESTIMATED DEFAULT matrix itself
// (firebridgelab.returns.correlation, D6) — the recommended v1 path; the single scalar
// `correlation` knob is the N≤2 fast-path only and the engine ignores it for N>2.
// So there is no allocation-order → mapped-subset reindex to do on the client
// (the 66-pair editor is explicitly deferred). If a future slice adds an explicit
// matrix editor, it must reindex the supplied matrix to the mapped subset before
// sending (the engine validates an explicit matrix against the MAPPED count).
function buildAllocationBody(col) {
  const fractions = allocationFractions(col);
  const entries = [];
  ASSET_CLASSES.forEach((c, i) => {
    if (fractions[i] <= 0) return; // drop 0%-weight menu classes
    entries.push({
      id: c.id,
      weight: fractions[i],
      real_return: Math.round((readReturn(i) / 100) * 1e4) / 1e4,
      volatility: Math.round(readVolFraction(i) * 1e4) / 1e4,
    });
  });
  return entries;
}

// Render the matrix header (title + link toggle + A│B column legend when
// unlinked) and one row per asset class. LINKED: [label][A weight]. UNLINKED:
// [label][A weight][B weight]. The toggle mirrors the per-field link button's
// look + aria (buildInputs / makeInput).
// `fromModel` (default false) forces the weight inputs to seed from the
// ASSET_CLASSES model (`c.weight`) instead of the existing DOM inputs. The
// normal re-render path (unlink/relink, glidepath toggle, link sync) must
// PRESERVE in-flight DOM edits, so it reads the live inputs. But a scenario
// LOAD (applyScenarioToForm) has just rewritten `c.weight` per class and the
// stale DOM still shows the pre-load weights; seeding from the DOM there would
// silently discard the loaded allocation (the saved-scenario revert bug). Apply
// passes fromModel=true so the just-written model wins.
function renderAllocationRows(fromModel = false) {
  const container = document.getElementById("allocation-rows");
  if (!container) return;

  // Capture existing weight values so re-renders preserve them. On UNLINK the B
  // column is seeded from A (one direction of truth); on RELINK B is dropped
  // (its inputs are removed, discarding its values). On a scenario LOAD
  // (fromModel) the DOM is stale, so seed every column from the model instead.
  const prev = {};
  for (const col of ALLOC_COLS) {
    prev[col] = ASSET_CLASSES.map((c, i) => {
      if (fromModel) return String(c.weight);
      const el = document.getElementById(weightId(col, i));
      return el ? el.value : String(c.weight);
    });
  }
  const cols = activeAllocCols();
  const value = (col, i) => {
    if (col === "b") {
      // Seed B from A when B doesn't exist yet (just unlinked); else keep B's
      // own current value across a re-render.
      const bEl = document.getElementById(weightId("b", i));
      return bEl ? prev.b[i] : prev.a[i];
    }
    return prev.a[i];
  };

  const allocLinkedNow = compareState.allocationLinked();
  container.innerHTML = "";
  container.classList.toggle("is-unlinked", !allocLinkedNow);

  ASSET_CLASSES.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "allocation-row";
    row.classList.toggle("is-unlinked", !allocLinkedNow);

    const label = document.createElement("label");
    label.className = "allocation-label";
    label.textContent = c.label;
    label.htmlFor = weightId("a", i);
    // "Testable since YYYY" data-start hint on the row (asset-class design D3 /
    // deliverable 7): the earliest year this class has real data, so the user can
    // see at a glance which classes will shorten a Historical replay window.
    if (c.data_start_year) {
      const hint = document.createElement("span");
      hint.className = "allocation-row-hint";
      hint.textContent = `since ${c.data_start_year}`;
      hint.title = `Real data from ${c.data_start_year} — Historical replay is limited to the shared window of the selected classes.`;
      label.appendChild(document.createTextNode(" "));
      label.appendChild(hint);
    }
    row.appendChild(label);

    const weights = document.createElement("div");
    weights.className = allocLinkedNow
      ? "allocation-weights linked"
      : "allocation-weights unlinked";
    for (const col of cols) {
      weights.appendChild(makeWeightCell(c, col, i, value(col, i)));
    }
    row.appendChild(weights);

    container.appendChild(row);
  });
}

// One weight cell: an integer % input, optionally tagged A/B (only when
// unlinked, matching the field A/B tag styling).
function makeWeightCell(c, col, i, value) {
  const cell = document.createElement("div");
  cell.className = "allocation-weight-cell";
  if (!compareState.allocationLinked()) {
    const tag = document.createElement("span");
    tag.className = `ab-tag ab-tag-${col}`;
    tag.textContent = col.toUpperCase();
    tag.setAttribute("aria-hidden", "true");
    cell.appendChild(tag);
  }
  const input = document.createElement("input");
  input.type = "number";
  input.id = weightId(col, i);
  input.name = weightId(col, i);
  input.className = "allocation-weight-input";
  input.value = value;
  input.min = "0";
  input.max = "100";
  input.step = "1";
  input.required = true;
  // A manual weight edit drops the preset label back to "Custom" (the matrix no
  // longer matches the named portfolio), then refreshes the live readout.
  input.addEventListener("input", () => {
    resetPresetToCustom();
    refreshAllocation();
  });
  const pct = document.createElement("span");
  pct.className = "allocation-pct";
  pct.textContent = "%";
  cell.appendChild(input);
  cell.appendChild(pct);
  return cell;
}

// The allocation link toggle in the section header, mirroring buildInputs'
// per-field button (glyph, is-linked class, aria-pressed, aria-label/title).
function renderAllocationToggle() {
  const btn = document.getElementById("allocation-link");
  if (!btn) return;
  const linked = compareState.allocationLinked();
  btn.textContent = linked ? LINK_GLYPH : UNLINK_GLYPH;
  btn.classList.toggle("is-linked", linked);
  btn.setAttribute("aria-pressed", linked ? "false" : "true");
  const aria = linked
    ? "Linked — one allocation for both scenarios. Click to unlink."
    : "Unlinked — separate A and B allocations. Click to relink.";
  btn.setAttribute("aria-label", aria);
  btn.title = aria;
}

// Slice 8: the allocation link toggle drives ONE atomic transition. The
// subscriber re-renders the matrix (renderAllocationRows seeds B = A on unlink,
// drops B on relink) and the rest of the chrome in one pass.
function toggleAllocationLink() {
  compareState.setAllocationLinked(!compareState.allocationLinked());
}

// Render the shared per-class CMA inputs in Advanced: one row per class with the
// real-return input and, co-located beside it, the Monte Carlo volatility input
// (Build 2). Both are SHARED across A and B (no column). The return input is
// always present; the vol cell is mode-gated — it renders only in Monte Carlo
// mode (vol is meaningless to Deterministic/Historical, which ignore it), and
// updateMcInputsVisibility() shows/hides the cells when the mode flips. Returns
// stay where they are; vol sits next to them so the two per-class knobs read as
// a pair. Both are in PERCENT (return 7.0, vol 17.4) and convert to fractions on
// the API path.
function renderReturnInputs() {
  const container = document.getElementById("returns-fields");
  if (!container) return;
  container.innerHTML = "";
  ASSET_CLASSES.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "field-row cma-row";

    const head = document.createElement("div");
    head.className = "field-head";
    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = c.label;
    label.htmlFor = returnId(i);
    head.appendChild(label);
    row.appendChild(head);

    // The two per-class inputs sit in a [return | volatility] grid. The vol cell
    // carries .cma-vol-cell so the mode gate can show/hide just it.
    const inputs = document.createElement("div");
    inputs.className = "cma-inputs";

    inputs.appendChild(
      makeCmaCell("Return", returnId(i), String(c.real_return), {
        // Floor matches the API's _RETURN_MIN (-5%): a few classes (broad
        // commodities) have a slightly-negative geometric-mean real return.
        min: "-5", max: "15", step: "0.1", suffix: "%",
      }),
    );
    inputs.appendChild(
      makeCmaCell("Volatility", volId(i), (c.volatility * 100).toFixed(1), {
        min: "0", max: "100", step: "0.1", suffix: "%", extraClass: "cma-vol-cell",
      }),
    );

    row.appendChild(inputs);
    container.appendChild(row);
  });
  renderCorrelationRow();
  updateMcInputsVisibility();
}

// One labelled CMA input cell (a sub-label above a percent input). Used for the
// per-class return + volatility inputs so they read as a clearly-named pair.
function makeCmaCell(subLabel, id, value, { min, max, step, suffix, extraClass } = {}) {
  const cell = document.createElement("div");
  cell.className = "cma-cell" + (extraClass ? ` ${extraClass}` : "");
  const sub = document.createElement("label");
  sub.className = "cma-sublabel";
  sub.textContent = subLabel;
  sub.htmlFor = id;
  cell.appendChild(sub);

  const inputWrap = document.createElement("div");
  inputWrap.className = "input-cell";
  const input = document.createElement("input");
  input.type = "number";
  input.id = id;
  input.name = id;
  input.value = value;
  if (min !== undefined) input.min = min;
  if (max !== undefined) input.max = max;
  if (step !== undefined) input.step = step;
  input.required = true;
  input.addEventListener("input", refreshAllocation);
  inputWrap.appendChild(input);
  if (suffix) {
    const s = document.createElement("span");
    s.className = "cma-suffix";
    s.textContent = suffix;
    inputWrap.appendChild(s);
  }
  cell.appendChild(inputWrap);
  return cell;
}

// Render the single equity–bond correlation input (Monte Carlo only, Build 2).
// One number in [-0.99, +0.99]; default +0.20. Mode-gated like the vol cells.
// Lives in #correlation-field (in the Advanced returns fieldset, beneath the
// per-class CMA rows) so the cross-class knob sits with the per-class ones.
function renderCorrelationRow() {
  const container = document.getElementById("correlation-field");
  if (!container) return;
  container.innerHTML = "";

  const row = document.createElement("div");
  row.className = "field-row correlation-row";

  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = "Equity–Bond correlation";
  label.htmlFor = CORRELATION_INPUT_ID;
  head.appendChild(label);
  row.appendChild(head);

  const wrap = document.createElement("div");
  wrap.className = "field-inputs linked";
  const cell = document.createElement("div");
  cell.className = "input-cell";
  const input = document.createElement("input");
  input.type = "number";
  input.id = CORRELATION_INPUT_ID;
  input.name = CORRELATION_INPUT_ID;
  input.value = String(mcCorrelation);
  input.min = String(-CORRELATION_LIMIT);
  input.max = String(CORRELATION_LIMIT);
  input.step = "0.05";
  input.required = true;
  input.addEventListener("input", refreshAllocation);
  cell.appendChild(input);
  wrap.appendChild(cell);
  row.appendChild(wrap);

  const hint = document.createElement("p");
  hint.className = "cma-hint";
  hint.textContent = `Range −${CORRELATION_LIMIT} to +${CORRELATION_LIMIT}. Default +0.20.`;
  row.appendChild(hint);

  container.appendChild(row);
}

// Show the Monte Carlo per-class volatility cells + the correlation row ONLY in
// Monte Carlo mode (they're meaningless to Deterministic/Historical, which
// ignore the field — surfacing them there would confuse those modes). The return
// inputs always stay visible. Called on load + on every mode flip (setMode).
function updateMcInputsVisibility() {
  const show = simMode === "monte_carlo";
  for (const el of document.querySelectorAll(".cma-vol-cell")) {
    el.hidden = !show;
  }
  const corr = document.getElementById("correlation-field");
  if (corr) corr.hidden = !show;
  // The fieldset legend reflects what's editable: returns-only outside MC, both
  // CMA knobs in MC.
  const legend = document.getElementById("returns-legend");
  if (legend) {
    legend.textContent = show
      ? "Asset-class returns & volatility (%)"
      : "Asset-class real returns (%)";
  }
}

// ---------- Editable guardrail knobs (variable-spending design slice 3) ----------
//
// The four Guyton-Klinger params, editable in the Advanced section ONLY in
// Guardrails spending mode. Band + nudge entered as PERCENT (20, 10); floor +
// ceiling as real dollars. Seeded from the engine research defaults
// (GUARDRAIL_DEFAULTS). Mirrors the per-class CMA-input pattern: rendered once
// into #guardrail-fields, shown/hidden by spending mode, read live into the
// request params and validated against the same bounds the API enforces.

// One labelled guardrail input cell (a label above a number input with a suffix).
function makeGuardrailCell(labelText, id, value, { min, max, step, suffix, hint }) {
  const row = document.createElement("div");
  row.className = "field-row guardrail-row";

  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = labelText;
  label.htmlFor = id;
  head.appendChild(label);
  row.appendChild(head);

  const wrap = document.createElement("div");
  wrap.className = "field-inputs linked";
  const cell = document.createElement("div");
  cell.className = "input-cell";
  const input = document.createElement("input");
  input.type = "number";
  input.id = id;
  input.name = id;
  input.value = value;
  if (min !== undefined) input.min = min;
  if (max !== undefined) input.max = max;
  if (step !== undefined) input.step = step;
  input.required = true;
  input.addEventListener("input", updateSpendingBreakdown);
  cell.appendChild(input);
  if (suffix) {
    const s = document.createElement("span");
    s.className = "cma-suffix";
    s.textContent = suffix;
    cell.appendChild(s);
  }
  wrap.appendChild(cell);
  row.appendChild(wrap);

  if (hint) {
    const h = document.createElement("p");
    h.className = "cma-hint";
    h.textContent = hint;
    row.appendChild(h);
  }
  return row;
}

// Render the four guardrail inputs into #guardrail-fields, seeded with the
// research defaults (band 20%, nudge 10%, floor $85k, ceiling $150k). Built once
// (on buildAllocation); the fieldset is shown/hidden by updateGuardrailInputsVisibility.
function renderGuardrailInputs() {
  const container = document.getElementById("guardrail-fields");
  if (!container) return;
  container.innerHTML = "";
  container.appendChild(
    makeGuardrailCell("Guardrail band", GUARD_BAND_INPUT_ID,
      String(Math.round(GUARDRAIL_DEFAULTS.guardBand * 100)),
      { min: "0", max: "99", step: "1", suffix: "%",
        hint: "Trip a cut/raise when the funded withdrawal rate drifts this far from its start." }),
  );
  container.appendChild(
    makeGuardrailCell("Nudge size", GUARD_ADJUST_INPUT_ID,
      String(Math.round(GUARDRAIL_DEFAULTS.guardAdjust * 100)),
      { min: "0", max: "100", step: "1", suffix: "%",
        hint: "How big each adjustment is. Crank toward 100% to turn the gentle nudge into a jump to the rail." }),
  );
  container.appendChild(
    makeGuardrailCell("Spending floor", SPENDING_FLOOR_INPUT_ID,
      String(GUARDRAIL_DEFAULTS.floor),
      { min: "0", step: "1000", suffix: "$",
        hint: "Spending never drops below this (default $85k). The $45k absolute floor is a survivable reference level, shown as a fainter line on the fan, not a clamp." }),
  );
  container.appendChild(
    makeGuardrailCell("Satiation ceiling", SPENDING_CEILING_INPUT_ID,
      String(GUARDRAIL_DEFAULTS.ceiling),
      { min: "0", step: "1000", suffix: "$",
        hint: "The most a year can absorb. Lower it and the median spend caps lower in strong markets." }),
  );
}

// Show the guardrail fieldset ONLY in Guardrails spending mode (the four params
// are read only by the GK strategy; meaningless in Constant/Lifecycle). Called on
// load + on every spending-mode flip (setSpendingMode).
function updateGuardrailInputsVisibility() {
  const group = document.getElementById("guardrail-group");
  if (group) group.hidden = spendingMode !== "guardrails";
}

// Read one guardrail input as a number, falling back to `dflt` when absent/blank/
// NaN. `scale` divides the entered value (100 for the percent inputs → fraction).
function readGuardrailNumber(id, dflt, scale = 1) {
  const el = document.getElementById(id);
  if (!el) return dflt;
  const raw = el.value.trim();
  if (raw === "") return dflt;
  const n = parseFloat(raw);
  if (Number.isNaN(n)) return dflt;
  return n / scale;
}

// The four guardrail params for the request body (dynamic-spending modes). Band +
// nudge converted from percent to fraction; floor + ceiling are dollars. Returns
// null under constant/lifecycle so the params object omits them (the API then uses
// the scenario defaults — keeps constant/lifecycle requests byte-identical).
//
// fr_smooth reuses the SAME floor/ceiling rails (the clamp is identical to the
// guardrail clamp), so it sends these too. The band/adjust pair is meaningless to
// the funded-ratio strategy (it has no band trip) but harmless: the engine ignores
// them and the API validates the supplied values regardless. Sending the whole
// block keeps one read path and lets a floor/ceiling set under guardrails carry
// across a mode flip.
function readGuardrails() {
  if (!isDynamicSpending()) return null;
  const params = {
    guard_band: readGuardrailNumber(GUARD_BAND_INPUT_ID, GUARDRAIL_DEFAULTS.guardBand, 100),
    guard_adjust: readGuardrailNumber(GUARD_ADJUST_INPUT_ID, GUARDRAIL_DEFAULTS.guardAdjust, 100),
    spending_floor: readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor),
    spending_ceiling: readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling),
  };
  // The amortization frontier rides the SAME dynamic-spending params path (so the
  // knob flows through the existing assembler Object.assign + the save/load block),
  // but ONLY under amortization mode — adding it to the guardrails / FR blocks would
  // change those modes' saved-scenario shape. Other dynamic modes never read it, and
  // the engine carries its own default, so omitting it elsewhere is byte-safe.
  if (spendingMode === "amortization") {
    params.amortization_real_return = readGuardrailNumber(
      AMORTIZATION_RETURN_INPUT_ID, AMORTIZATION_RETURN_DEFAULT, 100,
    );
  }
  return params;
}

// ---------- Amortization Frontier knob (amortization-frontier design) ----------
//
// ONE adjustable slider: the planning real return the frontier discounts the
// GI-netted gap stream at. Entered as PERCENT (1.0); converted to a fraction on the
// API path (readGuardrails). A range slider + a live readout so the user can DRAG it
// and watch the frontier rise / the late-life fan crater.

// Render the amortization-return slider into #amortization-fields, seeded at the
// engine default (1% real). Built once (on buildAllocation); shown/hidden by
// updateAmortizationInputsVisibility.
function renderAmortizationInput() {
  const container = document.getElementById("amortization-fields");
  if (!container) return;
  container.innerHTML = "";

  const row = document.createElement("div");
  row.className = "field-row amortization-row";

  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = "Amortization return (real)";
  label.htmlFor = AMORTIZATION_RETURN_INPUT_ID;
  head.appendChild(label);
  row.appendChild(head);

  const wrap = document.createElement("div");
  wrap.className = "field-inputs linked amortization-slider-wrap";
  const input = document.createElement("input");
  input.type = "range";
  input.id = AMORTIZATION_RETURN_INPUT_ID;
  input.name = AMORTIZATION_RETURN_INPUT_ID;
  input.min = "0";       // 0% real → straight-line amortization
  input.max = "7";       // up to a spicy all-equity expected real return
  input.step = "0.1";    // 0.1% increments
  input.value = String(AMORTIZATION_RETURN_DEFAULT * 100);
  input.className = "amortization-slider";

  const readout = document.createElement("span");
  readout.className = "amortization-readout cma-suffix";
  const sync = () => { readout.textContent = `${parseFloat(input.value).toFixed(1)}% real`; };
  sync();
  // Live readout on drag; recompute on release/commit (change), matching the other
  // tuning inputs which recompute on Recompute / input commit.
  input.addEventListener("input", sync);
  input.addEventListener("change", () => { sync(); rerender(); });

  wrap.appendChild(input);
  wrap.appendChild(readout);
  row.appendChild(wrap);

  const hint = document.createElement("p");
  hint.className = "cma-hint";
  hint.textContent =
    "The conservative real return the frontier amortizes against. Drag it up toward " +
    "your allocation's expected return to see the ceiling rise — and the downside fan crater.";
  row.appendChild(hint);

  container.appendChild(row);
}

// Show the amortization fieldset ONLY in Amortization Frontier spending mode (the
// knob is read only by that strategy). Called on load + every spending-mode flip.
function updateAmortizationInputsVisibility() {
  const group = document.getElementById("amortization-group");
  if (group) group.hidden = spendingMode !== "amortization";
  // Keep the live "X.X% real" readout in sync with the slider value (e.g. after a
  // scenario load writes the slider directly via setInputValueById).
  const input = document.getElementById(AMORTIZATION_RETURN_INPUT_ID);
  const readout = group ? group.querySelector(".amortization-readout") : null;
  if (input && readout) {
    readout.textContent = `${parseFloat(input.value || "0").toFixed(1)}% real`;
  }
  // The Amortization Frontier IGNORES annual_spending (it derives the spend from
  // portfolio + GI + horizon + the amortization return). Grey + lock the field in
  // that mode so editing it can't mislead; restore it in every other mode.
  updateAnnualSpendingGate();
}

// The field name + note copy for the annual-spending amortization gate.
const ANNUAL_SPENDING_FIELD = "annual_spending";
const ANNUAL_SPENDING_GATE_NOTE =
  "Ignored by the Amortization Frontier — spend is derived from your portfolio, " +
  "guaranteed income, and the amortization return.";

// Grey out + lock the Annual spending ($) field under Amortization Frontier mode,
// and restore it (editable) in every other spending mode. Mirrors the tax-location
// gate idiom (updateTaxLocationGate): toggle `disabled` on the input(s) + an
// `is-gated` class on the row, and show/hide an inline note. Idempotent across mode
// flips. We use `disabled` (not `readonly`): the payload read path
// (readFieldValue → input.value) reads `.value` directly with no :enabled filter
// and the app never relies on native form submission, so a disabled input STILL
// posts its value and round-trips through save/load. Re-applied from renderFieldRow
// so the gate survives a link/unlink re-render (which rebuilds the row's inputs).
function updateAnnualSpendingGate() {
  const row = document.querySelector(
    `.field-row[data-field="${ANNUAL_SPENDING_FIELD}"]`,
  );
  if (!row) return;
  const gated = spendingMode === "amortization";
  row.classList.toggle("is-gated", gated);
  // Both the A input and the (unlinked-only) B input. The link/unlink chain icon
  // and its A/B mechanism stay intact — we only grey + lock editing.
  for (const id of [aId(ANNUAL_SPENDING_FIELD), bId(ANNUAL_SPENDING_FIELD)]) {
    const inp = document.getElementById(id);
    if (inp) inp.disabled = gated;
  }
  // Inline note: create it once when gating, remove it when not (idempotent).
  let note = row.querySelector(".annual-spending-gate-note");
  if (gated) {
    if (!note) {
      note = document.createElement("p");
      note.className = "annual-spending-gate-note";
      note.setAttribute("role", "note");
      note.textContent = ANNUAL_SPENDING_GATE_NOTE;
      row.appendChild(note);
    }
  } else if (note) {
    note.remove();
  }
}

// Live update: rebuild the Total + blended-return readout ROWS. Called on every
// weight/return edit, on link/unlink, and once after the matrix is built. Does
// NOT fetch — Recompute does. Both render as a row in the SAME column grid as
// the weight rows: a left "Total" / "Blended" label, then one value cell per
// active scenario column, aligned under that column's weight inputs (LINKED: one
// column; UNLINKED: A│B). The A/B chips on the weight rows head the columns, so
// these rows carry NO A/B parenthetical. Each Total cell keeps its ✓/✗ 100%
// state; each Total reads from the SAME rounded fractions that get serialized, so
// "100%" shown ⇔ valid ⇔ that column's string sums to 1.
function refreshAllocation() {
  const cols = activeAllocCols();

  // Total + Blended now render as ROWS in the SAME column grid as the weight
  // rows: a left-column label, then one value cell PER active scenario column,
  // each cell aligned directly under that column's weight inputs (the A/B chips
  // on the weight rows already head the columns, so these rows carry no A/B
  // parenthetical — just a single "Total" / "Blended" label). Each row reuses
  // the weight-row layout classes (.allocation-readout mirrors .allocation-row;
  // the values container mirrors .allocation-weights linked/unlinked) so the
  // tracks line up identically in both linked (one column) and unlinked (A│B)
  // states. Math/validity come from the unchanged helpers below.

  const allocLinkedNow = compareState.allocationLinked();
  // Build one [label | value-cell per column] readout row.
  const makeReadoutRow = (labelText, cellFor) => {
    const row = document.createElement("div");
    row.className = "allocation-readout";
    row.classList.toggle("is-unlinked", !allocLinkedNow);

    const label = document.createElement("span");
    label.className = "allocation-readout-label";
    label.textContent = labelText;
    row.appendChild(label);

    const values = document.createElement("div");
    values.className = allocLinkedNow
      ? "allocation-readout-values linked"
      : "allocation-readout-values unlinked";
    for (const col of cols) values.appendChild(cellFor(col));
    row.appendChild(values);
    return row;
  };

  const totalsEl = document.getElementById("allocation-totals");
  if (totalsEl) {
    totalsEl.innerHTML = "";

    const totalRow = makeReadoutRow("Total", (col) => {
      const valid = allocationTotalValid(col);
      const cell = document.createElement("span");
      cell.className = "allocation-total-value allocation-readout-cell";
      cell.textContent = `${formatPct(allocationTotalPct(col))}%`;
      cell.classList.toggle("is-valid", valid);
      cell.classList.toggle("is-invalid", !valid);
      return cell;
    });
    totalsEl.appendChild(totalRow);

    // Invalid-column message(s) sit below the row, still naming the offending
    // scenario when comparing (the row itself no longer carries the A/B label).
    for (const col of cols) {
      if (allocationTotalValid(col)) continue;
      const msg = document.createElement("p");
      msg.className = "allocation-total-msg";
      msg.textContent = allocLinkedNow
        ? "must total 100%"
        : `Scenario ${col.toUpperCase()} must total 100%`;
      totalsEl.appendChild(msg);
    }
  }

  const blendedEl = document.getElementById("allocation-blended");
  if (blendedEl) {
    blendedEl.innerHTML = "";
    const blendedRow = makeReadoutRow("Blended", (col) => {
      const cell = document.createElement("span");
      cell.className = "allocation-blended-value allocation-readout-cell";
      cell.textContent = `${blendedRealReturn(col).toFixed(1)}%`;
      return cell;
    });
    blendedEl.appendChild(blendedRow);
  }
}

// Format a percent for display: a whole number reads "100" (no trailing ".0"),
// otherwise show up to 2 decimals with trailing zeros dropped (clean 60/40 →
// "100"; the 60.006/39.996 fraction case → "100.01"). 2 decimals is enough to
// reveal a 4-decimal-fraction (1e-4) discrepancy as a 0.01% deviation.
function formatPct(n) {
  if (Number.isInteger(n)) return String(n);
  return parseFloat(n.toFixed(2)).toString();
}

// Build the allocation matrix + return inputs and run the first live refresh.
// `fromModel` (default false) flows to renderAllocationRows: a scenario LOAD
// passes true so the weight inputs seed from the just-written ASSET_CLASSES
// model rather than the stale pre-load DOM values.
function buildAllocation(fromModel = false) {
  renderAllocationToggle();
  renderPresetOptions();
  renderAllocationRows(fromModel);
  renderReturnInputs();
  renderGuardrailInputs();
  updateGuardrailInputsVisibility();
  renderAmortizationInput();
  updateAmortizationInputsVisibility();
  refreshAllocation();
}

// ---------- glidepath / bond-tent (glidepath design) ----------

// Read an integer age input (Scenario A is the authoritative/shared value — the
// glidepath toggle is global). Falls back to the default scenario, then the given
// fallback, before the inputs are built.
function readAgeInput(name, fallback) {
  const el = aInputEl(name);
  if (el) {
    const n = parseInt(el.value.trim(), 10);
    if (!Number.isNaN(n)) return n;
  }
  if (defaultScenario && typeof defaultScenario[name] === "number") {
    return defaultScenario[name];
  }
  return fallback;
}

// GlidepathView: the PURE client-side mirror of firebridgelab.engine.allocation.glidepath_boundaries
// / the phase-label derivation. Split out as parameterized functions (taking ages
// explicitly, never reading the DOM) so a node:vm test can pin them against the
// engine's expected output without a browser — same testability structure as
// chart_view.js / comparison_state.js. The DOM-reading wrappers below
// (glidepathBoundaries/glidepathPhaseLabels) read the inputs and delegate here; the
// engine remains the source of truth for the math, these just drive labels + count.
//
// `ages` shape: { startAge, endAge, personAPension, personBPension, personASs, personBSs }.
const GlidepathView = {
  // Phase-START ages from the plan's inflection ages: {401k access 60} ∪
  // {pension/SS start ages}, clipped to (start_age, end_age], plus start_age itself
  // (the first phase always opens at the start). Ascending, distinct.
  boundaries(ages) {
    if (Array.isArray(ages.streamAges)) {
      // Household streams (M5): 401(k) access plus every stream start and end.
      const set = new Set([ages.startAge]);
      for (const age of [ALLOCATION_401K_ACCESS_AGE, ...ages.streamAges.map((x) => x.age)]) {
        if (age > ages.startAge && age <= ages.endAge) set.add(age);
      }
      return Array.from(set).sort((x, y) => x - y);
    }
    const inflections = [
      ALLOCATION_401K_ACCESS_AGE,
      ages.personAPension,
      ages.personBPension,
      ages.personASs,
      ages.personBSs,
    ];
    const set = new Set([ages.startAge]);
    for (const age of inflections) {
      if (age > ages.startAge && age <= ages.endAge) set.add(age);
    }
    return Array.from(set).sort((x, y) => x - y);
  },

  // The plain-English "event" that opens each boundary, for the phase label. When
  // two streams claim at the same age the events coincide — we name the most
  // retirement-salient one (Social Security ≻ Pension ≻ 401(k) ≻ Bridge), matching
  // how an advisor would read it. The "Phase" fallback is unreachable by
  // construction (every boundary is start_age, 401(k), a pension, or an SS age); if
  // it ever fires, the client mirror has drifted from the engine derivation — so we
  // warn loudly rather than silently mislabel (Fix 3).
  phaseEvent(boundaryAge, ages) {
    if (boundaryAge === ages.startAge) return "Bridge";
    if (Array.isArray(ages.streamAges)) {
      const kinds = ages.streamAges.filter((x) => x.age === boundaryAge).map((x) => x.kind);
      if (kinds.includes("social_security")) return "Social Security";
      if (kinds.includes("pension")) return "Pension";
      if (kinds.length) return "Income change";
      return "401(k) access";
    }
    if (boundaryAge === ages.personASs || boundaryAge === ages.personBSs)
      return "Social Security";
    if (boundaryAge === ages.personAPension || boundaryAge === ages.personBPension)
      return "Pension";
    if (boundaryAge === ALLOCATION_401K_ACCESS_AGE) return "401(k) access";
    console.warn(
      `glidepath: boundary age ${boundaryAge} matched no inflection — ` +
        `client label derivation has drifted from the engine`,
    );
    return "Phase";
  },

  // One "age-range · event" label per phase, e.g. "50–59 · Bridge",
  // "60–64 · Pension". The last phase runs through end_age. Uses an en-dash to match
  // the lifecycle breakdown's "50–74" styling.
  phaseLabels(ages) {
    const boundaries = GlidepathView.boundaries(ages);
    return boundaries.map((start, k) => {
      const end = k + 1 < boundaries.length ? boundaries[k + 1] - 1 : ages.endAge;
      const range = start === end ? `${start}` : `${start}–${end}`;
      return `${range} · ${GlidepathView.phaseEvent(start, ages)}`;
    });
  },
};
// Exposed for the node:vm test (tests/test_glidepath_view.mjs); not a stable API.
window.GlidepathView = GlidepathView;

// ---------- Glide presets (gpp1) ----------
//
// GlidePresets: the pure, DOM-free engine behind the "Glide preset" <select> in the
// glidepath section. It mirrors the portfolio Presets dropdown (a fire-once action),
// but instead of a per-class weight map each preset is ENDPOINT-DEFINED: a
// {startEquityPct, endEquityPct} pair that is linearly interpolated across however
// many DERIVED phases the plan currently has (first phase = start, last = end,
// rounded to whole %). "Match current (flat)" is the special case: copy the single
// allocation verbatim into every phase (no equity remap), preserving the full
// multi-class mix exactly.
//
// EQUITY classification is NOT embedded here — it is INJECTED as data. The single
// source of truth is firebridgelab.engine.buckets.EQUITY_CLASS_IDS, published per-class via
// /api/classes (the `is_equity` flag), loaded into ASSET_CLASSES and passed in as an
// `equityIds` Set (see equityClassIds()). The mapping is classification-agnostic: it
// asks "is this class id equity?" of the supplied set, so "equity%" reads identically
// to the effective-equity glidepath readout without a hand-kept JS replica.
const GlidePresets = {
  // The locked bundle. `flat: true` is the copy-current special; the rest are
  // endpoint-defined equity glides.
  PRESETS: [
    { id: "bond_tent", name: "Bond tent (rising)", startEquityPct: 30, endEquityPct: 70 },
    { id: "steep_bond_tent", name: "Steep bond tent", startEquityPct: 20, endEquityPct: 75 },
    { id: "declining", name: "Declining (target-date)", startEquityPct: 70, endEquityPct: 40 },
    { id: "match_current", name: "Match current (flat)", flat: true },
  ],

  // Linear interpolation of equity% across n phases: phase[0] = start, phase[n-1] =
  // end, evenly spaced in between, rounded to whole %. n === 1 → [start] (a single
  // phase can't carry a glide; report the start endpoint).
  interpolateEquity(startPct, endPct, n) {
    if (n <= 0) return [];
    if (n === 1) return [Math.round(startPct)];
    const out = [];
    for (let k = 0; k < n; k++) {
      const frac = k / (n - 1);
      out.push(Math.round(startPct + (endPct - startPct) * frac));
    }
    return out;
  },

  // Map a target equity% onto a per-class integer-% weight array, given the SEED
  // single allocation (`singleWeights`, parallel to `classIds`) and the equity
  // classification `equityIds` (a Set of equity class ids — sourced from
  // /api/classes' `is_equity`, NOT embedded here). Each equity class gets
  // round(equityTarget × singleWeight_i / ΣequitySingle); each non-equity class
  // gets round((100−equityTarget) × singleWeight_j / ΣbondSingle); preserving each
  // group's internal relative mix. The rounding residual is fixed on the
  // largest-weight class so the phase sums to exactly 100.
  //
  // Degenerate guard: if the seed has no non-equity weight, a <100% target can't be
  // met by scaling into bonds — clamp to the achievable all-equity mix (don't error).
  // Symmetric guard for no-equity seeds.
  mapEquityToPhase(equityTargetPct, singleWeights, classIds, equityIds) {
    const n = singleWeights.length;
    const eq = equityIds || new Set();
    const isEq = classIds.map((id) => eq.has(id));
    let sumEq = 0;
    let sumBond = 0;
    for (let i = 0; i < n; i++) {
      if (isEq[i]) sumEq += singleWeights[i];
      else sumBond += singleWeights[i];
    }

    // Degenerate: nothing to scale into for one of the groups → clamp the target so
    // the achievable group absorbs 100% in its own internal ratio.
    let target = equityTargetPct;
    if (sumEq <= 0) target = 0; // no equity classes → can't add equity; all-bond
    else if (sumBond <= 0) target = 100; // no bond classes → can't add bonds; all-equity

    const out = new Array(n).fill(0);
    for (let i = 0; i < n; i++) {
      if (isEq[i]) {
        out[i] = sumEq > 0 ? Math.round((target * singleWeights[i]) / sumEq) : 0;
      } else {
        out[i] = sumBond > 0 ? Math.round(((100 - target) * singleWeights[i]) / sumBond) : 0;
      }
    }

    // Fix the rounding residual on the largest-weight class so the phase totals
    // exactly 100 (the matrix enforces a 100% column total). If every class is 0
    // (impossible for a valid seed) there's nothing to fix.
    const sum = out.reduce((s, x) => s + x, 0);
    const residual = 100 - sum;
    if (residual !== 0) {
      let maxIdx = 0;
      for (let i = 1; i < n; i++) {
        if (out[i] > out[maxIdx]) maxIdx = i;
      }
      out[maxIdx] += residual;
    }
    return out;
  },

  // Build the full per-phase weight matrix [phaseIdx][classIdx] for a preset over n
  // phases, seeded from the single allocation. Flat preset → every phase = the seed
  // verbatim (classification-independent). Endpoint presets → interpolate equity%
  // then map each phase using `equityIds` (the equity classification Set, sourced
  // from /api/classes' `is_equity` — not embedded). Flat presets ignore equityIds.
  buildPhaseWeights(preset, singleWeights, classIds, n, equityIds) {
    if (!preset || n <= 0) return {};
    const phases = {};
    if (preset.flat) {
      for (let k = 0; k < n; k++) phases[k] = singleWeights.slice();
      return phases;
    }
    const equityByPhase = GlidePresets.interpolateEquity(
      preset.startEquityPct,
      preset.endEquityPct,
      n,
    );
    for (let k = 0; k < n; k++) {
      phases[k] = GlidePresets.mapEquityToPhase(
        equityByPhase[k],
        singleWeights,
        classIds,
        equityIds,
      );
    }
    return phases;
  },
};
// Exposed for the node:vm test (tests/test_glide_presets.mjs); not a stable API.
window.GlidePresets = GlidePresets;

// ChartExplainers (ctx1 — chart-explainers design): the pure, DOM-free engine
// behind the "How to read this chart ▾" disclosures. It holds the APPROVED
// how-to-read paragraphs VERBATIM (the exact wording is pinned by tests —
// do NOT reword casually), the paragraph-SELECTION logic for the main chart (comparing
// wins over sim-mode; overlay/side-by-side vs deterministic/historical/monte_carlo),
// and the key-parameters DIGEST formatter that humanizes the raw scenario fields
// into bullets (Starting portfolio · Spending · Allocation · Sourcing · Guaranteed
// income · Horizon · View).
//
// EVERYTHING here is pure + injectable (no document, no module globals): the live
// render path (renderChartExplainers) gathers the state — readScenarios(), the
// mode globals, compareState, ASSET_CLASSES — and passes it in, so the selection
// + formatting are unit-testable in a node:vm sandbox (tests/test_chart_explainers.mjs)
// exactly like GlidePresets / WindowAlign. Not a stable API.
const ChartExplainers = {
  // The approved how-to-read paragraphs, VERBATIM. Keyed by the selection result.
  PARAGRAPHS: {
    deterministic:
      "A single 'average returns every year' path — no good or bad luck, no " +
      "bumps. A clean baseline for the shape of a plan, but markets don't deliver " +
      "the average on schedule, so don't read the ending number as a prediction. " +
      "Switch to Historical or Monte Carlo for the range of what could actually happen.",
    historical:
      "Your plan replayed through every real market sequence since 1871 — one run " +
      "starting in each year. The dark line is the median; the shaded bands are the " +
      "middle 50% and 80% of those paths. A band that fans out wide late in life " +
      "means your result depends heavily on which market era you retire into — " +
      "sequence risk. Where the bottom reaches $0, some historical retirements ran out.",
    monte_carlo:
      "Like Historical, but instead of replaying actual history it simulates 10,000 " +
      "random futures from the return/volatility/correlation assumptions you set " +
      "(under Assumptions). Same read — median line plus bands for the spread. Use it " +
      "to stress-test conditions history hasn't shown, like higher volatility than the past.",
    overlay:
      "Two scenarios on one chart — blue is A, magenta is B — sharing a Y axis so " +
      "magnitudes compare honestly. The only things different between them are the " +
      "fields you unlinked; everything else is held identical. Read the gap between " +
      "the lines as the effect of just those differences.",
    side:
      "The same two scenarios, drawn as two separate charts. They still share a Y " +
      "axis, so a taller curve really is a bigger number — but each scenario is shown " +
      "alone, which reads more cleanly when the two lines would overlap and tangle. " +
      "Same idea: what differs is whatever you unlinked.",
    buckets:
      "Your portfolio split into three sub-balances — cash, income, growth — over " +
      "time, not one blended pool. Growth rises as guaranteed income arrives and the " +
      "cash/income buffers shrink: the point of buckets is that in a down year you " +
      "spend the safe buckets instead of selling growth at a loss. (Switch to " +
      "Historical or Monte Carlo to see the buffer actually tested.)",
    guardrails:
      "How much you actually spend each year under the guardrail rule — which flexes " +
      "with the markets — not a fixed number. The band is the range of spending across " +
      "market paths; dips are years the rule cut toward your floor, rises are " +
      "prosperity raises. The dashed lines are your spending floor and the " +
      "absolute-survivable floor.",
    frSmooth:
      "How much you actually spend each year under the funded-ratio rule — which eases " +
      "spending toward what the portfolio can fully fund, not a fixed number. Each year " +
      "the level moves a damped fraction of the way toward full funding, so changes stay " +
      "smooth: the band is the range of spending across market paths, with dips toward " +
      "your floor in lean years and raises toward the ceiling in flush ones. The dashed " +
      "lines are your spending floor and the absolute-survivable floor. Full funding is " +
      "judged at a fixed 1% real return on future gaps — the frontier's adjustable " +
      "amortization-return knob doesn't feed this rule.",
    deterministicSpendingFrSmooth:
      "Your funded-ratio spending on the single 'average returns every year' path — the " +
      "one spending trajectory that pairs with the portfolio line. You can see where the " +
      "rule eases spending up as the portfolio funds more, and trims it as the portfolio " +
      "draws down, plus your spending floor and the absolute-survivable floor (the dashed " +
      "lines). But this is one smooth path with no down years, so it shows no " +
      "sequence-of-returns spread. For how spending actually varies across market paths, " +
      "switch to Historical or Monte Carlo to see the spending fan.",
    frGuardrail:
      "How much you actually spend each year under the funded-ratio bands — which hold " +
      "spending steady while funding stays within ±20% of full, then nudge it ±10% past " +
      "the bands, not a fixed number. The band is the range of spending across market " +
      "paths, with cuts toward your floor when the portfolio falls underfunded and raises " +
      "toward the ceiling when it runs overfunded. The dashed lines are your spending " +
      "floor and the absolute-survivable floor. Full funding is judged at a fixed 1% real " +
      "return on future gaps — the frontier's adjustable amortization-return knob doesn't " +
      "feed this rule.",
    deterministicSpendingFrGuardrail:
      "Your funded-ratio-band spending on the single 'average returns every year' path — " +
      "the one spending trajectory that pairs with the portfolio line. You can see where " +
      "the bands hold spending flat, raise it when the portfolio runs overfunded, and cut " +
      "it when it falls underfunded, plus your spending floor and the absolute-survivable " +
      "floor (the dashed lines). But this is one smooth path with no down years, so it " +
      "shows no sequence-of-returns spread. For how spending actually varies across market " +
      "paths, switch to Historical or Monte Carlo to see the spending fan.",
    frTiers:
      "How much you actually spend each year under the funded-ratio tier ladder — the " +
      "legacy tier ladder from the original plan, kept for reference. Spending steps " +
      "through tiers by how well-funded the portfolio is (FLOOR " +
      "$85k up to MAX $160k in the go-go years), with hysteresis so it doesn't flip tiers " +
      "on noise, a crash brake that pulls spending down after a big drop, and a hard floor " +
      "that pins it to FLOOR when the portfolio runs low. The band is the range of spending " +
      "across market paths. Unlike the other funded-ratio rules this ladder is " +
      "self-contained: the tier amounts already shape the go-go/slow-go/later phases. " +
      "Full funding is judged at a fixed 1% real return on future gaps — the frontier's " +
      "adjustable amortization-return knob doesn't feed this rule.",
    deterministicSpendingFrTiers:
      "Your funded-ratio-tier spending on the single 'average returns every year' path — " +
      "the one spending trajectory that pairs with the portfolio line. You can see the " +
      "ladder step between tiers as funding shifts, hold through small wobbles (hysteresis), " +
      "and drop to FLOOR if the portfolio runs low. But this is one smooth path with no down " +
      "years, so it shows no sequence-of-returns spread. For how spending actually varies " +
      "across market paths, switch to Historical or Monte Carlo to see the spending fan.",
    deterministicSpending:
      "Your guardrails spending on the single 'average returns every year' path — the " +
      "one spending trajectory that pairs with the portfolio line. You can see exactly " +
      "where the rule ratchets spending up (prosperity raises) and where it trims late " +
      "as the portfolio draws down, plus your spending floor and the absolute-survivable " +
      "floor (the dashed lines). But this is one smooth path with no down years, so it " +
      "shows no sequence-of-returns spread — no bad-luck cuts, no good-luck surge. For " +
      "how spending actually varies across market paths, switch to Historical or Monte " +
      "Carlo to see the spending fan.",
    amortization:
      "The most you could responsibly spend each year — the max-consumption ceiling, not " +
      "a fixed number or a rule with a band. Each year it spends the constant-real amount " +
      "that exactly amortizes the portfolio over the rest of the plan against your " +
      "guaranteed-income-netted gaps, so it front-loads into the pre-SS bridge and go-go " +
      "years (it knows Social Security is coming) and drives the terminal portfolio toward " +
      "~$0 by the plan horizon. The band is the range across market paths. The dashed lines " +
      "are reference levels the frontier is free to cross, not limits — this is the upper " +
      "boundary of the envelope you measure your real strategy against, not a strategy to live by.",
    deterministicSpendingAmortization:
      "The max-consumption ceiling on the single 'average returns every year' path — the one " +
      "spending trajectory that pairs with the portfolio line. You can see it front-load into " +
      "the pre-SS bridge and go-go years and draw the portfolio down toward ~$0 by the plan " +
      "horizon, plus the dashed reference lines the frontier may cross (they are not limits). " +
      "But this is one smooth path with no down years, so it shows no sequence-of-returns " +
      "spread. For how the ceiling actually varies across market paths, switch to Historical " +
      "or Monte Carlo to see the spending fan.",
  },

  // Human-facing "View" lens label for the key-parameters digest + nothing else.
  VIEW_LABELS: {
    deterministic: "Deterministic",
    historical: "Historical",
    monte_carlo: "Monte Carlo",
  },

  // The MAIN projection chart's paragraph KEY. Comparing WINS over sim-mode
  // (design risk-callout): comparing → overlay/side text by the derived outputMode;
  // otherwise the sim-mode text. outputMode === "single" while comparing (a focus
  // collapse) falls through to the sim-mode text — there's one panel on screen, so
  // the single-scenario read is the honest one.
  selectMainParagraphKey({ comparing, outputMode, simMode }) {
    if (comparing && outputMode === "overlay") return "overlay";
    if (comparing && outputMode === "side") return "side";
    if (simMode === "historical") return "historical";
    if (simMode === "monte_carlo") return "monte_carlo";
    return "deterministic";
  },

  // "$1.8M" / "$950k" / "$0" compact portfolio form (matches the design copy's
  // capital-M millions; falls back to $k below $1M, plain dollars below $1k).
  fmtPortfolio(n) {
    const v = Number(n) || 0;
    if (Math.abs(v) >= 1_000_000) {
      const m = v / 1_000_000;
      const s = Number.isInteger(m) ? String(m) : m.toFixed(2).replace(/\.?0+$/, "");
      return `$${s}M`;
    }
    if (Math.abs(v) >= 1_000) {
      const k = v / 1_000;
      const s = Number.isInteger(k) ? String(k) : k.toFixed(1).replace(/\.0$/, "");
      return `$${s}k`;
    }
    return `$${Math.round(v)}`;
  },

  // "$150k" bare compact-$k form (no period suffix).
  fmtK(n) {
    const v = Number(n) || 0;
    const k = v / 1_000;
    const s = Number.isInteger(k) ? String(k) : k.toFixed(1).replace(/\.0$/, "");
    return `$${s}k`;
  },

  // "$150k/yr" compact spend form.
  fmtPerYear(n) {
    return `${this.fmtK(n)}/yr`;
  },

  // Spending bullet VALUE: amount + strategy. constant → "constant-real",
  // lifecycle → "lifecycle", guardrails → "guardrails (floor $X, ceiling $Y)".
  // The amount carries /yr; the floor/ceiling read as bare $k per the design copy.
  summarizeSpending(scenario, spendingMode) {
    const amt = this.fmtPerYear(scenario.annual_spending);
    if (spendingMode === "guardrails") {
      const floor = this.fmtK(scenario.spending_floor);
      const ceiling = this.fmtK(scenario.spending_ceiling);
      return `${amt}, guardrails (floor ${floor}, ceiling ${ceiling})`;
    }
    if (spendingMode === "fr_smooth") {
      const floor = this.fmtK(scenario.spending_floor);
      const ceiling = this.fmtK(scenario.spending_ceiling);
      return `${amt}, funded ratio – smooth (floor ${floor}, ceiling ${ceiling})`;
    }
    if (spendingMode === "fr_guardrail") {
      const floor = this.fmtK(scenario.spending_floor);
      const ceiling = this.fmtK(scenario.spending_ceiling);
      return `${amt}, funded ratio – bands (floor ${floor}, ceiling ${ceiling})`;
    }
    if (spendingMode === "fr_tiers") {
      // The tier ladder is self-contained (FLOOR $85k → MAX $160k go-go); it does
      // NOT use the operational floor/ceiling rails, so the summary names the
      // ladder envelope instead of the rails.
      return `${amt}, funded ratio – tiers (FLOOR $85k → MAX $160k)`;
    }
    if (spendingMode === "amortization") {
      // The frontier IGNORES annual_spending (it solves for the spend each year), so
      // the summary names the strategy + its planning return, never the input amount.
      // amortization_real_return is a fraction (0.01 = 1%); the slider steps in 0.1%,
      // so render one decimal to match the knob's readout.
      const r = Number(scenario.amortization_real_return) || 0;
      const pct = (r * 100).toFixed(1).replace(/\.0$/, "");
      return `amortization frontier (return ${pct}% real)`;
    }
    if (spendingMode === "lifecycle") return `${amt}, lifecycle`;
    return `${amt}, constant-real`;
  },

  // Equity FRACTION of an allocation body ([{id, weight}], weight is a 0–1
  // fraction) given the equity class-id set.
  _equityFraction(body, equityIds) {
    if (!Array.isArray(body)) return 0;
    let eq = 0;
    let tot = 0;
    for (const e of body) {
      const w = Number(e.weight) || 0;
      tot += w;
      if (equityIds.has(e.id)) eq += w;
    }
    return tot > 0 ? eq / tot : 0;
  },

  // Allocation bullet VALUE. constant → the non-zero mix labelled from the class
  // menu; a clean 2-class equity/bond split reads "60% US stocks / 40% US bonds";
  // glidepath → "Glidepath, ~30→70% equity"; otherwise list the non-zero classes.
  // `classes` is the live ASSET_CLASSES (id/label/is_equity), `labelMap` lets the
  // caller override the display names (test injection).
  summarizeAllocation(scenario, allocationMode, classes) {
    const byId = new Map();
    const equityIds = new Set();
    for (const c of classes) {
      byId.set(c.id, c);
      if (c.is_equity) equityIds.add(c.id);
    }

    if (allocationMode === "glidepath" && Array.isArray(scenario.phase_allocations) &&
        scenario.phase_allocations.length) {
      const phases = scenario.phase_allocations;
      const start = Math.round(this._equityFraction(phases[0], equityIds) * 100);
      const end = Math.round(
        this._equityFraction(phases[phases.length - 1], equityIds) * 100,
      );
      return `Glidepath, ~${start}→${end}% equity across phases`;
    }

    const body = (scenario.allocationBody || []).filter((e) => (Number(e.weight) || 0) > 0);
    if (!body.length) return "—";

    // 2-class equity/bond split → "60% US stocks / 40% US bonds".
    if (body.length === 2) {
      const eq = body.find((e) => equityIds.has(e.id));
      const bond = body.find((e) => !equityIds.has(e.id));
      if (eq && bond) {
        const eqPct = Math.round((Number(eq.weight) || 0) * 100);
        const bondPct = Math.round((Number(bond.weight) || 0) * 100);
        const eqName = this._shortClassName(byId.get(eq.id));
        const bondName = this._shortClassName(byId.get(bond.id));
        return `${eqPct}% ${eqName} / ${bondPct}% ${bondName}`;
      }
    }

    // General: list every non-zero class as "NN% Label".
    return body
      .map((e) => `${Math.round((Number(e.weight) || 0) * 100)}% ${this._className(byId.get(e.id), e.id)}`)
      .join(" / ");
  },

  // Full menu label, falling back to the raw id when a class is unknown (degraded
  // class-menu load).
  _className(cls, id) {
    return (cls && cls.label) || id;
  },

  // A tightened display name for the 2-class equity/bond summary: the canonical
  // legacy pair reads as plain "US stocks" / "US bonds"; anything else keeps its
  // full menu label.
  _shortClassName(cls) {
    if (!cls) return "";
    if (cls.id === "us_total_market") return "US stocks";
    if (cls.id === "us_agg_bonds") return "US bonds";
    return cls.label;
  },

  // Sourcing bullet VALUE. single → "single portfolio"; buckets → "buckets (cash Ny
  // / income My)" from the bucket config (n1 = cash years, n2 = income years).
  summarizeSourcing(scenario, sourcingMode) {
    if (sourcingMode !== "buckets") return "single portfolio";
    const b = scenario.buckets || {};
    const cash = Number(b.n1);
    const income = Number(b.n2);
    if (Number.isFinite(cash) && Number.isFinite(income)) {
      return `buckets (cash ${cash}y / income ${income}y)`;
    }
    return "buckets";
  },

  // Guaranteed-income SUMMARY: total ~$/yr once everything is on, plus the
  // earliest pension + SS start ages across both people. e.g.
  // "~$98.2k/yr by 70 (pensions at 60, SS at 67)". Returns "none" when no GI is set.
  summarizeGI(scenario) {
    const streams = [
      { amount: scenario.person_a_pension_amount, start: scenario.person_a_pension_start_age, kind: "pension" },
      { amount: scenario.person_b_pension_amount, start: scenario.person_b_pension_start_age, kind: "pension" },
      { amount: scenario.person_a_ss_amount, start: scenario.person_a_ss_start_age, kind: "ss" },
      { amount: scenario.person_b_ss_amount, start: scenario.person_b_ss_start_age, kind: "ss" },
    ].filter((s) => (Number(s.amount) || 0) > 0);

    if (!streams.length) return "none";

    const total = streams.reduce((sum, s) => sum + (Number(s.amount) || 0), 0);
    const fullyOnAge = streams.reduce((mx, s) => Math.max(mx, Number(s.start) || 0), 0);
    const pensionAges = streams.filter((s) => s.kind === "pension").map((s) => Number(s.start));
    const ssAges = streams.filter((s) => s.kind === "ss").map((s) => Number(s.start));

    const parts = [];
    if (pensionAges.length) {
      const lo = Math.min(...pensionAges);
      parts.push(`pensions at ${lo}`);
    }
    if (ssAges.length) {
      const lo = Math.min(...ssAges);
      parts.push(`SS at ${lo}`);
    }
    const tail = parts.length ? ` (${parts.join(", ")})` : "";
    return `~${this.fmtPerYear(total)} by ${fullyOnAge}${tail}`;
  },

  // Tax-location bullet VALUE, or null when no split is on the scenario. A scenario
  // carries the split as taxable_balance / tax_deferred_balance / roth_balance
  // (attached by attachTaxLocation when active). e.g.
  // "$1.5M taxable / $700k tax-deferred / $300k Roth". Returns null when absent so
  // the digest omits the line entirely (feature off → today's bullet set).
  summarizeTaxLocation(scenario) {
    const hasSplit =
      scenario &&
      (typeof scenario.taxable_balance === "number" ||
        typeof scenario.tax_deferred_balance === "number" ||
        typeof scenario.roth_balance === "number");
    if (!hasSplit) return null;
    return (
      `${this.fmtPortfolio(scenario.taxable_balance || 0)} taxable / ` +
      `${this.fmtPortfolio(scenario.tax_deferred_balance || 0)} tax-deferred / ` +
      `${this.fmtPortfolio(scenario.roth_balance || 0)} Roth`
    );
  },

  // Federal-tax-overlay bullet VALUE for the key-parameters digest (federal-tax
  // overlay design ft1, DECISION 8/item 6; tax-drag td1 adds the lever clause) —
  // shown ONLY when the tax-location split is active (the SAME gate
  // summarizeTaxLocation uses: the overlay rides tb1's split, DECISION 3).
  // Reports the live gains-fraction knob as a percent and confirms the overlay
  // is on, e.g. "50% taxable-gains fraction — federal overlay on"; when the
  // bracket-fill lever (DECISION 5) is also set, appends its target, e.g.
  // "...— federal overlay on, bracket-fill lever to $100.8k". Returns null when
  // the split is off so the digest omits the line (mirrors
  // summarizeTaxLocation's null-return convention).
  summarizeFederalTaxOverlay(scenario) {
    if (!this.summarizeTaxLocation(scenario)) return null;
    const frac =
      scenario && typeof scenario.taxable_gains_fraction === "number"
        ? scenario.taxable_gains_fraction
        : 0.5;
    const pct = Math.round(frac * 100);
    let s = `${pct}% taxable-gains fraction — federal overlay on`;
    if (scenario && typeof scenario.bracket_fill_target === "number") {
      s += `, bracket-fill lever to ${this.fmtPortfolio(scenario.bracket_fill_target)}`;
    }
    return s;
  },

  // The full key-parameters digest for ONE scenario → an ordered [{label, value}]
  // bullet list. `state` carries the four mode globals + the live class menu.
  formatScenarioParams(scenario, state) {
    const { simMode, spendingMode, allocationMode, sourcingMode, assetClasses } = state;
    const bullets = [
      { label: "Starting portfolio", value: this.fmtPortfolio(scenario.initial_portfolio) },
      { label: "Spending", value: this.summarizeSpending(scenario, spendingMode) },
      { label: "Allocation", value: this.summarizeAllocation(scenario, allocationMode, assetClasses) },
      { label: "Sourcing", value: this.summarizeSourcing(scenario, sourcingMode) },
      { label: "Guaranteed income", value: this.summarizeGI(scenario) },
      { label: "Horizon", value: `${scenario.start_age} → ${scenario.end_age}` },
      { label: "View", value: this.VIEW_LABELS[simMode] || "Deterministic" },
    ];
    // Tax-location split (tax-buckets design tb1): one extra line when a split is on
    // the scenario. Inserted after Sourcing (the related axis), before Guaranteed
    // income, so it reads with the other portfolio-shape facts. Omitted when off.
    const accounts = this.summarizeTaxLocation(scenario);
    if (accounts) {
      bullets.splice(4, 0, { label: "Accounts", value: accounts });
      // Federal-tax overlay (ft1): immediately after Accounts (the related axis —
      // the overlay rides the same split), still before Guaranteed income.
      // Omitted (falls through) when the split is off, same gate as Accounts.
      const overlay = this.summarizeFederalTaxOverlay(scenario);
      if (overlay) {
        bullets.splice(5, 0, { label: "Federal tax overlay", value: overlay });
      }
    }
    return bullets;
  },
};
// Exposed for the node:vm test (tests/test_chart_explainers.mjs); not a stable API.
window.ChartExplainers = ChartExplainers;

// BucketView: the client-side bucket constants the engine pins (the default
// 100%-weight class per bucket + the bucket id order/labels). The bucket TARGETS
// (cash/income year sizing) are derived ENGINE-side and ride in the payload — the
// client doesn't recompute them — so there's nothing target-shaped to pin here.
// What CAN drift is the seed mapping (cash → st_treasury, income → us_agg_bonds,
// growth → us_total_market) + the id order, which must match
// firebridgelab.default_bucket_config / the cash→income→growth payload order. Exposed for
// the node:vm test (tests/test_bucket_view.mjs) — same testability structure as
// GlidepathView. Not a stable API.
window.BucketView = {
  ids: BUCKET_IDS,
  labels: BUCKET_LABELS,
  defaultClass: BUCKET_DEFAULT_CLASS,
};

// WindowAlign: the comparison-window-alignment helpers (option 3). Exposed for the
// node:vm test (tests/test_bucket_view.mjs) — same testability structure as
// BucketView. The helpers take an injectable `starts` (class-id → data_start_year)
// map so they're pure + DOM-free; production callers pass the live class menu. Not
// a stable API.
window.WindowAlign = {
  scenarioClassIds: (params) => Array.from(scenarioClassIds(params)),
  scenarioTestableSince,
  commonComparisonFloor,
};

// TaxLocationView (tax-buckets design tb1): the pure, DOM-free logic behind the
// tax-location feature — the running-sum helper ("balanced / over / under"), the
// mode-aware bridge readout text, and the verbatim "how to read" explainer +
// pre-tax caveat. Kept DOM-free so the selection/formatting are unit-testable in a
// node:vm sandbox (tests/test_tax_location_view.mjs) exactly like ChartExplainers /
// BucketView / WindowAlign. The DOM seam (reading the inputs, writing the readout
// element) lives in the render functions below; this object owns only the strings
// and the branch logic. Not a stable API.
const TaxLocationView = {
  // The balance note, appended (quietly) to every bridge readout AND repeated in
  // the explainer. RETIRED the ft1/ft2 "balances are pre-tax" claim (tax-drag
  // design td1, DECISION 1): a tax-location split now ALWAYS drags — the
  // portfolio pays its own federal tax bill, withdrawals ARE grossed up to fund
  // it, and this is the honest post-tax depletion picture, not an optimistic
  // floor. What's still true and worth flagging: the SAME return sequence ran
  // twice (once dragged, once pre-tax) to produce the "estate cost of taxes"
  // readout below — that counterfactual, not this chart, is the one place a
  // pre-tax number survives.
  BALANCE_NOTE:
    "(These balances already pay the federal tax bill — withdrawals are grossed " +
    "up to cover it, so this is the honest after-tax depletion picture. The only " +
    "pre-tax number left is the separate \"estate cost of taxes\" counterfactual " +
    "below, which reruns the same market path without the drag for comparison.)",

  // The "How to read this chart" explainer paragraph for the tax-location chart.
  // Plain-English (a smart non-engineer): what the chart shows + what "the bridge"
  // is. The balance note is appended by the renderer (so the two stay in sync).
  // tax-drag td1 DECISION 4: from age 75, any Required Minimum Distribution
  // excess over spending now actually MOVES tax-deferred -> taxable at year end
  // (a real internal transfer, not just a taxed-but-untouched shadow number) —
  // so composition past 75 is honest: tax-deferred shrinks by the real
  // withdrawal, taxable grows by the after-tax-bill remainder. A correction
  // toward realism from the earlier tb1/ft1 chart, which left tax-deferred too
  // high past 75.
  EXPLAINER:
    "Your money split by where it lives for tax purposes — taxable brokerage, " +
    "tax-deferred (Traditional 401(k)/IRA), and Roth — rather than one blended pool. " +
    "It matters because the tax-deferred account is behind a 10% early-withdrawal " +
    "penalty until age 59½, so the years from retirement to 59½ are a 'bridge' you " +
    "have to fund from money you can actually reach: taxable and Roth. In " +
    "Deterministic the three lines are the exact balances over time. In Historical " +
    "and Monte Carlo the shaded band is the range of your taxable balance across " +
    "market paths (10th to 90th percentile, with the median line) — it shows how " +
    "often, and how early, the taxable account bottoms out before the penalty door " +
    "opens. From age 75, Required Minimum Distributions move any excess over your " +
    "spending need from tax-deferred into taxable at year end, so the composition " +
    "shift you see after 75 is real, not just a tax bill layered on top of " +
    "unchanged balances.",

  // Pure config builder for the Historical / Monte Carlo tax-location chart
  // (txlb hotfix). DOM-free so it can be unit-tested: it returns the Chart.js
  // { labels, datasets, options } that renderTaxLocationFan instantiates on the
  // tax-location-chart canvas. Composition:
  //   - the TOTAL-investments outer fan (total_p10/median/p90) — neutral slate,
  //     outer band only (no inner 25–75), via ChartView.fanDatasets;
  //   - THREE median LINES (taxable / tax-deferred / Roth) in the same colours as
  //     the Deterministic 3-line chart (TAX_LOCATION_LINE_COLORS), fill:false.
  // Legend shows the three account lines + the total median; the zero-width
  // band-edge datasets are filtered out (borderWidth > 0). Tooltip at the hovered
  // age: Total p90 / median / p10, then each account's median, money-aligned.
  // Median is NOT additive, so the three lines do not stack to the total median —
  // that's expected; the lines carry typical composition, the fan carries spread.
  fanConfig(risk, deps) {
    const { lineColors, totalRgb, totalLine, lineWidth, fmtMoney, fmtMillions } =
      deps;
    const ages = risk.ages;
    const bands = ages.map((age, i) => ({
      age,
      p10: risk.total_p10[i],
      median: risk.total_median[i],
      p90: risk.total_p90[i],
    }));
    const { datasets, medianIndex } = ChartView.fanDatasets(bands, {
      rgb: totalRgb,
      border: totalLine,
      bandAlpha: 0.15,
      includeInner: false,
      medianLabel: "Total investments — median (real $)",
      lineWidth,
    });
    // Three account median LINES appended after the band datasets. fill:false so
    // none of them participate in the band-fill chain.
    const line = (id, label, data) => ({
      label,
      data,
      borderColor: lineColors[id],
      backgroundColor: lineColors[id],
      fill: false,
      tension: 0.15,
      pointRadius: 0,
      borderWidth: 2,
    });
    datasets.push(
      line("taxable", "Taxable — median", risk.taxable_median),
      line("tax_deferred", "Tax-deferred — median", risk.tax_deferred_median),
      line("roth", "Roth — median", risk.roth_median),
    );
    return {
      labels: ages,
      datasets,
      options: {
        legend: {
          display: true,
          labels: {
            // Hide the zero-width band-edge datasets; show the median lines + the
            // total-median line (all borderWidth > 0).
            filter: (item, data) =>
              data.datasets[item.datasetIndex].borderWidth > 0,
          },
        },
        yAxisTitle: "Balance (real $)",
        yTickFormatter: (v) => fmtMillions(v),
        // The total-median dataset is the single tooltip anchor; its row set carries
        // the full readout (total band + the three account medians).
        tooltipFilter: (item) => item.datasetIndex === medianIndex,
        tooltipLabel: (item) => {
          const i = item.dataIndex;
          const b = bands[i];
          return ChartView.tooltipRows([
            ["Total p90", fmtMoney(b.p90)],
            ["Total median", fmtMoney(b.median)],
            ["Total p10", fmtMoney(b.p10)],
            ["Taxable", fmtMoney(risk.taxable_median[i])],
            ["Tax-deferred", fmtMoney(risk.tax_deferred_median[i])],
            ["Roth", fmtMoney(risk.roth_median[i])],
          ]);
        },
      },
    };
  },

  // Compact "$1.8M" / "$700k" / "$0" portfolio form — same thresholds as
  // ChartExplainers.fmtPortfolio, duplicated here so the module is self-contained
  // for the DOM-free test.
  fmtMoneyShort(n) {
    const v = Number(n) || 0;
    if (Math.abs(v) >= 1_000_000) {
      const m = v / 1_000_000;
      const s = Number.isInteger(m) ? String(m) : m.toFixed(2).replace(/\.?0+$/, "");
      return `$${s}M`;
    }
    if (Math.abs(v) >= 1_000) {
      const k = v / 1_000;
      const s = Number.isInteger(k) ? String(k) : k.toFixed(1).replace(/\.0$/, "");
      return `$${s}k`;
    }
    return `$${Math.round(v)}`;
  },

  // Are any of the three balances supplied? The feature is OFF only when ALL three
  // inputs are blank. A blank input is `null`; a supplied one is a finite number
  // (missing-but-some-present → treated as 0 client-side AND server-side).
  hasSplit({ taxable, taxDeferred, roth }) {
    return [taxable, taxDeferred, roth].some(
      (v) => v !== null && v !== undefined && Number.isFinite(v),
    );
  },

  // Running-sum helper state: compares the sum of the three (blank → 0) against
  // initial_portfolio, within a $1 tolerance (mirrors the API's sum rule). Returns
  // { active, sum, target, status, text } where status is
  // "off" | "balanced" | "over" | "under" and text is the helper line. `active`
  // is false when no split is supplied (the helper hides / reads as off).
  TOLERANCE: 1,
  sumHelper({ taxable, taxDeferred, roth, target }) {
    const t = Number.isFinite(taxable) ? taxable : 0;
    const d = Number.isFinite(taxDeferred) ? taxDeferred : 0;
    const r = Number.isFinite(roth) ? roth : 0;
    const sum = t + d + r;
    const tgt = Number(target) || 0;
    const active = this.hasSplit({ taxable, taxDeferred, roth });
    if (!active) {
      return { active: false, sum, target: tgt, status: "off", text: "" };
    }
    const diff = sum - tgt;
    const sumStr = this.fmtMoneyShort(sum);
    const tgtStr = this.fmtMoneyShort(tgt);
    if (Math.abs(diff) <= this.TOLERANCE) {
      return {
        active: true, sum, target: tgt, status: "balanced",
        text: `${sumStr} of ${tgtStr} — balanced`,
      };
    }
    if (diff > 0) {
      return {
        active: true, sum, target: tgt, status: "over",
        text: `${this.fmtMoneyShort(diff)} over (${sumStr} of ${tgtStr})`,
      };
    }
    return {
      active: true, sum, target: tgt, status: "under",
      text: `${this.fmtMoneyShort(-diff)} under (${sumStr} of ${tgtStr})`,
    };
  },

  // Is this age before the 59½ penalty door, on the integer-age axis? (<=59).
  _isPreDoor(age) {
    return Number.isFinite(age) && age <= TAX_BRIDGE_LAST_PRE_DOOR_AGE;
  },

  // The DETERMINISTIC bridge readout sentence. `tl` is the payload.tax_location
  // block ({ages, taxable, tax_deferred, roth, taxable_depletion_age,
  // reachable_depletion_age}). Returns the headline string (caller appends the
  // pre-tax caveat separately so it can be styled distinctly).
  //   - taxable never runs dry pre-door → "lasts the whole bridge", with the
  //     taxable balance still standing at age 59 ("$N to spare") when available.
  //   - taxable runs dry at age X → say so; if taxable+Roth are BOTH exhausted by
  //     age Y, add the forced-tax-deferred-draw clause.
  deterministicReadout(tl) {
    if (!tl) return "";
    const depAge = tl.taxable_depletion_age;
    if (depAge === null || depAge === undefined) {
      // Taxable survives the bridge. Report the spare at the last pre-door age
      // (59) if we can read it off the series; else just state it survived.
      const spare = this._taxableAtAge(tl, TAX_BRIDGE_LAST_PRE_DOOR_AGE);
      if (spare !== null) {
        return `Taxable lasts the whole bridge — you reach 59½ with ${this.fmtMoneyShort(spare)} still in the taxable account.`;
      }
      return "Taxable lasts the whole bridge — you reach 59½ with the taxable account still funded.";
    }
    let s = `Taxable runs dry at age ${depAge}, before the 59½ penalty door opens.`;
    const reach = tl.reachable_depletion_age;
    if (reach !== null && reach !== undefined) {
      s += ` Taxable and Roth are both exhausted by age ${reach} — you'd be forced to tap tax-deferred early and eat the penalty.`;
    }
    return s;
  },

  // Read the taxable balance at a given age off the deterministic series, or null
  // if the age isn't in `ages` (or arrays are absent).
  _taxableAtAge(tl, age) {
    if (!tl || !Array.isArray(tl.ages) || !Array.isArray(tl.taxable)) return null;
    const i = tl.ages.indexOf(age);
    if (i < 0 || i >= tl.taxable.length) return null;
    const v = tl.taxable[i];
    return Number.isFinite(v) ? v : null;
  },

  // The HISTORICAL / MONTE-CARLO bridge readout sentence. `risk` is the
  // payload.tax_location_risk block. Percentages are 0..100 in the payload (NOT
  // 0..1) — used verbatim. Reports the frequency taxable bottoms out before 59½
  // (+ median age), then the frequency of a forced early tax-deferred draw.
  stochasticReadout(risk) {
    if (!risk) return "";
    const pctTax = this._pct(risk.pct_taxable_depleted_pre_595);
    const pctReach = this._pct(risk.pct_reachable_depleted_pre_595);
    const medAge =
      risk.taxable_depletion && risk.taxable_depletion.median != null
        ? risk.taxable_depletion.median
        : null;
    const medianClause = medAge !== null ? ` (median age ${medAge})` : "";
    return (
      `Taxable bottoms out before 59½ in ${pctTax} of market paths${medianClause}. ` +
      `Taxable and Roth both run dry — forcing an early tax-deferred withdrawal — in ${pctReach} of paths.`
    );
  },

  // Format a 0..100 percentage as a clean integer-ish "Z%" (one decimal only when
  // it isn't whole, so 0 reads "0%" and 12.5 reads "12.5%").
  _pct(v) {
    const n = Number(v) || 0;
    const s = Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, "");
    return `${s}%`;
  },

  // The one-line tax-location summary for the key-parameters digest (ctx1), shown
  // only when a split is active. e.g. "$1.5M taxable / $700k tax-deferred / $300k Roth".
  summarizeSplit({ taxable, taxDeferred, roth }) {
    const t = Number.isFinite(taxable) ? taxable : 0;
    const d = Number.isFinite(taxDeferred) ? taxDeferred : 0;
    const r = Number.isFinite(roth) ? roth : 0;
    return `${this.fmtMoneyShort(t)} taxable / ${this.fmtMoneyShort(d)} tax-deferred / ${this.fmtMoneyShort(r)} Roth`;
  },

  // The default pre-fill split for a starting portfolio P (tb1b). Taxable and
  // tax-deferred are rounded to whole dollars at their fractions; ROTH IS THE
  // EXACT-SUM BALANCER (roth = P − taxable − tax_deferred) so the three always sum
  // to EXACTLY P regardless of rounding. P is coerced to a finite number (blank /
  // NaN → 0). Returns { taxable, tax_deferred, roth } — the same shape
  // setTaxLocationInputs writes. Pure (DOM-free) so it's unit-testable.
  defaultSplit(P) {
    const p = Number.isFinite(Number(P)) ? Number(P) : 0;
    const taxable = Math.round(TAX_DEFAULT_TAXABLE_FRAC * p);
    const tax_deferred = Math.round(TAX_DEFAULT_DEFERRED_FRAC * p);
    const roth = p - taxable - tax_deferred;
    return { taxable, tax_deferred, roth };
  },
};
// Exposed for the node:vm test (tests/test_tax_location_view.mjs); not a stable API.
window.TaxLocationView = TaxLocationView;

// FederalTaxView (federal-tax overlay design ft1): the pure, DOM-free logic behind
// the federal-tax readout + its "how to read" explainer — the sibling of
// TaxLocationView for the tax OVERLAY (as opposed to the account-balance split).
// Kept DOM-free so the selection/formatting is unit-testable in a node:vm sandbox
// (tests/test_federal_tax_view.mjs), exactly like TaxLocationView. The DOM seam
// (reading payload.federal_tax, writing the readout element) lives in
// renderFederalTaxReadout (app.js render path); this object owns only the strings
// and the branch logic. Not a stable API.
const FederalTaxView = {
  // The "How to read" explainer paragraph (ctx1 idiom) — its OWN disclosure,
  // separate from tax-location's. Rewritten for tax-drag design td1 (retires the
  // ft1/ft2 "display-only, portfolio never pays it" framing — DECISION 1):
  // states plainly that with a split supplied, the portfolio PAYS this bill —
  // the fan, the success rate, the depletion age, and the solved max spend
  // above are already after-tax, not a pre-tax number with a display-only
  // estimate laid beside it. Names the two retired falsehoods (composition now
  // moves via real RMDs; the portfolio's balance actually shrinks by the tax),
  // explains the bracket-fill lever, restates the remaining simplifications,
  // fences the MC max-spend solve, and discloses the stress + post-depletion
  // conventions. Tone: an estimate, not truth-to-the-dollar.
  EXPLAINER:
    "With a tax-location split supplied, the portfolio itself pays this bill — " +
    "the fan, the success rate, the depletion age, and the solved max " +
    "sustainable spend above are already after-tax, not a pre-tax number with a " +
    "display-only estimate laid beside it. Two earlier simplifications are " +
    "gone: a Required Minimum Distribution's excess over your spending need now " +
    "actually moves from tax-deferred into taxable at year end (a real " +
    "transfer, not a shadow calculation), and the portfolio's balance actually " +
    "shrinks by the tax it owes, the same way a real withdrawal would. Federal " +
    "tax only, filing Married Filing Jointly, no state income tax modeled " +
    "(as in a no-income-tax state). The optional bracket-fill lever, set alongside the tax-location " +
    "balances, draws extra from tax-deferred each year from age 60 through 74 " +
    "to fill your ordinary taxable income up to a target you choose, moving the " +
    "excess to taxable early — the point is to shrink the tax-deferred balance, " +
    "and therefore future Required Minimum Distributions, while you're likely " +
    "in a cheaper bracket than the one RMDs will eventually force you into. " +
    "Documented simplifications that remain: 85% of Social Security is treated " +
    "as taxable every year (the full phase-in formula is skipped); the " +
    "taxable-gains share of each brokerage withdrawal is the flat knob you set, " +
    "not a tracked cost basis; tax brackets, the standard deduction, and the " +
    "long-term capital-gains thresholds are held constant in real " +
    "(inflation-adjusted) dollars, current as of 2026 (IRS Rev. Proc. 2025-32); " +
    "the 3.8% net-investment-income tax threshold ($250k) is held fixed in real " +
    "dollars, so the estimate drifts mildly optimistic over the horizon; a " +
    "forced tax-deferred withdrawal before 59½ — including one the tax bill " +
    "itself forces, which the fixed point can now produce — eats the 10% " +
    "early-withdrawal penalty on top of ordinary tax; dividends aren't modeled " +
    "as their own taxable event; and foreign (non-US) taxation is out of scope. Once a " +
    "path depletes, guaranteed income keeps flowing and " +
    "stays taxable in reality, but the model stops charging tax to a portfolio " +
    "that no longer exists — the gross-outflow line and the tax totals simply " +
    "go quiet for that path from then on. Stress runs pay tax the same honest " +
    "way: a stressed trajectory's tooltip shows spending and the portfolio's " +
    "loss for the year, and the gap between them is the (hidden, not broken " +
    "out there) tax. The Monte Carlo solve for max sustainable spend isn't " +
    "available yet with a split supplied — each probe would have to re-run the " +
    "full year-by-year tax drag across thousands of trials — but the " +
    "Deterministic and Historical solves already ride these honest after-tax " +
    "numbers. Read every number here as a best estimate under the " +
    "simplifications above, not a truth-to-the-dollar tax return.",

  // Compact percent formatter for a FRACTION (0..1) input -> "12%" / "8.5%" (one
  // decimal only when it isn't whole). The payload's effective_rate /
  // median_effective_rate fields are fractions (tax ÷ SPENDING, per the engine —
  // never income), so this always feeds the "% of spending" phrasing below; the
  // wording NEVER says "effective tax rate" (reviewer-mandated — the denominator
  // is spending, not income).
  _pctOfFraction(frac) {
    const n = (Number(frac) || 0) * 100;
    const s = Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, "");
    return `${s}%`;
  },

  // Display-guarded forced-tax value: max(0, x). The engine deliberately does
  // NOT clamp the attribution split (sub-ULP negatives are possible from the
  // two-computation subtraction), so the reviewer-mandated guard lives here in
  // the web, applied at every display site.
  _guardForced(x) {
    return Math.max(0, Number(x) || 0);
  },

  // The (guarded) forced tax at one age, looked up from a per-age forced series
  // by age value (not index-zipped). 0 when the age isn't in `ages` or the
  // arrays are absent — the qualifier below then simply doesn't fire.
  _forcedAtAge(ages, forcedSeries, age) {
    if (!Array.isArray(ages) || !Array.isArray(forcedSeries)) return 0;
    const i = ages.indexOf(age);
    if (i < 0 || i >= forcedSeries.length) return 0;
    return this._guardForced(forcedSeries[i]);
  },

  // Worst-year qualifier (ft2, DECISION 9): the worst_tax scalar is TOTAL-bill-
  // based and RMD-dominated late-life, so the readout must not present it as a
  // consumption hit. When the forced share of the worst year exceeds half the
  // bill, say so. Data-driven off the per-age forced series at the worst age —
  // "" when the forced share is small or unknowable (age not in the series).
  _worstQualifier(ages, forcedSeries, worstAge, worstTax) {
    const forcedAtWorst = this._forcedAtAge(ages, forcedSeries, worstAge);
    if (worstTax > 0 && forcedAtWorst > 0.5 * worstTax) {
      return ", mostly paid from forced RMD distributions";
    }
    return "";
  },

  // The forced-tax sentence (ft2, DECISION 9): the lifetime forced-RMD share of
  // the bill, framed as an estate drag, never a spending cut. `forcedTotal` is
  // already display-guarded by the callers; "" when zero (no RMD years / draws
  // always ≥ RMD). `medianPrefix` distinguishes the Historical/MC phrasing.
  _forcedSentence(forcedTotal, medianPrefix) {
    if (!(forcedTotal > 0)) return "";
    const amount = TaxLocationView.fmtMoneyShort(forcedTotal);
    const lead = medianPrefix ? `A median ${amount}` : `${amount}`;
    return (
      ` ${lead} of the lifetime bill is tax on forced RMD distributions you ` +
      `don't spend — paid from the distributions themselves, an estate drag ` +
      `rather than a spending cut.`
    );
  },

  // The voluntary-lever sentence (tax-drag design td1, DECISION 5/6): the
  // lifetime bracket-fill-lever share of the bill, framed as money MOVED
  // (tax-deferred -> taxable), not spent. `voluntaryTotal` is already
  // display-guarded by the callers; "" when zero (no lever set, or the lever
  // never actually filled anything). `medianPrefix` distinguishes the
  // Historical/MC phrasing (mirrors _forcedSentence). Lever (<=74) and RMD
  // excess (>=75) never co-occur (DECISION 6), so in practice at most ONE of
  // this sentence / _forcedSentence fires alongside the base sentence — the
  // ft2 "at most two sentences" budget holds.
  _voluntarySentence(voluntaryTotal, medianPrefix) {
    if (!(voluntaryTotal > 0)) return "";
    const amount = TaxLocationView.fmtMoneyShort(voluntaryTotal);
    const lead = medianPrefix ? `A median ${amount}` : `${amount}`;
    return (
      ` ${lead} of the lifetime bill is tax on voluntary bracket-fill ` +
      `drawdowns — money moved from tax-deferred to taxable, not spent.`
    );
  },

  // The DETERMINISTIC readout (ft2: at most two sentences; td1 adds the
  // voluntary-lever twin of the forced sentence): average annual tax, tax as a
  // % of SPENDING, the largest single-year bill (worded as a BILL, not a
  // consumption hit — RMD-dominated late-life; qualified when mostly forced),
  // then the lifetime forced-RMD share OR the lifetime voluntary-lever share
  // (mutually exclusive in practice — DECISION 6). `ft` is the
  // payload.federal_tax block (avg_annual_tax, effective_rate, worst_tax,
  // worst_tax_age, ages, forced_tax, total_forced_tax, total_voluntary_tax).
  // Returns "" when absent.
  deterministicReadout(ft) {
    if (!ft) return "";
    const avg = TaxLocationView.fmtMoneyShort(ft.avg_annual_tax);
    const pct = this._pctOfFraction(ft.effective_rate);
    const worst = TaxLocationView.fmtMoneyShort(ft.worst_tax);
    const qual = this._worstQualifier(ft.ages, ft.forced_tax, ft.worst_tax_age, ft.worst_tax);
    return (
      `Federal tax averages ${avg}/yr — ${pct} of spending — with the largest ` +
      `single-year bill of ${worst} at age ${ft.worst_tax_age}${qual}.` +
      this._forcedSentence(this._guardForced(ft.total_forced_tax), false) +
      this._voluntarySentence(this._guardForced(ft.total_voluntary_tax), false)
    );
  },

  // The HISTORICAL / MONTE-CARLO readout: the median-of-paths twin of the
  // deterministic readout above (ft2: at most two sentences; the worst-year
  // clause is worded as a BILL, qualified off the per-age forced median at the
  // median worst age — a heuristic lookup, honest for the qualifier's coarse
  // >50% purpose; td1 adds the voluntary-lever twin). `ft` is the
  // payload.federal_tax aggregate block (median_avg_annual_tax,
  // median_effective_rate, median_worst_tax, median_worst_tax_age, ages,
  // forced_tax_median, median_total_forced_tax, median_total_voluntary_tax).
  // Returns "" when absent.
  stochasticReadout(ft) {
    if (!ft) return "";
    const avg = TaxLocationView.fmtMoneyShort(ft.median_avg_annual_tax);
    const pct = this._pctOfFraction(ft.median_effective_rate);
    const worst = TaxLocationView.fmtMoneyShort(ft.median_worst_tax);
    const qual = this._worstQualifier(
      ft.ages, ft.forced_tax_median, ft.median_worst_tax_age, ft.median_worst_tax,
    );
    return (
      `Federal tax runs a median ${avg}/yr — ${pct} of spending — with a ` +
      `median worst-year bill of ${worst} at age ${ft.median_worst_tax_age}${qual}.` +
      this._forcedSentence(this._guardForced(ft.median_total_forced_tax), true) +
      this._voluntarySentence(this._guardForced(ft.median_total_voluntary_tax), true)
    );
  },

  // The "estate cost of taxes" sentence (tax-drag design td1, DECISION 9):
  // pre-tax terminal minus dragged terminal, PAIRED with the success-rate
  // delta whenever it's nonzero — the mutual-depletion guard. When both the
  // pre-tax and dragged runs deplete, the per-path cost difference is $0 even
  // though taxes broke the after-tax plan and not the pre-tax one; without the
  // paired delta sentence, "$0 estate cost" would misread as "taxes are free"
  // in exactly that scenario. `ec` is the payload.estate_cost_of_taxes block
  // ({count, median_estate_cost, pretax_success_rate, dragged_success_rate,
  // success_rate_delta}); `endAge` is the scenario's end_age. `medianPrefix`
  // distinguishes the Historical/MC median-of-per-path-differences phrasing
  // (mirrors _forcedSentence). A NEGATIVE median_estate_cost is possible under
  // dynamic spending (DECISION 9's disclosed exception — each run reacts to
  // its own balance) and is framed honestly, not hidden. Returns "" when absent.
  _estateCostSentence(ec, endAge, medianPrefix) {
    if (!ec) return "";
    const cost = Number(ec.median_estate_cost) || 0;
    const amount = TaxLocationView.fmtMoneyShort(Math.abs(cost));
    let sentence;
    if (cost >= 0) {
      sentence = medianPrefix
        ? `Taxes cost your estate a median ~${amount} by age ${endAge}.`
        : `Taxes cost your estate ~${amount} by age ${endAge}.`;
    } else {
      sentence = medianPrefix
        ? `The after-tax run actually ends a median ~${amount} AHEAD of the ` +
          `pre-tax comparison by age ${endAge} (a dynamic-spending quirk — ` +
          `each run reacts to its own balance).`
        : `The after-tax run actually ends ~${amount} AHEAD of the pre-tax ` +
          `comparison by age ${endAge} (a dynamic-spending quirk — each run ` +
          `reacts to its own balance).`;
    }
    const delta = Number(ec.success_rate_delta) || 0;
    if (delta !== 0) {
      const pts = Math.abs(delta) * 100;
      const ptsStr = Number.isInteger(pts) ? String(pts) : pts.toFixed(1).replace(/\.0$/, "");
      const dir = delta < 0 ? "lower" : "higher";
      sentence +=
        ` The after-tax success rate runs ${ptsStr} points ${dir} than the ` +
        `pre-tax comparison's — so a small estate-cost number here doesn't ` +
        `mean taxes were free.`;
    }
    return sentence;
  },

  // Public per-mode estate-cost readouts (mirrors deterministicReadout /
  // stochasticReadout's split), both delegating to the shared sentence above.
  estateCostDeterministicReadout(ec, endAge) {
    return this._estateCostSentence(ec, endAge, false);
  },
  estateCostStochasticReadout(ec, endAge) {
    return this._estateCostSentence(ec, endAge, true);
  },
};
// Exposed for the node:vm test (tests/test_federal_tax_view.mjs); not a stable API.
window.FederalTaxView = FederalTaxView;

// Read the current GI / horizon ages off the inputs into the `ages` shape the pure
// GlidepathView functions take.
function glidepathAges() {
  // Household editor on: the phases derive from the streams' start and end
  // ages on person A's axis, as the engine derives them.
  const HE = householdApi();
  if (HE && householdEnabled()) {
    const { startAge, endAge } = HE.planAges(householdState);
    return { startAge, endAge, streamAges: HE.axisAges(householdState) };
  }
  return {
    startAge: readAgeInput("start_age", 50),
    endAge: readAgeInput("end_age", 95),
    personAPension: readAgeInput("person_a_pension_start_age", 60),
    personBPension: readAgeInput("person_b_pension_start_age", 65),
    // The claim age IS the effective SS start age, so glidepath phase boundaries
    // read it straight off the claim-age slider (slice 2: the start-age input was
    // replaced by the 62–70 slider; the value semantics are identical).
    personASs: readAgeInput("person_a_ss_claim_age", 67),
    personBSs: readAgeInput("person_b_ss_claim_age", 70),
  };
}

// DOM-reading wrappers: read the inputs, delegate to the pure GlidepathView.
function glidepathBoundaries() {
  return GlidepathView.boundaries(glidepathAges());
}

function glidepathPhaseLabels() {
  return GlidepathView.phaseLabels(glidepathAges());
}

// The seed weights for a fresh phase = the current single (Scenario A) allocation,
// as integer percents parallel to ASSET_CLASSES. So toggling glidepath on, then
// off, is a no-op (the degenerate glide reproduces the constant result), and the user
// edits each phase from a sane baseline.
function singleAllocationWeights() {
  return ASSET_CLASSES.map((_, i) => readWeight("a", i));
}

// Ensure glidepathPhaseWeights has an entry for every CURRENT derived phase,
// seeding any new phase from the single allocation and dropping stale indices
// beyond the current count. Preserves already-edited phases by index across a GI-
// age re-derivation, so changing an SS age doesn't wipe tuned phases.
function ensureGlidepathPhaseWeights() {
  const n = glidepathBoundaries().length;
  const seed = singleAllocationWeights();
  const next = {};
  for (let k = 0; k < n; k++) {
    next[k] = glidepathPhaseWeights[k] ? glidepathPhaseWeights[k].slice() : seed.slice();
  }
  glidepathPhaseWeights = next;
}

// DOM id for a glidepath phase weight input: phase index + class index.
function glidepathWeightId(phase, i) {
  return `glide-w-${phase}-${i}`;
}

// Read a phase's weight for class i as an integer percent (empty/NaN → 0, so a
// half-typed value reads as "off 100" rather than throwing). Reads the live input
// when present, else the stored state.
function readGlidepathWeight(phase, i) {
  const el = document.getElementById(glidepathWeightId(phase, i));
  if (el) {
    const n = parseInt(el.value.trim(), 10);
    return Number.isNaN(n) ? 0 : n;
  }
  const row = glidepathPhaseWeights[phase];
  return row && typeof row[i] === "number" ? row[i] : 0;
}

// A phase's weights as 4-decimal fractions (the same rounding buildAllocationBody
// emits), so validity is checked on the fractions the API receives.
function glidepathPhaseFractions(phase) {
  return ASSET_CLASSES.map(
    (_, i) => Math.round((readGlidepathWeight(phase, i) / 100) * 1e4) / 1e4,
  );
}

function glidepathPhaseTotalPct(phase) {
  return glidepathPhaseFractions(phase).reduce((s, x) => s + x, 0) * 100;
}

function glidepathPhaseTotalValid(phase) {
  const sum = glidepathPhaseFractions(phase).reduce((s, x) => s + x, 0);
  return Math.abs(sum - 1) < 1e-6;
}

// True iff EVERY derived phase totals 100% (the per-column validity rule, applied
// per phase). Used to gate Recompute the same way the single matrix's total does.
function glidepathAllPhasesValid() {
  if (allocationMode !== "glidepath") return true;
  const n = glidepathBoundaries().length;
  for (let k = 0; k < n; k++) {
    if (!glidepathPhaseTotalValid(k)) return false;
  }
  return true;
}

// A phase's blended real return (percent) = Σ (weight/100 × shared real_return).
function glidepathPhaseBlended(phase) {
  return ASSET_CLASSES.reduce(
    (sum, _, i) => sum + (readGlidepathWeight(phase, i) / 100) * readReturn(i),
    0,
  );
}

// The id-native POST allocation body for one phase: {id, weight, real_return,
// volatility} entries for the NON-ZERO-weight classes, mirroring buildAllocationBody.
function buildGlidepathPhaseBody(phase) {
  const fractions = glidepathPhaseFractions(phase);
  const entries = [];
  ASSET_CLASSES.forEach((c, i) => {
    if (fractions[i] <= 0) return;
    entries.push({
      id: c.id,
      weight: fractions[i],
      real_return: Math.round((readReturn(i) / 100) * 1e4) / 1e4,
      volatility: Math.round(readVolFraction(i) * 1e4) / 1e4,
    });
  });
  return entries;
}

// The phase_allocations array for the API: one phase body per derived phase, in
// phase order. Only emitted under glidepath mode.
function buildGlidepathPhaseAllocations() {
  const n = glidepathBoundaries().length;
  const phases = [];
  for (let k = 0; k < n; k++) phases.push(buildGlidepathPhaseBody(k));
  return phases;
}

// Render one editable allocation matrix per derived phase into #glidepath-phases.
// Each carries an "age-range · event" header, one integer-% weight input per asset
// class, a per-phase Total (100% validation), and a blended-return line — reusing
// the constant matrix's row classes so the look is consistent. The matrix set is
// DYNAMIC (N phases, re-derived from the GI ages), not a fixed 4.
function renderGlidepathPhases() {
  const container = document.getElementById("glidepath-phases");
  if (!container) return;
  ensureGlidepathPhaseWeights();
  const labels = glidepathPhaseLabels();
  container.innerHTML = "";

  labels.forEach((label, phase) => {
    const card = document.createElement("div");
    card.className = "glidepath-phase";
    card.dataset.phase = String(phase);

    const head = document.createElement("div");
    head.className = "glidepath-phase-head";
    head.textContent = label;
    card.appendChild(head);

    const rows = document.createElement("div");
    rows.className = "allocation-rows glidepath-phase-rows";
    ASSET_CLASSES.forEach((c, i) => {
      const row = document.createElement("div");
      row.className = "allocation-row";

      const lbl = document.createElement("label");
      lbl.className = "allocation-label";
      lbl.textContent = c.label;
      lbl.htmlFor = glidepathWeightId(phase, i);
      row.appendChild(lbl);

      const weights = document.createElement("div");
      weights.className = "allocation-weights linked";
      const cell = document.createElement("div");
      cell.className = "allocation-weight-cell";
      const input = document.createElement("input");
      input.type = "number";
      input.id = glidepathWeightId(phase, i);
      input.name = glidepathWeightId(phase, i);
      input.className = "allocation-weight-input";
      input.value = String(glidepathPhaseWeights[phase][i]);
      input.min = "0";
      input.max = "100";
      input.step = "1";
      input.required = true;
      input.addEventListener("input", () => {
        // Persist the edit into state (so a GI-age re-derive preserves it), then
        // refresh just this card's readout.
        glidepathPhaseWeights[phase][i] = readGlidepathWeight(phase, i);
        refreshGlidepathPhase(phase);
      });
      const pct = document.createElement("span");
      pct.className = "allocation-pct";
      pct.textContent = "%";
      cell.appendChild(input);
      cell.appendChild(pct);
      weights.appendChild(cell);
      row.appendChild(weights);
      rows.appendChild(row);
    });
    card.appendChild(rows);

    const totals = document.createElement("div");
    totals.className = "allocation-totals glidepath-phase-totals";
    card.appendChild(totals);

    const blended = document.createElement("div");
    blended.className = "allocation-blended glidepath-phase-blended";
    card.appendChild(blended);

    container.appendChild(card);
    refreshGlidepathPhase(phase);
  });
}

// Refresh one phase card's Total + Blended readout (per-phase 100% validation).
function refreshGlidepathPhase(phase) {
  const card = document.querySelector(
    `#glidepath-phases .glidepath-phase[data-phase="${phase}"]`,
  );
  if (!card) return;
  const totals = card.querySelector(".glidepath-phase-totals");
  if (totals) {
    const valid = glidepathPhaseTotalValid(phase);
    totals.innerHTML = "";
    const row = document.createElement("div");
    row.className = "allocation-readout";
    const lbl = document.createElement("span");
    lbl.className = "allocation-readout-label";
    lbl.textContent = "Total";
    const vals = document.createElement("div");
    vals.className = "allocation-readout-values linked";
    const cell = document.createElement("span");
    cell.className = "allocation-total-value allocation-readout-cell";
    cell.textContent = `${formatPct(glidepathPhaseTotalPct(phase))}%`;
    cell.classList.toggle("is-valid", valid);
    cell.classList.toggle("is-invalid", !valid);
    vals.appendChild(cell);
    row.appendChild(lbl);
    row.appendChild(vals);
    totals.appendChild(row);
    if (!valid) {
      const msg = document.createElement("p");
      msg.className = "allocation-total-msg";
      msg.textContent = "must total 100%";
      totals.appendChild(msg);
    }
  }
  const blended = card.querySelector(".glidepath-phase-blended");
  if (blended) {
    blended.innerHTML = "";
    const row = document.createElement("div");
    row.className = "allocation-readout";
    const lbl = document.createElement("span");
    lbl.className = "allocation-readout-label";
    lbl.textContent = "Blended";
    const vals = document.createElement("div");
    vals.className = "allocation-readout-values linked";
    const cell = document.createElement("span");
    cell.className = "allocation-blended-value allocation-readout-cell";
    cell.textContent = `${glidepathPhaseBlended(phase).toFixed(1)}%`;
    vals.appendChild(cell);
    row.appendChild(lbl);
    row.appendChild(vals);
    blended.appendChild(row);
  }
}

// Show/hide the constant matrix vs the glidepath phase matrices + caption per the
// active allocation mode, and reflect the toggle chrome.
function updateAllocationModeView() {
  const constantBody = document.getElementById("constant-allocation-body");
  const phasesEl = document.getElementById("glidepath-phases");
  const captionEl = document.getElementById("glidepath-caption");
  const isGlide = allocationMode === "glidepath";
  if (constantBody) constantBody.hidden = isGlide;
  // The allocation A│B link toggle only makes sense for the constant matrix.
  const linkBtn = document.getElementById("allocation-link");
  if (linkBtn) linkBtn.hidden = isGlide;
  if (phasesEl) phasesEl.hidden = !isGlide;
  if (captionEl) captionEl.hidden = !isGlide;
  // gpp1: the glide-preset picker only makes sense in glidepath mode.
  const glidePresetEl = document.getElementById("glide-preset");
  if (glidePresetEl) glidePresetEl.hidden = !isGlide;
  for (const btn of document.querySelectorAll(
    "#allocation-mode-toggle .mode-toggle-btn",
  )) {
    const selected = btn.dataset.allocationMode === allocationMode;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// Flip the allocation shape and re-render. No-op if already active. On entering
// glidepath it (re-)derives the phases seeded from the single allocation; on
// leaving it just hides them (state is kept so re-entering restores tuned phases).
function setAllocationMode(mode) {
  if (mode !== "constant" && mode !== "glidepath") return;
  if (mode === allocationMode) return;
  allocationMode = mode;
  if (mode === "glidepath") {
    ensureGlidepathPhaseWeights();
    renderGlidepathPhases();
  }
  updateAllocationModeView();
  setError("");
  rerender();
}

// ---------- Glide preset picker (gpp1) ----------
//
// Mirrors the portfolio Presets <select> (renderPresetOptions / applyPreset /
// resetPresetToCustom), but applies a NAMED GLIDE SHAPE to the per-phase glidepath
// weights instead of a single allocation. Fire-once: after applying, the <select>
// resets to its placeholder (the user can then hand-edit any phase).

// Populate the glide-preset <select> from GlidePresets.PRESETS (after the
// placeholder). Each option's value is the preset index; the label is its name.
function renderGlidePresetOptions() {
  const sel = document.getElementById("glide-preset-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">Glide preset…</option>';
  GlidePresets.PRESETS.forEach((p, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
}

// Reset the glide-preset <select> back to its placeholder — fire-once behavior
// matching the portfolio Preset dropdown (the preset is a starting point, not a
// lock; hand-edits shouldn't leave a stale "selected" preset showing).
function resetGlidePresetToPlaceholder() {
  const sel = document.getElementById("glide-preset-select");
  if (sel && sel.value !== "") sel.value = "";
}

// Apply a glide preset by index: switch to glidepath mode if needed, fill
// glidepathPhaseWeights per phase from the preset's interpolated equity glide (or
// the verbatim single allocation for "Match current"), render the phase matrices,
// and recompute EXACTLY ONCE.
//
// Sequencing to avoid a double recompute: setAllocationMode("glidepath") itself
// renders + rerenders. So rather than call it and then re-fill + rerender again
// (two recomputes), we fill the weights FIRST, then drive the mode transition
// inline — replicating setAllocationMode's body (set mode, render, update view)
// MINUS its rerender — and fire a single rerender() at the end. When already in
// glidepath mode we just re-render the phases + one rerender(). Either path =
// exactly one recompute.
function applyGlidePreset(index) {
  const preset = GlidePresets.PRESETS[index];
  if (!preset) return;

  // Seed = the current single (Scenario A) allocation, as integer percents parallel
  // to ASSET_CLASSES (the same seed ensureGlidepathPhaseWeights uses).
  const seed = singleAllocationWeights();
  const classIds = ASSET_CLASSES.map((c) => c.id);
  const n = glidepathBoundaries().length;
  // Equity classification is DATA from the loaded class menu (/api/classes'
  // `is_equity`, which the API sources from firebridgelab.engine.buckets.EQUITY_CLASS_IDS) — not
  // a hardcoded list. The preset mapping receives it as input.
  const equityIds = equityClassIds();

  glidepathPhaseWeights = GlidePresets.buildPhaseWeights(preset, seed, classIds, n, equityIds);

  if (allocationMode === "glidepath") {
    renderGlidepathPhases();
  } else {
    // Inline mode transition without setAllocationMode's own rerender (avoid the
    // double recompute). renderGlidepathPhases() must NOT call ensureGlidepathPhaseWeights
    // over our freshly-written weights — it preserves existing phases by index, so the
    // preset weights survive.
    allocationMode = "glidepath";
    renderGlidepathPhases();
    updateAllocationModeView();
    setError("");
  }
  rerender();
  resetGlidePresetToPlaceholder();
}

// ---------- Buckets sourcing: editable bucket matrices (bucket-strategy design slice 1b) ----------
//
// Three editable per-bucket allocation matrices (Cash / Income / Growth) reusing
// the glidepath per-phase matrix pattern: one integer-% weight input per asset
// class, a per-bucket Total (100% validation) + Blended-return readout. The bucket
// config is GLOBAL in v1 (one set applied to both A and B). buildBucketsBody emits
// the id-native POST `buckets` body; readScenarios attaches it on BOTH scenarios.

// DOM id for a bucket weight input: bucket id + class index.
function bucketWeightId(bucketId, i) {
  return `bucket-w-${bucketId}-${i}`;
}

// Index of a class id in ASSET_CLASSES, or -1 if absent (the 2-class fallback
// before /api/classes loads won't carry st_treasury, so growth-default seeding
// degrades gracefully — see seedBucketWeights).
function classIndexById(classId) {
  return ASSET_CLASSES.findIndex((c) => c.id === classId);
}

// Seed each bucket's weights to its default 100%-weight class (mirrors
// firebridgelab.default_bucket_config): cash → st_treasury, income → us_agg_bonds, growth
// → us_total_market. Seeded ONCE on the first Buckets enable, then preserved across
// re-renders (so toggling Buckets off/on keeps the user's edits). If a default
// class isn't in the menu (the 2-class fallback), that bucket seeds to the first
// class so it still totals 100% and the matrix is editable.
function seedBucketWeights() {
  if (bucketWeightsSeeded) return;
  for (const bid of BUCKET_IDS) {
    const arr = ASSET_CLASSES.map(() => 0);
    let idx = classIndexById(BUCKET_DEFAULT_CLASS[bid]);
    if (idx < 0) idx = 0;
    if (arr.length) arr[idx] = 100;
    bucketWeights[bid] = arr;
  }
  bucketWeightsSeeded = true;
}

// Read one bucket weight input's integer percent (empty/NaN → 0, like the single
// matrix), syncing the value back into state so a re-render preserves edits.
function readBucketWeight(bucketId, i) {
  const el = document.getElementById(bucketWeightId(bucketId, i));
  if (!el) {
    const row = bucketWeights[bucketId];
    return row && row[i] != null ? row[i] : 0;
  }
  const n = parseInt(el.value.trim(), 10);
  return Number.isNaN(n) ? 0 : n;
}

// One bucket's weights as rounded FRACTIONS (percent/100), exactly as the POST body
// emits them — the single source of truth for validity + the displayed Total.
function bucketFractions(bucketId) {
  return ASSET_CLASSES.map(
    (_, i) => Math.round((readBucketWeight(bucketId, i) / 100) * 1e4) / 1e4,
  );
}

function bucketTotalPct(bucketId) {
  return bucketFractions(bucketId).reduce((s, x) => s + x, 0) * 100;
}

function bucketTotalValid(bucketId) {
  const sum = bucketFractions(bucketId).reduce((s, x) => s + x, 0);
  return Math.abs(sum - 1) < 1e-6;
}

// All three buckets total 100% (the Recompute gate, like glidepathAllPhasesValid).
// True (no-op) outside buckets mode.
function bucketsAllValid() {
  if (sourcingMode !== "buckets") return true;
  return BUCKET_IDS.every((bid) => bucketTotalValid(bid));
}

// A bucket's blended real return (percent) = Σ (weight/100 × class real_return),
// using the SAME shared per-class return inputs the single/glidepath matrices read.
function bucketBlended(bucketId) {
  return ASSET_CLASSES.reduce(
    (sum, _, i) => sum + (readBucketWeight(bucketId, i) / 100) * readReturn(i),
    0,
  );
}

// The id-native POST allocation body for one bucket (a list of {id, weight,
// real_return, volatility} entries, one per non-zero-weight class) — the SAME
// shape buildAllocationBody emits, so a bucket validates exactly like the single
// matrix (100%-sum + per-class bounds on the API).
function buildBucketAllocationBody(bucketId) {
  const fractions = bucketFractions(bucketId);
  const entries = [];
  ASSET_CLASSES.forEach((c, i) => {
    if (fractions[i] <= 0) return;
    entries.push({
      id: c.id,
      weight: fractions[i],
      real_return: Math.round((readReturn(i) / 100) * 1e4) / 1e4,
      volatility: Math.round(readVolFraction(i) * 1e4) / 1e4,
    });
  });
  return entries;
}

// Read N1/N2 off the inputs (empty/NaN → the seeded default, clamped ≥ 0), syncing
// back into state. The API also range-checks (422 on negative); this keeps a
// half-typed value from throwing the live readout.
function readBucketN1() {
  const el = document.getElementById("bucket-n1");
  if (!el) return bucketN1;
  const n = parseInt(el.value.trim(), 10);
  return Number.isNaN(n) ? bucketN1 : Math.max(0, n);
}
function readBucketN2() {
  const el = document.getElementById("bucket-n2");
  if (!el) return bucketN2;
  const n = parseInt(el.value.trim(), 10);
  return Number.isNaN(n) ? bucketN2 : Math.max(0, n);
}

// The full id-native `buckets` body for the POST request (bucket-strategy design):
// the three per-bucket allocations + N1/N2. GLOBAL in v1 (applied to both A and B).
function buildBucketsBody() {
  return {
    n1: readBucketN1(),
    n2: readBucketN2(),
    cash: { id: "cash", allocation: buildBucketAllocationBody("cash") },
    income: { id: "income", allocation: buildBucketAllocationBody("income") },
    growth: { id: "growth", allocation: buildBucketAllocationBody("growth") },
  };
}

// Render the three editable bucket allocation matrices into #bucket-matrices. Each
// is a card with a bucket-name header, one integer-% weight input per asset class,
// a per-bucket Total (100% validation) + a blended-return line — reusing the
// allocation-row / readout classes the glidepath phase matrices use, so the look
// is consistent. Mirrors renderGlidepathPhases (3 fixed instances vs N dynamic).
function renderBucketMatrices() {
  const container = document.getElementById("bucket-matrices");
  if (!container) return;
  seedBucketWeights();
  container.innerHTML = "";

  BUCKET_IDS.forEach((bid) => {
    const card = document.createElement("div");
    card.className = "glidepath-phase bucket-matrix";
    card.dataset.bucket = bid;

    const head = document.createElement("div");
    head.className = "glidepath-phase-head bucket-matrix-head";
    head.textContent = BUCKET_LABELS[bid];
    card.appendChild(head);

    const rows = document.createElement("div");
    rows.className = "allocation-rows glidepath-phase-rows";
    ASSET_CLASSES.forEach((c, i) => {
      const row = document.createElement("div");
      row.className = "allocation-row";

      const lbl = document.createElement("label");
      lbl.className = "allocation-label";
      lbl.textContent = c.label;
      lbl.htmlFor = bucketWeightId(bid, i);
      row.appendChild(lbl);

      const weights = document.createElement("div");
      weights.className = "allocation-weights linked";
      const cell = document.createElement("div");
      cell.className = "allocation-weight-cell";
      const input = document.createElement("input");
      input.type = "number";
      input.id = bucketWeightId(bid, i);
      input.name = bucketWeightId(bid, i);
      input.className = "allocation-weight-input";
      input.value = String(bucketWeights[bid][i]);
      input.min = "0";
      input.max = "100";
      input.step = "1";
      input.required = true;
      input.addEventListener("input", () => {
        bucketWeights[bid][i] = readBucketWeight(bid, i);
        refreshBucketMatrix(bid);
      });
      const pct = document.createElement("span");
      pct.className = "allocation-pct";
      pct.textContent = "%";
      cell.appendChild(input);
      cell.appendChild(pct);
      weights.appendChild(cell);
      row.appendChild(weights);
      rows.appendChild(row);
    });
    card.appendChild(rows);

    const totals = document.createElement("div");
    totals.className = "allocation-totals glidepath-phase-totals";
    card.appendChild(totals);

    const blended = document.createElement("div");
    blended.className = "allocation-blended glidepath-phase-blended";
    card.appendChild(blended);

    container.appendChild(card);
    refreshBucketMatrix(bid);
  });
}

// Refresh one bucket card's Total + Blended readout (per-bucket 100% validation).
// Mirrors refreshGlidepathPhase.
function refreshBucketMatrix(bucketId) {
  const card = document.querySelector(
    `#bucket-matrices .bucket-matrix[data-bucket="${bucketId}"]`,
  );
  if (!card) return;
  const totals = card.querySelector(".glidepath-phase-totals");
  if (totals) {
    const valid = bucketTotalValid(bucketId);
    totals.innerHTML = "";
    const row = document.createElement("div");
    row.className = "allocation-readout";
    const lbl = document.createElement("span");
    lbl.className = "allocation-readout-label";
    lbl.textContent = "Total";
    const vals = document.createElement("div");
    vals.className = "allocation-readout-values linked";
    const cell = document.createElement("span");
    cell.className = "allocation-total-value allocation-readout-cell";
    cell.textContent = `${formatPct(bucketTotalPct(bucketId))}%`;
    cell.classList.toggle("is-valid", valid);
    cell.classList.toggle("is-invalid", !valid);
    vals.appendChild(cell);
    row.appendChild(lbl);
    row.appendChild(vals);
    totals.appendChild(row);
    if (!valid) {
      const msg = document.createElement("p");
      msg.className = "allocation-total-msg";
      msg.textContent = "must total 100%";
      totals.appendChild(msg);
    }
  }
  const blended = card.querySelector(".glidepath-phase-blended");
  if (blended) {
    blended.innerHTML = "";
    const row = document.createElement("div");
    row.className = "allocation-readout";
    const lbl = document.createElement("span");
    lbl.className = "allocation-readout-label";
    lbl.textContent = "Blended";
    const vals = document.createElement("div");
    vals.className = "allocation-readout-values linked";
    const cell = document.createElement("span");
    cell.className = "allocation-blended-value allocation-readout-cell";
    cell.textContent = `${bucketBlended(bucketId).toFixed(1)}%`;
    vals.appendChild(cell);
    row.appendChild(lbl);
    row.appendChild(vals);
    blended.appendChild(row);
  }
}

// Show/hide the bucket-config block (N1/N2 + the 3 matrices) per the active
// sourcing mode. Shown only in Buckets; hidden + state-preserved in Single.
function updateSourcingModeView() {
  const cfg = document.getElementById("bucket-config");
  if (cfg) cfg.hidden = sourcingMode !== "buckets";
}

// ---------- Buckets sourcing: bucket-balance chart (bucket-strategy design slice 1b) ----------

// The three bucket-line colours. Distinct from the A/B scenario blue/magenta so the
// 3-line chart doesn't read as a comparison: cash teal, income amber, growth green.
// FUNCTION (not a baked object): reads the tokens at render time inside
// renderBucketBalanceChart, so the value is never cached at module load.
const bucketLineColors = () => ({
  cash: cssVar("--chart-bucket-cash"),     // teal — the safe/liquid buffer
  income: cssVar("--chart-bucket-income"), // amber — the intermediate bond bucket
  growth: cssVar("--chart-bucket-growth"), // blue — the risk/growth bucket
});

function destroyBucketChart() {
  if (bucket.chart) {
    bucket.chart.destroy();
    bucket.chart = null;
  }
}

// Draw the 3-line bucket-balance chart (cash/income/growth over age) from a
// {ages, cash, income, growth} series via ChartView.lineChart (REUSE, not a new
// chart type). `title` + `note` annotate which series this is (the exact
// deterministic balances, or the worst cohort's). Destroys the prior instance
// first (no leak), like the per-panel / historical charts.
//
// `scenario` (the active A-side scenario, echoed on the payload) feeds the
// tooltip FOOTER: the hovered age's Spending + Guaranteed income, so the user can
// see what spending the buckets are covering at that age (the projection chart +
// year-by-year table already show this; the bucket chart didn't). They ride in a
// footer (not extra dataset rows) because interaction.mode "index" returns one row
// per dataset via tooltipLabel — there's no 4th/5th dataset to hang spend/GI on.
// Spending is the age-deterministic constant/lifecycle value (guardrails is fenced
// in buckets mode), so deterministicSpendAtAge is correct in BOTH the deterministic
// and historical-worst-cohort views. Omitted when no scenario is available.
function renderBucketBalanceChart(series, { title, note, scenario }) {
  const canvas = document.getElementById("bucket-balance-chart");
  if (!canvas) return;
  destroyBucketChart();
  const titleEl = document.getElementById("bucket-chart-title");
  const noteEl = document.getElementById("bucket-chart-note");
  if (titleEl) titleEl.textContent = title;
  if (noteEl) noteEl.textContent = note || "";

  const bucketColors = bucketLineColors();
  const ds = (id, label) => ({
    label,
    data: series[id],
    borderColor: bucketColors[id],
    backgroundColor: bucketColors[id],
    fill: false,
    tension: 0.15,
    pointRadius: 0,
    borderWidth: 2,
  });
  bucket.chart = ChartView.lineChart(canvas, {
    labels: series.ages,
    datasets: [
      ds("cash", "Cash bucket"),
      ds("income", "Income bucket"),
      ds("growth", "Growth bucket"),
    ],
    options: {
      legend: { display: true, position: "bottom" },
      tooltipDisplayColors: true,
      yTickFormatter: (v) => fmtMillions(v),
      // interaction.mode "index" fires this callback ONCE PER DATASET at the
      // hovered index (3 buckets => 3 calls). Each call must return only ITS
      // OWN row, keyed by item.datasetIndex — same pattern as the comparison
      // overlay tooltip above. Returning the full 3-row set on every call
      // duplicated the buckets 3x (9 rows total). We still build all three rows
      // through tooltipRows so the monospace columns stay aligned across the
      // set, then index out the one row this dataset owns.
      tooltipLabel: (item) => {
        const i = item.dataIndex;
        const rows = ChartView.tooltipRows([
          ["Cash", fmtMoney(series.cash[i])],
          ["Income", fmtMoney(series.income[i])],
          ["Growth", fmtMoney(series.growth[i])],
        ]);
        return rows[item.datasetIndex];
      },
      // Footer fires ONCE per tooltip (below the 3 bucket rows): the hovered
      // age's Spending + Guaranteed income, so the user sees what the buckets are
      // covering. Reuses the same client helpers the fan/projection tooltips use
      // (deterministicSpendAtAge / guaranteedIncomeAtAge). The age is the hovered
      // label (series.ages[dataIndex]); both items share it under "index" mode.
      // No scenario (or a spend the helper can't compute) → no footer line.
      tooltipFooter: bucketTooltipFooter(series, scenario),
    },
  });
}

// Build the bucket-balance chart's tooltip footer fn: a once-per-tooltip line
// showing the hovered age's Spending + Guaranteed income, e.g.
// "Spend $100,000 · GI $40,000". Returns null (no footer) when no scenario is
// available, so the tooltip just shows the 3 bucket rows. `scenario` is the active
// (A-side) scenario; `series.ages` maps the hovered dataIndex back to an age.
function bucketTooltipFooter(series, scenario) {
  if (!scenario) return undefined;
  return (items) => {
    if (!items || !items.length) return undefined;
    const age = series.ages[items[0].dataIndex];
    const spend = deterministicSpendAtAge(scenario, age);
    const gi = guaranteedIncomeAtAge(scenario, age);
    const spendStr = spend !== null && spend !== undefined ? fmtMoney(spend) : "n/a";
    return `Spend ${spendStr} · GI ${fmtMoney(gi)}`;
  };
}

// Show/draw or hide/tear-down the bucket-balance chart from a payload, per the
// active sourcing + sim mode:
//   - single sourcing                -> hidden + torn down.
//   - buckets + Deterministic        -> the exact 3 balances (payload.bucket_balances).
//   - buckets + Historical           -> the WORST cohort's 3 balances
//     (payload.worst_bucket_cohort), titled so it's clear it's the worst cohort +
//     its start year (the window note rides in the chart note).
//   - buckets + Monte Carlo          -> OMITTED (synthetic trials have no calendar
//     cohort to draw — MC shows the buffer-survival stats only, consistent with the
//     guardrails worst-cohort drill-down being Historical-only). The chart is hidden.
// `payload` is the (A-side / single) payload; null clears the chart.
function updateBucketChartView(payload) {
  const view = document.getElementById("bucket-chart-view");
  const matchedNote = document.getElementById("bucket-matched-note");
  if (sourcingMode !== "buckets") {
    if (view) view.hidden = true;
    if (matchedNote) matchedNote.hidden = true;
    destroyBucketChart();
    return;
  }
  // Matched-allocation guidance shows whenever a comparison is active in buckets
  // mode (a fair head-to-head sets the same overall split on both).
  if (matchedNote) matchedNote.hidden = !isComparing();

  const p = payload && !payload.error ? payload : null;

  if (simMode === "monte_carlo") {
    // MC: no calendar cohort to draw — stats only. Hide + tear down the chart.
    if (view) view.hidden = true;
    destroyBucketChart();
    return;
  }

  if (simMode === "deterministic") {
    const bb = p ? p.bucket_balances : null;
    if (!bb) {
      if (view) view.hidden = true;
      destroyBucketChart();
      return;
    }
    if (view) view.hidden = false;
    renderBucketBalanceChart(bb, {
      title: "Bucket balances over time (Deterministic)",
      note:
        "Deterministic mode has no down years, so the buckets just refill every " +
        "year — the protective 'ride the downturn' behavior can't show here. " +
        "Switch to Historical or Monte Carlo to see the buffer tested.",
      scenario: p.scenario,
    });
    return;
  }

  // Historical: draw the worst cohort's 3 balances.
  const wc = p ? p.worst_bucket_cohort : null;
  if (!wc) {
    if (view) view.hidden = true;
    destroyBucketChart();
    return;
  }
  if (view) view.hidden = false;
  const since = p && p.testable_since != null ? p.testable_since : null;
  const windowClause = since != null
    ? `Tested since ${since} — the shortest-history asset class in your buckets restricts the cohort window. `
    : "";
  const breachClause = wc.first_forced_sale_age != null
    ? `Its buffer first broke at age ${wc.first_forced_sale_age} (a forced growth sale).`
    : "Its buffer was never breached — no forced growth sale (this config rode every downturn).";
  renderBucketBalanceChart(wc, {
    title: `Bucket balances — the worst cohort (started ${wc.start_year})`,
    note: `${windowClause}${breachClause}`,
    scenario: p.scenario,
  });
}

// ---------- Tax-location balance chart + bridge readout (tax-buckets design tb1) ----------

// The three account-line colours — deliberately DISTINCT from the bucket chart's
// cash/income/growth set (teal/amber/blue) AND the A/B scenario blue/magenta, so the
// tax-location chart never reads as either the bucket chart or a comparison. Taxable
// = steel (the freely-reachable money), tax-deferred = plum (the walled
// account), Roth = gold.
// FUNCTIONS (not baked values): read the tokens at render time inside
// renderTaxLocationLines / renderTaxLocationFan, never cached at module load.
const taxLocationLineColors = () => ({
  taxable: cssVar("--chart-loc-taxable"),           // steel — reachable anytime
  tax_deferred: cssVar("--chart-loc-tax-deferred"), // plum — walled until 59½
  roth: cssVar("--chart-loc-roth"),                 // gold — reachable, with the documented caveat
});
// The taxable fan colour for Historical / Monte Carlo (the steel, as an "R, G, B"
// triple for ChartView.fanDatasets' rgba band fills).
const TAX_TAXABLE_FAN_RGB = () => cssVar("--chart-loc-taxable-rgb");
// Neutral slate "R, G, B" for the TOTAL-investments fan (txlb hotfix). Deliberately
// distinct from the three account line colours (steel / plum / gold) so the
// total spread band + its median line read as the aggregate, not a fourth account.
const TAX_TOTAL_FAN_RGB = () => cssVar("--chart-total-fan-rgb"); // slate-500
const TAX_TOTAL_FAN_LINE = () => cssVar("--chart-total-fan-line"); // slate-600 — the total-median line

// Update the live running-sum helper under the inputs ("$1.8M of $1.8M — balanced" /
// "$150k over" / "$150k under"). Empty inputs (feature off) → hidden. The "over" /
// "under" states carry the .is-imbalanced class (red). Reads the live split + the
// starting-portfolio input; no recompute. Hidden entirely in buckets mode (the gate
// note replaces it there).
function updateTaxLocationSum() {
  // tb1b: the enable checkbox is a "has split" indicator — keep it in lockstep with
  // the inputs on every change this helper fires for (manual typing, portfolio edit,
  // gate refresh). Done first so it reflects state in every branch below.
  syncTaxLocationEnable();
  const el = document.getElementById("tax-location-sum");
  if (!el) return;
  if (sourcingMode === "buckets") {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const split = readTaxLocation();
  let target = 0;
  try {
    target = readFieldValue(
      FIELDS.find((f) => f.name === "initial_portfolio"),
      aInputEl("initial_portfolio"),
    );
  } catch (_err) {
    target = 0;
  }
  const helper = TaxLocationView.sumHelper({ ...split, target });
  if (!helper.active) {
    el.hidden = true;
    el.textContent = "";
    el.classList.remove("is-imbalanced");
    return;
  }
  el.hidden = false;
  el.textContent = helper.text;
  el.classList.toggle("is-imbalanced", helper.status !== "balanced");
}

// Gate the tax-location inputs to single sourcing (tb1). Under Buckets the three
// inputs are disabled + dimmed and the gate note shows; back in Single they're
// re-enabled and the live sum-helper returns. Called from setSourcingMode +
// applyScenarioToForm + init so the gate always reflects the live sourcing mode.
function updateTaxLocationGate() {
  const isBuckets = sourcingMode === "buckets";
  const section = document.getElementById("tax-location-inputs");
  const note = document.getElementById("tax-location-gate-note");
  for (const id of [
    TAX_TAXABLE_INPUT_ID,
    TAX_DEFERRED_INPUT_ID,
    TAX_ROTH_INPUT_ID,
    TAX_LOCATION_ENABLE_ID, // tb1b: grey the enable checkbox under buckets too
    TAX_GAINS_FRACTION_INPUT_ID, // ft1: the gains knob rides the same buckets gate
    BRACKET_FILL_TARGET_INPUT_ID, // td1: the lever rides the same buckets gate
  ]) {
    const inp = document.getElementById(id);
    if (inp) inp.disabled = isBuckets;
  }
  if (section) section.classList.toggle("is-gated", isBuckets);
  if (note) note.hidden = !isBuckets;
  updateTaxLocationSum();
}

function destroyTaxLocationChart() {
  if (taxLocation.chart) {
    taxLocation.chart.destroy();
    taxLocation.chart = null;
  }
}

// Draw the DETERMINISTIC 3-account-line chart (taxable / tax-deferred / Roth over
// age) from payload.tax_location. REUSE ChartView.lineChart (not a new chart type),
// mirroring renderBucketBalanceChart. Labelled by ACCOUNT TYPE, never "buckets".
function renderTaxLocationLines(tl) {
  const canvas = document.getElementById("tax-location-chart");
  if (!canvas) return;
  destroyTaxLocationChart();
  const locColors = taxLocationLineColors();
  const ds = (id, label) => ({
    label,
    data: tl[id],
    borderColor: locColors[id],
    backgroundColor: locColors[id],
    fill: false,
    tension: 0.15,
    pointRadius: 0,
    borderWidth: 2,
  });
  taxLocation.chart = ChartView.lineChart(canvas, {
    labels: tl.ages,
    datasets: [
      ds("taxable", "Taxable brokerage"),
      ds("tax_deferred", "Tax-deferred (401k/IRA)"),
      ds("roth", "Roth"),
    ],
    options: {
      legend: { display: true, position: "bottom" },
      tooltipDisplayColors: true,
      yTickFormatter: (v) => fmtMillions(v),
      // interaction.mode "index" fires once per dataset at the hovered index (3
      // accounts => 3 calls); each call returns only ITS OWN row, same pattern as
      // the bucket chart tooltip. Build all three rows so the monospace columns
      // align, then index out the one this dataset owns.
      tooltipLabel: (item) => {
        const i = item.dataIndex;
        const rows = ChartView.tooltipRows([
          ["Taxable", fmtMoney(tl.taxable[i])],
          ["Tax-deferred", fmtMoney(tl.tax_deferred[i])],
          ["Roth", fmtMoney(tl.roth[i])],
        ]);
        return rows[item.datasetIndex];
      },
    },
  });
}

// Draw the Historical / Monte-Carlo tax-location chart (txlb hotfix): a TOTAL-
// investments outer fan (total_p10/median/p90) plus three account median LINES
// (taxable / tax-deferred / Roth, same colours as the Deterministic chart). The
// total fan equals the portfolio fan by construction; the median lines show
// typical composition. The DOM-free config build lives in TaxLocationView.fanConfig
// (unit-tested); this thin wrapper grabs the canvas + instantiates the chart.
function renderTaxLocationFan(risk) {
  const canvas = document.getElementById("tax-location-chart");
  if (!canvas) return;
  destroyTaxLocationChart();
  const cfg = TaxLocationView.fanConfig(risk, {
    lineColors: taxLocationLineColors(),
    totalRgb: TAX_TOTAL_FAN_RGB(),
    totalLine: TAX_TOTAL_FAN_LINE(),
    lineWidth: LINE_WIDTH,
    fmtMoney,
    fmtMillions,
  });
  taxLocation.chart = ChartView.fanChart(canvas, cfg);
}

// Write the mode-aware bridge readout + the pre-tax caveat beneath the chart.
// Deterministic uses payload.tax_location; Historical/MC uses
// payload.tax_location_risk. The caveat is always shown (quietly, in a muted note).
function renderTaxLocationBridge(payload) {
  const bridgeEl = document.getElementById("tax-location-bridge");
  const caveatEl = document.getElementById("tax-location-caveat");
  if (!bridgeEl) return;
  let text = "";
  if (simMode === "deterministic") {
    text = TaxLocationView.deterministicReadout(payload ? payload.tax_location : null);
  } else {
    text = TaxLocationView.stochasticReadout(payload ? payload.tax_location_risk : null);
  }
  bridgeEl.textContent = text;
  if (caveatEl) {
    caveatEl.textContent = text ? TaxLocationView.BALANCE_NOTE : "";
    caveatEl.hidden = !text;
  }
}

// Show/draw or hide/tear-down the tax-location balance chart + bridge readout from
// a payload, per the active split + sourcing + sim + comparison state:
//   - no split, or buckets sourcing, or COMPARING -> hidden + torn down. (tb1 scopes
//     the split to the non-comparison single-scenario view — it's a per-scenario,
//     single-mode feature; combining it with A/B is out of scope, so we hide rather
//     than render something ambiguous.)
//   - split + Deterministic -> the 3 account lines (payload.tax_location).
//   - split + Historical / MC -> the taxable fan (payload.tax_location_risk).
// `payload` is the (A-side / single) payload; null clears the chart.
function updateTaxLocationView(payload) {
  const view = document.getElementById("tax-location-view");
  // Off / gated / comparing → hide + tear down. Comparing is hidden in tb1 (the
  // split is single-scenario only); readScenarios still sends the split on both, but
  // the chart/readout would be ambiguous, so we don't draw them while comparing.
  if (!taxLocationActive() || isComparing()) {
    if (view) view.hidden = true;
    destroyTaxLocationChart();
    return;
  }
  const p = payload && !payload.error ? payload : null;

  if (simMode === "deterministic") {
    const tl = p ? p.tax_location : null;
    if (!tl) {
      if (view) view.hidden = true;
      destroyTaxLocationChart();
      return;
    }
    if (view) view.hidden = false;
    const titleEl = document.getElementById("tax-location-chart-title");
    const noteEl = document.getElementById("tax-location-chart-note");
    if (titleEl) titleEl.textContent = "Account balances over time (Deterministic)";
    if (noteEl) {
      noteEl.textContent =
        "The exact taxable, tax-deferred, and Roth balances on one average-returns " +
        "path. Switch to Historical or Monte Carlo to see how often the taxable " +
        "account bottoms out before 59½ across market paths.";
    }
    renderTaxLocationLines(tl);
    renderTaxLocationBridge(p);
    renderFederalTaxReadout(p);
    renderEstateCostReadout(p);
    return;
  }

  // Historical / Monte Carlo: the taxable fan. Needs the percentile bands.
  const risk = p ? p.tax_location_risk : null;
  if (!risk || !Array.isArray(risk.ages) || !Array.isArray(risk.taxable_median)) {
    if (view) view.hidden = true;
    destroyTaxLocationChart();
    return;
  }
  if (view) view.hidden = false;
  const titleEl = document.getElementById("tax-location-chart-title");
  const noteEl = document.getElementById("tax-location-chart-note");
  if (titleEl) titleEl.textContent = "Investment balances by location (real $)";
  if (noteEl) {
    const modeName = simMode === "monte_carlo" ? "simulated" : "historical";
    noteEl.textContent =
      `The median balance in each account (taxable, tax-deferred, Roth) across ` +
      `${modeName} paths, plus the shaded spread of your TOTAL investments (10th–90th ` +
      `percentile, with the total median). Median isn't additive, so the three lines ` +
      `won't stack to the total — the lines show typical composition, the band shows ` +
      `the range. Where taxable bottoms out before 59½, the bridge years are exposed.`;
  }
  renderTaxLocationFan(risk);
  renderTaxLocationBridge(p);
  renderFederalTaxReadout(p);
  renderEstateCostReadout(p);
}

// Write the federal-tax numeric readout (federal-tax overlay design ft1, DECISION
// 8) joining the tb1 bridge readout above. Mode-aware, mirroring
// renderTaxLocationBridge: Deterministic reads the path scalars off
// payload.federal_tax; Historical/MC reads the median-of-paths aggregate off the
// SAME key (FederalTaxView picks the matching sentence). Data-driven — keys off
// payload.federal_tax's presence, not a re-derived client gate, the same
// discipline updateSpendingFan uses for payload.spending_bands. Called from
// updateTaxLocationView right alongside renderTaxLocationBridge, so it rides the
// IDENTICAL show/hide gate as the whole #tax-location-view block (off / gated /
// comparing -> hidden) — tb1's compare-mode treatment, deliberately not
// reinvented here.
function renderFederalTaxReadout(payload) {
  const el = document.getElementById("federal-tax-readout");
  if (!el) return;
  const ft = payload && !payload.error ? payload.federal_tax : null;
  const text = !ft
    ? ""
    : simMode === "deterministic"
    ? FederalTaxView.deterministicReadout(ft)
    : FederalTaxView.stochasticReadout(ft);
  el.textContent = text;
  el.hidden = !text;
}

// Write the "estate cost of taxes" readout (tax-drag design td1, DECISION 9)
// joining the readouts above. Mode-aware, mirroring renderFederalTaxReadout:
// Deterministic reads the single pair off payload.estate_cost_of_taxes;
// Historical/MC reads the median-of-per-path-differences off the SAME key
// (FederalTaxView picks the matching sentence). `endAge` comes off
// payload.scenario.end_age (echoed on every payload shape). Called from
// updateTaxLocationView right alongside renderFederalTaxReadout, so it rides
// the IDENTICAL show/hide gate as the whole #tax-location-view block.
function renderEstateCostReadout(payload) {
  const el = document.getElementById("estate-cost-readout");
  if (!el) return;
  const ec = payload && !payload.error ? payload.estate_cost_of_taxes : null;
  const endAge = payload && payload.scenario ? payload.scenario.end_age : null;
  const text = !ec
    ? ""
    : simMode === "deterministic"
    ? FederalTaxView.estateCostDeterministicReadout(ec, endAge)
    : FederalTaxView.estateCostStochasticReadout(ec, endAge);
  el.textContent = text;
  el.hidden = !text;
}

// ---------- preset picker (slice 4b, deliverable 5) ----------

// Populate the preset <select> from the loaded PRESETS (after Custom). Each
// option's value is the preset index; the label is the preset name (with an
// "(approx)" suffix when the preset isn't a faithful reconstruction, so the user
// knows it collapses a factor/EM split the menu doesn't carry).
function renderPresetOptions() {
  const sel = document.getElementById("preset-select");
  if (!sel) return;
  // Keep the leading "Custom" option; rebuild the rest.
  sel.innerHTML = '<option value="">Custom</option>';
  PRESETS.forEach((p, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = p.faithful ? p.name : `${p.name} (approx)`;
    sel.appendChild(opt);
  });
}

// Apply the chosen preset to the matrix: set every class's weight from the
// preset's {class_id: percent} (classes not in the preset go to 0%), into the
// focused column (A when linked or focused; the active column when comparing).
// Then refresh the live Total/blended readout and recompute. Returns are left
// at their per-class menu defaults (a preset is an allocation, not a CMA set).
function applyPreset(index) {
  const p = PRESETS[index];
  if (!p) return;
  // Which column the preset targets: A when linked; when unlinked, default to A
  // (the canonical/focused side) — the user can still edit B independently.
  const col = "a";
  ASSET_CLASSES.forEach((c, i) => {
    const pct = p.weights[c.id] || 0;
    const el = document.getElementById(weightId(col, i));
    if (el) el.value = String(pct);
    c.weight = pct; // keep the model in sync for any re-render
  });
  showPresetNote(p);
  refreshAllocation();
  // Recompute so the chart reflects the preset immediately (deliverable 5).
  rerender();
}

// Show the preset name + faithful/approx note beside the picker.
function showPresetNote(preset) {
  const note = document.getElementById("preset-note");
  if (!note) return;
  if (!preset) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.hidden = false;
  note.textContent = preset.faithful
    ? `${preset.name} applied`
    : `${preset.name} applied — approximate (collapses splits the 12-class menu doesn't carry)`;
  note.classList.toggle("is-approx", !preset.faithful);
}

// Reset the picker to "Custom" (no note) — called whenever the user edits a
// weight by hand, so the dropdown never claims a preset the matrix no longer
// matches.
function resetPresetToCustom() {
  const sel = document.getElementById("preset-select");
  if (sel && sel.value !== "") sel.value = "";
  showPresetNote(null);
}

// ---------- reading inputs into two scenarios ----------

// Read one field's numeric value from a given input element. Throws on empty /
// non-numeric. Converts percent fields back to fractions for the API.
function readFieldValue(f, input) {
  const raw = input.value.trim();
  if (raw === "") throw new Error(`${f.label} is required`);
  const num = f.type === "int" ? parseInt(raw, 10) : parseFloat(raw);
  if (Number.isNaN(num)) throw new Error(`${f.label} must be a number`);
  return f.pct ? num / 100 : num;
}

// ---------- tax-location split (tax-buckets design tb1) ----------

// Read one tax-location balance input by id → a finite number, or null when blank.
// A non-numeric entry also reads as null (the live sum-helper + the inline
// validation guard the user before any POST). Unlike the FIELDS inputs these are
// OPTIONAL, so blank is a valid "feature-off" state, not an error.
function readTaxBalance(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (raw === "") return null;
  const num = parseFloat(raw);
  return Number.isFinite(num) ? num : null;
}

// The current tax-location split as { taxable, taxDeferred, roth } (each a number
// or null). Pure DOM read — the gating + sum logic live in TaxLocationView.
function readTaxLocation() {
  return {
    taxable: readTaxBalance(TAX_TAXABLE_INPUT_ID),
    taxDeferred: readTaxBalance(TAX_DEFERRED_INPUT_ID),
    roth: readTaxBalance(TAX_ROTH_INPUT_ID),
  };
}

// True when a tax-location split is supplied AND it's allowed (single sourcing —
// tb1 is single-mode only; under buckets the split is no-op'd server-side, so we
// don't send it). The gate keeps the feature strictly additive.
function taxLocationActive() {
  return sourcingMode !== "buckets" && TaxLocationView.hasSplit(readTaxLocation());
}

// The thin DOM-READ half of the old readScenarios(): gather the raw field values
// + the conditional sub-bodies + the mode globals off the DOM into a plain JS
// `formState` object. NO assembly logic lives here — that moved VERBATIM to
// PayloadAssembler.buildScenario(formState). The only behavior to preserve from
// the old loop is the LAZY B-input read: readFieldValue throws on a blank input,
// and the old code only read B's input when the field was UNLINKED. So here, when
// a field is linked, we leave bVal undefined (B's input is NOT read); the
// assembler reuses aVal. When unlinked we read B's input eagerly (same throw
// timing as before). Same for the allocation body: B's column is only built when
// allocation is unlinked.
function readFormState() {
  const fields = {};
  const fieldNames = [];
  for (const f of FIELDS) {
    fieldNames.push(f.name);
    const aVal = readFieldValue(f, aInputEl(f.name));
    const linked = compareState.isLinked(f.name);
    // Read B's input ONLY when unlinked (preserves the old throw-on-blank timing).
    const bVal = linked ? undefined : readFieldValue(f, bInputEl(f.name));
    fields[f.name] = { aVal, bVal, linked };
  }
  // Allocation can diverge. A always builds its column; B's column is built ONLY
  // when allocation is unlinked (else the assembler reuses A's body). Returns are
  // shared (buildAllocationBody reads the same #returns-fields for both columns).
  const allocationLinked = compareState.allocationLinked();
  const allocationBodyA = buildAllocationBody("a");
  const allocationBodyB = allocationLinked ? undefined : buildAllocationBody("b");
  return {
    fieldNames,
    fields,
    allocationBodyA,
    allocationBodyB,
    allocationLinked,
    spendingMode,
    allocationMode,
    // Built only when glidepath (the assembler only reads it then).
    phaseAllocations:
      allocationMode === "glidepath" ? buildGlidepathPhaseAllocations() : undefined,
    sourcingMode,
    // Built only when buckets (the assembler only reads it then).
    bucketsBody: sourcingMode === "buckets" ? buildBucketsBody() : undefined,
    // null when not under guardrails — the assembler omits them then.
    guardrails: readGuardrails(),
    correlation: readCorrelation(),
    // The split + the active gate, pre-resolved off the DOM. attachTaxLocation in
    // the assembler reads .active to decide inclusion (byte-identical to today).
    taxLocation: (() => {
      const { taxable, taxDeferred, roth } = readTaxLocation();
      return { active: taxLocationActive(), taxable, taxDeferred, roth };
    })(),
    // Taxable-gains-fraction knob (federal-tax overlay design ft1, DECISION 4):
    // attached ONLY when the tax-location split is active (the SAME gate as
    // taxLocation.active above — DECISION 3, the overlay has no meaning without
    // the split). undefined -> the assembler omits it, so an inactive request
    // stays byte-identical to pre-ft1.
    taxableGainsFraction: taxLocationActive() ? readTaxableGainsFraction() : undefined,
    // Bracket-fill lever (tax-drag design td1, DECISION 5): attached ONLY when
    // the split is active (the SAME gate as taxableGainsFraction above — the
    // lever needs the accounts to move money between). readBracketFillTarget()
    // returns null when the field is blank ("empty = off"); the assembler's
    // attachBracketFillTarget omits the key for anything that isn't a real
    // number, so a blank lever on an active split still sends no
    // bracket_fill_target (byte-identical to pre-td1 for that field).
    bracketFillTarget: taxLocationActive() ? readBracketFillTarget() : undefined,
    modelAssumptions: readModelAssumptions(),
    // Household editor (M5): the people / streams / events / adjustments body,
    // or null while the editor is off (the legacy FIELDS then describe the
    // household and the request is unchanged). Throws on a bad row, like
    // readFieldValue, so the error shows before any POST.
    household: readHouseholdBody(),
    // M4 tax options: only non-default keys, only with an active split; null
    // otherwise.
    taxOptions: readTaxOptionsBody(),
    // Imported plan-file values the UI does not edit (null when none).
    planExtras: planExtras && Object.keys(planExtras).length ? planExtras : null,
  };
}

// Build the two API param objects from current input state. Thin orchestration:
// read the DOM into a plain formState, then hand it to the PURE assembler. The
// observable result is byte-identical to the old DOM-fused readScenarios().
function readScenarios() {
  return PayloadAssembler.buildScenario(readFormState());
}

// ---------- Model assumptions (review 2026-10-11) ----------
//
// One shared panel (#model-assumptions, static markup in index.html): investment
// cost, assumed inflation, pension treatment (nominal until start + COLA rule),
// Monte Carlo return shape, the survivor case, and the guardrail base clamp.
// readModelAssumptions() returns ONLY the keys that differ from the engine
// defaults, so an untouched panel leaves every request byte-identical.
const MODEL_ASSUMPTION_DEFAULTS = {
  annual_fee: 0,
  assumed_inflation: 0.025,
  pension_nominal_until_start: false,
  pension_cola: "full",
  mc_distribution: "normal",
  guard_clamp_base: false,
  survivor_age: null,
  survivor_decedent: "b",
  survivor_pension_fraction: 0.5,
  survivor_spending_fraction: 0.75,
};

function maEl(id) {
  return document.getElementById(id);
}

function maNumber(id, fallback, scale) {
  const el = maEl(id);
  if (!el) return fallback;
  const v = parseFloat(el.value);
  return Number.isFinite(v) ? v / (scale || 1) : fallback;
}

function readModelAssumptions() {
  if (!maEl("model-assumptions")) return null;
  const d = MODEL_ASSUMPTION_DEFAULTS;
  const out = {};
  const fee = maNumber("ma-annual-fee", d.annual_fee, 100);
  if (fee !== d.annual_fee) out.annual_fee = fee;
  const infl = maNumber("ma-assumed-inflation", d.assumed_inflation, 100);
  if (Math.abs(infl - d.assumed_inflation) > 1e-12) out.assumed_inflation = infl;
  if (maEl("ma-pension-nominal").checked) out.pension_nominal_until_start = true;
  const cola = maEl("ma-pension-cola").value;
  if (cola !== d.pension_cola) out.pension_cola = cola;
  const dist = maEl("ma-mc-distribution").value;
  if (dist !== d.mc_distribution) out.mc_distribution = dist;
  if (maEl("ma-guard-clamp").checked) out.guard_clamp_base = true;
  if (maEl("ma-survivor-on").checked) {
    out.survivor_age = Math.round(maNumber("ma-survivor-age", 80, 1));
    out.survivor_decedent = maEl("ma-survivor-decedent").value;
    out.survivor_pension_fraction = maNumber("ma-survivor-pension", 50, 1) / 100;
    out.survivor_spending_fraction = maNumber("ma-survivor-spending", 75, 1) / 100;
  }
  return out;
}

function syncSurvivorRows() {
  const on = maEl("ma-survivor-on") && maEl("ma-survivor-on").checked;
  document.querySelectorAll("#model-assumptions .ma-survivor").forEach((row) => {
    row.hidden = !on;
  });
}

// Inverse of readModelAssumptions: set the panel from a saved block (missing
// keys -> defaults). null/undefined resets the whole panel.
function applyModelAssumptions(saved) {
  if (!maEl("model-assumptions")) return;
  const m = { ...MODEL_ASSUMPTION_DEFAULTS, ...(saved || {}) };
  maEl("ma-annual-fee").value = String(+(m.annual_fee * 100).toFixed(4));
  maEl("ma-assumed-inflation").value = String(+(m.assumed_inflation * 100).toFixed(4));
  maEl("ma-pension-nominal").checked = !!m.pension_nominal_until_start;
  maEl("ma-pension-cola").value = m.pension_cola;
  maEl("ma-mc-distribution").value = m.mc_distribution;
  maEl("ma-guard-clamp").checked = !!m.guard_clamp_base;
  const survivorOn = typeof m.survivor_age === "number";
  maEl("ma-survivor-on").checked = survivorOn;
  if (survivorOn) maEl("ma-survivor-age").value = String(m.survivor_age);
  maEl("ma-survivor-decedent").value = m.survivor_decedent;
  maEl("ma-survivor-pension").value = String(Math.round(m.survivor_pension_fraction * 100));
  maEl("ma-survivor-spending").value = String(Math.round(m.survivor_spending_fraction * 100));
  syncSurvivorRows();
}

function initModelAssumptions() {
  const panel = maEl("model-assumptions");
  if (!panel) return;
  applyModelAssumptions(null);
  panel.querySelectorAll("input, select").forEach((el) => {
    el.addEventListener("change", () => {
      syncSurvivorRows();
      rerender();
    });
  });
}

// ---------- Household editor (M5) ----------
//
// One or two people with their own ages and planning horizons, a list of income
// streams, cash events and required-spending adjustments (the API's `people`,
// `income_streams`, `cash_events`, `spending_adjustments`; docs/plan-files.md
// documents every field). OFF by default: the legacy two-pension / two-Social-
// Security FIELDS describe the plan, exactly as before. Turning it on converts
// those four into income streams (HouseholdEditor.fromLegacy) unless the editor
// already holds rows. The household is SHARED by scenarios A and B, like the
// model assumptions.
//
// The rows live in `householdState` (plain data); renderHouseholdEditor() draws
// them and every input writes straight back into the state. The pure shaping —
// state -> API body, validation, legacy conversion — is web/household_editor.js.
// Everything here no-ops when that module is not loaded (the node:vm tests load
// app.js without it).

function householdApi() {
  return typeof window !== "undefined" && window.HouseholdEditor ? window.HouseholdEditor : null;
}

let householdState = null; // HouseholdEditor state, or null before init
// Plan-file values the UI has no control for (lifecycle phase ages and
// multipliers, spending and pension drift, Student-t degrees of freedom). Set
// from an imported plan, sent with every request and written back on export.
let planExtras = null;
// The imported plan's name, description and run settings (trials, seed,
// stress), so a downloaded plan keeps them.
let planMeta = null;

function householdEnabled() {
  return !!(householdState && householdState.enabled);
}

function hhEl(id) {
  return document.getElementById(id);
}

// The legacy fields the conversion reads, off the live form (A side).
function readLegacyHouseholdFields() {
  const out = {};
  for (const name of [
    "start_age", "end_age",
    "person_a_pension_amount", "person_a_pension_start_age",
    "person_b_pension_amount", "person_b_pension_start_age",
    "person_a_ss_fra_amount", "person_a_ss_claim_age",
    "person_b_ss_fra_amount", "person_b_ss_claim_age",
  ]) {
    const el = aInputEl(name);
    out[name] = el ? Number(el.value) : NaN;
  }
  return out;
}

// The household body for the request, or null when the editor is off.
function readHouseholdBody() {
  const HE = householdApi();
  if (!HE || !householdEnabled()) return null;
  return HE.buildHousehold(householdState);
}

// ---- tax options (M4 fields) ----

const TAX_OPTION_IDS = {
  ss_taxation: "tx-ss-taxation",
  payroll_tax: "tx-payroll",
  irmaa: "tx-irmaa",
  irmaa_prior_magi: "tx-irmaa-prior",
  roth_target_choice: "tx-roth-target",
  roth_target_dollars: "tx-roth-dollars",
  roth_conversion_start_age: "tx-roth-start",
  roth_conversion_end_age: "tx-roth-end",
  roth_heir_tax_rate: "tx-roth-heir",
  state: "tx-state",
  state_move_age: "tx-state-move-age",
  state_after_move: "tx-state-move-to",
  state_custom_rate: "tx-state-rate",
  state_custom_exempt_ss: "tx-state-exempt-ss",
  state_custom_exempt_pensions: "tx-state-exempt-pensions",
  state_custom_exempt_ira: "tx-state-exempt-ira",
};

// Read the tax-options panel into a HouseholdEditor tax state. Blank selects
// read as the default (so a fake or missing panel reads as "all defaults").
// Percent inputs (heir rate, custom state rate) convert to fractions.
function readTaxOptionsDom() {
  const v = (key) => {
    const el = hhEl(TAX_OPTION_IDS[key]);
    return el && typeof el.value === "string" ? el.value.trim() : "";
  };
  const c = (key) => {
    const el = hhEl(TAX_OPTION_IDS[key]);
    return !!(el && el.checked === true);
  };
  const pct = (key) => {
    const raw = v(key);
    if (raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n / 100 : raw;
  };
  const choice = v("roth_target_choice");
  let rothTarget = null;
  if (choice === "dollars") rothTarget = v("roth_target_dollars");
  else if (choice) rothTarget = choice;
  return {
    ss_taxation: v("ss_taxation") || "provisional",
    payroll_tax: c("payroll_tax"),
    irmaa: c("irmaa"),
    irmaa_prior_magi: v("irmaa_prior_magi"),
    roth_conversion_target: rothTarget,
    roth_conversion_start_age: v("roth_conversion_start_age"),
    roth_conversion_end_age: v("roth_conversion_end_age"),
    roth_heir_tax_rate: pct("roth_heir_tax_rate"),
    state: v("state") || null,
    state_move_age: v("state_move_age"),
    state_after_move: v("state_after_move") || null,
    state_custom_rate: pct("state_custom_rate"),
    state_custom_exempt_ss: c("state_custom_exempt_ss"),
    state_custom_exempt_pensions: c("state_custom_exempt_pensions"),
    state_custom_exempt_ira: c("state_custom_exempt_ira"),
  };
}

// Inverse of readTaxOptionsDom: write a tax state (null -> defaults).
function applyTaxOptionsDom(taxState) {
  const HE = householdApi();
  if (!HE || !hhEl("tax-options")) return;
  const t = { ...HE.TAX_DEFAULTS, ...(taxState || {}) };
  const set = (key, value) => {
    const el = hhEl(TAX_OPTION_IDS[key]);
    if (el) el.value = value === null || value === undefined ? "" : String(value);
  };
  const check = (key, on) => {
    const el = hhEl(TAX_OPTION_IDS[key]);
    if (el) el.checked = !!on;
  };
  set("ss_taxation", t.ss_taxation);
  check("payroll_tax", t.payroll_tax);
  check("irmaa", t.irmaa);
  set("irmaa_prior_magi", t.irmaa_prior_magi);
  const target = t.roth_conversion_target;
  if (target === null || target === undefined || target === "") {
    set("roth_target_choice", "");
    set("roth_target_dollars", "");
  } else if (HE.ROTH_BRACKETS.includes(target)) {
    set("roth_target_choice", target);
    set("roth_target_dollars", "");
  } else {
    set("roth_target_choice", "dollars");
    set("roth_target_dollars", target);
  }
  set("roth_conversion_start_age", t.roth_conversion_start_age);
  set("roth_conversion_end_age", t.roth_conversion_end_age);
  set("roth_heir_tax_rate", +(Number(t.roth_heir_tax_rate) * 100).toFixed(4));
  set("state", t.state);
  set("state_move_age", t.state_move_age);
  set("state_after_move", t.state_after_move);
  set("state_custom_rate", +(Number(t.state_custom_rate) * 100).toFixed(4));
  check("state_custom_exempt_ss", t.state_custom_exempt_ss);
  check("state_custom_exempt_pensions", t.state_custom_exempt_pensions);
  check("state_custom_exempt_ira", t.state_custom_exempt_ira);
  syncTaxOptionRows();
}

// Show only the rows that apply: the IRMAA prior-income box under IRMAA, the
// Roth window under a Roth target, the move and custom-rate rows under a state.
function syncTaxOptionRows() {
  const panel = hhEl("tax-options");
  if (!panel || typeof panel.querySelectorAll !== "function") return;
  const t = readTaxOptionsDom();
  const show = (cls, on) => {
    panel.querySelectorAll(cls).forEach((row) => {
      row.hidden = !on;
    });
  };
  show(".tx-irmaa-row", t.irmaa);
  show(".tx-roth-row", t.roth_conversion_target !== null);
  const dollarsEl = hhEl(TAX_OPTION_IDS.roth_target_dollars);
  if (dollarsEl) dollarsEl.hidden = hhEl(TAX_OPTION_IDS.roth_target_choice).value !== "dollars";
  show(".tx-state-row", !!t.state);
  show(".tx-custom-row", t.state === "custom" || t.state_after_move === "custom");
}

// The M4 tax keys for the request: only non-default values, and only while a
// tax-location split is active (the API rejects the opt-ins without one).
function readTaxOptionsBody() {
  const HE = householdApi();
  if (!HE || !hhEl("tax-options")) return null;
  if (!taxLocationActive()) return null;
  const out = HE.buildTaxOptions(readTaxOptionsDom());
  return Object.keys(out).length ? out : null;
}

// ---- rendering ----

function hhOption(sel, value, label, selected) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label;
  if (selected) o.selected = true;
  sel.appendChild(o);
}

function hhOwnerLabel(id) {
  const people = householdState.people;
  if (id === "household") return "Household";
  const idx = id === "b" ? 1 : 0;
  const name = people[idx] && people[idx].name ? ` (${people[idx].name})` : "";
  return `Person ${id.toUpperCase()}${name}`;
}

function hhOwnerOptions() {
  return householdState.people.length === 2 ? ["a", "b", "household"] : ["a", "household"];
}

// One labelled control bound to `obj[field]`. kind: "number", "text",
// "checkbox" or "select" (with `options` as [value, label] pairs). Edits write
// straight into the state; `onChange` (optional) runs on change for fields
// that alter which other controls show.
function hhControl(obj, field, label, kind, opts = {}) {
  const wrap = document.createElement("label");
  wrap.className = kind === "checkbox" ? "hh-field hh-check" : "hh-field";
  let input;
  if (kind === "select") {
    input = document.createElement("select");
    for (const [value, text] of opts.options) {
      hhOption(input, value, text, String(obj[field] ?? "") === String(value));
    }
  } else {
    input = document.createElement("input");
    input.type = kind;
    if (kind === "checkbox") {
      input.checked = !!obj[field];
    } else {
      input.value = obj[field] === null || obj[field] === undefined ? "" : String(obj[field]);
      if (kind === "number") {
        if (opts.step !== undefined) input.step = String(opts.step);
        if (opts.min !== undefined) input.min = String(opts.min);
        if (opts.max !== undefined) input.max = String(opts.max);
        input.inputMode = "decimal";
      }
      if (opts.placeholder) input.placeholder = opts.placeholder;
    }
  }
  if (opts.dataField) input.dataset.field = opts.dataField;
  const read = () => {
    if (kind === "checkbox") return input.checked;
    if (opts.scale) {
      const raw = input.value.trim();
      if (raw === "") return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n / opts.scale : raw;
    }
    return input.value;
  };
  if (opts.scale && obj[field] !== null && obj[field] !== undefined && kind === "number") {
    input.value = String(+(Number(obj[field]) * opts.scale).toFixed(4));
  }
  input.addEventListener(kind === "select" || kind === "checkbox" ? "change" : "input", () => {
    obj[field] = read();
    if (opts.onChange) opts.onChange();
  });
  const span = document.createElement("span");
  span.className = "hh-field-label";
  span.textContent = label;
  if (kind === "checkbox") {
    wrap.appendChild(input);
    wrap.appendChild(span);
  } else {
    wrap.appendChild(span);
    wrap.appendChild(input);
  }
  if (opts.hidden) wrap.hidden = true;
  return wrap;
}

function hhRemoveButton(label, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-secondary hh-remove";
  btn.textContent = "Remove";
  btn.setAttribute("aria-label", label);
  btn.addEventListener("click", onClick);
  return btn;
}

function renderHouseholdPeople() {
  const box = hhEl("hh-people");
  if (!box) return;
  box.innerHTML = "";
  const count = document.createElement("label");
  count.className = "hh-field";
  const countLabel = document.createElement("span");
  countLabel.className = "hh-field-label";
  countLabel.textContent = "People in the plan";
  const sel = document.createElement("select");
  sel.id = "hh-people-count";
  hhOption(sel, "1", "One person", householdState.people.length === 1);
  hhOption(sel, "2", "Two people", householdState.people.length === 2);
  sel.addEventListener("change", () => setHouseholdPeopleCount(Number(sel.value)));
  count.appendChild(countLabel);
  count.appendChild(sel);
  box.appendChild(count);

  householdState.people.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "hh-row hh-person";
    const head = document.createElement("div");
    head.className = "hh-row-head";
    head.textContent = `Person ${i === 0 ? "A" : "B"}`;
    row.appendChild(head);
    const grid = document.createElement("div");
    grid.className = "hh-grid";
    grid.appendChild(hhControl(p, "name", "Name (optional)", "text", { dataField: "name" }));
    grid.appendChild(hhControl(p, "start_age", "Age now", "number",
      { step: 1, min: 18, max: 110, dataField: "start_age", onChange: syncHouseholdDerived }));
    grid.appendChild(hhControl(p, "end_age", "Plan to age", "number",
      { step: 1, min: 19, max: 120, dataField: "end_age", onChange: syncHouseholdDerived }));
    row.appendChild(grid);
    box.appendChild(row);
  });
}

function setHouseholdPeopleCount(n) {
  const people = householdState.people;
  if (n === 1 && people.length === 2) {
    people.splice(1);
    // Person B's rows have no owner left: move them to the household.
    for (const list of [householdState.streams, householdState.cashEvents, householdState.adjustments]) {
      for (const item of list) if (item.owner === "b") item.owner = "household";
    }
  } else if (n === 2 && people.length === 1) {
    people.push({ id: "b", name: "", start_age: people[0].start_age, end_age: people[0].end_age });
  }
  renderHouseholdEditor();
  syncHouseholdDerived();
}

function streamRowFields(st) {
  const isSS = st.kind === "social_security";
  const two = householdState.people.length === 2;
  const refresh = () => renderHouseholdEditor();
  const fields = [
    hhControl(st, "id", "Id", "text", { dataField: "id" }),
    hhControl(st, "kind", "Kind", "select", {
      dataField: "kind",
      options: householdApi().STREAM_KINDS.map((k) => [k, householdApi().KIND_LABELS[k]]),
      onChange: () => {
        if (st.kind === "social_security") {
          st.end_age = null;
          st.cola = "full";
          if (st.start_age < 62 || st.start_age > 70) st.start_age = 67;
        }
        refresh();
        syncHouseholdDerived();
      },
    }),
    hhControl(st, "owner", "Owner", "select", {
      dataField: "owner",
      options: hhOwnerOptions().map((o) => [o, hhOwnerLabel(o)]),
      onChange: syncHouseholdDerived,
    }),
    hhControl(st, "amount", isSS ? "Benefit at full retirement age ($/yr)"
      : st.nominal_until_start ? "Check at start ($/yr, nominal)" : "Amount ($/yr, today's dollars)",
      "number", { step: 1000, min: 0, dataField: "amount" }),
    hhControl(st, "start_age", isSS ? "Claim age (62-70)" : "Starts at owner's age", "number",
      { step: 1, min: isSS ? 62 : 18, max: isSS ? 70 : 120, dataField: "start_age",
        onChange: syncHouseholdDerived }),
  ];
  if (!isSS) {
    fields.push(hhControl(st, "end_age", "Stops at age (blank = for life)", "number",
      { step: 1, min: 18, max: 120, dataField: "end_age", placeholder: "for life",
        onChange: syncHouseholdDerived }));
    fields.push(hhControl(st, "cola", "Cost-of-living rule", "select", {
      dataField: "cola",
      options: [
        ["full", "Full CPI (keeps its value)"],
        ["none", "None (fixed in dollars)"],
        ["fixed", "Fixed rate a year"],
        ["fers_diet", "FERS-style (diet COLA)"],
      ],
      onChange: refresh,
    }));
    fields.push(hhControl(st, "cola_rate", "COLA rate (% a year, nominal)", "number",
      { step: 0.1, dataField: "cola_rate", scale: 100, hidden: st.cola !== "fixed" }));
    fields.push(hhControl(st, "nominal_until_start", "Fixed in dollars until it starts", "checkbox",
      { dataField: "nominal_until_start", onChange: refresh }));
    fields.push(hhControl(st, "survivor_fraction", "Continues after owner's death (%)", "number",
      { step: 5, min: 0, max: 100, dataField: "survivor_fraction", scale: 100,
        placeholder: "default", hidden: !two || st.owner === "household" }));
  }
  const taxOptions = [["", "Default"], ["ordinary", "Ordinary income"], ["tax_free", "Tax-free"]];
  if (isSS) taxOptions.push(["social_security", "Social Security rules"]);
  fields.push(hhControl(st, "tax_treatment", "Tax treatment", "select",
    { dataField: "tax_treatment", options: taxOptions }));
  if (st.kind === "work") {
    fields.push(hhControl(st, "self_employed", "Self-employed (payroll option)", "checkbox",
      { dataField: "self_employed" }));
  }
  fields.push(hhControl(st, "label", "Label (optional)", "text", { dataField: "label" }));
  return fields;
}

function renderHouseholdList(containerId, list, title, fieldsFor, noun) {
  const box = hhEl(containerId);
  if (!box) return;
  box.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("p");
    empty.className = "hh-empty";
    empty.textContent = `No ${noun}.`;
    box.appendChild(empty);
    return;
  }
  list.forEach((item, i) => {
    const row = document.createElement("div");
    row.className = "hh-row";
    row.dataset.index = String(i);
    const head = document.createElement("div");
    head.className = "hh-row-head";
    const t = document.createElement("span");
    t.textContent = title(item, i);
    head.appendChild(t);
    head.appendChild(hhRemoveButton(`Remove ${t.textContent}`, () => {
      list.splice(i, 1);
      renderHouseholdEditor();
      syncHouseholdDerived();
    }));
    row.appendChild(head);
    const grid = document.createElement("div");
    grid.className = "hh-grid";
    for (const f of fieldsFor(item)) grid.appendChild(f);
    row.appendChild(grid);
    box.appendChild(row);
  });
}

function cashEventFields(ev) {
  const refresh = () => renderHouseholdEditor();
  return [
    hhControl(ev, "id", "Id (optional)", "text", { dataField: "id" }),
    hhControl(ev, "owner", "Owner", "select",
      { dataField: "owner", options: hhOwnerOptions().map((o) => [o, hhOwnerLabel(o)]) }),
    hhControl(ev, "age", "At owner's age", "number", { step: 1, min: 18, max: 120, dataField: "age" }),
    hhControl(ev, "amount", "Amount ($, + in / - out)", "number", { step: 1000, dataField: "amount" }),
    hhControl(ev, "tax_treatment", "Tax treatment", "select", {
      dataField: "tax_treatment",
      options: [["tax_free", "Tax-free"], ["ordinary", "Ordinary income"], ["capital_gain", "Capital gain"]],
      onChange: refresh,
    }),
    hhControl(ev, "basis_fraction", "Basis (% not taxed)", "number",
      { step: 5, min: 0, max: 100, scale: 100, dataField: "basis_fraction",
        hidden: ev.tax_treatment !== "capital_gain" }),
    hhControl(ev, "label", "Label (optional)", "text", { dataField: "label" }),
  ];
}

function adjustmentFields(adj) {
  const two = householdState.people.length === 2;
  return [
    hhControl(adj, "id", "Id (optional)", "text", { dataField: "id" }),
    hhControl(adj, "owner", "Owner", "select",
      { dataField: "owner", options: hhOwnerOptions().map((o) => [o, hhOwnerLabel(o)]) }),
    hhControl(adj, "amount", "Extra spending ($/yr)", "number", { step: 500, min: 0, dataField: "amount" }),
    hhControl(adj, "start_age", "From owner's age", "number", { step: 1, min: 18, max: 120, dataField: "start_age" }),
    hhControl(adj, "end_age", "Until age (not included)", "number", { step: 1, min: 18, max: 120, dataField: "end_age" }),
    hhControl(adj, "survivor_fraction", "Continues after owner's death (%)", "number",
      { step: 5, min: 0, max: 100, scale: 100, dataField: "survivor_fraction", hidden: !two }),
    hhControl(adj, "label", "Label (optional)", "text", { dataField: "label" }),
  ];
}

function renderHouseholdEditor() {
  const HE = householdApi();
  if (!HE || !householdState || !hhEl("household-group")) return;
  const enabled = householdEnabled();
  const box = hhEl("hh-enabled");
  if (box) box.checked = enabled;
  const body = hhEl("hh-body");
  if (body) body.hidden = !enabled;
  const offNote = hhEl("hh-off-note");
  if (offNote) offNote.hidden = enabled;
  if (!enabled) return;
  renderHouseholdPeople();
  renderHouseholdList("hh-streams", householdState.streams,
    (st, i) => `${i + 1}. ${HE.KIND_LABELS[st.kind] || st.kind} · ${hhOwnerLabel(st.owner)}`,
    streamRowFields, "income streams");
  renderHouseholdList("hh-events", householdState.cashEvents,
    (ev, i) => `Cash event ${i + 1}`, cashEventFields, "cash events");
  renderHouseholdList("hh-adjustments", householdState.adjustments,
    (adj, i) => `Required spending ${i + 1}`, adjustmentFields, "required spending");
}

// Keep the rest of the form consistent with the household: the horizon fields
// show the plan's start and end on person A's axis (other views read them), the
// legacy guaranteed-income fieldsets and legacy-only pension assumptions hide,
// and the glidepath phases re-derive from the streams.
function syncHouseholdDerived() {
  const HE = householdApi();
  if (!HE || !householdState) return;
  const enabled = householdEnabled();
  if (enabled) {
    const { startAge, endAge } = HE.planAges(householdState);
    if (Number.isFinite(startAge)) setFieldInputValue("start_age", startAge);
    if (Number.isFinite(endAge)) setFieldInputValue("end_age", endAge);
  }
  for (const name of ["start_age", "end_age"]) {
    const el = aInputEl(name);
    if (el) el.disabled = enabled;
  }
  if (typeof document.querySelectorAll === "function") {
    document.querySelectorAll(".legacy-gi").forEach((el) => {
      el.hidden = enabled;
    });
  }
  const note = hhEl("hh-horizon-note");
  if (note) note.hidden = !enabled;
  if (allocationMode === "glidepath") renderGlidepathPhases();
}

// The enable checkbox. Turning it on with an empty editor converts the legacy
// fields; turning it off keeps the rows (so turning it back on restores them).
function setHouseholdEnabled(on) {
  const HE = householdApi();
  if (!HE) return;
  if (on && !householdState.streams.length && !householdState.cashEvents.length
      && !householdState.adjustments.length) {
    const ma = { ...MODEL_ASSUMPTION_DEFAULTS, ...(readModelAssumptions() || {}) };
    householdState = HE.fromLegacy(readLegacyHouseholdFields(), ma);
  }
  householdState.enabled = !!on;
  renderHouseholdEditor();
  syncHouseholdDerived();
}

// Replace the editor from a saved block / import (null -> off and empty).
function applyHouseholdState(saved) {
  const HE = householdApi();
  if (!HE) return;
  const start = readAgeInput("start_age", 50);
  const end = readAgeInput("end_age", 95);
  if (saved && typeof saved === "object" && Array.isArray(saved.people)) {
    householdState = {
      ...HE.defaultState(start, end),
      ...JSON.parse(JSON.stringify(saved)),
    };
  } else {
    householdState = HE.defaultState(start, end);
  }
  renderHouseholdEditor();
  syncHouseholdDerived();
}

function initHouseholdEditor() {
  const HE = householdApi();
  if (!HE) return;
  householdState = HE.defaultState(readAgeInput("start_age", 50), readAgeInput("end_age", 95));
  const box = hhEl("hh-enabled");
  if (box && typeof box.addEventListener === "function") {
    box.addEventListener("change", () => setHouseholdEnabled(box.checked));
  }
  const addKind = hhEl("hh-add-kind");
  if (addKind && addKind.options && addKind.options.length === 0) {
    for (const k of HE.STREAM_KINDS) hhOption(addKind, k, HE.KIND_LABELS[k], k === "pension");
  }
  const bind = (id, fn) => {
    const el = hhEl(id);
    if (el && typeof el.addEventListener === "function") el.addEventListener("click", fn);
  };
  bind("hh-add-stream", () => {
    const kind = addKind ? addKind.value : "pension";
    householdState.streams.push(HE.newStream(householdState, kind, "a"));
    renderHouseholdEditor();
    syncHouseholdDerived();
  });
  bind("hh-add-event", () => {
    householdState.cashEvents.push(HE.newCashEvent(householdState, "a"));
    renderHouseholdEditor();
  });
  bind("hh-add-adjustment", () => {
    householdState.adjustments.push(HE.newAdjustment(householdState, "a"));
    renderHouseholdEditor();
  });
  const tax = hhEl("tax-options");
  if (tax && typeof tax.querySelectorAll === "function") {
    tax.querySelectorAll("input, select").forEach((el) => {
      el.addEventListener("change", syncTaxOptionRows);
    });
  }
  applyTaxOptionsDom(null);
  renderHouseholdEditor();
  syncHouseholdDerived();
}

// ---------- Plan files and examples (M5) ----------
//
// The UI reads and writes the same TOML plan files as the command line
// (docs/plan-files.md). Parsing and writing stay in Python: the UI posts the
// file's text to /api/plan/import and gets back the API body, then turns it into
// a saved-scenario blob (PlanIO.blobFromImport) and loads it with the same
// applyScenarioToForm a saved scenario uses. Download posts the current scenario
// A body to /api/plan/export and saves the TOML it returns. The Examples picker
// loads /api/examples/<name> through the same path.

const PLAN_IMPORT_URL = "api/plan/import";
const PLAN_EXPORT_URL = "api/plan/export";
const EXAMPLES_URL = "api/examples";

function setPlanStatus(msg, isError) {
  const el = hhEl("plan-file-status");
  if (!el) return;
  el.textContent = msg || "";
  el.hidden = !msg;
  if (el.classList) el.classList.toggle("is-error", !!isError);
}

async function planApiError(resp) {
  let detail = `HTTP ${resp.status}`;
  try {
    const j = await resp.json();
    if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
  } catch (_e) { /* keep the status */ }
  return new Error(detail);
}

// Load an import payload ({name, description, trials, seed, stress, body}) into
// the form: one recompute, like loading a saved scenario.
async function loadPlanPayload(payload, sourceLabel) {
  const blob = PlanIO.blobFromImport(payload, {
    classIds: ASSET_CLASSES.map((c) => c.id),
    simMode,
  });
  await applyScenarioToForm(blob);
  const warn = blob.warnings && blob.warnings.length ? ` ${blob.warnings.join(" ")}` : "";
  setPlanStatus(`Loaded ${sourceLabel}.${warn}`, false);
  return blob;
}

async function importPlanText(text, filename) {
  const resp = await fetch(PLAN_IMPORT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ toml: text, filename: filename || "plan.toml" }),
  });
  if (!resp.ok) throw await planApiError(resp);
  return loadPlanPayload(await resp.json(), filename || "the plan file");
}

async function onPlanFileChosen(e) {
  const input = e.target;
  const file = input && input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    await importPlanText(text, file.name);
  } catch (err) {
    setPlanStatus(`Could not load ${file.name}: ${err.message}`, true);
  } finally {
    input.value = "";
  }
}

// The request body for scenario A (the focused side while comparing), exactly
// what Recompute posts, wrapped for /api/plan/export.
function buildExportRequest() {
  const { a, b } = readScenarios();
  const side = compareState.focusedScenario() === "B" ? b : a;
  const body = buildPostBody(side);
  // Outside the dynamic rules the request omits the guardrail knobs; the plan
  // file still records them, so take them from their inputs.
  if (!isDynamicSpending()) {
    body.guard_band = readGuardrailNumber(GUARD_BAND_INPUT_ID, GUARDRAIL_DEFAULTS.guardBand, 100);
    body.guard_adjust = readGuardrailNumber(GUARD_ADJUST_INPUT_ID, GUARDRAIL_DEFAULTS.guardAdjust, 100);
    body.spending_floor = readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor);
    body.spending_ceiling = readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling);
  }
  return PlanIO.exportRequest(body, planMeta);
}

async function downloadPlanFile() {
  let req;
  try {
    req = buildExportRequest();
  } catch (err) {
    setPlanStatus(`Cannot write a plan file: ${err.message}`, true);
    return null;
  }
  try {
    const resp = await fetch(PLAN_EXPORT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    if (!resp.ok) throw await planApiError(resp);
    const { toml } = await resp.json();
    const name = PlanIO.fileName(planMeta && planMeta.name);
    const blob = new Blob([toml], { type: "application/toml" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setPlanStatus(`Downloaded ${name}. Run it with: firebridgelab run ${name}`, false);
    return toml;
  } catch (err) {
    setPlanStatus(`Cannot write a plan file: ${err.message}`, true);
    return null;
  }
}

async function loadExamplesMenu() {
  const sel = hhEl("examples-select");
  if (!sel || typeof sel.addEventListener !== "function") return;
  try {
    const resp = await fetch(EXAMPLES_URL);
    if (!resp.ok) return;
    const rows = await resp.json();
    for (const row of rows) {
      const o = document.createElement("option");
      o.value = row.id;
      o.textContent = row.name;
      o.title = row.description || "";
      sel.appendChild(o);
    }
  } catch (_e) {
    // Examples are optional; the picker keeps its placeholder.
  }
  sel.addEventListener("change", async () => {
    const id = sel.value;
    if (!id) return;
    try {
      const resp = await fetch(`${EXAMPLES_URL}/${encodeURIComponent(id)}`);
      if (!resp.ok) throw await planApiError(resp);
      const payload = await resp.json();
      await loadPlanPayload(payload, `the example "${payload.name || id}"`);
      if (payload.description) {
        setPlanStatus(`Loaded the example "${payload.name || id}": ${payload.description}`, false);
      }
    } catch (err) {
      setPlanStatus(`Could not load the example: ${err.message}`, true);
    } finally {
      sel.value = "";
    }
  });
}

function initPlanFiles() {
  const file = hhEl("plan-file-input");
  if (file && typeof file.addEventListener === "function") {
    file.addEventListener("change", onPlanFileChosen);
  }
  const load = hhEl("plan-load-btn");
  if (load && file && typeof load.addEventListener === "function") {
    load.addEventListener("click", () => file.click());
  }
  const dl = hhEl("plan-download-btn");
  if (dl && typeof dl.addEventListener === "function") dl.addEventListener("click", downloadPlanFile);
  loadExamplesMenu();
}

// Exposed for tests; not a stable API.
window.HouseholdUI = {
  state: () => householdState,
  setEnabled: setHouseholdEnabled,
  applyState: applyHouseholdState,
  readTaxOptionsDom,
  buildExportRequest,
  loadPlanPayload,
};

// ---------- Saved Scenarios (sv1) ----------
//
// Persist the current scenario as a named, reloadable blob (one JSON per save,
// server-backed in the synced vault). captureScenario() is the FORWARD map (the
// live form -> the blob); applyScenarioToForm() is its INVERSE (the blob -> the
// live form), the missing counterpart of readScenarios(). The acceptance
// invariant: captureScenario() after applyScenarioToForm(blob) reproduces the
// same readScenarios() payload (round-trip equivalence). The four global modes
// (sim/spending/allocation/sourcing) ride in `view`; the single focused
// input-set + its conditional glidepath/buckets/guardrails/MC blocks ride in
// `inputs`. The server stamps id/created_at/updated_at/app_revision.

const SCENARIO_SCHEMA_VERSION = 1;

// Capture the current scenario into a save blob. A save is ONE input-set: the
// focused side while comparing (compareState.focusedScenario(), default "A"),
// else the single A scenario. The conditional blocks (glidepath phase weights,
// buckets, guardrails, MC correlation/volatilities) are captured only when the
// corresponding mode is active, mirroring what readScenarios() serializes.
function captureScenario(name) {
  const { a, b } = readScenarios();
  const side = compareState.focusedScenario() === "B" ? b : a;

  // Glidepath: build the EXPLICIT per-phase weight map (the integer-% rows the
  // user tuned), keyed by phase index as strings — NOT just the derived bodies,
  // so a reload can re-pin weights by index after the phase count re-derives.
  let glidepathPhaseWeights = null;
  if (allocationMode === "glidepath") {
    const map = {};
    const n = glidepathBoundaries().length;
    for (let k = 0; k < n; k++) {
      map[String(k)] = ASSET_CLASSES.map((_, i) => readGlidepathWeight(k, i));
    }
    glidepathPhaseWeights = map;
  }
  // Buckets: the full bucket config (n1/n2 + the three per-bucket allocations).
  const bucketsBody = sourcingMode === "buckets" ? buildBucketsBody() : null;
  // Guardrails: the four editable knobs (already fractions for band/adjust).
  let guardrails = null;
  if (isDynamicSpending()) {
    const g = readGuardrails();
    if (g) guardrails = g;
  }
  // Monte Carlo: the cross-class correlation + the per-class vol map (keyed by
  // class id) so a reload reseeds Advanced exactly.
  let monteCarlo = null;
  if (simMode === "monte_carlo") {
    const vols = {};
    ASSET_CLASSES.forEach((c, i) => {
      vols[c.id] = readVolFraction(i);
    });
    monteCarlo = { correlation: readCorrelation(), volatilities: vols };
  }
  // Tax-location split (tax-buckets design tb1): captured only when ACTIVE (single
  // sourcing + a split supplied), as the three balances (missing → 0, mirroring the
  // API + readScenarios). Omitted when off, so an off save carries NO tax_location
  // block. The values ride from the chosen side's params (attachTaxLocation already
  // put them there when active).
  let taxLocation = null;
  if (taxLocationActive()) {
    taxLocation = {
      taxable: Number(side.taxable_balance) || 0,
      tax_deferred: Number(side.tax_deferred_balance) || 0,
      roth: Number(side.roth_balance) || 0,
    };
  }
  // Taxable-gains-fraction knob (federal-tax overlay design ft1): captured ONLY
  // when ACTIVE (the SAME gate as taxLocation above), so an off save carries no
  // taxable_gains_fraction — an old save (or one made with the split off) then
  // loads at the feature default (50%), per DECISION 4.
  let taxableGainsFraction = null;
  if (taxLocationActive()) {
    taxableGainsFraction = readTaxableGainsFraction();
  }
  // Bracket-fill lever (tax-drag design td1, DECISION 5): captured ONLY when the
  // split is ACTIVE (the SAME gate as taxableGainsFraction above). Unlike the
  // gains knob, the lever itself can be off (blank) even while the split is
  // active — readBracketFillTarget() returns null then, and ScenarioStore.capture
  // omits the key for anything that isn't a number, so a save with the split on
  // but the lever off round-trips as "lever off" (same as the split-inactive case).
  let bracketFillTarget = null;
  if (taxLocationActive()) {
    bracketFillTarget = readBracketFillTarget();
  }

  // The DOM/global reads are resolved above; ScenarioStore.capture does the PURE,
  // byte-identical blob shaping (same keys/nesting/values as the old DOM-fused
  // captureScenario). See web/scenario_store.js for the seam.
  const blob = ScenarioStore.capture({
    schemaVersion: SCENARIO_SCHEMA_VERSION,
    name,
    fieldNames: FIELDS.map((f) => f.name),
    side,
    view: {
      sim_mode: simMode,
      spending_mode: spendingMode,
      allocation_mode: allocationMode,
      sourcing_mode: sourcingMode,
    },
    glidepathPhaseWeights,
    bucketsBody,
    guardrails,
    monteCarlo,
    taxLocation,
    taxableGainsFraction,
    bracketFillTarget,
    modelAssumptions: readModelAssumptions(),
  });
  // M5 blocks, present only when used, so a save without them is unchanged.
  if (householdEnabled()) blob.inputs.household = JSON.parse(JSON.stringify(householdState));
  const HE = householdApi();
  if (HE && hhEl("tax-options")) {
    const tax = HE.buildTaxOptions(readTaxOptionsDom());
    if (Object.keys(tax).length) blob.inputs.tax_options = HE.taxStateFromBody(tax);
  }
  if (planExtras && Object.keys(planExtras).length) blob.inputs.plan_extras = { ...planExtras };
  if (planMeta) blob.inputs.plan_meta = { ...planMeta };
  return blob;
}

// Set one scalar field input's value (the A/shared input; on load we present a
// single, fully-linked scenario). No-op if the input isn't built yet.
function setFieldInputValue(name, value) {
  const el = aInputEl(name);
  if (!el) return;
  const f = FIELDS.find((x) => x.name === name);
  el.value = f ? fmtFieldValue(f, value) : String(value);
  // SS claim-age slider: restoring its position (or its FRA-amount mate) must
  // refresh the live readout so a loaded scenario shows the right effective benefit.
  // The claim-age slider IS a FIELDS member, so applyScenarioToForm writes it here
  // exactly like any field; we just nudge the readout after the write.
  if (f && f.control === "slider") {
    refreshSsReadout(el);
  } else if (f && /_ss_fra_amount$/.test(name)) {
    refreshAllSsReadouts();
  }
}

// Write the three tax-location inputs (tax-buckets design tb1). `split` is
// {taxable, tax_deferred, roth} (numbers) to RESTORE a saved split, or null to
// CLEAR them to blank (feature off — also the path for an old save lacking the
// block, and for Reset). Whole-dollar integers in the inputs (the values are
// already real dollars). Used by applyScenarioToForm + resetToDefaults.
function setTaxLocationInputs(split) {
  const pairs = [
    [TAX_TAXABLE_INPUT_ID, split ? split.taxable : null],
    [TAX_DEFERRED_INPUT_ID, split ? split.tax_deferred : null],
    [TAX_ROTH_INPUT_ID, split ? split.roth : null],
  ];
  for (const [id, val] of pairs) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.value = Number.isFinite(val) ? String(Math.round(val)) : "";
  }
  // Keep the enable checkbox in lockstep with the inputs it just wrote (tb1b).
  syncTaxLocationEnable();
}

// Read the starting portfolio (#initial_portfolio) as a finite number (tb1b),
// treating blank / non-numeric as 0. Mirrors how updateTaxLocationSum reads the
// sum target, but tolerant of a missing FIELDS descriptor / element so it's safe
// to call from the checkbox handler before any recompute.
function readStartingPortfolio() {
  try {
    const v = readFieldValue(
      FIELDS.find((f) => f.name === "initial_portfolio"),
      aInputEl("initial_portfolio"),
    );
    return Number.isFinite(v) ? v : 0;
  } catch (_err) {
    return 0;
  }
}

// Reflect the "is a split present" state onto the enable checkbox (tb1b). The
// checkbox is an INDICATOR, not a state flag: it tracks readTaxLocation()/hasSplit
// so manual typing checks it, clearing unchecks it, and a saved/reset round-trip
// lands it in the matching state. taxLocationActive() stays driven by the inputs,
// so manual entry without ever touching the box keeps working exactly as before.
function syncTaxLocationEnable() {
  const box = document.getElementById(TAX_LOCATION_ENABLE_ID);
  if (!box) return;
  box.checked = TaxLocationView.hasSplit(readTaxLocation());
  // tb1c: keep the reveal (the three balance fields + sum helper) in lockstep with
  // the same has-split/checkbox state this function tracks. Folding it in here means
  // every path that re-syncs the checkbox — manual typing, the enable toggle's
  // fill/clear, a saved-scenario load, Reset, the buckets gate refresh — also opens
  // or collapses the fields, with no extra wiring at each call site.
  updateTaxLocationReveal();
}

// Reveal (or collapse) the three tax-location balance fields + the sum helper based
// on whether a split is present — i.e. the enable checkbox's checked state, which
// syncTaxLocationEnable() drives off readTaxLocation()/hasSplit. OFF → the section is
// just the one muted checkbox line (#tax-location-reveal hidden + collapsed); ON →
// the fields show. Pure DOM toggle, no recompute. Called from syncTaxLocationEnable.
function updateTaxLocationReveal() {
  const box = document.getElementById(TAX_LOCATION_ENABLE_ID);
  const reveal = document.getElementById("tax-location-reveal");
  const section = document.getElementById("tax-location-inputs");
  const active = box ? box.checked : false;
  if (reveal) reveal.hidden = !active;
  // .tax-location-quiet styles the section muted/compact when OFF; drop it when the
  // fields are showing so the active state reads as a normal input group.
  if (section) section.classList.toggle("tax-location-quiet", !active);
}

// The enable-checkbox handler (tb1b). CHECK → pre-fill the three inputs from the
// starting portfolio at the default split (Roth balancing to the exact sum), then
// refresh the live sum + gate; the inputs remain editable. UNCHECK → clear all
// three (feature off, today's behavior). NO recompute — the app is button-driven
// (the user hits Recompute). setTaxLocationInputs re-syncs the checkbox itself, so
// the box ends up reflecting the inputs it just wrote.
function onTaxLocationEnableToggle() {
  const box = document.getElementById(TAX_LOCATION_ENABLE_ID);
  if (!box) return;
  if (box.checked) {
    const P = readStartingPortfolio();
    setTaxLocationInputs(TaxLocationView.defaultSplit(P));
  } else {
    setTaxLocationInputs(null);
  }
  updateTaxLocationSum();
  updateTaxLocationGate();
}

// The INVERSE of readScenarios() (none existed): drive the live form from a
// saved blob so the existing render path redraws the identical chart. Order
// matters — see the inline steps. The final rendered state must equal having
// configured the scenario by hand.
//
// MODE RESTORATION: the four setters early-return when the requested mode equals
// the current one (and only THEN run their internal rerender()). To get a clean,
// deterministic result with exactly ONE recompute regardless of the current
// state, we set the four mode globals + their conditional state/chrome directly
// (the same work the setters do MINUS the rerender), then fire a single
// rerender() at the end. This avoids 4 redundant recomputes and the
// "already in that mode -> setter no-ops -> stale UI" trap.
async function applyScenarioToForm(blob) {
  if (!blob || typeof blob !== "object") return null;
  const view = blob.view || {};
  const inputs = blob.inputs || {};
  applyModelAssumptions(inputs.model_assumptions);
  // M5: the household editor, tax options and plan-file extras. Absent blocks
  // (an older save, Reset) turn the editor off and restore the defaults.
  planExtras = inputs.plan_extras && typeof inputs.plan_extras === "object"
    ? { ...inputs.plan_extras } : null;
  planMeta = inputs.plan_meta && typeof inputs.plan_meta === "object"
    ? { ...inputs.plan_meta } : null;
  applyTaxOptionsDom(inputs.tax_options || null);

  // Suppress subscriber-driven recomputes while we write the new form values.
  // Collapsing an active comparison below notifies compareState subscribers ->
  // onComparisonStateChanged -> rerender(), which would read the STILL-STALE
  // form (we haven't written the new values yet) and fetch the wrong scenario.
  // The guard makes onComparisonStateChanged skip its rerender() during apply;
  // we clear it in `finally` (so an exception can't wedge it set) and then fire
  // exactly ONE rerender() with the fully-written form.
  applyingScenario = true;
  try {
    // The load presents a single, fully-linked scenario (sv1 replaces the current
    // scenario; comparison is rebuilt by hand or in sv2). Collapse any comparison
    // so A is the one authoritative input-set before we write values.
    if (compareState.isComparing()) compareState.exitComparison();
    if (!compareState.allocationLinked()) compareState.setAllocationLinked(true);
    // A single-scenario load supersedes any active compare overlay; drop the
    // sv2 chart-adjacent note so a stale mode-mismatch line doesn't linger.
    setCompareNote("");

    // 1) The 12 scalar field inputs (incl. GI ages). buildInputs() must have run.
    for (const f of FIELDS) {
      if (inputs[f.name] !== undefined && inputs[f.name] !== null) {
        setFieldInputValue(f.name, inputs[f.name]);
      }
    }

    // 1b) The household editor (M5). After the scalar fields, because an
    // enabled household rewrites the horizon fields from its people.
    applyHouseholdState(inputs.household || null);

    // 2) The allocation matrix. Drive ASSET_CLASSES from the saved allocation
    // (matched by class id), zeroing any class not present, then rebuild the
    // matrix + return/vol inputs from that model. Returns + volatilities are
    // shared (no A/B column), so writing the ASSET_CLASSES values is enough for
    // buildAllocation() to re-render them.
    const allocById = {};
    for (const e of inputs.allocation || []) allocById[e.id] = e;
    ASSET_CLASSES.forEach((c) => {
      const e = allocById[c.id];
      if (e) {
        // weight stored as a FRACTION (0..1) -> integer percent for the UI.
        c.weight = Math.round(e.weight * 100);
        if (typeof e.real_return === "number") {
          // real_return stored as a FRACTION -> percent for the UI.
          // Rounded to 0.1% (the return input's step="0.1") so tool-produced
          // saves round-trip exactly; only finer hand-edited JSON values round.
          c.real_return = Math.round(e.real_return * 1000) / 10;
        }
        if (typeof e.volatility === "number") c.volatility = e.volatility;
      } else {
        c.weight = 0;
      }
    });
    // MC per-class volatilities (a class-id -> fraction map) override the seed so
    // Advanced reseeds exactly; applied before buildAllocation renders the inputs.
    if (inputs.volatilities && typeof inputs.volatilities === "object") {
      ASSET_CLASSES.forEach((c) => {
        const v = inputs.volatilities[c.id];
        if (typeof v === "number") c.volatility = v;
      });
    }
    // MC correlation seed (read into the model so renderCorrelationRow shows it).
    if (typeof inputs.correlation === "number") mcCorrelation = inputs.correlation;
    // fromModel=true: seed the weight inputs from the ASSET_CLASSES weights just
    // written above, NOT the stale pre-load DOM. Without this, renderAllocationRows
    // would re-read the old weight inputs (e.g. a fresh page's default 60/40) and
    // silently discard the loaded allocation — the saved-scenario revert bug.
    buildAllocation(true);
    // The preset picker no longer reflects a named portfolio after a load.
    resetPresetToCustom();

    // 3) Glidepath: restore GI ages first (done in step 1) -> rebuild the phase
    // count -> overwrite the re-derived weights from the saved map by index ->
    // re-render. Best-effort index alignment if a GI-age edit later changes the
    // phase count (same as the live UI's behavior, documented in the design).
    if (inputs.glidepath_phase_weights) {
      ensureGlidepathPhaseWeights();
      const n = glidepathBoundaries().length;
      for (let k = 0; k < n; k++) {
        const saved = inputs.glidepath_phase_weights[String(k)];
        if (Array.isArray(saved)) {
          glidepathPhaseWeights[k] = ASSET_CLASSES.map((_, i) =>
            typeof saved[i] === "number" ? saved[i] : 0,
          );
        }
      }
    }

    // 4) Bucket config (n1/n2 + per-bucket weights). Seed the structure, then
    // overwrite from the saved bucket allocations (per-bucket {id, weight} lists).
    if (inputs.buckets) {
      seedBucketWeights();
      const bk = inputs.buckets;
      if (typeof bk.n1 === "number") bucketN1 = bk.n1;
      if (typeof bk.n2 === "number") bucketN2 = bk.n2;
      // The #bucket-n1/#bucket-n2 inputs are STATIC HTML (value="2"/"7") that is
      // never re-rendered, and readBucketN1/N2 read the DOM FIRST (falling back to
      // the global only when blank). Setting the globals alone leaves the old 2/7
      // in the DOM, so the recompute would use the wrong horizons. Write the
      // inputs too (integer years, matching the step="1" inputs).
      setInputValueById("bucket-n1", bucketN1);
      setInputValueById("bucket-n2", bucketN2);
      for (const bid of BUCKET_IDS) {
        const cfg = bk[bid];
        if (cfg && Array.isArray(cfg.allocation)) {
          const arr = ASSET_CLASSES.map(() => 0);
          for (const e of cfg.allocation) {
            const idx = classIndexById(e.id);
            if (idx >= 0) arr[idx] = Math.round(e.weight * 100);
          }
          bucketWeights[bid] = arr;
        }
      }
    }

    // 5) Guardrail knobs (the floor/ceiling are dollars; band/adjust are fractions
    // in the blob -> percent for the inputs). Written into the inputs if present;
    // the inputs only exist/are read under guardrails mode (set below).
    if (inputs.guardrails) {
      const g = inputs.guardrails;
      setInputValueById(GUARD_BAND_INPUT_ID, g.guard_band, 100);
      setInputValueById(GUARD_ADJUST_INPUT_ID, g.guard_adjust, 100);
      setInputValueById(SPENDING_FLOOR_INPUT_ID, g.spending_floor);
      setInputValueById(SPENDING_CEILING_INPUT_ID, g.spending_ceiling);
      // The amortization knob rides the same dynamic-spending block (readGuardrails
      // includes it). Restore it (stored as a fraction → percent slider) when present;
      // an older save without it leaves the slider at its default.
      if (g.amortization_real_return != null) {
        setInputValueById(AMORTIZATION_RETURN_INPUT_ID, g.amortization_real_return, 100);
      }
    }

    // 5b) Tax-location split (tax-buckets design tb1). A save with a split carries an
    // inputs.tax_location block ({taxable, tax_deferred, roth}); write the three
    // inputs from it. A save WITHOUT the block (feature was off, OR an OLD save
    // predating tb1) must load as feature-OFF — so when absent we CLEAR the three
    // inputs to blank. This keeps old saves loading (treated as off) and prevents a
    // stale split lingering from a previously-loaded scenario. Gate/view chrome runs
    // below once sourcingMode is restored.
    setTaxLocationInputs(inputs.tax_location || null);

    // 5c) Taxable-gains-fraction knob (federal-tax overlay design ft1, DECISION 4).
    // A save captured while the split was active carries inputs.taxable_gains_
    // fraction (a fraction 0..1); a save made with the split off, OR an OLD save
    // predating ft1, lacks it — load it at the feature default (50%), same
    // "absent -> off/default" discipline as 5b.
    setTaxGainsFractionInput(
      typeof inputs.taxable_gains_fraction === "number"
        ? inputs.taxable_gains_fraction
        : TAX_GAINS_FRACTION_DEFAULT,
    );

    // 5d) Bracket-fill lever (tax-drag design td1, DECISION 5). A save captured
    // with the lever ON carries inputs.bracket_fill_target (a dollar amount); a
    // save made with the lever off, the split off, OR an OLD save predating
    // td1, lacks it — clear the field to blank (off), same "absent -> off"
    // discipline as 5b (unlike 5c's gains-fraction, the lever has no default
    // value to fall back to).
    setBracketFillTargetInput(
      typeof inputs.bracket_fill_target === "number"
        ? inputs.bracket_fill_target
        : null,
    );

    // 6) Restore the four global modes by setting the globals + their conditional
    // state/chrome directly (the setter body MINUS rerender), then one rerender().
    simMode = _SCENARIO_SIM_MODES.has(view.sim_mode) ? view.sim_mode : "deterministic";
    spendingMode = _SCENARIO_SPENDING_MODES.has(view.spending_mode)
      ? view.spending_mode
      : "constant";
    allocationMode = _SCENARIO_ALLOCATION_MODES.has(view.allocation_mode)
      ? view.allocation_mode
      : "constant";
    sourcingMode = _SCENARIO_SOURCING_MODES.has(view.sourcing_mode)
      ? view.sourcing_mode
      : "single";

    // Mode-dependent chrome + sub-panel rendering, matching what each setter does.
    updateModeToggleSelection();
    updateModeRegions();
    updateViewToggle();
    updateMcInputsVisibility();

    updateSpendingToggleSelection();
    updateGuardrailInputsVisibility();
    updateAmortizationInputsVisibility();
    updateSpendingBreakdown();

    if (allocationMode === "glidepath") {
      ensureGlidepathPhaseWeights();
      renderGlidepathPhases();
    }
    updateAllocationModeView();

    updateSourcingToggleSelection();
    if (sourcingMode === "buckets") {
      seedBucketWeights();
      renderBucketMatrices();
    }
    updateSourcingModeView();
    updateBufferSurvivalReadout(null);
    updateEffectiveEquityReadout(null);
    updateBucketChartView(null);

    // Tax-location (tax-buckets design tb1): refresh the input gate (single vs
    // buckets) + the live sum helper now that sourcingMode is restored, and tear
    // down the tax-location chart until the single guarded rerender below redraws it.
    updateTaxLocationGate();
    updateTaxLocationView(null);
    // Deterministic guardrails spending line (dsc1): tear down until the guarded
    // rerender below redraws it. Reset's default scenario is constant spending, so
    // it correctly stays hidden (the gate re-evaluates on the rerender).
    updateDeterministicSpending(null);

    updateAllocationCaption();
    updateSolveControl();
    setError("");
  } finally {
    // Clear BEFORE the final rerender so the one intended recompute runs, and
    // unconditionally (finally) so a throw above can't leave the guard wedged.
    applyingScenario = false;
  }
  // Exactly ONE recompute, now reading the fully-written NEW form values.
  return await rerender();
}

// Mode vocabularies for applyScenarioToForm validation (mirror the setters).
const _SCENARIO_SIM_MODES = new Set(["deterministic", "historical", "monte_carlo"]);
const _SCENARIO_SPENDING_MODES = new Set([
  "constant",
  "lifecycle",
  "guardrails",
  "fr_smooth",
  "fr_guardrail",
  "fr_tiers",
  "amortization",
]);
const _SCENARIO_ALLOCATION_MODES = new Set(["constant", "glidepath"]);
const _SCENARIO_SOURCING_MODES = new Set(["single", "buckets"]);

// Set an input's value by id, optionally scaling a stored fraction up to the
// percent the input displays (band/adjust are stored as fractions, shown as %).
function setInputValueById(id, value, scale = 1) {
  const el = document.getElementById(id);
  if (!el || typeof value !== "number") return;
  el.value = String(value * scale);
}

// ---------- Reset to defaults (rst1) ----------
//
// "Reset" restores the FULL default-load state WITHOUT a page reload. The only
// pre-rst1 way back to a clean slate was a hard browser reload (init() re-fetches
// the default scenario and re-populates the form); this gives the user the same
// result instantly after loading saves / applying presets / building a comparison.
//
// The clean path REUSES applyScenarioToForm(): we build a default-shaped blob from
// the cached `defaultScenario` echo (the same source init() seeds the form from)
// plus the default-load view modes, and run it through the existing INVERSE map.
// That single call already does everything reset needs and nothing it doesn't:
//   - collapses any comparison (exitComparison + setAllocationLinked(true)),
//   - clears the #compare-note,
//   - rewrites the 12 scalar fields + the allocation matrix,
//   - rewrites the per-class volatilities + correlation (so MC Advanced reseeds
//     exactly as a fresh load does via seedVolatilitiesFromScenario/Correlation),
//   - restores the four global modes to the default-load lens,
//   - fires EXACTLY ONE recompute under the applyingScenario guard (the
//     comparison-collapse + mode resets don't trigger extra rerenders).
// It does NOT touch the saved-scenario library (no DELETE/save calls), so vault
// saves persist — reset only affects the current working scenario.

// The four default-load view modes (the state init() leaves the toggles in).
const _DEFAULT_VIEW_MODES = Object.freeze({
  sim_mode: "deterministic",
  spending_mode: "constant",
  allocation_mode: "constant",
  sourcing_mode: "single",
});

// Build a default-shaped blob from the cached default-scenario echo: the scalar
// fields + the allocation array (already {id, weight, real_return, volatility}
// fractions, exactly the shape applyScenarioToForm's inputs.allocation expects),
// the per-class volatility map + the correlation (so Advanced reseeds like a fresh
// load), and the default view modes. Returns null if the default echo isn't cached
// yet (init() hasn't completed) — the caller then no-ops rather than wiping to a
// half-known state.
function buildDefaultScenarioBlob() {
  if (!defaultScenario || typeof defaultScenario !== "object") return null;
  const inputs = {};
  for (const f of FIELDS) {
    // Key off the SAME default mapping the form seeds from (defaultFieldValue):
    // `defaultFrom` redirects the SS claim-age slider + FRA-amount fields onto the
    // DERIVED person_a_ss_amount/person_a_ss_start_age the echo carries (the request sends
    // fra_amount + claim_age, but the echo has no such keys). Without this the four
    // SS fields key on their own (absent) names, get skipped, and Reset leaves them
    // at their edited values. Single source of truth = the FIELDS `defaultFrom`.
    const v = defaultValueFromEcho(f, defaultScenario);
    if (typeof v === "number") inputs[f.name] = v;
  }
  const allocation = Array.isArray(defaultScenario.allocation)
    ? defaultScenario.allocation.map((e) => ({ ...e }))
    : [];
  inputs.allocation = allocation;
  // Per-class vols + correlation: mirror what init() seeds via
  // seedVolatilitiesFromScenario / seedCorrelationFromScenario, so a reset reaches
  // the SAME MC Advanced state a fresh load does (the deterministic canary doesn't
  // read these, but "equals a fresh load" must hold in every mode).
  const vols = {};
  for (const e of allocation) {
    if (e && typeof e.id === "string" && typeof e.volatility === "number") {
      vols[e.id] = e.volatility;
    }
  }
  if (Object.keys(vols).length) inputs.volatilities = vols;
  if (typeof defaultScenario.correlation === "number") {
    inputs.correlation = defaultScenario.correlation;
  }
  // Taxable-gains-fraction knob (federal-tax overlay design ft1): the engine echo
  // always carries this field (it has a scenario-level default, unlike the
  // Optional tax-location balances), so mirror it defensively the same way as
  // correlation above — if the compiled-in default ever moves off 50%, Reset still
  // tracks "equals a fresh load" rather than a hardcoded literal. When the echo is
  // somehow missing the field, applyScenarioToForm's own TAX_GAINS_FRACTION_DEFAULT
  // fallback (50%) covers it, per DECISION 4.
  if (typeof defaultScenario.taxable_gains_fraction === "number") {
    inputs.taxable_gains_fraction = defaultScenario.taxable_gains_fraction;
  }
  return {
    schema_version: SCENARIO_SCHEMA_VERSION,
    name: "Defaults",
    view: { ..._DEFAULT_VIEW_MODES },
    inputs,
  };
}

// Restore the full default-load state in place. Lightweight confirm first (reset
// discards the current working scenario / any comparison; saved scenarios are
// kept). On confirm, replay the default blob through applyScenarioToForm (one
// recompute, comparison collapsed, modes + inputs restored) and clear the
// Scenarios popover status line. The result equals a fresh page load.
async function resetToDefaults() {
  applyModelAssumptions(null);
  // 1) Lightweight confirm — reset throws away the working scenario / comparison.
  if (typeof confirm === "function") {
    if (!confirm("Reset everything to defaults? Your saved scenarios are kept.")) {
      return null; // cancelled -> no-op (no state touched, no recompute)
    }
  }
  const blob = buildDefaultScenarioBlob();
  if (!blob) return null; // default echo not cached yet -> nothing safe to reset to
  // 2-5) applyScenarioToForm does the comparison-collapse, input + allocation +
  // mode restore, #compare-note clear, and the single guarded recompute.
  const result = await applyScenarioToForm(blob);
  // Clear any lingering Scenarios-popover status (e.g. "Comparing A with 'X'").
  setScenariosStatus("");
  return result;
}

// ---------- Saved Scenarios (sv2): load-2-to-compare ----------
//
// "Compare with…" loads a SECOND saved scenario as scenario B against the
// CURRENT form (scenario A), overlaying them. The comparison axis the app
// supports is per-FIELD link/unlink + the constant allocation matrix's A│B
// columns; the four engine/spending/allocation/sourcing modes are GLOBAL (not
// per-side). So a compare diffs the 12 scalar FIELDS + the constant allocation
// matrix, unlinks only what differs, and NOTES anything it can't express
// per-side (a mode mismatch, a glidepath/buckets shape that isn't the constant
// matrix). It never silently changes a global mode out from under A.

// Normalize a saved/allocation list ([{id, weight (fraction)}, ...]) into an
// id -> integer-percent map. Thin delegate to ScenarioStore.allocationPercentMap
// (moved VERBATIM into web/scenario_store.js); compareWithScenario() reuses it to
// build B's weight column from the saved (constant) allocation.
function allocationPercentMap(allocation) {
  return ScenarioStore.allocationPercentMap(allocation);
}

// PURE diff between the SAVED blob (candidate B) and the CURRENT scenario A.
// Thin delegate to ScenarioStore.diff (moved VERBATIM into web/scenario_store.js)
// — see that module for the decision-record contract. No DOM, no mutation.
function diffScenarioForCompare(savedBlob, currentA, fieldNames) {
  return ScenarioStore.diff(savedBlob, currentA, fieldNames);
}

// Read the CURRENT scenario A into the {view, inputs} shape diffScenarioForCompare
// consumes. Reuses captureScenario(), which already serializes the focused side
// (A by default) + the four modes — so A's allocation here is the same
// id/weight(fraction) list the saved blob carries, making the diff apples-to-apples.
function currentScenarioForCompare() {
  const blob = captureScenario("");
  return { view: blob.view, inputs: blob.inputs };
}

// Imperative driver: load a saved scenario as B against the current A.
//   1. Fetch the saved blob.
//   2. Diff vs current A (scalars + constant allocation matrix).
//   3. Identical -> note + bail (no empty comparison).
//   4. For each differing scalar: unlink it and set its B input to the saved value.
//   5. Allocation differs -> unlink allocation, populate B's weight column.
//   6. Modes differ -> NOTE (no global mode change).
//   7. glidepath/buckets active -> NOTE (the diff covers scalars + constant matrix only).
//   8. Exactly ONE recompute (the applyingScenario guard suppresses the
//      compareState-subscriber rerenders while we mutate; one rerender() at the end).
async function compareWithScenario(id) {
  let saved;
  try {
    const res = await fetch(`${SCENARIOS_API}/${encodeURIComponent(id)}`);
    if (!res.ok) {
      setScenariosStatus(`Compare failed: HTTP ${res.status}`);
      return null;
    }
    saved = await res.json();
  } catch (err) {
    setScenariosStatus(`Compare failed: ${err.message}`);
    return null;
  }

  const name = (saved && saved.name) || id;
  const fieldNames = FIELDS.map((f) => f.name);
  const currentA = currentScenarioForCompare();
  const decision = diffScenarioForCompare(saved, currentA, fieldNames);

  if (decision.identical) {
    setScenariosStatus(
      `'${name}' is identical to the current scenario — nothing to compare.`,
    );
    return null;
  }

  // Build the note text BEFORE mutating, so it reflects A's pre-compare modes.
  const notes = [];
  if (decision.modeMismatch) {
    notes.push(
      `Loaded inputs from '${name}' as B — its ` +
        decision.mismatchedModes.join("/") +
        " mode differs and can't be applied per-side; the comparison uses scenario A's modes.",
    );
  }
  // Known limit: glidepath phase-weights / bucket configs are GLOBAL client
  // state, not per-side. When the constant matrix isn't the active allocation
  // (glidepath) or sourcing is bucketed, the compare varies the scalar inputs
  // (+ the constant matrix) only; the glide shape / bucket config stays shared.
  if (allocationMode === "glidepath") {
    notes.push(
      "Allocation is in glidepath mode — the comparison varies the scalar inputs only; the glide shape is shared.",
    );
  }
  if (sourcingMode === "buckets") {
    notes.push(
      "Sourcing is in buckets mode — the comparison varies the scalar inputs only; the bucket config is shared.",
    );
  }

  // Mutate under the apply guard so the compareState subscriber's per-transition
  // rerender() is suppressed (it would otherwise fetch a half-built B). We fire
  // exactly one rerender() at the end. (Same guard applyScenarioToForm uses.)
  //
  // ORDER: flip ALL the unlink transitions FIRST, THEN write the B values. Each
  // setFieldLink/setAllocationLinked fires the subscriber (guard-suppressed),
  // which re-renders rows; doing the value-writes last means no later transition
  // can reseed a B input we already set (B seeds from A only on the unlink
  // re-render — a write that follows the last transition is final).
  applyingScenario = true;
  try {
    // 1) Unlink the differing scalar fields + (if it differs) the allocation.
    for (const fieldName of decision.fieldsToUnlink) {
      if (compareState.isLinked(fieldName)) {
        compareState.setFieldLink(fieldName, false);
      }
    }
    if (decision.allocationDiffers && compareState.allocationLinked()) {
      compareState.setAllocationLinked(false);
    }

    // 2) Write the saved values into the now-rendered B inputs. Scalars use the
    // same fmtFieldValue formatting applyScenarioToForm uses for the A input.
    for (const fieldName of decision.fieldsToUnlink) {
      const f = FIELDS.find((x) => x.name === fieldName);
      const bEl = bInputEl(fieldName);
      if (bEl && f) {
        bEl.value = fmtFieldValue(f, (saved.inputs || {})[fieldName]);
      }
    }
    // Allocation B column from the saved (constant) allocation (0 for classes
    // absent from the saved allocation, matching applyScenarioToForm's zero-fill).
    if (decision.allocationDiffers) {
      const bAlloc = allocationPercentMap((saved.inputs || {}).allocation);
      ASSET_CLASSES.forEach((c, i) => {
        const bEl = document.getElementById(weightId("b", i));
        if (bEl) bEl.value = String(bAlloc[c.id] || 0);
      });
      refreshAllocation();
    }
  } finally {
    applyingScenario = false;
  }

  const noteText = notes.join(" ");
  if (noteText) {
    // Surface the per-side limitation BOTH near the chart (persists) and in the
    // popover status. Keep the popover OPEN so the explainer stays visible.
    setCompareNote(noteText);
    setScenariosStatus(`Comparing A with '${name}' (B). ${noteText}`);
  } else {
    // Clean overlay (modes match, constant matrix is the active allocation):
    // no limitation note, so close the popover to reveal the chart.
    setCompareNote("");
    setScenariosStatus(`Comparing the current scenario (A) with '${name}' (B).`);
    closeScenariosPopover();
  }
  // Exactly ONE recompute, now reading the fully-built A│B form.
  return await rerender();
}

// Set (or clear) the chart-adjacent compare note. Empty -> hidden.
function setCompareNote(msg) {
  const el = document.getElementById("compare-note");
  if (!el) return;
  el.textContent = msg || "";
  el.hidden = !msg;
}

// Expose the forward/inverse maps + the sv2 compare diff for the node:vm tests
// (same testability structure as GlidepathView). Not a stable API.
window.SavedScenarios = {
  capture: captureScenario,
  apply: applyScenarioToForm,
  compare: compareWithScenario,
  diffForCompare: diffScenarioForCompare,
  reset: resetToDefaults,
  // Exposed for the fresh-state load regression test: lets a unit reproduce the
  // 12-class menu that init() loads from /api/classes, so apply() runs against a
  // populated ASSET_CLASSES (not the 2-class fallback) — the configuration in
  // which the saved-scenario allocation-revert bug actually manifested.
  loadClassMenu,
};

// ---------- Saved Scenarios: the "Scenarios ▾" popover ----------
//
// A plain DOM popover anchored to the action-bar button (NOT a modal). Lists the
// saved scenarios (click a row to load, × to delete with confirm) and an inline
// "Save current as [name]" row. Populated on open from GET /api/scenarios.

const SCENARIOS_API = "api/scenarios";

// Fetch the saved-scenario metadata list. Returns [] on any failure (the popover
// degrades to just the save row rather than throwing).
async function fetchScenarioList() {
  try {
    const res = await fetch(SCENARIOS_API);
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data) ? data : [];
  } catch (_) {
    return [];
  }
}

function scenariosPopoverEl() {
  return document.getElementById("scenarios-popover");
}

function isScenariosPopoverOpen() {
  const el = scenariosPopoverEl();
  return !!el && !el.hidden;
}

function closeScenariosPopover() {
  const el = scenariosPopoverEl();
  if (el) el.hidden = true;
  const btn = document.getElementById("scenarios-btn");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

async function openScenariosPopover() {
  const el = scenariosPopoverEl();
  if (!el) return;
  el.hidden = false;
  const btn = document.getElementById("scenarios-btn");
  if (btn) btn.setAttribute("aria-expanded", "true");
  await renderScenariosPopover();
}

async function toggleScenariosPopover() {
  if (isScenariosPopoverOpen()) {
    closeScenariosPopover();
  } else {
    await openScenariosPopover();
  }
}

// (Re)render the popover body: the saved list (load / delete) + the save-as row,
// plus the focused-side note when mid-comparison.
async function renderScenariosPopover() {
  const el = scenariosPopoverEl();
  if (!el) return;
  el.innerHTML = "";

  const heading = document.createElement("div");
  heading.className = "scenarios-popover-heading";
  heading.textContent = "Saved scenarios";
  el.appendChild(heading);

  // Note when a save would capture only the focused side (mid-comparison).
  if (compareState.isComparing()) {
    const note = document.createElement("p");
    note.className = "scenarios-popover-note";
    const side = compareState.focusedScenario() === "B" ? "B" : "A";
    note.textContent = `Saving captures the focused scenario (${side}) only.`;
    el.appendChild(note);
  }

  const list = document.createElement("div");
  list.className = "scenarios-list";
  const items = await fetchScenarioList();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "scenarios-empty";
    empty.textContent = "No saved scenarios yet.";
    list.appendChild(empty);
  } else {
    for (const item of items) {
      list.appendChild(makeScenarioRow(item));
    }
  }
  el.appendChild(list);

  // Inline "Save current as [name]" row. The known list (captured here) lets
  // onSaveScenario detect an existing slug and confirm before overwriting.
  const knownItems = items;
  const saveRow = document.createElement("div");
  saveRow.className = "scenarios-save-row";
  const input = document.createElement("input");
  input.type = "text";
  input.id = "scenario-name-input";
  input.className = "scenarios-name-input";
  input.placeholder = "Name this scenario";
  input.setAttribute("aria-label", "Scenario name");
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.id = "scenario-save-btn";
  saveBtn.className = "btn btn-secondary";
  saveBtn.textContent = "Save";
  // Empty/whitespace-name guard: Save is disabled until the name has content,
  // so the empty save can't even be attempted. (onSaveScenario also guards, so
  // the Enter-key path is covered too.)
  const syncSaveDisabled = () => {
    saveBtn.disabled = input.value.trim().length === 0;
  };
  saveBtn.addEventListener("click", () => onSaveScenario(input.value, knownItems));
  input.addEventListener("input", syncSaveDisabled);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onSaveScenario(input.value, knownItems);
    }
  });
  syncSaveDisabled();
  saveRow.appendChild(input);
  saveRow.appendChild(saveBtn);
  el.appendChild(saveRow);

  const status = document.createElement("p");
  status.className = "scenarios-status";
  status.id = "scenarios-status";
  status.setAttribute("role", "status");
  el.appendChild(status);
}

// One saved-scenario row: a load button (the name) + a "Compare" (⇄) button
// (sv2: load this save as B against the current A) + a delete (×) button.
function makeScenarioRow(item) {
  const label = item.name || item.id;
  const row = document.createElement("div");
  row.className = "scenarios-row";
  row.dataset.id = item.id;

  const loadBtn = document.createElement("button");
  loadBtn.type = "button";
  loadBtn.className = "scenarios-load-btn";
  loadBtn.textContent = label;
  loadBtn.title = `Load "${label}"`;
  loadBtn.addEventListener("click", () => onLoadScenario(item.id));
  row.appendChild(loadBtn);

  // sv2 "Compare with…": load this save as B against the current scenario A.
  const cmpBtn = document.createElement("button");
  cmpBtn.type = "button";
  cmpBtn.className = "scenarios-compare-btn";
  cmpBtn.textContent = "⇄"; // ⇄ (left-right arrow)
  cmpBtn.setAttribute(
    "aria-label",
    `Compare "${label}" against the current scenario`,
  );
  cmpBtn.title = `Compare "${label}" against the current scenario`;
  cmpBtn.addEventListener("click", () => onCompareScenario(item.id));
  row.appendChild(cmpBtn);

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "scenarios-delete-btn";
  delBtn.textContent = "×"; // ×
  delBtn.setAttribute("aria-label", `Delete "${label}"`);
  delBtn.title = "Delete";
  delBtn.addEventListener("click", () => onDeleteScenario(item.id, item.name));
  row.appendChild(delBtn);

  return row;
}

// Compare handler: load the saved scenario as B against the current A. The
// status note (mode-mismatch / glidepath-buckets-limit / identical) is set by
// compareWithScenario; the popover closes on a successful overlay so the chart
// is visible. On identical/no-op the popover stays open with the explainer.
async function onCompareScenario(id) {
  await compareWithScenario(id);
}

function setScenariosStatus(msg) {
  const el = document.getElementById("scenarios-status");
  if (el) el.textContent = msg || "";
}

// Save the current scenario under `name` (PUT by slug). Empty-name guarded;
// overwrite-confirmed when the derived slug already names a saved scenario.
// `knownItems` is the popover's current list (metadata), used only to detect
// the slug collision for the confirm — the server is the upsert authority.
async function onSaveScenario(name, knownItems) {
  const trimmed = (name || "").trim();
  if (!trimmed) {
    setScenariosStatus("Enter a name to save.");
    return;
  }
  let blob;
  try {
    blob = captureScenario(trimmed);
  } catch (err) {
    setScenariosStatus(`Can't save: ${err.message}`);
    return;
  }
  const slug = slugifyClient(trimmed);
  if (!slug) {
    setScenariosStatus("That name has no usable characters.");
    return;
  }
  // Overwrite-confirm: if the derived slug already exists, confirm before the
  // PUT (which is an upsert and would silently overwrite otherwise). Use the
  // existing display name in the prompt when we have it.
  const existing = Array.isArray(knownItems)
    ? knownItems.find((it) => it && it.id === slug)
    : null;
  if (existing && typeof confirm === "function") {
    const existingLabel = existing.name || slug;
    if (!confirm(`Overwrite '${existingLabel}'?`)) {
      setScenariosStatus("Save cancelled.");
      return;
    }
  }
  try {
    const res = await fetch(`${SCENARIOS_API}/${slug}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(blob),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) { /* keep generic */ }
      setScenariosStatus(`Save failed: ${detail}`);
      return;
    }
    setScenariosStatus(`Saved "${trimmed}".`);
    await renderScenariosPopover();
  } catch (err) {
    setScenariosStatus(`Save failed: ${err.message}`);
  }
}

// Load a saved scenario by id (GET the full blob -> applyScenarioToForm).
async function onLoadScenario(id) {
  try {
    const res = await fetch(`${SCENARIOS_API}/${encodeURIComponent(id)}`);
    if (!res.ok) {
      setScenariosStatus(`Load failed: HTTP ${res.status}`);
      return;
    }
    const blob = await res.json();
    await applyScenarioToForm(blob);
    closeScenariosPopover();
  } catch (err) {
    setScenariosStatus(`Load failed: ${err.message}`);
  }
}

// Delete a saved scenario by id (with a confirm).
async function onDeleteScenario(id, name) {
  const label = name || id;
  if (typeof confirm === "function" && !confirm(`Delete "${label}"?`)) return;
  try {
    const res = await fetch(`${SCENARIOS_API}/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (!res.ok && res.status !== 404) {
      setScenariosStatus(`Delete failed: HTTP ${res.status}`);
      return;
    }
    setScenariosStatus(`Deleted "${label}".`);
    await renderScenariosPopover();
  } catch (err) {
    setScenariosStatus(`Delete failed: ${err.message}`);
  }
}

// Client-side slug derivation, mirroring the server's slugify so the PUT path
// id matches what the server stores (lowercase, non-alnum runs -> "-", trim
// dashes, cap 64). The server is authoritative; this just keeps the URL clean.
function slugifyClient(name) {
  return String(name)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/^-+|-+$/g, "");
}

function wireScenariosControl() {
  const btn = document.getElementById("scenarios-btn");
  if (btn) btn.addEventListener("click", () => toggleScenariosPopover());
  // Dismiss on outside click (it's a popover, not a modal — no backdrop).
  document.addEventListener("click", (e) => {
    if (!isScenariosPopoverOpen()) return;
    const pop = scenariosPopoverEl();
    const ctl = document.getElementById("scenarios-control");
    if (ctl && !ctl.contains(e.target)) closeScenariosPopover();
  });
  // Dismiss on Escape.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isScenariosPopoverOpen()) closeScenariosPopover();
  });
}

function setError(msg) {
  document.getElementById("form-error").textContent = msg || "";
}

// ---------- per-panel render ----------

// `yMax` (optional) fixes the y-axis top so both comparison charts share one
// scale — without it each chart auto-scales to its own data, which makes a $10M
// scenario and a $2M scenario look deceptively similar. Bottom is always pinned
// to 0 (honest baseline for a portfolio chart; we never truncate the bottom).
// `overlayFlows` (slice 1): when true, overlay the deterministic
// annual-spending (solid) + guaranteed-income-floor (dashed) series on a
// SECONDARY right-hand axis. Gated by the caller to the Deterministic SINGLE
// view ONLY (shouldOverlayFlows()) — the comparison panels never pass it, so
// their charts stay single-axis and byte-identical.
function renderChart(panelId, points, yMax, overlayFlows = false) {
  const canvas = document.getElementById(`trajectory-chart-${panelId}`);
  const state = panels[panelId];
  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
  }
  const datasets = [
    {
      label: "Portfolio (real $, end of year)",
      data: points.map((p) => p.end_portfolio),
      borderColor: cssVar("--chart-band-a"),
      backgroundColor: `rgba(${cssVar("--chart-band-a-rgb")}, 0.1)`,
      fill: true,
      tension: 0.15,
      pointRadius: 0,
      borderWidth: 2,
    },
  ];
  // Shared scale across both panels: when a shared max is supplied, pin the
  // top to it (with a little headroom via suggestedMax so Chart.js still
  // rounds to a nice tick and the taller line isn't clipped at the very edge).
  // Falls back to auto-scale if yMax is absent.
  const options = {
    tooltipDisplayColors: false,
    tooltipLabel: (item) => {
      const p = points[item.dataIndex];
      return ChartView.tooltipRows([
        ["Annual Spend", fmtMoney(p.spending)],
        ["Annual Income", fmtMoney(p.guaranteed_income)],
        ["YoY Growth", fmtMoney(p.growth)],
        ["EoY Value", fmtMoney(p.end_portfolio)],
      ]);
    },
    suggestedYMax: yMax != null ? yMax * 1.05 : undefined,
    yTickFormatter: (v) => fmtMillions(v),
  };
  if (overlayFlows) {
    const overlay = SpendingSeries.flowOverlay(points);
    datasets.push(...overlay.datasets);
    options.secondaryYAxis = overlay.secondaryYAxis;
    // Show the legend so the two new series read by name (the portfolio-only
    // chart kept the legend off). Top placement matches the det-spending line.
    options.legend = { display: true, position: "top" };
    // interaction.mode "index" fires the body callback once per visible dataset
    // at the hovered age; restrict it to the portfolio dataset (index 0) so the
    // existing 4-row Spend/Income/Growth/Value tooltip surfaces EXACTLY ONCE and
    // the two overlay lines don't each add a duplicate row.
    options.tooltipFilter = (item) => item.datasetIndex === 0;
  }
  state.chart = ChartView.lineChart(canvas, {
    labels: points.map((p) => p.age),
    datasets,
    options,
  });
}

function renderTable(panelId, points) {
  renderTableInto(`#trajectory-table-${panelId} tbody`, points);
}

// Render year-by-year rows into the tbody matching `tbodySelector`. Factored
// out of renderTable so the overlay view can reuse the exact same per-year
// table rendering for its two stacked drill-downs (different table ids).
function renderTableInto(tbodySelector, points) {
  const tbody = document.querySelector(tbodySelector);
  tbody.innerHTML = "";
  for (const p of points) {
    const tr = document.createElement("tr");
    const cells = [
      String(p.age),
      fmtMoney(p.start_portfolio),
      fmtMoney(p.growth),
      fmtMoney(p.guaranteed_income),
      fmtMoney(p.spending),
      fmtMoney(p.end_portfolio),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function panelSummaryEl(panelId) {
  return document.querySelector(`.panel[data-panel="${panelId}"] .summary-line`);
}

// ---------- fetch + render orchestration ----------
//
// Both comparison charts share one y-axis scale, so rendering can't happen
// per-panel-as-soon-as-it-arrives: we need BOTH scenarios in hand to compute
// the shared y-max before drawing either chart. So fetching and rendering are
// split — fetchScenario just gets the data, and a separate render step (driven
// by renderBoth) draws each panel once the shared max is known.

// Fetch one scenario's trajectory. Returns { scenario, trajectory } on success,
// or null on failure (with the panel's summary + form error set).
// Build the POST JSON body for a trajectory/solve request from a `params` object
// (asset-class design B4 transport). `params` carries the scalar scenario fields
// (initial_portfolio, ages, GI, spending_mode, correlation, …) PLUS an
// `allocationBody` array (id-native allocation, set by readScenarios) and,
// optionally, `mode` / `confidence`. This reshapes that flat object into the
// ScenarioBody the API expects: `allocation` is the array; every other key rides
// through verbatim. The 2-class default and the full 12-class menu use the SAME
// POST path — there is no GET allocation string anymore (the engine's GET alias
// stays as a back-compat shareable-link entry point only, unused by this UI).
function buildPostBody(params) {
  return PayloadAssembler.buildPostBody(params);
}

// POST a scenario to one of the JSON endpoints and return the parsed payload, or
// throw with the API's `detail` (the friendly 422 message for a non-PSD broad MC
// mix or a no-replayable-window Historical run) so callers surface it cleanly.
async function postJson(url, params) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildPostBody(params)),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* keep generic */ }
    throw new Error(detail);
  }
  return await res.json();
}

async function fetchScenario(panelId, params) {
  // params: optional object of API-shape values (incl. `allocationBody`).
  // null/undefined → server defaults (POST with an empty body resolves the
  // default scenario, same as a bare GET used to).
  try {
    return await postJson(API_URL, params);
  } catch (err) {
    panelSummaryEl(panelId).textContent =
      `Failed to load trajectory: ${err.message}`;
    setError(err.message);
    return null;
  }
}

// Draw one panel (summary + chart + table) from already-fetched data, using the
// supplied shared y-max so both charts read on the same scale.
function renderPanel(panelId, payload, yMax, overlayFlows = false) {
  const { scenario, trajectory } = payload;
  panelSummaryEl(panelId).textContent = summaryLine(trajectory, scenario);
  renderChart(panelId, trajectory.points, yMax, overlayFlows);
  renderTable(panelId, trajectory.points);
}

// The shared y-axis maximum = the largest end-of-year portfolio value across
// BOTH scenarios' full trajectories. Ignores nulls (a failed fetch). Returns
// undefined when neither side has data, letting renderChart fall back to auto.
function computeSharedYMax(payloads) {
  let max;
  for (const payload of payloads) {
    if (!payload) continue;
    for (const p of payload.trajectory.points) {
      if (max === undefined || p.end_portfolio > max) max = p.end_portfolio;
    }
  }
  return max;
}

// Fetch both scenarios in parallel, compute the shared y-max from both, then
// render each panel on that one scale. A panel whose fetch failed keeps its
// error message and isn't redrawn. Returns the resolved payloads.
async function renderBoth(paramsA, paramsB) {
  const [payloadA, payloadB] = await Promise.all([
    fetchScenario("a", paramsA),
    fetchScenario("b", paramsB),
  ]);
  const yMax = computeSharedYMax([payloadA, payloadB]);
  if (payloadA) renderPanel("a", payloadA, yMax);
  if (payloadB) renderPanel("b", payloadB, yMax);
  return { payloadA, payloadB };
}

// Slice 10b: overlay view — ONE chart, TWO lines (Scenario A + Scenario B),
// one shared y-axis (inherent: both datasets on one chart). Draws into the
// dedicated #overlay-chart canvas, tracked in `overlay.chart` so it can be
// destroyed cleanly on the next render / mode switch. The index-mode tooltip
// shows the hovered age plus BOTH scenarios' end-of-year portfolio values,
// labelled by scenario, reusing the monospace alignment of the per-panel
// tooltip.
function renderOverlayChart(pointsA, pointsB) {
  const canvas = document.getElementById("overlay-chart");
  if (overlay.chart) {
    overlay.chart.destroy();
    overlay.chart = null;
  }
  // X-axis spans the UNION of both scenarios' ages (min start_age → max
  // end_age across A and B). When start/end age are LINKED the two age ranges
  // are identical, so the union equals either scenario's ages and the result is
  // byte-identical to the old single-scenario labels. When UNLINKED and one
  // scenario has a longer horizon, the union axis carries every age so the
  // longer scenario's trailing years are no longer clipped.
  //
  // Each dataset is aligned BY AGE onto the union axis: a scenario contributes
  // a value at the ages it actually carries and `null` elsewhere (Chart.js
  // draws a gap, which is correct for a scenario that starts later or ends
  // earlier — it is NOT stretched or truncated). The tooltip indexes `dataIndex`
  // into the union axis, so we also build per-age-aligned point arrays parallel
  // to the data so the tooltip reads the right scenario row at every age.
  const ages = unionAges(pointsA, pointsB);
  const alignedA = alignPointsByAge(pointsA, ages);
  const alignedB = alignPointsByAge(pointsB, ages);
  // One chart => one inherently shared y-scale; both datasets drive the auto
  // max, so the taller scenario is honestly taller. Bottom pinned to 0 by the
  // ChartView default (never truncate a portfolio chart's baseline).
  overlay.chart = ChartView.lineChart(canvas, {
    labels: ages,
    datasets: [
      {
        label: "Scenario A",
        data: alignedA.map((p) => (p ? p.end_portfolio : null)),
        borderColor: LINE_A(),
        backgroundColor: `rgba(${RGB_A()}, 0.08)`,
        fill: false,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: LINE_WIDTH,
      },
      {
        label: "Scenario B",
        data: alignedB.map((p) => (p ? p.end_portfolio : null)),
        borderColor: LINE_B(),
        backgroundColor: `rgba(${RGB_B()}, 0.08)`,
        fill: false,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: LINE_WIDTH,
      },
    ],
    options: {
      legend: { display: true, position: "top" },
      // One row per scenario with portfolio + spending + GI at the hovered
      // age (Fix 3). The points carry `spending` + `guaranteed_income`
      // per year, so the comparison overlay reads the same three figures the
      // single-panel deterministic tooltip does. `dataIndex` is the union-axis
      // position, so we read from the age-aligned point arrays (null where a
      // scenario doesn't reach that age).
      tooltipLabel: (item) => {
        const pts = item.datasetIndex === 0 ? alignedA : alignedB;
        const p = pts[item.dataIndex];
        const which = item.dataset.label; // "Scenario A" / "Scenario B"
        if (!p) return `${which}: ${fmtMoney(item.parsed.y)}`;
        return (
          `${which}: portfolio ${fmtMoney(p.end_portfolio)}, ` +
          `spend ${fmtMoney(p.spending)}, GI ${fmtMoney(p.guaranteed_income)}`
        );
      },
      yTickFormatter: (v) => fmtMillions(v),
    },
  });
}

// Union of the two scenarios' ages, ascending. When the age ranges are linked
// (the common default) both point arrays carry the same ages, so this returns
// exactly those ages and the overlay labels are unchanged. When unlinked it
// spans min(start_age) → max(end_age) across A and B. Trajectory ages are
// dense consecutive integers per scenario; the union of two such ranges is
// itself dense, so we don't need to reconcile interior holes.
function unionAges(pointsA, pointsB) {
  const set = new Set();
  for (const p of pointsA) set.add(p.age);
  for (const p of pointsB) set.add(p.age);
  return Array.from(set).sort((x, y) => x - y);
}

// Project a scenario's points onto the union `ages` axis: returns an array the
// same length as `ages`, holding the matching point at each age it carries and
// `null` at ages outside its own range. Lets each line plot at its true ages
// (gaps where it doesn't reach) instead of index-aligning from position 0.
function alignPointsByAge(points, ages) {
  const byAge = new Map(points.map((p) => [p.age, p]));
  return ages.map((age) => byAge.get(age) || null);
}

// Fetch both scenarios in parallel (same payloads renderBoth would consume — no
// new API calls), then render the overlay: one chart with both lines, both
// summary lines (colour-coded to match), and both stacked year-by-year tables.
async function renderOverlay(paramsA, paramsB) {
  const [payloadA, payloadB] = await Promise.all([
    fetchScenario("a", paramsA),
    fetchScenario("b", paramsB),
  ]);
  const pointsA = payloadA ? payloadA.trajectory.points : [];
  const pointsB = payloadB ? payloadB.trajectory.points : [];
  renderOverlayChart(pointsA, pointsB);

  if (payloadA) {
    document.getElementById("overlay-summary-a").textContent =
      `Scenario A — ${summaryLine(payloadA.trajectory, payloadA.scenario)}`;
    renderTableInto("#overlay-table-a tbody", pointsA);
  }
  if (payloadB) {
    document.getElementById("overlay-summary-b").textContent =
      `Scenario B — ${summaryLine(payloadB.trajectory, payloadB.scenario)}`;
    renderTableInto("#overlay-table-b tbody", pointsB);
  }
  return { payloadA, payloadB };
}

// Slice 10a: single-scenario path. When nothing is unlinked A and B are
// identical, so we fetch ONCE and render only the (visible) panel A. Panel B
// stays hidden by CSS (.panels.is-single), so its stale chart/summary are
// irrelevant — we deliberately don't redraw it. The y-axis just uses this
// scenario's own max (no cross-scenario sharing needed when there's one chart).
async function renderSingle(params) {
  const payload = await fetchScenario("a", params);
  if (payload) {
    const yMax = computeSharedYMax([payload]);
    // Flow-overlay: overlay spending + GI-floor on the deterministic
    // single chart. shouldOverlayFlows() gates it to Deterministic single — stress
    // runs through renderSingle too, but it returns false there (simMode "stress").
    renderPanel("a", payload, yMax, shouldOverlayFlows());
  }
  // Buffer-survival readout (bucket-strategy slice 1a): in Deterministic mode the
  // deterministic trajectory carries no buffer-survival block (no down years), so
  // this surfaces the "switch to Historical/MC" caveat under buckets and stays
  // hidden under single. (Historical/MC have their own call in renderHistoricalSingle.)
  updateBufferSurvivalReadout(payload);
  // Effective-equity glidepath (buckets-only): present in all three modes (a
  // property of the setup, off the deterministic reference); the deterministic
  // payload carries it here too. Hidden under single.
  updateEffectiveEquityReadout(payload);
  return { payloadA: payload, payloadB: null };
}

// ---------- Return Variability: Historical mode render ----------
//
// Builds 1–2 shipped the single-scenario fan: read Scenario A's inputs, call
// /api/trajectory?mode=historical, draw the median + 10–90 band (+ inner 25–75)
// and a descriptive readout. Build 3 makes Historical work through the SAME
// two-scenario comparison the deterministic path uses: when isComparing() it
// runs the historical sim for BOTH scenarios (two ?mode=historical calls) and
// renders two fans — overlay (default) or side-by-side via the shared toggle.

function destroyHistoricalChart(key) {
  if (historical[key]) {
    historical[key].destroy();
    historical[key] = null;
  }
}

// Tear down ALL historical chart instances (single + overlay + the side pair).
// Used when leaving Historical mode and when switching historical sub-view, so
// no Chart.js instance leaks and each canvas is free to re-init.
function destroyAllHistoricalCharts() {
  for (const key of ["single", "overlay", "sideA", "sideB", "spendingFan"]) {
    destroyHistoricalChart(key);
  }
}

// Median-ending + 10–90 ending-range fragment, e.g. "median $9.1m (10–90:
// $6.2m–$13m)". Shared by the single readout and the per-scenario comparison
// readouts.
function historicalEndingFragment(payload) {
  const bands = payload.bands;
  const endMedian = bands.length ? bands[bands.length - 1].median : 0;
  const range = payload.ending_range || { p10: 0, p90: 0 };
  return (
    `median ${fmtMillions(endMedian)} ` +
    `(10–90: ${fmtMillions(range.p10)}–${fmtMillions(range.p90)})`
  );
}

// The honest, NON-failure depletion sentence/clause for one scenario. `terse`
// (comparison readouts) drops the framing for a compact "N of M depleted" /
// "none depleted" that fits two scenarios on one line; the full form is used by
// the single-scenario readout.
//
// Monte Carlo (payload.mode === "monte_carlo") phrases it as the brief's
// descriptive sentence: "X% of N simulated retirements reached zero before age
// A; earliest at age A" — N is the trial count, framed as "simulated" not
// "historical (since YEAR)". Historical keeps its "historical retirements (since
// YEAR)" framing. Both are descriptive (no success/failure language).
function historicalDepletionText(payload, { terse = false } = {}) {
  const { count, earliest_age } = payload.depletion;
  const endAge = payload.scenario.end_age;

  if (payload.mode === "monte_carlo") {
    const n = payload.trial_count;
    const pct = n ? ((count / n) * 100).toFixed(1) : "0.0";
    if (terse) {
      return count === 0
        ? "none depleted"
        : `${pct}% depleted (earliest age ${earliest_age})`;
    }
    if (count === 0) {
      return `None of ${n.toLocaleString("en-US")} simulated retirements reached zero before age ${endAge}.`;
    }
    return (
      `${pct}% of ${n.toLocaleString("en-US")} simulated retirements reached zero ` +
      `before age ${endAge}; earliest at age ${earliest_age}.`
    );
  }

  const n = payload.cohort_count;
  const since = payload.start_year;
  if (terse) {
    return count === 0
      ? "none depleted"
      : `${count} of ${n} depleted (earliest age ${earliest_age})`;
  }
  if (count === 0) {
    return `None of ${n} historical retirements (since ${since}) reached zero before age ${endAge}.`;
  }
  return (
    `${count} of ${n} historical retirements (since ${since}) reached zero ` +
    `before age ${endAge}; earliest at age ${earliest_age}.`
  );
}

// The "testable since YYYY" window-policy clause (asset-class design D3 /
// deliverable 7). Historical multi-class runs carry `testable_since` (the
// earliest replayable cohort start year) + `cohort_count`; when a short-history
// class shortens the window past the 1871 default, this surfaces it so the user
// knows the replay is over a narrower window. Monte Carlo has no replay window
// (returns are sampled), so it returns no clause. Empty string when the field is
// absent (older payloads / deterministic).
function testableSinceClause(payload) {
  if (payload.mode === "monte_carlo") return "";
  const since = payload.testable_since;
  if (since === undefined || since === null) return "";
  const n = payload.cohort_count;
  return `Testable since ${since}${n ? ` (${n} historical windows)` : ""}. `;
}

// Compose the single-scenario descriptive readout (Builds 1–2): median ending +
// 10–90 ending range, then the full depletion sentence. Slice 4b prepends the
// "testable since YYYY" window-policy clause for Historical runs.
function historicalReadout(payload) {
  const bands = payload.bands;
  const endMedian = bands.length ? bands[bands.length - 1].median : 0;
  const range = payload.ending_range || { p10: 0, p90: 0 };
  return (
    testableSinceClause(payload) +
    `Median ends ~${fmtMillions(endMedian)} ` +
    `(10th–90th percentile: ${fmtMillions(range.p10)}–${fmtMillions(range.p90)}). ` +
    historicalDepletionText(payload)
  );
}

// One scenario's compact comparison readout, e.g.
//   "A: median $9.1m (10–90: $6.2m–$13m) · none depleted".
function historicalScenarioReadout(label, payload) {
  return `${label}: ${historicalEndingFragment(payload)} · ${historicalDepletionText(payload, { terse: true })}`;
}

// The percentile-fan datasets for ONE scenario, in load-bearing bottom→top order
// (Chart.js fills each band DOWN to the dataset one index below via `fill:'-1'`):
//   p10 (invisible floor) → p90 (fills to p10: the 10–90 band) →
//   [p25 (invisible) → p75 (fills to p25: inner band) when includeInner] →
//   median (solid line on top).
// `rgb` is the scenario colour's "r, g, b" triple; `border` its solid hex (for
// the median line). `bandAlpha` lets the overlay use a higher alpha so two
// translucent bands blend to a readable purple. `includeInner` adds the 25–75
// band (single + side-by-side: on; overlay: OFF, per the brief — two inner bands
// are too busy). Returns { datasets, medianIndex } so the tooltip can target the
// right median dataset (its index depends on whether the inner band is present).
// Slice 6: the percentile-band datasets live on ChartView. This thin wrapper
// pins the median line width to the app-wide LINE_WIDTH so single + fan
// medians read the same stroke. See chart_view.js for the load-bearing
// band-fill chain.
function fanDatasets(bands, opts) {
  return ChartView.fanDatasets(bands, { ...opts, lineWidth: LINE_WIDTH });
}

// Build 2: the single-scenario FAN — median + 10–90 band + inner 25–75, all in
// Scenario-A blue. Tooltip (Fix 3) shows Portfolio + Spending + Guaranteed income
// at the hovered age (plus the portfolio 10–90), off the median dataset. `scenario`
// (the echoed payload scenario) supplies the GI streams + the constant/lifecycle
// spend; `spendingBands` (guardrails-only) supplies the spending median per age.
// `yMax` (optional) pins a shared Y top (unused in single, present for API symmetry
// with the comparison fans).
//
// `flowOverlayOpts` (slice 2): when supplied, overlay the deterministic
// flows on a secondary right-hand axis — `{ flows, spending }` where `flows` is the
// payload's per-age {age, spending, guaranteed_income} list and `spending` gates the
// spending line (constant/lifecycle only; GI floor always). Omitted (the default)
// keeps the fan single-axis and BYTE-IDENTICAL — the comparison overlay / side-by-
// side fans never pass it, and never gain the right axis.
//
// `federalTax` (tax-drag design td1, DECISION 7): the optional aggregate
// payload.federal_tax block, present iff the tax-location split is active
// (single-scenario only — this function has exactly one caller,
// renderHistoricalSingle, so threading it here never reaches the comparison
// fans). Adds the gross-outflow-median decomposition to the SAME tooltip the
// portfolio fan already shows (portfolioFanTooltipRows) — the one hist/MC
// surface that carries a "Spending" row for constant/lifecycle too (the
// dedicated spending fan, renderSpendingFanChart, is dynamic-spending-only).
function renderHistoricalChart(bands, yMax, scenario, spendingBands, flowOverlayOpts = null, federalTax = null) {
  const canvas = document.getElementById("historical-chart");
  destroyHistoricalChart("single");
  const { datasets, medianIndex } = fanDatasets(bands, {
    rgb: RGB_A(),
    border: LINE_A(),
    bandAlpha: 0.15,
    includeInner: true,
    medianLabel: "Median (real $, end of year)",
  });
  const options = {
    yMax,
    yTickFormatter: (v) => fmtMillions(v),
    tooltipFilter: (item) => item.datasetIndex === medianIndex,
    tooltipLabel: (item) => portfolioFanTooltipRows(
      bands[item.dataIndex],
      bands[item.dataIndex].age,
      scenario,
      spendingBands ? spendingBands[item.dataIndex] : null,
      federalTax,
    ),
  };
  if (flowOverlayOpts && flowOverlayOpts.flows && flowOverlayOpts.flows.length) {
    const overlay = SpendingSeries.flowOverlay(flowOverlayOpts.flows, {
      spending: !!flowOverlayOpts.spending,
    });
    datasets.push(...overlay.datasets);
    options.secondaryYAxis = overlay.secondaryYAxis;
    // Show the legend so the overlay lines read by name (the bare fan kept it off).
    options.legend = { display: true, position: "top" };
    // The fan tooltip runs index-mode and surfaces ONE row off the median dataset
    // (tooltipFilter above). The overlay datasets ride the secondary "yFlows" axis
    // and must NOT add duplicate rows — mirror slice 1 by excluding any yFlows
    // dataset on top of the median-only filter (defence-in-depth: the median filter
    // already excludes them, but the explicit yAxisID guard keeps the intent local
    // and survives any future filter change).
    options.tooltipFilter = (item) =>
      item.datasetIndex === medianIndex && item.dataset.yAxisID !== "yFlows";
  }
  historical.single = ChartView.fanChart(canvas, {
    labels: bands.map((p) => p.age),
    datasets,
    options,
  });
}

// Variable-spending design slice 1: the single-scenario SPENDING fan (guardrails
// mode only). Same percentile-fan datasets the portfolio fan uses (median + 10–90
// + inner 25–75, Scenario-A blue), but the Y axis is plain real dollars (spending
// lives in the $45k–$150k rails, not the millions the portfolio reaches) and the
// median legend/tooltip read "spending". `spending_bands` carries the same
// {age, p10, p25, median, p75, p90} shape as the portfolio `bands`, so fanDatasets
// is reused verbatim.
// The reference-line levels drawn on the spending fan (variable-spending design
// acceptance fix). TWO lines:
//   - the OPERATIONAL floor at the current `spending_floor` knob (default $85k,
//     follows the knob) — "we don't dip below this", the clamp; prominent.
//   - the ABSOLUTE / survivable floor at $45k — the viable-but-modest
//     level guaranteed income roughly reaches; context, drawn fainter beneath it.
// $45k is a fixed structural level (GI-backed), not a knob, so it's a constant.
const ABSOLUTE_FLOOR = 45000;

// Slice 6: reference-line dataset lives on ChartView. ChartView guarantees
// fill:false so the line never participates in the fan's band-fill chain
// — a reference line that filled to the median would be a serious regression.
// The reference lines stay OUT of the index tooltip — the chart's top-level
// tooltip filter restricts the tooltip to the median dataset only (see
// renderSpendingFanChart), so these flat guide lines never surface a row.
const refLineDataset = ChartView.refLineDataset;

// SpendingSeries — the per-series PRESENTATION helper for the spending chart(s).
//
// dsc1 sub-slice A (refactor-first, ZERO behavior change): the chart SHAPE lives
// in ChartView (lineChart/fanChart/fanDatasets/refLineDataset/...); the spending
// SERIES SEMANTICS — color/border, the $-thousands axis title + tick formatter,
// the two guide-line datasets (operational floor off the live knob + the $45k
// absolute/survivable floor), and the tooltip rows — live HERE so a second chart
// shape (the deterministic guardrails spending line, sub-slice B) can reuse them
// instead of copy-pasting the fan's inline glue.
//
// The fan (renderSpendingFanChart) consumes guideLines()/axisOptions() and keeps
// its OWN band tooltip (spendingFanTooltipRows: median/p10/p90/GI). The
// single-value tooltip (singleValueTooltipRows: spending + GI) is exposed here for
// sub-slice B's single line and is intentionally NOT wired into anything yet.
const SpendingSeries = {
  // The spending-line color/border (Scenario-A blue) + the median-band label the
  // fan uses. The deterministic single line (sub-slice B) will want lineLabel.
  // GETTERS (not baked values): this object lives at module scope, so reading the
  // token via a getter defers the cssVar() read to render time (when lineConfig
  // dereferences this.rgb / this.border), never caching a load-time value.
  get rgb() { return RGB_A(); },
  get border() { return LINE_A(); },
  medianLabel: "Median spending (real $)",
  lineLabel: "Spending (real $)",

  // Flow-overlay colors — read live off the tokens so the theme toggle
  // re-tints them each render. Warm orange = actual annual spending; teal/green =
  // the guaranteed-income floor. GETTERS so the cssVar() read defers to render time.
  get spendingLineColor() { return cssVar("--chart-spending-line"); },
  get giFloorColor() { return cssVar("--chart-gi-floor"); },

  // The absolute/survivable floor level ($45k, GI-backed) — a fixed structural
  // level, not a knob. Kept in sync with the module-level ABSOLUTE_FLOOR const.
  absoluteFloor: ABSOLUTE_FLOOR,

  // The two reference-line datasets the spending chart draws, built for an
  // arbitrary point-count `n`:
  //   - operational floor (amber dashed [6,4]),
  //   - absolute/survivable floor at $45k (grey dashed [3,4]).
  // Appended AFTER the fan/line datasets so they draw on top; their dataset
  // indices never affect the median tooltip filter (the filter keys off the
  // median index, and these flat guide lines carry no surfaced tooltip row).
  //
  // The operational floor LEVEL: by default the LIVE spending_floor knob (read
  // fresh each call so a user-edited floor moves the line). Under a STRESS that
  // scales the rails (cost_of_living_shock ×1.20), the run clamps against the PERTURBED
  // floor, not the live knob — so callers pass `effectiveFloor` (the payload's
  // `effective_spending_floor`) to draw + LABEL the line at the value the
  // spending line actually floors against. For Family-A stresses + ss_haircut the
  // effective floor equals the knob, so passing it is a no-op there (the guide
  // line stays correct, as it already was). When `effectiveFloor` is omitted
  // (the non-stress fan / line) the live knob is used exactly as before.
  // `ceiling` (amortization-frontier design decision 5): when supplied, also draw the
  // SATIATION CEILING as a reference line (amber dashed, like the floor). The
  // amortization frontier is UNCLAMPED — the floor + ceiling are reference lines the
  // frontier line is FREE to cross (above the ceiling → over-funded headroom; below
  // the floor → underfunded), so the diagnostic the frontier exists for survives.
  // The clamped strategies never pass it (their spend physically can't exceed the
  // ceiling, so a ceiling guide would be redundant). Omitted → today's two floor
  // lines exactly.
  guideLines(n, { effectiveFloor, ceiling } = {}) {
    const operFloor = effectiveFloor != null
      ? effectiveFloor
      : readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor);
    const lines = [
      refLineDataset(`Floor (${fmtK(operFloor)})`, operFloor, n, cssVar("--chart-floor-operational"), [6, 4]),
      refLineDataset(`Absolute floor (survivable, ${fmtK(ABSOLUTE_FLOOR)})`, ABSOLUTE_FLOOR, n, cssVar("--chart-floor-absolute"), [3, 4]),
    ];
    if (ceiling != null) {
      lines.push(
        refLineDataset(`Ceiling (${fmtK(ceiling)})`, ceiling, n, cssVar("--chart-ceiling"), [6, 4]),
      );
    }
    return lines;
  },

  // The satiation-ceiling level for the amortization frontier's reference line: the
  // LIVE spending_ceiling knob (default $150k), read fresh so an edit moves the line.
  // Only the frontier draws it (it's the lone unclamped strategy).
  ceilingLevel() {
    return readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling);
  },

  // The spending y-axis pieces: plain real dollars in the $45k–$150k rails (not
  // the millions the portfolio reaches), formatted as $-thousands.
  yAxisTitle: "Spending (real $)",
  yTickFormatter: (v) => fmtK(v),
  axisOptions() {
    return { yAxisTitle: this.yAxisTitle, yTickFormatter: this.yTickFormatter };
  },

  // Single-value tooltip for the deterministic spending LINE (sub-slice B): the
  // one spending value at the hovered age + that age's guaranteed income (the
  // guardrail rule acts on spending-net-of-GI, so GI context matters). Mirrors the
  // alignment of the fan's band tooltip.
  singleValueTooltipRows(spending, age, scenario) {
    return alignMoneyRows([
      ["Spending", spending],
      ["Guaranteed income", guaranteedIncomeAtAge(scenario, age)],
    ]);
  },

  // Pure config builder for the DETERMINISTIC guardrails spending LINE (dsc1
  // sub-slice B). DOM-free (mirrors TaxLocationView.fanConfig) so it's unit-testable
  // in a node:vm sandbox without Chart.js: it returns the { labels, datasets,
  // options } that renderDeterministicSpendingChart instantiates on the
  // det-spending-chart canvas. Composition:
  //   - ONE solid spending line (dataset index 0, Scenario-A blue, fill:false) from
  //     the deterministic points' per-year spending (the REALIZED, fundable-capped
  //     consumption for a split-active run — DECISION 2/td-c),
  //   - the GROSS-OUTFLOW line (tax-drag design td1, DECISION 7 — retires the ft2
  //     net-of-tax line), drawn ABOVE spending, appended right after the spending
  //     line, when the caller supplies opts.federalTax (payload.federal_tax;
  //     present iff the tax-location split is active, DECISION 3): "what the
  //     portfolio actually funds" = spending + tax + penalty, so the GAP between
  //     this line and the spending line, per age, is the tax bill,
  //   - the SAME two guide lines the fan draws (guideLines(n)) appended AFTER, so
  //     they draw on top and never carry a tooltip row.
  // The tooltip is restricted to the spending line (datasetIndex 0) + the gross-
  // outflow line (when present); the flat guide lines surface no row. The y-axis
  // uses axisOptions() ($-thousands, "Spending (real $)"). Single-scenario (no
  // A/B) — the line is the central deterministic path. guideLines reads the floor
  // knob LIVE, so an edited floor moves the line — UNLESS `opts.effectiveFloor` is
  // supplied (the stress path), where the floor guide is drawn + labelled at the
  // perturbed rail the run actually clamps to. Stress runs pay tax the same
  // honest way (td1): opts.federalTax rides through unchanged, so a stressed
  // trajectory gets the SAME gross-outflow decomposition as any other run.
  lineConfig(points, scenario, opts = {}) {
    const ages = points.map((p) => p.age);
    const spend = points.map((p) => p.spending);
    const datasets = [
      {
        label: this.lineLabel,
        data: spend,
        borderColor: this.border,
        backgroundColor: `rgba(${this.rgb}, 0.08)`,
        fill: false,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: LINE_WIDTH,
      },
    ];
    // Gross-outflow line (tax-drag design td1, DECISION 7). Age-keyed against
    // THIS chart's own `ages` (not index-zipped against opts.federalTax.ages) so
    // a mismatched age range degrades to per-point gaps rather than a silently
    // misaligned overlay. The tooltip decomposes the gap into its three-way
    // attribution (DECISION 6): spending-attributed tax (spending_tax, which
    // already includes any early-withdrawal penalty by convention — surfaced as
    // its own qualifier row when nonzero), voluntary tax on the bracket-fill
    // lever, and forced tax on RMD excess — voluntary/forced/penalty rows appear
    // ONLY when actually nonzero (lever and RMD never co-occur in practice —
    // DECISION 6 — so at most one of the two ever fires). Forced/voluntary/
    // penalty values are display-guarded max(0, x): the engine deliberately
    // doesn't clamp the attribution split, so sub-ULP negatives are possible
    // (reviewer-mandated guard lives here in the web).
    let grossIndex = -1;
    let grossByIdx = null;
    let spendTaxByIdx = null;
    let voluntaryTaxByIdx = null;
    let forcedTaxByIdx = null;
    let penaltyByIdx = null;
    if (opts.federalTax && Array.isArray(opts.federalTax.ages)) {
      const ft = opts.federalTax;
      const rowForAge = new Map(ft.ages.map((age, i) => [age, i]));
      const byAge = (series) => ages.map((age) => {
        const i = rowForAge.get(age);
        return i === undefined ? null : series[i];
      });
      grossByIdx = byAge(ft.gross_outflow);
      spendTaxByIdx = byAge(ft.spending_tax);
      voluntaryTaxByIdx = byAge(ft.voluntary_tax);
      forcedTaxByIdx = byAge(ft.forced_tax);
      penaltyByIdx = byAge(ft.penalty);
      grossIndex = datasets.length;
      datasets.push({
        label: "What the portfolio funds (spending + tax)",
        data: grossByIdx,
        borderColor: cssVar("--chart-tax-gross-line"),
        backgroundColor: cssVar("--chart-tax-gross-line"),
        fill: false,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: LINE_WIDTH,
      });
    }
    datasets.push(...this.guideLines(points.length, {
      effectiveFloor: opts.effectiveFloor,
      ceiling: opts.ceiling,
    }));
    const { yAxisTitle, yTickFormatter } = this.axisOptions();
    return {
      labels: ages,
      datasets,
      options: {
        // Legend labels the spending line + the gross-outflow line (when
        // present) + the two floor guide lines; all carry a visible stroke
        // (borderWidth > 0), so no filter is needed.
        legend: { display: true, position: "top" },
        tooltipDisplayColors: false,
        // The spending line (index 0) and the gross-outflow line (when present)
        // are the tooltip anchors; the flat guide lines never surface a row. A
        // null gross-outflow point (mismatched-age edge case) is excluded too,
        // so tooltipLabel below never has to guard a null value. interaction
        // "index" fires the BODY callback once per visible dataset at the
        // hovered point — this filter restricts the body to those datasets so
        // each surfaces EXACTLY ONE row, not once per guide line.
        tooltipFilter: (item) =>
          item.datasetIndex === 0 ||
          (item.datasetIndex === grossIndex && grossByIdx[item.dataIndex] != null),
        tooltipLabel: (item) => {
          if (item.datasetIndex === grossIndex) {
            const i = item.dataIndex;
            const rows = [
              ["Portfolio funds (spending + tax)", grossByIdx[i]],
              ["Spending", spend[i]],
              ["Federal tax — spending", spendTaxByIdx[i]],
            ];
            const voluntary = Math.max(0, Number(voluntaryTaxByIdx[i]) || 0);
            if (voluntary > 0) {
              rows.push(["Federal tax — voluntary (bracket-fill lever)", voluntary]);
            }
            const forced = Math.max(0, Number(forcedTaxByIdx[i]) || 0);
            if (forced > 0) {
              rows.push(["Federal tax — forced (RMD, not a spending cut)", forced]);
            }
            const penalty = Math.max(0, Number(penaltyByIdx[i]) || 0);
            if (penalty > 0) {
              rows.push(["Includes early-withdrawal penalty", penalty]);
            }
            return alignMoneyRows(rows);
          }
          return this.singleValueTooltipRows(spend[item.dataIndex], ages[item.dataIndex], scenario);
        },
        yAxisTitle,
        yTickFormatter,
      },
    };
  },

  // Pure config builder for the Historical/MC spending FAN's gross-outflow-
  // median line (tax-drag design td1, DECISION 7 — retires the ft2 net-of-tax
  // median line). DOM-free (mirrors lineConfig's identical discipline on the
  // deterministic twin, just cssVar() for the color) so it's unit-testable
  // without Chart.js: renderSpendingFanChart calls this, pushes `.dataset`,
  // and calls `.tooltipRows(i, spendMedian)` when the hovered point is this
  // line. Returns null when `federalTax` is absent/malformed, so the caller
  // omits the dataset entirely (byte-identical to no split active).
  //
  // `tooltipRows(i, spendMedian)` takes the ALREADY-RESOLVED median spend at
  // index i (spendingBands[i].median — the spending fan's own median, which
  // is a DYNAMIC-spending-only surface, so this is always sourced that way,
  // never client-reconstructed here) rather than deriving it: gross_outflow
  // median − tax median != spend median in general (the three medians can be
  // picked from different paths' realizations), so "Spending, median" is
  // whatever the caller already resolved, not computed by subtraction here.
  grossOutflowMedianDataset(spendingBands, federalTax) {
    if (!federalTax || !Array.isArray(federalTax.ages)) return null;
    const rowForAge = new Map(federalTax.ages.map((age, i) => [age, i]));
    const byAge = (series) => spendingBands.map((p) => {
      const i = rowForAge.get(p.age);
      return i === undefined ? null : series[i];
    });
    const grossByIdx = byAge(federalTax.gross_outflow_median);
    const taxByIdx = byAge(federalTax.tax_median);
    const voluntaryByIdx = byAge(federalTax.voluntary_tax_median);
    const forcedByIdx = byAge(federalTax.forced_tax_median);
    return {
      dataset: {
        label: "Portfolio funds, median (spending + tax)",
        data: grossByIdx,
        borderColor: cssVar("--chart-tax-gross-line"),
        backgroundColor: cssVar("--chart-tax-gross-line"),
        fill: false,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: LINE_WIDTH,
      },
      grossByIdx,
      tooltipRows(i, spendMedian) {
        const rows = [
          ["Portfolio funds, median", grossByIdx[i]],
          ["Spending, median", spendMedian],
          ["Federal tax, median", taxByIdx[i]],
        ];
        const voluntary = Math.max(0, Number(voluntaryByIdx[i]) || 0);
        if (voluntary > 0) {
          rows.push(["Voluntary (bracket-fill lever), median", voluntary]);
        }
        const forced = Math.max(0, Number(forcedByIdx[i]) || 0);
        if (forced > 0) {
          rows.push(["Forced (RMD, not a spending cut), median", forced]);
        }
        return alignMoneyRows(rows);
      },
    };
  },

  // Flow-overlay builder (slice 1). For the DETERMINISTIC single
  // trajectory chart (#trajectory-chart-a), build the two "annual flows" series
  // that ride a SECONDARY right-hand y-axis alongside the portfolio line:
  //   - Annual spending — SOLID (the per-year ACTUAL spend; falls onto the GI
  //     floor once the portfolio depletes, the "forced onto income" moment),
  //   - Guaranteed-income floor — DASHED.
  // Both pulled per-point off `spending` / `guaranteed_income`. Returns the
  // dataset(s) (each tagged with `yAxisID` so they bind to the right axis) plus the
  // `secondaryYAxis` config ChartView.lineChart / fanChart consumes ("Annual flows
  // ($/yr)", ticks as $Nk, suggestedMax = max spending × 1.2 for headroom). DOM-free
  // + theme-aware (colors via the live tokens) so it's unit-testable in a vm sandbox.
  //
  // The portfolio (millions) keeps the existing LEFT axis; spending/GI (~$0–260k)
  // would be a flat line pinned to the floor there, hence the dedicated right ruler.
  //
  // `spending` (slice 2): the GI-floor dataset is ALWAYS emitted; the
  // spending dataset only when `spending` is true. The deterministic chart (slice 1)
  // calls flowOverlay(points) with the default → BOTH datasets, byte-identical to
  // before. The fan caller passes { spending: spendingMode === "constant" ||
  // spendingMode === "lifecycle" }: under those modes spending is a deterministic
  // function of age (one honest line); under guardrails / FR / amortization spending
  // is path-dependent (a separate spending fan exists), so only the GI floor is drawn.
  FLOWS_AXIS_ID: "yFlows",
  flowOverlay(points, { spending = true } = {}) {
    const spend = points.map((p) => p.spending);
    const gi = points.map((p) => p.guaranteed_income);
    const maxSpend = spend.reduce((m, v) => (v > m ? v : m), 0);
    const datasets = [];
    if (spending) {
      datasets.push({
        label: "Annual spending",
        data: spend,
        yAxisID: this.FLOWS_AXIS_ID,
        borderColor: this.spendingLineColor,
        backgroundColor: this.spendingLineColor,
        borderWidth: 2,
        fill: false,
        tension: 0.15,
        pointRadius: 0,
      });
    }
    datasets.push({
      label: "Guaranteed-income floor",
      data: gi,
      yAxisID: this.FLOWS_AXIS_ID,
      borderColor: this.giFloorColor,
      backgroundColor: this.giFloorColor,
      borderWidth: 2,
      borderDash: [6, 4],
      fill: false,
      tension: 0.15,
      pointRadius: 0,
    });
    return {
      datasets,
      secondaryYAxis: {
        id: this.FLOWS_AXIS_ID,
        title: "Annual flows ($/yr)",
        suggestedMax: maxSpend * 1.2,
        tickFormatter: (v) => `$${Math.round(v / 1000)}k`,
      },
    };
  },
};
// Exposed for the node:vm test (tests/test_spending_series.mjs); not a stable API.
window.SpendingSeries = SpendingSeries;

// `federalTax`: the optional aggregate payload.federal_tax block (tax-drag
// design td1, DECISION 7) — present iff the tax-location split is active (same
// gate as tax_location_risk, DECISION 3). When supplied, an additional
// gross-outflow-median line is drawn from its gross_outflow_median series
// (spending fan is a DYNAMIC-spending-only surface — guardrails / funded-ratio
// / amortization — so `spendingBands` here IS the median-of-paths spend read
// DECISION 7 calls for), age-keyed against this fan's own ages (not
// index-zipped — mirrors SpendingSeries.lineConfig's identical discipline on
// the deterministic twin).
function renderSpendingFanChart(spendingBands, scenario, federalTax) {
  const canvas = document.getElementById("spending-fan-chart");
  destroyHistoricalChart("spendingFan");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const { datasets, medianIndex } = fanDatasets(spendingBands, {
    rgb: RGB_A(),
    border: LINE_A(),
    bandAlpha: 0.15,
    includeInner: true,
    medianLabel: "Median spending (real $)",
  });
  // Reference lines: the OPERATIONAL floor at the current spending_floor knob
  // (prominent, amber dashed — default $85k, follows the knob) + the ABSOLUTE /
  // survivable floor at $45k (fainter, grey dashed). Owned by SpendingSeries so the
  // deterministic spending line reuses the identical pair. Appended AFTER the fan
  // datasets so they draw on top; their dataset indices don't affect the median
  // tooltip filter. SpendingSeries.guideLines reads the floor live off the input so
  // the line tracks a user-edited floor.
  const n = spendingBands.length;
  const { yAxisTitle, yTickFormatter } = SpendingSeries.axisOptions();
  // The amortization frontier is UNCLAMPED, so it ALSO draws the $150k satiation
  // ceiling as a reference line the fan is free to cross (over-funded headroom). The
  // clamped strategies omit it (their spend can't exceed the ceiling). Read the
  // ceiling live off the knob so an edit moves the line.
  const ceiling = spendingMode === "amortization" ? SpendingSeries.ceilingLevel() : undefined;
  datasets.push(...SpendingSeries.guideLines(n, { ceiling }));
  // Gross-outflow-median line (tax-drag design td1, DECISION 7 — retires the
  // ft2 net-of-tax median line), appended AFTER the guide lines (so it draws on
  // top, like they do) whenever the aggregate federal_tax block is present:
  // "what the portfolio actually funds," median across paths. The dataset +
  // tooltip-row logic live in the pure, unit-tested SpendingSeries.
  // grossOutflowMedianDataset (mirrors lineConfig's identical discipline on the
  // deterministic twin) — this thin DOM wrapper just wires it into the fan.
  const grossCfg = SpendingSeries.grossOutflowMedianDataset(spendingBands, federalTax);
  let grossMedianIndex = -1;
  if (grossCfg) {
    grossMedianIndex = datasets.length;
    datasets.push(grossCfg.dataset);
  }
  historical.spendingFan = ChartView.fanChart(canvas, {
    labels: spendingBands.map((p) => p.age),
    datasets,
    options: {
      // Show the legend so the reference lines (median + floor + absolute floor)
      // are labelled. The fan's own p10/p25/p75/p90 datasets carry zero-width
      // transparent borders; the filter hides them so only visible-stroke
      // datasets surface a legend entry.
      legend: {
        display: true,
        labels: {
          filter: (item, data) => data.datasets[item.datasetIndex].borderWidth > 0,
        },
      },
      tooltipFilter: (item) =>
        item.datasetIndex === medianIndex ||
        (item.datasetIndex === grossMedianIndex && grossCfg.grossByIdx[item.dataIndex] != null),
      tooltipLabel: (item) => {
        if (item.datasetIndex === grossMedianIndex) {
          // The median spend read here (spendingBands[i].median) is the SAME
          // guardrails/FR spend-fan median the base tooltip shows — never
          // derived by subtracting gross_outflow_median − tax_median, since
          // median(gross) − median(tax) != median(spend) (the three medians
          // come from potentially DIFFERENT paths' realizations, not one
          // path's numbers). There is NO per-age penalty median in the
          // aggregate payload (FederalTaxOverlayAggregate carries no penalty
          // series), so unlike the deterministic tooltip this one can't call
          // out the penalty separately.
          return grossCfg.tooltipRows(item.dataIndex, spendingBands[item.dataIndex].median);
        }
        return spendingFanTooltipRows(
          spendingBands[item.dataIndex],
          spendingBands[item.dataIndex].age,
          scenario,
        );
      },
      yAxisTitle,
      yTickFormatter,
    },
  });
}

// Show or hide + (re)draw the single-scenario spending fan. Visible ONLY in
// guardrails spending mode with a payload that carries `spending_bands`; otherwise
// the wrap is hidden and any stale chart is torn down. Keeps the spending fan
// strictly additive — Constant/Lifecycle never see it.
function updateSpendingFan(payload) {
  const wrap = document.getElementById("spending-fan-wrap");
  if (!wrap) return;
  const bands = payload && !payload.error ? payload.spending_bands : null;
  if (isDynamicSpending() && bands && bands.length) {
    wrap.hidden = false;
    // Mode-aware framing: Monte Carlo trials are synthetic (no calendar year), so
    // the fan reads "across 10,000 simulated retirements" rather than "across every
    // historical cohort" (variable-spending design slice 3 — keep the MC read clean).
    const isMC = simMode === "monte_carlo";
    const title = document.getElementById("spending-fan-title");
    const note = document.getElementById("spending-fan-note");
    if (title) {
      title.textContent = isMC
        ? "Spending across simulated futures (real $)"
        : "Spending across history (real $)";
    }
    if (note) {
      const across = isMC
        ? "across 10,000 simulated retirements"
        : "across every historical cohort";
      let lead;
      if (spendingMode === "fr_smooth") {
        lead =
          "Under the funded-ratio rule, spending eases toward full funding " +
          "each year: an underfunded portfolio is trimmed toward the floor, " +
          "an overfunded one is raised toward the ceiling, damped so the " +
          "changes stay smooth.";
      } else if (spendingMode === "fr_guardrail") {
        lead =
          "Under the funded-ratio bands, spending holds while funding stays " +
          "within ±20% of full, then nudges ±10%: an underfunded portfolio is " +
          "cut toward the floor, an overfunded one is raised toward the ceiling.";
      } else if (spendingMode === "fr_tiers") {
        lead =
          "Under the funded-ratio tiers, spending steps through a ladder by how " +
          "well-funded the portfolio is (FLOOR $85k → MAX $160k go-go), with " +
          "hysteresis so it doesn't flip tiers on noise, a crash brake, and a hard " +
          "floor that pins spending to FLOOR when the portfolio runs low.";
      } else {
        lead =
          "Under guardrails, spending bends with the market: a bad sequence " +
          "forces cuts toward the floor, a good one allows raises toward the " +
          "ceiling.";
      }
      note.textContent =
        `${lead} The band shows the 10th–90th percentile of actual ` +
        `spending at each age ${across}.`;
    }
    renderSpendingFanChart(bands, payload.scenario, payload.federal_tax);
  } else {
    wrap.hidden = true;
    destroyHistoricalChart("spendingFan");
  }
}

// ---------- Deterministic guardrails spending LINE (dsc1 sub-slice B) ----------
//
// The deterministic analog of the spending fan. Historical/MC draw a guardrails
// spending FAN (the spread of the Guyton-Klinger spend across paths); deterministic
// mode has a single return path, so it draws a single guardrails spending LINE —
// the one spending trajectory that pairs with the portfolio line already shown.
// You can see the prosperity raises and late-life cuts on the central case, plus the
// floor lines, but there's NO sequence-of-returns spread (one smooth path).
//
// Reuses SpendingSeries (guideLines / axisOptions / singleValueTooltipRows) via the
// DOM-free SpendingSeries.lineConfig builder; this thin wrapper grabs the canvas +
// instantiates the chart, mirroring renderTaxLocationFan / renderSpendingFanChart.

function destroyDetSpendingChart() {
  if (detSpending.chart) {
    detSpending.chart.destroy();
    detSpending.chart = null;
  }
}

function renderDeterministicSpendingChart(points, scenario, opts = {}) {
  const canvas = document.getElementById("det-spending-chart");
  destroyDetSpendingChart();
  if (!canvas) return;
  const cfg = SpendingSeries.lineConfig(points, scenario, opts);
  detSpending.chart = ChartView.lineChart(canvas, cfg);
}

// Show/draw or hide/tear-down the deterministic spending line from a payload,
// mirroring updateSpendingFan's discipline. Shown in TWO single-trajectory cases:
//   - Deterministic mode + a DYNAMIC spending strategy (the original dsc1 case —
//     the guardrails/FR cut-and-raise path that pairs with the portfolio line); or
//   - Stress mode (any spending strategy). A stress is one specified adverse world
//     producing ONE trajectory, and the spending-level chart was
//     originally missing under stress — seeing spending forced DOWN under the shock (for
//     guardrails/FR) is the valuable view, and even a flat constant line confirms
//     the spend held while the portfolio drew down. So under stress we draw the
//     line for EVERY spending strategy, not just the dynamic ones.
// Hidden + torn down otherwise (Historical/MC are owned by the fan; comparing is
// single-scenario-out-of-scope here, mirroring updateTaxLocationView).
function updateDeterministicSpending(payload) {
  const wrap = document.getElementById("det-spending-wrap");
  if (!wrap) return;
  const points = payload && !payload.error && payload.trajectory
    ? payload.trajectory.points
    : null;
  const deterministicDynamic = simMode === "deterministic" && isDynamicSpending();
  const active =
    (deterministicDynamic || isStressMode()) &&
    !isComparing() &&
    points &&
    points.length;
  if (active) {
    // The static HTML title reads "Spending under guardrails"; under stress the
    // strategy may be constant/lifecycle, so retitle to the neutral stress framing.
    const title = document.getElementById("det-spending-title");
    if (title) {
      if (isStressMode()) {
        title.textContent = "Spending under stress (real $)";
      } else if (spendingMode === "amortization") {
        title.textContent = "Amortization frontier (real $)";
      } else {
        title.textContent = "Spending under guardrails (real $)";
      }
    }
    wrap.hidden = false;
    // Under stress the run clamps against the EFFECTIVE (post-transform) floor —
    // cost_of_living_shock scales it ×1.20 — so draw the guide line at the payload's
    // effective_spending_floor, not the live knob (which would mislabel "Floor
    // ($85k)" 24k below where the line actually floors). For Family A + ss_haircut
    // the payload value equals the knob, so this is a no-op there. Outside stress
    // (deterministic dynamic spending) we pass nothing → guideLines uses the live
    // knob exactly as before. The echoed payload.scenario stays un-perturbed, so
    // the input knobs are never repainted (the overlay invariant).
    // The amortization frontier is UNCLAMPED, so it ALSO draws the $150k satiation
    // ceiling as a reference line the line is free to cross. Other dynamic modes
    // omit it (their spend can't exceed the ceiling). Read live off the knob.
    const opts = isStressMode()
      ? { effectiveFloor: payload.effective_spending_floor }
      : (spendingMode === "amortization" ? { ceiling: SpendingSeries.ceilingLevel() } : {});
    // Federal-tax overlay (ft1): the tax-adjusted net line rides alongside the
    // spending line whenever the split is active, in every spending mode/stress
    // combination this function already draws for — additive, no new gate.
    if (payload.federal_tax) opts.federalTax = payload.federal_tax;
    renderDeterministicSpendingChart(points, payload.scenario, opts);
  } else {
    wrap.hidden = true;
    destroyDetSpendingChart();
  }
}

// ---------- Adjustment events: cost-of-flexibility readout + worst-cohort drill ----------
//
// Variable-spending design slice 2. The spending fan (above) shows the SPREAD of
// spending; this surfaces the ADJUSTMENTS that produce it — the cost of
// flexibility. The aggregate readout reports frequency/timing/depth across all
// cohorts; the drill-down narrates the single most-squeezed cohort year-by-year
// with $ triggers (GI-driven raises labelled distinctly from market-driven ones).
// Guardrails + Historical (single view) only — additive to slice 1.

// Percent (0..1 fraction → "73%"). Rounds to whole percent for the headline read;
// the stat is about shape, not precision.
function fmtPct(frac) {
  return `${Math.round(frac * 100)}%`;
}

// A whole-number count of years, pluralised ("1 year" / "3 years"). The medians
// are integer-valued in practice (year counts), but guard a stray .5 by rounding.
function fmtYears(n) {
  const r = Math.round(n);
  return `${r} ${r === 1 ? "year" : "years"}`;
}

// One stat card: a big value over a small label, optionally with a one-line
// sub-note. Mirrors the compact, scannable style of the rest of the readouts.
function statCard(value, label, sub) {
  const card = document.createElement("div");
  card.className = "stat-card";
  const v = document.createElement("div");
  v.className = "stat-value";
  v.textContent = value;
  const l = document.createElement("div");
  l.className = "stat-label";
  l.textContent = label;
  card.appendChild(v);
  card.appendChild(l);
  if (sub) {
    const s = document.createElement("div");
    s.className = "stat-sub";
    s.textContent = sub;
    card.appendChild(s);
  }
  return card;
}

// Render the aggregate cost-of-flexibility stat cards from `adjustment_stats`.
// Frames each as the SHAPE of flexibility, not a pass/fail (design). The deepest
// cut is the p10 of each history's lowest year ("a typical bad case"); the
// global single-year minimum (`lowest_single_year_spend`) rides along, labelled
// distinctly so it can't be conflated with the drill-down cohort's "bottomed at"
// figure (that cohort is the lowest-TOTAL one, a different cohort with a higher
// own-minimum).
function renderAdjustmentStats(stats) {
  const container = document.getElementById("adjustment-stats");
  if (!container) return;
  container.innerHTML = "";

  container.appendChild(statCard(
    fmtPct(stats.pct_ever_cut),
    "of histories ever cut spending below your start",
  ));
  container.appendChild(statCard(
    fmtPct(stats.pct_ever_raised),
    "ever raised spending above your start",
  ));
  // `median_first_cut_age` is conditioned on the cohorts that EVER cut, so tie
  // it to the cut rate — a median first-cut age is misleading if read as
  // unconditional when few histories cut at all.
  container.appendChild(statCard(
    stats.median_first_cut_age != null ? `age ${stats.median_first_cut_age}` : "—",
    "median age of the first cut",
    stats.median_first_cut_age == null
      ? "no history ever cut"
      : `among the ${fmtPct(stats.pct_ever_cut)} of histories that cut`,
  ));
  container.appendChild(statCard(
    fmtK(stats.deepest_cut),
    "deepest typical cut",
    `p10 of each history's lowest year · single lowest year across all ${stats.cohort_count} histories: ${fmtK(stats.lowest_single_year_spend)}`,
  ));
  container.appendChild(statCard(
    fmtPct(stats.pct_ever_forced_to_floor),
    "of histories ever forced down to your floor",
    `median ${fmtYears(stats.median_years_at_floor)} pinned at your minimum`,
  ));
}

// The display action class for a worst-cohort path row, so cuts/raises read
// distinctly (cut = warm, raise = cool, hold = muted). GI-driven raises get
// their own tint so the narrative doesn't read a guaranteed-income arrival as
// market outperformance (design resolved decision).
function rowActionClass(year) {
  if (year.action === "cut") return "row-cut";
  if (year.action === "raise") return year.gi_driven ? "row-raise-gi" : "row-raise";
  return "row-hold";
}

// The post-depletion guaranteed-income clause for the worst-cohort summary
//. The old copy asserted GI "carrying the floor thereafter"
// unconditionally — false whenever the four streams sum below the
// floor (e.g. GI below the $85k floor, worse with early SS
// claims). Compute it instead from the echoed scenario the chart rendered with:
//
//   - reference = `spending_floor`. The drill-down renders ONLY for dynamic
//     spending modes, and every one of them clamps to the operational floor
//     (amortization draws it as a reference rail) — so "the floor" in this
//     sentence IS spending_floor. Fallback to annual_spending (the target) only
//     if a scenario somehow rides without a positive floor.
//   - GI is age-varying (streams switch on at their start ages, never off), so
//     coverage can STEP UP inside the post-depletion window when depletion lands
//     before the last stream's start age — say so, naming that last start age.
//
// Cases (pure; returns the clause after "depleted at age X, "):
//   zero GI : "with no guaranteed income to fall back on"
//   (a) GI >= floor every post-depletion year
//           : "with guaranteed income covering the floor thereafter"
//   (b) GI < floor, all streams already on
//           : "with guaranteed income covering N% of the floor thereafter"
//   (c) a stream starts inside the window
//           : "with guaranteed income covering [the floor | N% of the floor]
//              from age Y (less before then)"
function depletedGiClause(scenario, depletedAge) {
  if (!scenario) return "with no guaranteed income to fall back on";
  const firstAge = depletedAge + 1; // "thereafter" = the years after depletion
  const endAge = typeof scenario.end_age === "number" ? scenario.end_age : firstAge;
  const reference = scenario.spending_floor > 0
    ? scenario.spending_floor
    : (scenario.annual_spending || 0);
  const eventualGi = guaranteedIncomeAtAge(scenario, endAge);
  if (eventualGi <= 0) return "with no guaranteed income to fall back on";
  // (a): GI only ever steps up with age, so covering the floor in the FIRST
  // post-depletion year means covering it in all of them.
  if (guaranteedIncomeAtAge(scenario, firstAge) >= reference) {
    return "with guaranteed income covering the floor thereafter";
  }
  // Last stream start inside the post-depletion window — the age the coverage
  // statement becomes fully true. Starts at firstAge are already on for the
  // whole window; starts past end_age never activate.
  let lastStart = null;
  for (const [amount, start] of giStreams(scenario)) {
    if (typeof amount !== "number" || typeof start !== "number" || amount <= 0) continue;
    if (start > firstAge && start <= endAge && (lastStart === null || start > lastStart)) {
      lastStart = start;
    }
  }
  const coverage = eventualGi >= reference
    ? "the floor"
    : `${Math.round((eventualGi / reference) * 100)}% of the floor`;
  return lastStart === null
    ? `with guaranteed income covering ${coverage} thereafter`
    : `with guaranteed income covering ${coverage} from age ${lastStart} (less before then)`;
}

// The worst-cohort one-line summary (pure, so the depletion wording is testable
// without a DOM). `scenario` is the payload's echoed scenario — the same one the
// chart rendered with.
function worstCohortSummary(wc, scenario) {
  const depleted = wc.depleted_age != null
    ? ` Its portfolio depleted at age ${wc.depleted_age}, ${depletedGiClause(scenario, wc.depleted_age)}.`
    : "";
  return (
    `Starting in ${wc.start_year} gave the lowest total real spending of any ` +
    `history. Spending bottomed at ${fmtK(wc.min_spend)}.${depleted}`
  );
}

// Render the worst-cohort drill-down: a title labelling the cohort by start year,
// a one-line summary, and a year-by-year table with $ triggers. Each adjustment
// year (cut/raise) is highlighted; holds are de-emphasised. The trigger reads in
// dollars ("portfolio fell to ~$1.7M") even though the engine logic is rate-based.
function renderWorstCohort(wc, scenario) {
  const titleEl = document.getElementById("worst-cohort-title");
  const summaryEl = document.getElementById("worst-cohort-summary");
  const tbody = document.getElementById("worst-cohort-tbody");
  if (!titleEl || !summaryEl || !tbody) return;

  titleEl.textContent = `The most-squeezed retirement: the ${wc.start_year} cohort`;
  summaryEl.textContent = worstCohortSummary(wc, scenario);

  tbody.innerHTML = "";
  for (const year of wc.path) {
    const tr = document.createElement("tr");
    tr.className = rowActionClass(year);

    const age = document.createElement("td");
    age.textContent = year.age;

    const action = document.createElement("td");
    action.className = "worst-cohort-action";
    action.textContent = year.label;

    const spend = document.createElement("td");
    spend.textContent = fmtMoney(year.spend);

    const port = document.createElement("td");
    // begin-of-year portfolio in compact millions — the "$ trigger" the design
    // wants ("portfolio fell to ~$1.7M"). Shown for every row so the reader can
    // see the balance that drove each decision.
    port.textContent = fmtMillions(year.begin_portfolio);

    const gi = document.createElement("td");
    gi.textContent = year.gi > 0 ? fmtMoney(year.gi) : "—";

    tr.appendChild(age);
    tr.appendChild(action);
    tr.appendChild(spend);
    tr.appendChild(port);
    tr.appendChild(gi);
    tbody.appendChild(tr);
  }
}

// Show or hide + (re)draw the adjustment-events panel. Visible ONLY in guardrails
// spending mode with a payload carrying `adjustment_stats` + `worst_cohort`;
// otherwise hidden. Keeps the panel strictly additive — Constant/Lifecycle and
// the comparison/MC sub-views never see it.
function updateAdjustmentPanel(payload) {
  const panel = document.getElementById("adjustment-panel");
  if (!panel) return;
  const stats = payload && !payload.error ? payload.adjustment_stats : null;
  const wc = payload && !payload.error ? payload.worst_cohort : null;
  // The aggregate stat-card readout shows whenever the payload carries
  // `adjustment_stats` (Historical AND Monte Carlo — slice 3 adds MC). The
  // worst-cohort drill-down is HISTORICAL-ONLY: MC trials are synthetic (no
  // calendar year), so the "1969 cohort" narrative does not translate and the MC
  // payload omits `worst_cohort` (the design: prefer fan + aggregate stats for MC).
  // So the panel is gated on `stats` alone, and the drill-down sub-section is
  // shown/hidden by whether `worst_cohort` is present.
  const wcSection = document.getElementById("worst-cohort");
  if (isDynamicSpending() && stats) {
    panel.hidden = false;
    // Mode-aware intro: "historical retirements" vs "simulated retirements".
    const note = document.getElementById("adjustment-note");
    if (note) {
      const key = simMode === "monte_carlo" ? "monteCarlo" : "historical";
      if (note.dataset[key]) note.textContent = note.dataset[key];
    }
    renderAdjustmentStats(stats);
    if (wc) {
      if (wcSection) wcSection.hidden = false;
      renderWorstCohort(wc, payload.scenario);
    } else if (wcSection) {
      wcSection.hidden = true;
    }
  } else {
    panel.hidden = true;
  }
}

// Build 3: the OVERLAY fan — TWO scenarios on ONE chart. Each scenario draws as
// median + a translucent 10–90 band ONLY (inner band dropped so the purple
// overlap reads cleanly). A blue, B magenta → overlap blends to purple. Shared Y
// axis is inherent (one chart); `yMax` pins it so the band that's taller off-
// screen isn't clipped. Tooltip (Fix 3) shows each scenario's portfolio median +
// its spending + guaranteed income at the hovered age (one labelled row per
// scenario). `scenarioA`/`scenarioB` (echoed payload scenarios) + the optional
// guardrail spending bands feed the spending/GI values.
function renderHistoricalOverlayChart(bandsA, bandsB, yMax, opts = {}) {
  const { scenarioA, scenarioB, spendingBandsA, spendingBandsB } = opts;
  const canvas = document.getElementById("historical-overlay-chart");
  destroyHistoricalChart("overlay");
  const labels = (bandsA.length ? bandsA : bandsB).map((p) => p.age);
  const fanA = fanDatasets(bandsA, {
    rgb: RGB_A(), border: LINE_A(), bandAlpha: 0.16, includeInner: false,
    medianLabel: "Scenario A median",
  });
  const fanB = fanDatasets(bandsB, {
    rgb: RGB_B(), border: LINE_B(), bandAlpha: 0.16, includeInner: false,
    medianLabel: "Scenario B median",
  });
  // A's datasets first, then B's; the two median datasets are the only ones the
  // tooltip surfaces (filter on their indices). With no inner band each fan is
  // [p10, p90, median] → median indices are fanA.medianIndex and
  // fanA.datasets.length + fanB.medianIndex.
  const datasets = [...fanA.datasets, ...fanB.datasets];
  const medA = fanA.medianIndex;
  const medB = fanA.datasets.length + fanB.medianIndex;
  historical.overlay = ChartView.fanChart(canvas, {
    labels,
    datasets,
    options: {
      yMax,
      yTickFormatter: (v) => fmtMillions(v),
      legend: false,
      tooltipFilter: (item) => item.datasetIndex === medA || item.datasetIndex === medB,
      tooltipLabel: (item) => {
        const isA = item.datasetIndex === medA;
        const bands = isA ? bandsA : bandsB;
        const which = isA ? "A" : "B";
        const scenario = isA ? scenarioA : scenarioB;
        const spendingBands = isA ? spendingBandsA : spendingBandsB;
        const p = bands[item.dataIndex];
        const age = p.age;
        const gi = guaranteedIncomeAtAge(scenario, age);
        const spend = spendingBands && spendingBands[item.dataIndex]
          ? spendingBands[item.dataIndex].median
          : deterministicSpendAtAge(scenario, age);
        const spendStr = (spend !== null && spend !== undefined)
          ? `, spend ${fmtMoney(spend)}`
          : "";
        return `${which}: portfolio ${fmtMoney(p.median)}${spendStr}, GI ${fmtMoney(gi)}`;
      },
    },
  });
}

// Slice 6: shared fan options live on ChartView.fanChartOptions; the four
// fan renderers above pass {yMax, yTickFormatter: fmtMillions, tooltipFilter,
// tooltipLabel, legend} into ChartView.fanChart which threads them through.
// The spending fan additionally passes yAxisTitle + a $-thousands formatter +
// a legend.labels.filter — see renderSpendingFanChart.

// Monospace-align an array of [label, moneyValue] rows. Lives on ChartView
// (slice 6); this thin shim injects app.js's fmtMoney formatter so the
// alignment math is shared but the money formatting stays under app.js.
function alignMoneyRows(rows) {
  return ChartView.alignMoneyRows(rows, fmtMoney);
}

// The monospace median/p10/p90 tooltip block for one band point (single +
// side-by-side fans). Mirrors the deterministic per-panel tooltip alignment.
function fanTooltipRows(p) {
  return alignMoneyRows([
    ["Median", p.median],
    ["10th pct", p.p10],
    ["90th pct", p.p90],
  ]);
}

// Portfolio-fan tooltip (variable-spending design acceptance fix — Fix 3): show
// the hovered age's Portfolio (the fan median), Spending, and Guaranteed income,
// not just portfolio. Under guardrails spending is driven by spending-net-of-GI,
// so seeing GI + spending at the hovered age is essential. Spending comes from the
// spending-fan median at that age (`spendBand`) when present (guardrails); else
// it's the deterministic constant/lifecycle spend computed from the scenario. GI
// is computed client-side from the scenario's stream amounts + start ages. The
// p10/p90 of the portfolio fan stay below so the spread is still legible.
//
// `federalTaxAgg` (tax-drag design td1, DECISION 7, item 1): the optional
// aggregate payload.federal_tax block — present only for the single-scenario
// portfolio fan (renderHistoricalChart), never the comparison fans, since a
// tax-location split is single-scenario-only throughout. This is the ONE
// hist/MC surface with a "Spending" row for CONSTANT/LIFECYCLE too (the
// dedicated spending fan is dynamic-spending-only), so it's where the design's
// "reconstruct the spend client-side from the shaped target" (constant/
// lifecycle) vs "read spending_bands" (guardrails) guidance applies here: the
// `spend` value above is ALREADY that resolved value (spendBand.median or
// deterministicSpendAtAge) — added rows below read gross_outflow_median /
// tax_median / voluntary_tax_median / forced_tax_median straight off the
// payload and never derive "Spending" or "Portfolio funds" by subtracting one
// median from another (median(gross) − median(tax) != median(spend); the three
// medians are picked from potentially different paths' realizations). Absent
// (undefined, the comparison-fan default) → byte-identical to before: no new
// rows, no behavior change.
function portfolioFanTooltipRows(band, age, scenario, spendBand, federalTaxAgg) {
  const gi = guaranteedIncomeAtAge(scenario, age);
  const spend = spendBand && typeof spendBand.median === "number"
    ? spendBand.median
    : deterministicSpendAtAge(scenario, age);
  const rows = [["Portfolio", band.median]];
  const ftIdx = federalTaxAgg && Array.isArray(federalTaxAgg.ages)
    ? federalTaxAgg.ages.indexOf(age)
    : -1;
  if (ftIdx >= 0) {
    rows.push(["Portfolio funds, median (incl. tax)", federalTaxAgg.gross_outflow_median[ftIdx]]);
  }
  if (spend !== null && spend !== undefined) rows.push(["Spending", spend]);
  if (ftIdx >= 0) {
    rows.push(["Federal tax, median", federalTaxAgg.tax_median[ftIdx]]);
    const voluntary = Math.max(0, Number(federalTaxAgg.voluntary_tax_median[ftIdx]) || 0);
    if (voluntary > 0) rows.push(["Voluntary (bracket-fill lever), median", voluntary]);
    const forced = Math.max(0, Number(federalTaxAgg.forced_tax_median[ftIdx]) || 0);
    if (forced > 0) rows.push(["Forced (RMD, not a spending cut), median", forced]);
  }
  rows.push(["Guaranteed income", gi]);
  rows.push(["Portfolio 10th pct", band.p10]);
  rows.push(["Portfolio 90th pct", band.p90]);
  return alignMoneyRows(rows);
}

// Spending-fan tooltip (Fix 3): the spending median/p10/p90 PLUS the hovered age's
// guaranteed income, so the spending read is GI-aware (the guardrail rule acts on
// spending-net-of-GI). `age` + `scenario` let GI be computed client-side.
function spendingFanTooltipRows(p, age, scenario) {
  return alignMoneyRows([
    ["Spending (median)", p.median],
    ["Spending 10th pct", p.p10],
    ["Spending 90th pct", p.p90],
    ["Guaranteed income", guaranteedIncomeAtAge(scenario, age)],
  ]);
}

// Build 3: one side-by-side fan (full fan: median + 10–90 + inner 25–75) into a
// per-scenario canvas, on the shared yMax. `key` is the historical instance slot
// ("sideA"/"sideB"); `rgb`/`border` the scenario colour. `scenario` +
// `spendingBands` (Fix 3) feed the GI + spending tooltip rows.
function renderHistoricalSideChart(canvasId, key, bands, { rgb, border, yMax, scenario, spendingBands }) {
  const canvas = document.getElementById(canvasId);
  destroyHistoricalChart(key);
  const { datasets, medianIndex } = fanDatasets(bands, {
    rgb, border, bandAlpha: 0.15, includeInner: true,
    medianLabel: "Median (real $, end of year)",
  });
  historical[key] = ChartView.fanChart(canvas, {
    labels: bands.map((p) => p.age),
    datasets,
    options: {
      yMax,
      yTickFormatter: (v) => fmtMillions(v),
      tooltipFilter: (item) => item.datasetIndex === medianIndex,
      tooltipLabel: (item) => portfolioFanTooltipRows(
        bands[item.dataIndex],
        bands[item.dataIndex].age,
        scenario,
        spendingBands ? spendingBands[item.dataIndex] : null,
      ),
    },
  });
}

// The shared Y max for the comparison fans = the largest p90 across BOTH
// scenarios' bands (the tallest the band ever reaches). null payloads ignored.
function historicalSharedYMax(payloadA, payloadB) {
  let max;
  for (const payload of [payloadA, payloadB]) {
    if (!payload || payload.error || !payload.bands || !payload.bands.length) continue;
    for (const b of payload.bands) {
      if (max === undefined || b.p90 > max) max = b.p90;
    }
  }
  return max;
}

// One fan-mode fetch (?mode=historical or ?mode=monte_carlo). Returns the parsed
// payload or null on failure (mirrors fetchScenario): records the error so the
// caller can surface it. Defaults to the active simMode so the historical and
// Monte Carlo paths share one fetcher — both return the identical fan payload.
async function fetchHistorical(params, mode = simMode) {
  // Same id-native POST transport as fetchScenario; `mode` (historical /
  // monte_carlo) rides in the body. A 422 (broad MC non-PSD, or no replayable
  // Historical window) is surfaced via its friendly `detail` on the readout line
  // (renderHistoricalSingle / *From) rather than a broken chart.
  try {
    return await postJson(API_URL, { ...(params || {}), mode });
  } catch (err) {
    setError(err.message);
    return { error: err.message };
  }
}

// Single-scenario Historical (Builds 1–2): fetch Scenario A, draw its fan + the
// full descriptive readout. Returns { payloadA, payloadB: null } so the caller's
// cache shape matches the comparison paths.
async function renderHistoricalSingle(paramsA) {
  // Single (non-comparison) runs keep each scenario's native window — clear any
  // stale comparison-window note from a prior A/B view.
  updateComparisonWindowNote(null, null, null);
  const summaryEl = document.getElementById("historical-summary");
  const payload = await fetchHistorical(paramsA);
  if (payload && !payload.error) {
    summaryEl.textContent = historicalReadout(payload);
    // Flow-overlay (slice 2): on the single-scenario fan, overlay the
    // deterministic GI floor (always) + spending line (constant/lifecycle only) on a
    // secondary axis. Gated to the stochastic single view (shouldOverlayFanFlows) —
    // the comparison fans never receive it, so they stay single-axis + byte-identical.
    const fanFlowOverlay = shouldOverlayFanFlows()
      ? {
          flows: payload.flows,
          spending: spendingMode === "constant" || spendingMode === "lifecycle",
        }
      : null;
    renderHistoricalChart(
      payload.bands,
      undefined,
      payload.scenario,
      payload.spending_bands,
      fanFlowOverlay,
      payload.federal_tax,
    );
    // Spending fan (guardrails-only). Drawn under the portfolio fan when the
    // payload carries `spending_bands`; hidden + torn down otherwise.
    updateSpendingFan(payload);
    // Adjustment-events panel (slice 2, guardrails-only): the cost-of-flexibility
    // readout + worst-cohort drill-down. Shown when the payload carries them.
    updateAdjustmentPanel(payload);
    // Buffer-survival readout (bucket-strategy slice 1a, buckets-only): the text
    // thesis-payoff line. Shown when the payload carries `buffer_survival`.
    updateBufferSurvivalReadout(payload);
    // Effective-equity glidepath (buckets-only): the allocation the gap-based
    // sizing imposes; carried on the Historical / MC payload too (setup property).
    updateEffectiveEquityReadout(payload);
    return { payloadA: payload, payloadB: null };
  }
  // 422 (broad MC non-PSD, or no replayable Historical window) or a transport
  // error: show the friendly `detail` on the readout line and tear down any stale
  // fan chart so the user sees the message, not a leftover/broken chart.
  destroyHistoricalChart("single");
  updateSpendingFan(null);
  updateAdjustmentPanel(null);
  updateBufferSurvivalReadout(null);
  updateEffectiveEquityReadout(null);
  const label = simMode === "monte_carlo" ? "Monte Carlo" : "Historical";
  summaryEl.textContent = `${label}: ${payload && payload.error ? payload.error : "no data"}`;
  return { payloadA: null, payloadB: null };
}

// Build 3: render the historical OVERLAY (two fans, one chart) from payloads.
// Per-scenario readout lines above, then the overlap fan. Draws whichever
// scenarios resolved; a failed side shows its error in its readout line.
function renderHistoricalOverlayFrom(payloadA, payloadB) {
  const yMax = historicalSharedYMax(payloadA, payloadB);
  const summaryA = document.getElementById("historical-overlay-summary-a");
  const summaryB = document.getElementById("historical-overlay-summary-b");
  summaryA.textContent = payloadA && !payloadA.error
    ? historicalScenarioReadout("A", payloadA)
    : `A: failed — ${payloadA ? payloadA.error : "no data"}`;
  summaryB.textContent = payloadB && !payloadB.error
    ? historicalScenarioReadout("B", payloadB)
    : `B: failed — ${payloadB ? payloadB.error : "no data"}`;
  const bandsA = payloadA && !payloadA.error ? payloadA.bands : [];
  const bandsB = payloadB && !payloadB.error ? payloadB.bands : [];
  renderHistoricalOverlayChart(bandsA, bandsB, yMax, {
    scenarioA: payloadA && !payloadA.error ? payloadA.scenario : null,
    scenarioB: payloadB && !payloadB.error ? payloadB.scenario : null,
    spendingBandsA: payloadA && !payloadA.error ? payloadA.spending_bands : null,
    spendingBandsB: payloadB && !payloadB.error ? payloadB.spending_bands : null,
  });
}

// Build 3: render the historical SIDE-BY-SIDE fans (two charts, shared Y) from
// payloads. Each panel gets its own readout + full fan.
function renderHistoricalSideFrom(payloadA, payloadB) {
  const yMax = historicalSharedYMax(payloadA, payloadB);
  const summaryA = document.getElementById("historical-side-summary-a");
  const summaryB = document.getElementById("historical-side-summary-b");
  if (payloadA && !payloadA.error) {
    summaryA.textContent = historicalScenarioReadout("A", payloadA);
    renderHistoricalSideChart("historical-side-chart-a", "sideA", payloadA.bands, {
      rgb: RGB_A(), border: LINE_A(), yMax,
      scenario: payloadA.scenario, spendingBands: payloadA.spending_bands,
    });
  } else {
    summaryA.textContent = `A: failed — ${payloadA ? payloadA.error : "no data"}`;
    destroyHistoricalChart("sideA");
  }
  if (payloadB && !payloadB.error) {
    summaryB.textContent = historicalScenarioReadout("B", payloadB);
    renderHistoricalSideChart("historical-side-chart-b", "sideB", payloadB.bands, {
      rgb: RGB_B(), border: LINE_B(), yMax,
      scenario: payloadB.scenario, spendingBands: payloadB.spending_bands,
    });
  } else {
    summaryB.textContent = `B: failed — ${payloadB ? payloadB.error : "no data"}`;
    destroyHistoricalChart("sideB");
  }
}

// Build 3: two-scenario Historical comparison. Fetch BOTH scenarios in parallel
// (two ?mode=historical calls, exactly as the deterministic comparison fires two
// /api/trajectory calls), then render the active sub-view (overlay default /
// side-by-side via the toggle). Returns { payloadA, payloadB } for the cache so
// flipping the view re-renders without re-simulating.
async function renderHistoricalComparison(paramsA, paramsB) {
  // Comparison-window alignment (option 3): when the two scenarios have DIFFERENT
  // native testable windows, score BOTH on the common (latest) one so the
  // head-to-head measures them over the SAME cohorts. The floor is computed
  // client-side from the loaded class menu's data starts (zero extra round-trips —
  // the two fetches below carry it), and BOTH requests get the identical floor, so
  // the scoring is fair regardless of small estimate slop. The note reports the
  // realized common window off the engine's echoed testable_since (source of truth).
  const floor = commonComparisonFloor(paramsA, paramsB);
  const aReq = floor != null ? { ...paramsA, window_start_year: floor } : paramsA;
  const bReq = floor != null ? { ...paramsB, window_start_year: floor } : paramsB;
  const [payloadA, payloadB] = await Promise.all([
    fetchHistorical(aReq),
    fetchHistorical(bReq),
  ]);
  updateComparisonWindowNote(floor, payloadA, payloadB);
  if (compareState.view() === "overlay") {
    renderHistoricalOverlayFrom(payloadA, payloadB);
  } else {
    renderHistoricalSideFrom(payloadA, payloadB);
  }
  return { payloadA, payloadB };
}

// Show/hide the comparison-window-alignment note. Shown only when a floor is
// active (the two scenarios' native windows differed and BOTH were clamped to the
// common, latest testable window). The reported year is the engine's realized
// testable_since off the clamped payloads (they agree when the clamp held), so the
// note can't drift from the data; falls back to the requested floor if a payload
// errored. Hidden when no clamp is active (windows already matched, or single run).
function updateComparisonWindowNote(floor, payloadA, payloadB) {
  const note = document.getElementById("comparison-window-note");
  if (!note) return;
  if (floor == null) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  const sinceA = payloadA && !payloadA.error ? payloadA.testable_since : null;
  const sinceB = payloadB && !payloadB.error ? payloadB.testable_since : null;
  const shown = sinceA != null && sinceB != null ? Math.max(sinceA, sinceB)
    : sinceA != null ? sinceA
    : sinceB != null ? sinceB
    : floor;
  note.textContent =
    `Both scenarios scored since ${shown} — the common testable window. ` +
    `These scenarios have different histories, so the head-to-head clamps both to ` +
    `the later start year, measuring depletion, medians, and the fans over the same cohorts.`;
  note.hidden = false;
}

// Show the right historical sub-view container (single / overlay / side-by-side)
// for the current comparison + view state, and tear down the charts that just
// left the screen. Mirrors updateOutputMode() for the deterministic region.
function updateHistoricalSubView() {
  const mode = currentOutputMode(); // "single" | "overlay" | "sidebyside"
  const single = document.getElementById("historical-single");
  const overlayEl = document.getElementById("historical-overlay");
  const side = document.getElementById("historical-sidebyside");
  if (single) single.hidden = mode !== "single";
  if (overlayEl) overlayEl.hidden = mode !== "overlay";
  if (side) side.hidden = mode !== "sidebyside";
  // Destroy the charts for the sub-views that are now off-screen.
  if (mode !== "single") {
    destroyHistoricalChart("single");
    // The spending fan lives inside historical-single; tear it down too (slice 1
    // renders the spending fan only in the single Historical view).
    destroyHistoricalChart("spendingFan");
  }
  if (mode !== "overlay") destroyHistoricalChart("overlay");
  if (mode !== "sidebyside") {
    destroyHistoricalChart("sideA");
    destroyHistoricalChart("sideB");
  }
}

// Read Scenario A's params + (when comparing) B's, and render the historical
// view via the path matching the comparison state. Single when nothing is
// unlinked; otherwise the two-scenario comparison in the active sub-view.
async function renderHistorical(scenarios) {
  updateHistoricalSubView();
  let result;
  if (!isComparing()) {
    result = await renderHistoricalSingle(scenarios.a);
  } else {
    result = await renderHistoricalComparison(scenarios.a, scenarios.b);
  }
  historicalLastPayloads = result;
  return result;
}

// Show/hide the deterministic vs historical output regions for the current mode,
// destroying the charts that just left the screen so nothing leaks. In
// Historical mode the whole deterministic region (single/overlay/side-by-side)
// AND the comparison view-toggle hide; in Deterministic mode the historical view
// hides and the normal output-mode machinery takes over.
function updateModeRegions() {
  const histView = document.getElementById("historical-view");
  const detRegion = document.getElementById("deterministic-region");
  // Both stochastic modes (Historical + Monte Carlo) render into the fan view;
  // only Deterministic uses the line/comparison region.
  const isHistorical = isStochasticMode();
  if (histView) histView.hidden = !isHistorical;
  if (detRegion) detRegion.hidden = isHistorical;
  if (isHistorical) {
    // Tear down deterministic charts; they're off-screen now.
    destroyPanelCharts();
    destroyOverlayChart();
    // Show the right historical sub-view (single / overlay / side-by-side) for
    // the current comparison state; rerender() then draws into it.
    updateHistoricalSubView();
  } else {
    destroyAllHistoricalCharts();
    // Leaving Historical mode — clear the comparison-window note so it can't
    // linger behind the deterministic region.
    updateComparisonWindowNote(null, null, null);
  }
}

// Reflect the selected mode on the toggle buttons (styling + aria-pressed).
function updateModeToggleSelection() {
  for (const btn of document.querySelectorAll("#mode-toggle .mode-toggle-btn")) {
    const selected = btn.dataset.mode === simMode;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// Show the Stress Test sub-selector only when the Stress Test lens is active
// (stress-test design: one VIEW entry opens a sub-selector). Hidden in every other
// mode so the picker doesn't clutter the lens bar.
function updateStressSelectorVisibility() {
  const sel = document.getElementById("stress-selector");
  if (sel) sel.hidden = !isStressMode();
}

// The comparison view-toggle (Overlay | Side-by-side) is shown whenever a
// comparison is active, in EITHER mode (Build 3 makes Historical comparison real
// and reuses this same toggle). When nothing is unlinked there's no Scenario B,
// so the toggle hides regardless of mode.
function updateViewToggle() {
  const toggle = document.getElementById("view-toggle");
  if (toggle) toggle.hidden = !isComparing();
}

// Flip simulation mode and re-render. No-op if the mode is already active.
// Swaps which output region is visible, updates chrome, and re-renders through
// the mode-aware rerender().
function setMode(mode) {
  if (!["deterministic", "historical", "monte_carlo", "stress"].includes(mode)) return;
  if (mode === simMode) return;
  simMode = mode;
  updateModeToggleSelection();
  updateModeRegions();
  updateStressSelectorVisibility(); // show the stress sub-selector only in stress mode
  updateViewToggle();
  updateMcInputsVisibility(); // show/hide the MC vol + correlation inputs
  updateSolveControl();   // dial is Historical-only; Solve greyed in MC (view-only)
  // The deterministic-model caveat is DETERMINISTIC-ONLY. A mode flip doesn't
  // touch link state, so without this it can get stranded visible: unlink the
  // allocation in Deterministic (caption shows), then switch to Historical /
  // Monte Carlo (which ARE the variability the caption says hasn't arrived) and
  // the caption would otherwise linger. Re-evaluate it on every mode change.
  updateAllocationCaption();
  // Buffer-survival readout (buckets-only): the Deterministic-vs-stochastic caveat
  // flips with the mode, so clear it here; the next render repopulates it.
  updateBufferSurvivalReadout(null);
  // Effective-equity readout (buckets-only): repopulated from the next render's
  // payload (same in every mode); clear the stale line in the meantime.
  updateEffectiveEquityReadout(null);
  // Bucket-balance chart (buckets-only): the data source flips with the mode
  // (Deterministic balances → worst-cohort → MC hides it), so tear it down here;
  // the next render redraws the right one.
  updateBucketChartView(null);
  setError("");
  rerender();
}

// ---------- Lifecycle spending: toggle + phase breakdown ----------

// Reflect the selected spending mode on the toggle buttons (styling +
// aria-pressed). Mirrors updateModeToggleSelection for the sim-mode toggle.
function updateSpendingToggleSelection() {
  for (const btn of document.querySelectorAll("#spending-toggle .mode-toggle-btn")) {
    const selected = btn.dataset.spendingMode === spendingMode;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// The current go-go (peak) spending level = Scenario A's annual-spending input.
// The toggle is global, so the breakdown is computed from A's input (the shared/
// authoritative value). Falls back to the default scenario, then 0, before the
// inputs are built. Returns a number (real $ / yr).
function currentGoGoSpending() {
  const el = aInputEl("annual_spending");
  if (el) {
    const n = parseFloat(el.value.trim());
    if (!Number.isNaN(n)) return n;
  }
  if (defaultScenario && typeof defaultScenario.annual_spending === "number") {
    return defaultScenario.annual_spending;
  }
  return 0;
}

// Render the read-only phase breakdown from the current go-go input. Shown ONLY
// in lifecycle mode; hidden (and emptied) in constant mode. Phase $ levels come
// from the fixed SPENDING_PHASES multipliers — the same the engine applies — so
// what the user reads matches what the API computes. Ages are start_age..74 for
// go-go (start_age from Scenario A's input, defaulting to 50), then the fixed
// 75-84 / 85+ boundaries. e.g.
//   "Go-go (50–74) $110k · Slow-go (75–84) $93.5k · No-go (85+) $82.5k".
function updateSpendingBreakdown() {
  const el = document.getElementById("spending-phase-breakdown");
  if (!el) return;
  // Guardrails: the annual-spending input IS the guardrail's START / base spend.
  // Slice 3 makes the band / nudge / floor / ceiling user-editable (in Advanced),
  // so this one-line note now reads the LIVE knob values (falling back to the
  // research defaults) instead of hard-coding them, and reminds the user the fan
  // only carries information in Historical/MC.
  if (spendingMode === "guardrails") {
    const start = currentGoGoSpending();
    const band = Math.round(
      readGuardrailNumber(GUARD_BAND_INPUT_ID, GUARDRAIL_DEFAULTS.guardBand, 100) * 100,
    );
    const nudge = Math.round(
      readGuardrailNumber(GUARD_ADJUST_INPUT_ID, GUARDRAIL_DEFAULTS.guardAdjust, 100) * 100,
    );
    const floor = readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor);
    const ceiling = readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling);
    el.textContent =
      `Start spend ${fmtK(start)} · cuts/raises ±${nudge}% when the funded ` +
      `withdrawal rate drifts ±${band}% · floor ${fmtK(floor)} · ` +
      `ceiling ${fmtK(ceiling)} (tapered). ` +
      `The spending fan is only meaningful in Historical / Monte Carlo.`;
    el.hidden = false;
    return;
  }
  if (spendingMode === "fr_smooth") {
    // The funded-ratio smooth rule: the start spend is the baseline (FR = 1 funds
    // it), each year eases a fixed half-speed (alpha 0.5, baked) toward full
    // funding, clamped to the same flat floor / tapered ceiling as guardrails.
    const start = currentGoGoSpending();
    const floor = readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor);
    const ceiling = readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling);
    el.textContent =
      `Start spend ${fmtK(start)} · eases toward full funding at half speed each ` +
      `year · floor ${fmtK(floor)} · ceiling ${fmtK(ceiling)} (tapered). ` +
      `The spending fan is only meaningful in Historical / Monte Carlo.`;
    el.hidden = false;
    return;
  }
  if (spendingMode === "fr_guardrail") {
    // The funded-ratio band rule: the start spend is the baseline (FR = 1 funds
    // it), spending holds while funding stays within ±20% of full, then nudges
    // ±10% past either band, clamped to the same flat floor / tapered ceiling.
    // Band/step are baked v1 presets (no inputs).
    const start = currentGoGoSpending();
    const floor = readGuardrailNumber(SPENDING_FLOOR_INPUT_ID, GUARDRAIL_DEFAULTS.floor);
    const ceiling = readGuardrailNumber(SPENDING_CEILING_INPUT_ID, GUARDRAIL_DEFAULTS.ceiling);
    el.textContent =
      `Start spend ${fmtK(start)} · holds spend while funding stays within ±20% ` +
      `of full · nudges ±10% past the bands · floor ${fmtK(floor)} · ` +
      `ceiling ${fmtK(ceiling)} (tapered). ` +
      `The spending fan is only meaningful in Historical / Monte Carlo.`;
    el.hidden = false;
    return;
  }
  if (spendingMode === "fr_tiers") {
    // The funded-ratio TIER ladder: a baked preset (no editable knobs). Spending
    // steps by funded-ratio tier, with hysteresis, a crash brake, and a hard
    // floor. Self-contained — it does NOT use the operational floor/ceiling rails
    // and applies no activity taper (the tier amounts already encode the phases).
    el.textContent =
      `Tiered by funded ratio: FLOOR $85k → MAX $160k (go-go), $130k (slow-go), ` +
      `flat $85k after 75 · hysteresis · crash brake · hard floor (baked preset). ` +
      `The spending fan is only meaningful in Historical / Monte Carlo.`;
    el.hidden = false;
    return;
  }
  if (spendingMode !== "lifecycle") {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const peak = currentGoGoSpending();
  const slowGo = peak * SPENDING_PHASES.slowGoMultiplier;
  const noGo = peak * SPENDING_PHASES.noGoMultiplier;
  // Go-go starts at the plan's start age (Scenario A input), defaulting to 50.
  let startAge = 50;
  const startEl = aInputEl("start_age");
  if (startEl) {
    const n = parseInt(startEl.value.trim(), 10);
    if (!Number.isNaN(n)) startAge = n;
  } else if (defaultScenario && typeof defaultScenario.start_age === "number") {
    startAge = defaultScenario.start_age;
  }
  const goGoEnd = SPENDING_PHASES.slowGoStartAge - 1; // 74
  const noGoStart = SPENDING_PHASES.noGoStartAge;      // 85
  el.textContent =
    `Go-go (${startAge}–${goGoEnd}) ${fmtK(peak)} · ` +
    `Slow-go (${SPENDING_PHASES.slowGoStartAge}–${noGoStart - 1}) ${fmtK(slowGo)} · ` +
    `No-go (${noGoStart}+) ${fmtK(noGo)}`;
  el.hidden = false;
}

// Flip the spending shape and re-render. No-op if already active. Updates the
// toggle chrome + the read-only phase breakdown, then re-renders through the
// mode-aware rerender() (which now serializes spending_mode onto both scenarios).
function setSpendingMode(mode) {
  if (
    mode !== "constant" &&
    mode !== "lifecycle" &&
    mode !== "guardrails" &&
    mode !== "fr_smooth" &&
    mode !== "fr_guardrail" &&
    mode !== "fr_tiers" &&
    mode !== "amortization"
  )
    return;
  if (mode === spendingMode) return;
  spendingMode = mode;
  updateSpendingToggleSelection();
  updateGuardrailInputsVisibility();  // guardrail knobs editable only under guardrails
  updateAmortizationInputsVisibility();  // amortization knob editable only under amortization
  updateSpendingBreakdown();
  updateSolveControl();   // guardrails is view-only → grey the solver cluster
  setError("");
  rerender();
}

// ---------- Buckets sourcing (bucket-strategy design, slice 1a thin wiring) ----------

// Reflect the selected sourcing mode on the toggle buttons (styling + aria-pressed).
// Mirrors updateSpendingToggleSelection / updateModeToggleSelection.
function updateSourcingToggleSelection() {
  for (const btn of document.querySelectorAll("#sourcing-toggle .mode-toggle-btn")) {
    const selected = btn.dataset.sourcingMode === sourcingMode;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// Render the text buffer-survival readout from a Historical / Monte Carlo payload's
// `buffer_survival` block (the thesis payoff metric: how often the cash+income
// buffer was breached and a growth sale forced). Shown ONLY in buckets sourcing +
// a stochastic mode with the block present; hidden + emptied otherwise (single
// sourcing, or Deterministic — no down years to test the buffer). Slice 1a is a
// plain text line (the bucket matrices + bucket-balance chart are slice 1b).
function updateBufferSurvivalReadout(payload) {
  const el = document.getElementById("buffer-survival-readout");
  if (!el) return;
  if (sourcingMode !== "buckets") {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  if (simMode === "deterministic") {
    el.textContent =
      "Buckets in Deterministic mode can't show the buffer being tested — there are " +
      "no down years. Switch to Historical or Monte Carlo to see how often the cash " +
      "+ income buffer carried a retirement through without a forced growth sale.";
    el.hidden = false;
    return;
  }
  const bs = payload && !payload.error ? payload.buffer_survival : null;
  if (!bs) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  // The honest read combines TWO payload facts that, alone, mislead:
  //   1. Protection — how often growth was force-sold in a downturn
  //      (`buffer_survival`). For any adequately-buffered config this is ~always
  //      0 by design, so reporting only it reads as unalloyed success.
  //   2. Outcome — how often the portfolio still ran to zero by the end age
  //      (`depletion.count`, present on both the Historical and MC payloads).
  // Linking them surfaces the real cost: holding a large low-return safe buffer
  // protects the withdrawal SEQUENCE but drags long-term growth (cash drag), so
  // the OUTCOME doesn't improve. Numbers are pulled live from the payload.
  const label = simMode === "monte_carlo" ? "simulated retirements" : "historical retirements";
  const noun = simMode === "monte_carlo" ? "trials" : "cohorts";
  const n = bs.count;
  const depletedRaw = payload.depletion ? payload.depletion.count : null;
  const protectedAll = bs.median_years_buffer_lasted == null;

  // (1) Protection clause.
  let protection;
  if (protectedAll) {
    protection =
      `Growth was never force-sold in a downturn — the cash + income buffer ` +
      `absorbed every one of the ${n} ${noun}' down years.`;
  } else {
    const pct = fmtPct(bs.pct_cohorts_forced_to_sell_growth);
    protection =
      `Growth was force-sold at a loss in ${pct} of the ${n} ${label} ` +
      `(first at age ${bs.worst_forced_sale_age}); the rest rode the downturn ` +
      `on the buffer.`;
  }

  // (2) Outcome clause, explicitly linked to the protection result. When the
  //     depletion count is unavailable, fall back to the protection clause
  //     alone rather than asserting a number we don't have.
  let outcome = "";
  if (depletedRaw != null) {
    const d = depletedRaw;
    if (d === 0 && protectedAll) {
      outcome =
        ` And none depleted — the buffer carried every ${noun.replace(/s$/, "")}, ` +
        `at the cost of the lowest growth.`;
    } else if (d === 0) {
      outcome =
        ` And none ran to zero by 95 — but bucket sourcing protects the withdrawal ` +
        `SEQUENCE (you're never forced to sell at a loss), not the OUTCOME; the safe ` +
        `buffer's low return is a real cost.`;
    } else {
      const ranToZero =
        d === 1
          ? `1 of the ${n} ${noun} still ran the portfolio to zero by 95`
          : `${d} of the ${n} ${noun} still ran the portfolio to zero by 95`;
      outcome =
        ` Yet ${ranToZero} — bucket sourcing protects the withdrawal SEQUENCE ` +
        `(you're never forced to sell at a loss), but it doesn't raise the OUTCOME; ` +
        `the safe buffer's low return is a real cost.`;
    }
  }

  // (3) Fair-comparison nudge — point to the A/B compare; the matched-allocation
  //     caption already carries the "same equity/bond split" rule. Do NOT compute
  //     a single-mode number inline.
  const nudge =
    " Compare against Single sourcing at the same allocation to see the tradeoff for your plan.";

  el.textContent = protection + outcome + nudge;
  el.hidden = false;
}

// Render the effective-equity glidepath readout from a payload's `effective_equity`
// block (present in ALL three modes when buckets is on — it's a property of the
// SETUP, computed off the deterministic reference). Gap-based bucket sizing puts
// most of a high-spend portfolio in the low-equity safe buckets, so the EFFECTIVE
// equity share starts well below the growth bucket's nominal weight and rises as
// guaranteed income lands (the safe targets shrink). Surfaces that glide as one
// line. Shown ONLY in buckets sourcing with the block present; hidden + emptied
// otherwise (single sourcing). The phrasing is built from the REAL anchor values
// (start / full-GI / end ages) — no hardcoded "rising to 100%": a setup whose share
// is already high, or barely rises, reads honestly off its own numbers.
function updateEffectiveEquityReadout(payload) {
  const el = document.getElementById("effective-equity-readout");
  if (!el) return;
  if (sourcingMode !== "buckets") {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const ee = payload && !payload.error ? payload.effective_equity : null;
  if (!ee || !ee.ages || !ee.shares || ee.ages.length === 0) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const shareAt = (age) => {
    const idx = ee.ages.indexOf(age);
    return idx >= 0 ? ee.shares[idx] : null;
  };
  const startShare = shareAt(ee.start_age);
  const fullGiShare = shareAt(ee.full_gi_age);
  const endShare = shareAt(ee.end_age);

  // The peak the glide reaches over the horizon (usually the end share, but read
  // the max so a non-monotone tail still reads honestly).
  const peakShare = Math.max(...ee.shares);
  // Does the share materially RISE from start to peak? (> 5 percentage points.)
  const rises = peakShare - startShare > 0.05;
  // Depletion read: along the deterministic reference the effective-equity share
  // falls to ~0 once the growth bucket is drained / the portfolio runs to zero.
  // The FIRST in-horizon age where it hits ~0 marks where the equity is gone —
  // phrase that honestly rather than claiming a "0% allocation" the setup never
  // chose. (A genuinely all-safe setup never starts above ~0, so this only fires
  // when the share was meaningful and then collapsed.)
  const drainIdx = ee.shares.findIndex((s) => s < 0.005);
  const drains = drainIdx >= 0 && startShare >= 0.05;
  const drainAge = drains ? ee.ages[drainIdx] : null;

  let text = `Effective equity: ${fmtPct(startShare)} at age ${ee.start_age}`;
  if (drains) {
    // The growth side is exhausted at drainAge — the gap outran the buffer; the
    // implied glidepath never "rises", it collapses as growth funds the shortfall.
    text +=
      `, then falling to ~0% by age ${drainAge} as the growth bucket is drained` +
      ` funding the spending gap`;
  } else if (rises) {
    // Where the glide effectively finishes landing — name the full-GI anchor when
    // it differs from start, then the end share.
    const lands = `${fmtPct(endShare)} by ${ee.end_age}`;
    const giClause =
      ee.full_gi_age !== ee.start_age && fullGiShare != null
        ? `, ${fmtPct(fullGiShare)} by ${ee.full_gi_age}`
        : "";
    text += `${giClause}, rising to ${lands} as guaranteed income lands`;
  } else {
    // Already high / flat — don't promise a rise that isn't there.
    text += `, holding near ${fmtPct(endShare)} through ${ee.end_age}`;
  }
  text +=
    ". Gap-based bucket sizing sets this allocation implicitly — the safe buckets" +
    " hold the low-equity remainder.";
  el.textContent = text;
  el.hidden = false;
}

// Flip the sourcing shape and re-render. No-op if already active. Updates the
// toggle chrome, then re-renders through the mode-aware rerender() (which now
// serializes sourcing_mode onto both scenarios). Buckets + guardrails is fenced at
// the API (422) — flipping to buckets while guardrails is active surfaces the
// API's friendly error on the readout line, the same way other 422s do.
function setSourcingMode(mode) {
  if (mode !== "single" && mode !== "buckets") return;
  if (mode === sourcingMode) return;
  sourcingMode = mode;
  updateSourcingToggleSelection();
  if (mode === "buckets") {
    seedBucketWeights();
    renderBucketMatrices();
  }
  updateSourcingModeView();           // show/hide the N1/N2 + 3 matrices block
  updateBufferSurvivalReadout(null);  // clear stale readout until the next render
  updateEffectiveEquityReadout(null); // clear stale effective-equity line until re-render
  updateBucketChartView(null);        // tear down / hide the bucket chart until re-render
  // Tax-location (tax-buckets design tb1): tb1 supports the split in SINGLE sourcing
  // only. Toggle the input gate (disable + note under buckets) + the live sum
  // helper, and tear down the tax-location chart until the next render (it no-ops
  // anyway when gated). The split inputs are preserved across the toggle — switching
  // back to Single re-enables them with their values intact.
  updateTaxLocationGate();
  updateTaxLocationView(null);
  // Deterministic guardrails spending line (dsc1): tear down across the sourcing
  // toggle; the guarded rerender redraws it if still deterministic + guardrails.
  updateDeterministicSpending(null);
  updateSolveControl();   // buckets is view-only → grey the solver cluster
  setError("");
  rerender();
}

// ---------- Spending solver (slice 2): confidence dial + Solve ----------
//
// The INVERSE of a projection: instead of typing a spend and reading the
// success rate, the user sets a confidence and the solver finds the MAX
// sustainable spend, writes it into the annual-spending field, and re-renders
// the projection at that level. Single-scenario only — while comparing, the
// cluster (dial + Solve) is DISABLED + greyed (with a tooltip on Solve), so the
// user must Focus a scenario to collapse the comparison before solving. The dial
// is Historical-only (Deterministic is a single path → no confidence); the Solve
// button shows in both modes. Same idiom as setMode / setSpendingMode.

// Reflect the selected confidence on the dial buttons (styling + aria-pressed).
// Mirrors updateModeToggleSelection / updateSpendingToggleSelection.
function updateSolveDialSelection() {
  for (const btn of document.querySelectorAll("#solve-dial .solve-dial-btn")) {
    const selected = parseInt(btn.dataset.confidence, 10) === solveConfidence;
    btn.classList.toggle("is-selected", selected);
    btn.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

// Pick a confidence dial stop. No-op if already selected. Just records the
// selection + refreshes the dial chrome — it does NOT auto-solve (the user
// presses Solve), matching the explicit-Recompute idiom of the rest of the app.
function setConfidence(pct) {
  if (![80, 90, 95, 100].includes(pct)) return;
  if (pct === solveConfidence) return;
  solveConfidence = pct;
  updateSolveDialSelection();
}

// Reflect the current comparison + mode state on the solve cluster (dial +
// Solve). It stays VISIBLE at all times now:
//   - comparing  -> DISABLE + grey the whole cluster (Solve button + every dial
//     stop), and put a tooltip on Solve explaining why it's off. Solve is
//     single-scenario only, so it's unusable while two scenarios are compared.
//   - not comparing -> RE-ENABLE the cluster and clear Solve's tooltip. The dial
//     rides along ONLY in Historical mode (hidden in Deterministic — one path,
//     no dial); the Solve button shows in both modes.
// Bound to the same chrome refresh the rest of the app uses (afterLinkChange +
// setMode + init), so it reacts live to link/unlink and mode flips. Composes
// with the existing Focus A/B control: collapsing a comparison re-enables Solve.
const SOLVE_COMPARING_TITLE = "Focus a scenario to solve it";
// Monte Carlo is view-the-distribution only: the solve cluster is greyed/disabled
// there (the dial is hidden too). Solve runs in Deterministic or Historical.
const SOLVE_MC_TITLE = "Max-spend solve runs in Deterministic or Historical mode";
// Guardrails is a VIEW-ONLY spending strategy: its spend is portfolio-dependent,
// so the max-spend solver (which assumes portfolio-independent spend) would
// return a confidently-wrong number. The cluster is greyed/disabled there and
// the solver is fenced off in the engine + API too (defense-in-depth).
const SOLVE_GUARDRAILS_TITLE =
  "Dynamic spending (guardrails / funded-ratio) is view-only — set your start " +
  "spend and read the consequences";
// Buckets sourcing is view-only in slice 1a: the cash-first / one-sided-refill
// sourcing is fenced from the max-spend solver (at the engine + API too).
const SOLVE_BUCKETS_TITLE =
  "Buckets sourcing is view-only — set your spend and read the buffer-survival metric";
function updateSolveControl() {
  const control = document.getElementById("solve-control");
  const solveBtn = document.getElementById("solve-btn");
  const dial = document.getElementById("solve-dial");
  const comparing = isComparing();
  const isMonteCarlo = simMode === "monte_carlo";
  const isStress = isStressMode();
  const isGuardrails = isDynamicSpending();
  const isBuckets = sourcingMode === "buckets";
  // Solve is available in Deterministic and Historical only. Monte Carlo and
  // Stress are view-only (the engine/API solve stays, just unreachable from the
  // UI). Guardrails spending is view-only (portfolio-dependent spend breaks the
  // solver's monotonicity assumption). Buckets sourcing is view-only in slice 1a
  // (fenced from the solver at the engine/API too — defense-in-depth). An active
  // comparison also disables Solve (single-scenario only). Any greys it.
  const disabled = comparing || isMonteCarlo || isStress || isGuardrails || isBuckets;
  // Grey the whole cluster via a single class on the control; disable the
  // interactive controls so they're truly inert (not just visually dimmed).
  if (control) control.classList.toggle("is-disabled", disabled);
  if (solveBtn) {
    solveBtn.disabled = disabled;
    // Comparison takes precedence in the tooltip (it's the more transient
    // state); then guardrails (view-only); otherwise explain the MC restriction.
    if (comparing) {
      solveBtn.title = SOLVE_COMPARING_TITLE;
    } else if (isBuckets) {
      solveBtn.title = SOLVE_BUCKETS_TITLE;
    } else if (isGuardrails) {
      solveBtn.title = SOLVE_GUARDRAILS_TITLE;
    } else if (isMonteCarlo) {
      solveBtn.title = SOLVE_MC_TITLE;
    } else {
      solveBtn.removeAttribute("title");
    }
  }
  for (const btn of document.querySelectorAll("#solve-dial .solve-dial-btn")) {
    btn.disabled = disabled;
  }
  // A readout from a previous single-scenario solve would otherwise linger while
  // comparing — or after switching into Monte Carlo, where Solve is unavailable.
  // Clear it (empty text hides both readout + caption) whenever the cluster is
  // disabled. Otherwise leave any existing readout alone; a fresh single
  // scenario simply has none until solved.
  if (disabled) setSolveReadout("");
  // The confidence dial is Historical-only: shown in Historical, hidden in
  // Deterministic (one path, no dial) AND Monte Carlo (Solve unavailable there).
  if (dial) dial.hidden = simMode !== "historical";
}

// Set the solve readout line (the achieved-% result, or an error). `isError`
// styles it red and suppresses the caption; a successful readout reveals the
// caption beneath it. Empty text hides both.
function setSolveReadout(text, { isError = false } = {}) {
  const readout = document.getElementById("solve-readout");
  const caption = document.getElementById("solve-caption");
  if (readout) {
    readout.textContent = text || "";
    readout.hidden = !text;
    readout.classList.toggle("is-error", isError);
  }
  // The caption is the "this is your safe spend if you never adjust" note — it
  // only makes sense alongside a successful solved number.
  if (caption) caption.hidden = !text || isError;
}

// Compose the success readout from a /api/solve response. Historical phrases it
// as a confidence + achieved fraction; Deterministic drops the confidence
// framing (single path). Lifecycle appends "(go-go peak)" since the solved
// number is the peak the tapered curve hangs from. hit_ceiling reports the cap
// with the guaranteed-income explanation instead of a survival fraction.
function solveReadoutText(data) {
  const spend = `${fmtMoney(data.solved_spend)}/yr`;
  const peak = data.spending_mode === "lifecycle" ? " (go-go peak)" : "";
  if (data.hit_ceiling) {
    return `≥ ${spend}${peak} (guaranteed income covers your spending).`;
  }
  const achievedPct = (data.achieved_fraction * 100).toFixed(1);
  if (data.mode === "historical") {
    const target = Math.round(data.confidence_target * 100);
    return (
      `Max sustainable spend at ${target}% confidence: ${spend}${peak} ` +
      `— survives ${achievedPct}% of histories.`
    );
  }
  if (data.mode === "monte_carlo") {
    const target = Math.round(data.confidence_target * 100);
    return (
      `Max sustainable spend at ${target}% confidence: ${spend}${peak} ` +
      `— survives ${achievedPct}% of simulated retirements.`
    );
  }
  // Deterministic: no confidence phrasing. Report the blended real return used.
  const realPct = blendedRealReturn("a").toFixed(1);
  return `Max spend without running out (at ${realPct}% real): ${spend}${peak}.`;
}

// Solve for the max sustainable spend at the selected confidence, write it into
// the annual-spending field, and re-render the projection at that level.
// Single-scenario only: reads Scenario A's params (the shared, authoritative
// inputs), so it must not be reachable while comparing (the button is hidden
// then — guard anyway). Reuses the SAME param-builder (readScenarios) + the same
// allocation validation as Recompute, then writes the solved number back through
// the canonical annual-spending input and triggers the existing rerender() path.
async function onSolve() {
  if (isComparing()) return; // single-scenario only; button is hidden, guard anyway
  if (simMode === "monte_carlo") return; // MC is view-only; button is disabled, guard anyway

  // Same submit-time allocation guard as Recompute: a bad allocation would 422
  // (or solve a wrong scenario), so block before fetching with a clear message.
  refreshAllocation();
  const fieldErr = validateAllocationInputs();
  if (fieldErr) {
    setSolveReadout(fieldErr, { isError: true });
    return;
  }
  if (!allocationTotalValid("a")) {
    setSolveReadout(
      `Allocation must total 100% (currently ${formatPct(allocationTotalPct("a"))}%)`,
      { isError: true },
    );
    return;
  }

  // Build Scenario A's params via the shared builder (same shape Recompute
  // sends), then add mode + (historical) confidence. The annual_spending the
  // builder reads is irrelevant to the solve (the engine searches it), but it's
  // harmless to send.
  let scenarios;
  try {
    scenarios = readScenarios();
  } catch (err) {
    setSolveReadout(err.message, { isError: true });
    return;
  }
  const params = { ...scenarios.a, mode: simMode };
  // Both stochastic modes (Historical + Monte Carlo) solve against the confidence
  // dial. The correlation knob already rides in scenarios.a (readScenarios), so
  // the MC solve uses the live vol + correlation. Deterministic omits confidence
  // (single path → implicit "the one path must survive").
  if (isStochasticMode()) params.confidence = solveConfidence / 100;

  const btn = document.getElementById("solve-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Solving…";
  }
  try {
    // Working indicator (tax-drag design td1): the solve fetch itself is a "run
    // request in flight" too (Historical solve under drag measured ~3-5s at the
    // timing gate) — wrapped in its own try/finally so it clears on success OR
    // error before the outer solve-readout handling runs. MC never reaches here
    // (guarded above), so mode is always "deterministic" or "historical".
    let data;
    startRunProgress({ splitActive: taxLocationActive(), mode: simMode });
    try {
      data = await postJson(SOLVE_URL, params);
    } finally {
      stopRunProgress();
    }
    // Write the solved spend back into the annual-spending field. It's a single
    // linked field here (single-scenario only), so the A input is canonical.
    const spendEl = aInputEl("annual_spending");
    if (spendEl) spendEl.value = String(data.solved_spend);
    // Keep the lifecycle phase breakdown in sync with the new peak (no-op in
    // constant mode), then re-render the projection at the solved spend via the
    // existing recompute path.
    updateSpendingBreakdown();
    await rerender();
    setSolveReadout(solveReadoutText(data));
  } catch (err) {
    setSolveReadout(err.message, { isError: true });
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Solve max spend";
    }
  }
}

// Read the current input state and render via the path that matches the
// simulation mode and (in Deterministic mode) the comparison state. Historical
// mode is single-scenario in Build 1, so it renders the median from Scenario A's
// inputs only. Deterministic dispatches single / overlay / side-by-side exactly
// as before. Shared by the Recompute submit and every live link/unlink change.
async function rerender() {
  // rerender() is the universal redraw, reached by Recompute AND by
  // non-Recompute paths (link/unlink toggles, comparison-state changes, theme
  // toggle). EVERY entry point re-fetches (readScenarios + the API). onRecompute
  // already gated an invalid/all-zero allocation, but the other entry points
  // sailed past it — buildPostBody then OMITS the all-zero allocation body and
  // the server silently falls back to its 60/40 default, while the live Total
  // row still reads 0% (a confusing silent mismatch). Run the SAME guard here so
  // every fetch path behaves consistently: refresh the live Total (so the
  // invalid-state indicator shows) and bail without a fetch.
  refreshAllocation();
  const strategyErr = validateStrategyAllocation();
  if (strategyErr) {
    setError(strategyErr);
    return null;
  }
  let scenarios;
  try {
    scenarios = readScenarios();
  } catch (err) {
    setError(err.message);
    return null;
  }
  setError("");

  // Working indicator (tax-drag design td1 — timing acceptance): rerender()
  // is the universal fetch funnel (Recompute, mode toggles, link/unlink changes
  // — every path that hits the API), so starting/stopping the indicator HERE
  // covers all of them, not just the Recompute button. splitActive + the live
  // simMode pick the expected-duration copy (prominent for split-active MC); the
  // indicator only reveals itself past ~2s, so fast runs never see it. The
  // try/finally spans every return path below (stochastic fan / stress /
  // three-way dispatch) so a failed OR successful run always clears it.
  const runProgressSplitActive = taxLocationActive();
  startRunProgress({ splitActive: runProgressSplitActive, mode: simMode });
  try {
    // Chart explainers (ctx1): refresh the "How to read this chart" prose + the
    // live key-parameters digest on every recompute, in every mode (the selection
    // logic keys off simMode + the comparison state). Runs before the mode dispatch
    // so the digest reflects the just-read scenarios.
    renderChartExplainers();

    // The deterministic-model caveat is DETERMINISTIC-ONLY. Re-evaluate it on every
    // render path (single / overlay / side-by-side / stochastic fan) so a Recompute
    // or any transition can't leave it stranded visible in Historical / Monte Carlo.
    updateAllocationCaption();

    // Stochastic modes (Historical + Monte Carlo): the fan view. Single fan when
    // all linked; two fans through the existing comparison when a field is
    // unlinked. renderHistorical picks the path, switches the fan sub-view, and
    // updates historicalLastPayloads (its own view-toggle cache). fetchHistorical
    // sends the active simMode, so the SAME render path draws either engine's fan
    // (identical payload shape). lastPayloads is the DETERMINISTIC cache — untouched.
    if (isStochasticMode()) {
      const fanResult = await renderHistorical(scenarios);
      // Bucket-balance chart (bucket-strategy design slice 1b): Historical draws the
      // worst cohort's 3 balances off the A-side payload; MC hides it (stats only).
      // Drawn from the A-side payload so the chart tracks the focused/primary
      // scenario (the bucket config is global, so A and B carry the same buckets).
      updateBucketChartView(fanResult ? fanResult.payloadA : null);
      // Tax-location view (tax-buckets design tb1): Historical/MC draws the taxable
      // fan + the stochastic bridge readout off the A-side payload's
      // tax_location_risk. Hidden when off / gated / comparing (no-op there).
      updateTaxLocationView(fanResult ? fanResult.payloadA : null);
      // Deterministic guardrails spending line (dsc1): stochastic modes are owned by
      // the spending FAN, so tear the deterministic line down here (the gate no-ops on
      // a non-deterministic simMode anyway, but tear down explicitly for symmetry).
      updateDeterministicSpending(null);
      return fanResult;
    }

    // Stress Test (stress-test design slice 1): a Family-A synthetic-cohort stress
    // OVERLAID on the current plan. It produces ONE trajectory, so it reuses the
    // deterministic single-line render path (renderSingle → renderPanel). The only
    // difference from a plain deterministic run is the request flavor: mode=stress +
    // the selected stress_id, injected onto Scenario A's params via
    // PayloadAssembler.buildStressParams (the allocation / spend / GI params are
    // left untouched — the overlay). Slice 1 is single-scenario only (no A/B); the
    // comparison surface for a stress is a later slice.
    if (isStressMode()) {
      const stressParams = PayloadAssembler.buildStressParams(scenarios.a, stressId);
      const result = await renderSingle(stressParams);
      lastPayloads = result;
      updateBucketChartView(result ? result.payloadA : null);
      updateTaxLocationView(result ? result.payloadA : null);
      updateDeterministicSpending(result ? result.payloadA : null);
      return result;
    }

    // Three-way dispatch (slice 10b):
    //   not comparing           -> single panel (10a, unchanged)
    //   comparing + overlay      -> overlay (one chart, two lines) [DEFAULT]
    //   comparing + sidebyside   -> two side-by-side panels (existing, kept working)
    const mode = currentOutputMode();
    let result;
    if (mode === "single") {
      result = await renderSingle(scenarios.a);
    } else if (mode === "overlay") {
      result = await renderOverlay(scenarios.a, scenarios.b);
    } else {
      result = await renderBoth(scenarios.a, scenarios.b);
    }
    // Slice 10c: cache the fetched payloads so a subsequent view-toggle can
    // re-render the other view from this data without a new API round-trip.
    lastPayloads = result;
    // Bucket-balance chart (bucket-strategy design slice 1b): Deterministic draws the
    // exact 3 balances off the A-side payload. Hidden + torn down under single
    // sourcing (updateBucketChartView no-ops there).
    updateBucketChartView(result ? result.payloadA : null);
    // Tax-location view (tax-buckets design tb1): Deterministic draws the 3 account
    // lines + the deterministic bridge readout off the A-side payload's tax_location.
    // Hidden + torn down when off / gated / comparing (updateTaxLocationView no-ops).
    updateTaxLocationView(result ? result.payloadA : null);
    // Deterministic guardrails spending line (dsc1): in Deterministic mode + guardrails
    // + not comparing, draw the single guardrails spending path off the A-side
    // payload's trajectory.points. Hidden + torn down otherwise (constant/lifecycle
    // spending, or comparing — updateDeterministicSpending self-gates).
    updateDeterministicSpending(result ? result.payloadA : null);
    return result;
  } finally {
    stopRunProgress();
  }
}

// Slice 10c: re-render the current comparison view from the cached payloads
// (no API call) when we have fresh data in hand; otherwise fall back to a full
// rerender() (which fetches). Used by the view toggle so flipping overlay <->
// side-by-side is instant and flash-free. Only meaningful while comparing — the
// single state has no toggle.
function renderFromCache() {
  // Chart explainers (ctx1): the view-only toggle path (overlay↔side-by-side,
  // focus null↔A/B) changes WHICH main-chart paragraph applies without a
  // recompute, so refresh the explainers here too. Cheap; reads live state only.
  renderChartExplainers();

  // Tax-location view (tax-buckets design tb1): this path runs only while comparing
  // (the overlay↔side toggle), and the tax-location view is scoped to the
  // non-comparison single-scenario view in tb1 — so keep it hidden/torn-down here.
  updateTaxLocationView(null);
  // Deterministic guardrails spending line (dsc1): single-scenario only in dsc1, so
  // keep it hidden/torn-down on the comparing-only view-toggle path.
  updateDeterministicSpending(null);

  // Build 3: in a stochastic mode (Historical or Monte Carlo) the toggle
  // re-renders the two-fan comparison from the fan cache (no re-simulation).
  // Fall back to a full rerender if there's no fresh fan data.
  if (isStochasticMode()) {
    if (!historicalLastPayloads) return rerender();
    const { payloadA, payloadB } = historicalLastPayloads;
    if (compareState.view() === "overlay") {
      renderHistoricalOverlayFrom(payloadA, payloadB);
    } else {
      renderHistoricalSideFrom(payloadA, payloadB);
    }
    return Promise.resolve(historicalLastPayloads);
  }

  if (!lastPayloads) return rerender();
  const { payloadA, payloadB } = lastPayloads;
  if (compareState.view() === "overlay") {
    const pointsA = payloadA ? payloadA.trajectory.points : [];
    const pointsB = payloadB ? payloadB.trajectory.points : [];
    renderOverlayChart(pointsA, pointsB);
    if (payloadA) {
      document.getElementById("overlay-summary-a").textContent =
        `Scenario A — ${summaryLine(payloadA.trajectory, payloadA.scenario)}`;
      renderTableInto("#overlay-table-a tbody", pointsA);
    }
    if (payloadB) {
      document.getElementById("overlay-summary-b").textContent =
        `Scenario B — ${summaryLine(payloadB.trajectory, payloadB.scenario)}`;
      renderTableInto("#overlay-table-b tbody", pointsB);
    }
  } else {
    const yMax = computeSharedYMax([payloadA, payloadB]);
    if (payloadA) renderPanel("a", payloadA, yMax);
    if (payloadB) renderPanel("b", payloadB, yMax);
  }
  return Promise.resolve(lastPayloads);
}

// ---------- Chart explainers (ctx1) ----------
//
// The DOM seam for the "How to read this chart ▾" disclosures. Gathers the live
// state (readScenarios + the four mode globals + compareState + ASSET_CLASSES),
// hands it to the pure ChartExplainers engine, and writes the selected paragraph +
// the key-parameters bullets into the three expander bodies:
//
//   #explainer-main-text   — the selected MAIN-chart paragraph (overlay/side wins
//                            over sim-mode), keyed off compareState + simMode.
//   #explainer-main-params — the key-parameters digest: ONE list when single, TWO
//                            (Scenario A / Scenario B, each complete) when comparing.
//   #explainer-bucket-text — the Bucket-balances paragraph (paragraph only, no
//                            params); the expander rides with #bucket-chart-view's
//                            visibility, so its text just needs to be current.
//   #explainer-spending-text — the Guardrails spending-fan paragraph (paragraph
//                            only); rides with #spending-fan-wrap.
//
// Called from every render path (rerender + renderFromCache) so the prose + the
// live digest track the current scenario + view. The expanders themselves are
// collapsed-by-default in the markup ([details] with no `open`); this only fills
// their bodies — it never forces them open.
function renderChartExplainers() {
  let scenarios;
  try {
    scenarios = readScenarios();
  } catch (_err) {
    // A malformed form (validation error) already surfaces via setError; leave the
    // last-good explainer text in place rather than blanking it.
    return;
  }

  const comparing = compareState.isComparing();
  const outputMode = compareState.outputMode();

  // --- Main chart paragraph ---
  const mainKey = ChartExplainers.selectMainParagraphKey({
    comparing,
    outputMode,
    simMode,
  });
  setExplainerText("explainer-main-text", ChartExplainers.PARAGRAPHS[mainKey]);

  // --- Main chart key-parameters digest ---
  const baseState = {
    simMode,
    spendingMode,
    allocationMode,
    sourcingMode,
    assetClasses: ASSET_CLASSES,
  };
  const paramsEl = document.getElementById("explainer-main-params");
  if (paramsEl) {
    paramsEl.innerHTML = "";
    if (comparing) {
      paramsEl.appendChild(
        buildParamsBlock("Scenario A", ChartExplainers.formatScenarioParams(scenarios.a, baseState)),
      );
      paramsEl.appendChild(
        buildParamsBlock("Scenario B", ChartExplainers.formatScenarioParams(scenarios.b, baseState)),
      );
    } else {
      paramsEl.appendChild(
        buildParamsBlock(null, ChartExplainers.formatScenarioParams(scenarios.a, baseState)),
      );
    }
  }

  // --- Separate-chart paragraphs (paragraph only, no params) ---
  setExplainerText("explainer-bucket-text", ChartExplainers.PARAGRAPHS.buckets);
  // The spending-fan + deterministic-line explainers are mode-aware: each dynamic
  // spending rule reads differently (guardrails trips a rate band, fr_smooth eases
  // toward full funding, fr_guardrail trips an FR band), so pick the matching copy.
  setExplainerText(
    "explainer-spending-text",
    spendingMode === "fr_smooth"
      ? ChartExplainers.PARAGRAPHS.frSmooth
      : spendingMode === "fr_guardrail"
      ? ChartExplainers.PARAGRAPHS.frGuardrail
      : spendingMode === "fr_tiers"
      ? ChartExplainers.PARAGRAPHS.frTiers
      : spendingMode === "amortization"
      ? ChartExplainers.PARAGRAPHS.amortization
      : ChartExplainers.PARAGRAPHS.guardrails,
  );
  // Deterministic dynamic-spending-line explainer (dsc1). Rides with
  // #det-spending-wrap's visibility (shown only in Deterministic + a dynamic
  // spending mode), so its text just needs to be current; no JS toggles the
  // expander itself.
  setExplainerText(
    "explainer-det-spending-text",
    spendingMode === "fr_smooth"
      ? ChartExplainers.PARAGRAPHS.deterministicSpendingFrSmooth
      : spendingMode === "fr_guardrail"
      ? ChartExplainers.PARAGRAPHS.deterministicSpendingFrGuardrail
      : spendingMode === "fr_tiers"
      ? ChartExplainers.PARAGRAPHS.deterministicSpendingFrTiers
      : spendingMode === "amortization"
      ? ChartExplainers.PARAGRAPHS.deterministicSpendingAmortization
      : ChartExplainers.PARAGRAPHS.deterministicSpending,
  );
  // Tax-location explainer (tax-buckets design tb1): the verbatim how-to-read +
  // the pre-tax caveat appended. Rides with #tax-location-view's visibility, so its
  // text just needs to be current (no JS toggles the expander itself).
  setExplainerText(
    "explainer-tax-location-text",
    `${TaxLocationView.EXPLAINER} ${TaxLocationView.BALANCE_NOTE}`,
  );
  // Federal-tax overlay explainer (federal-tax overlay design ft1): its OWN
  // verbatim "how to read" text, separate from tax-location's above. Rides with
  // #tax-location-view's visibility (shown only when the split is active), so
  // its text just needs to be current (no JS toggles the expander itself).
  setExplainerText("explainer-federal-tax-text", FederalTaxView.EXPLAINER);
}

// Set an explainer paragraph's text by id (no-op when the element is absent, e.g.
// in the reduced test DOM).
function setExplainerText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// Build one key-parameters block: an optional scenario heading (Scenario A / B
// when comparing) + a <ul> of "Label — value" bullets. Returns a DOM fragment-ish
// wrapper <div>.
function buildParamsBlock(heading, bullets) {
  const wrap = document.createElement("div");
  wrap.className = "explainer-params-block";
  if (heading) {
    const h = document.createElement("p");
    h.className = "explainer-params-heading";
    h.textContent = heading;
    wrap.appendChild(h);
  }
  const ul = document.createElement("ul");
  ul.className = "explainer-params-list";
  for (const b of bullets) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "explainer-param-label";
    label.textContent = b.label;
    li.appendChild(label);
    li.appendChild(document.createTextNode(` — ${b.value}`));
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  return wrap;
}

// ---------- wiring ----------

// The single allocation/strategy validity gate, shared by onRecompute AND
// rerender(). Returns the first error message, or "" when the
// strategy inputs are all valid. Covers, in order: per-field weight/return/vol
// bounds, the per-PHASE (glidepath) or per-COLUMN 100% total, the per-bucket
// 100% totals, and the tax-location split sum. The all-zero allocation case is
// caught by the per-column total check (a 0% column != 100%), so every
// FETCH-bearing rerender path — link/unlink toggles, view changes — short-circuits
// here instead of omitting the allocation body and letting the server silently
// fall back to its 60/40 default (the bug). Callers own the messaging: both
// surface the returned string via setError and refuse to fetch.
function validateStrategyAllocation() {
  // No allocation-weight inputs in the DOM at all → the matrix hasn't rendered
  // (pre-render, or a unit-test sandbox whose querySelectorAll/getElementById
  // returns nothing). Treat the strategy portion as valid and skip the gate —
  // validateAllocationInputs would otherwise read every absent weight as ""
  // ("must be a whole number 0–100") and the per-column total would read 0%,
  // both false-positives against a matrix that simply isn't there. This is
  // STRICTLY a presence guard: when the inputs DO exist but read 0% (
  // all-zero bug — 12 zeroed weight fields), they ARE present, so we fall through
  // and the per-column total check below still rejects them.
  if (!allocationWeightInputsPresent()) return "";
  const fieldErr = validateAllocationInputs();
  if (fieldErr) return fieldErr;
  // Glidepath (glidepath design): per-PHASE 100% check. Under glidepath the
  // constant matrix is hidden but its (seeded, valid) inputs still pass the
  // per-column check below; the live surface is the phase matrices, so validate
  // each phase here. A clear, per-phase message names the offending phase.
  if (allocationMode === "glidepath") {
    const labels = glidepathPhaseLabels();
    for (let k = 0; k < labels.length; k++) {
      if (!glidepathPhaseTotalValid(k)) {
        return (
          `Glidepath phase "${labels[k]}" must total 100% ` +
          `(currently ${formatPct(glidepathPhaseTotalPct(k))}%)`
        );
      }
    }
  } else {
    // Per-column 100% check (A always; B only when unlinked). A clear, per-column
    // message so the user knows WHICH allocation is off. This is the check that
    // also rejects an all-zero column (0% != 100%) — guard.
    for (const col of activeAllocCols()) {
      if (!allocationTotalValid(col)) {
        const where = compareState.allocationLinked() ? "Allocation" : `Scenario ${col.toUpperCase()} allocation`;
        return `${where} must total 100% (currently ${formatPct(allocationTotalPct(col))}%)`;
      }
    }
  }
  // Buckets sourcing (bucket-strategy design slice 1b): each of the three bucket
  // allocation matrices must total 100% before we send. A clear, per-bucket message
  // names the offending bucket. Block the recompute otherwise (the live readout
  // already shows the ✗ state).
  if (sourcingMode === "buckets") {
    for (const bid of BUCKET_IDS) {
      if (!bucketTotalValid(bid)) {
        return (
          `${BUCKET_LABELS[bid]} bucket must total 100% ` +
          `(currently ${formatPct(bucketTotalPct(bid))}%)`
        );
      }
    }
  }
  // Tax-location split (tax-buckets design tb1): when a split is active (single
  // sourcing + at least one balance supplied), the three balances must sum to
  // initial_portfolio within $1, or the API 422s. Validate client-side first so the
  // user gets the friendly inline error instead of a server round-trip. Off (no
  // split / buckets mode) → no check.
  return validateTaxLocation();
}

// Submit-time validation of the allocation matrix inputs. The live readers
// (readWeight/readReturn) map empty/NaN to 0 for the Total display, but the
// submit path must NOT silently send a 0 — a cleared "Bonds return" would
// serialize as 0.0000 and produce a wrong 0%-return projection with no error.
// So here we require EVERY active weight (0–100 integer) and EVERY shared return
// (0–15) to be a non-empty, finite, in-range number, mirroring the input bounds.
// Slice 11b-2: weights are validated per ACTIVE column (A always; B when
// unlinked); returns are shared (validated once). Returns the first error
// message, or "" when all fields are valid.
function validateAllocationInputs() {
  for (let i = 0; i < ASSET_CLASSES.length; i++) {
    const name = ASSET_CLASSES[i].label;
    for (const col of activeAllocCols()) {
      const el = document.getElementById(weightId(col, i));
      const raw = el ? el.value.trim() : "";
      const num = parseInt(raw, 10);
      const label = compareState.allocationLinked()
        ? `${name} weight`
        : `Scenario ${col.toUpperCase()} ${name} weight`;
      if (raw === "" || Number.isNaN(num) || !Number.isInteger(num) || num < 0 || num > 100) {
        return `${label} must be a whole number between 0 and 100`;
      }
    }
    const rEl = document.getElementById(returnId(i));
    const rRaw = rEl ? rEl.value.trim() : "";
    const rNum = parseFloat(rRaw);
    // Floor -5% matches the API's _RETURN_MIN (some classes are slightly negative).
    if (rRaw === "" || Number.isNaN(rNum) || rNum < -5 || rNum > 15) {
      return `${name} return must be a number between -5 and 15`;
    }
    // Monte Carlo only: the per-class volatility input must be a non-negative
    // percent (the API 422s vol < 0). The inputs render only in MC mode.
    if (simMode === "monte_carlo") {
      const vEl = document.getElementById(volId(i));
      const vRaw = vEl ? vEl.value.trim() : "";
      const vNum = parseFloat(vRaw);
      if (vRaw === "" || Number.isNaN(vNum) || vNum < 0) {
        return `${name} volatility must be a number ≥ 0`;
      }
    }
  }
  // Monte Carlo only: the equity–bond correlation must be in [−0.99, +0.99]
  // (the API 422s outside that — ±1 is rank-deficient for the Cholesky factor).
  if (simMode === "monte_carlo") {
    const cEl = document.getElementById(CORRELATION_INPUT_ID);
    const cRaw = cEl ? cEl.value.trim() : "";
    const cNum = parseFloat(cRaw);
    if (cRaw === "" || Number.isNaN(cNum) || Math.abs(cNum) > CORRELATION_LIMIT) {
      return `Equity–Bond correlation must be between −${CORRELATION_LIMIT} and +${CORRELATION_LIMIT}`;
    }
  }
  return "";
}

// Tax-location split client-side validation (tax-buckets design tb1). Returns "" when
// off (no split, or buckets mode) or when the three balances sum to
// initial_portfolio within $1; otherwise a friendly inline message naming the gap.
// Mirrors the API's sum rule so the user never has to round-trip to discover a
// mismatch. The initial_portfolio target reads off the live A-side input.
function validateTaxLocation() {
  if (!taxLocationActive()) return "";
  const split = readTaxLocation();
  let target = 0;
  try {
    target = readFieldValue(
      FIELDS.find((f) => f.name === "initial_portfolio"),
      aInputEl("initial_portfolio"),
    );
  } catch (_err) {
    // The starting-portfolio field is itself invalid/empty; its own required-field
    // error will surface on submit. Don't pile on a tax-location message.
    return "";
  }
  const helper = TaxLocationView.sumHelper({ ...split, target });
  if (helper.status === "balanced") return "";
  return (
    `Tax-location balances must add up to your starting portfolio ` +
    `(${TaxLocationView.fmtMoneyShort(target)}). Right now they're ${helper.text}.`
  );
}

async function onRecompute(e) {
  e.preventDefault();
  // Re-entrancy guard: a Recompute (esp. MC + buckets, ~16s) is long enough that
  // a user may click twice. Ignore clicks while a run is already in flight so we
  // never fire a second overlapping run. (Validation below can still bail early
  // and set an error WITHOUT entering the busy state — those return paths leave
  // recomputeInFlight false because we only set it just before the fetch.)
  if (recomputeInFlight) return;
  // Slice 11b-1: block the recompute if the allocation weights don't sum to
  // 100%, OR any weight/return field is empty/NaN/out-of-range. Reflect the
  // invalid state on the Total row and surface the form error; do NOT fetch.
  // refreshAllocation() also keeps the Total state live as the user types, so
  // this is the backstop at submit time. validateStrategyAllocation() owns the
  // full block (field/column/glidepath/buckets/tax-location); rerender() reuses
  // the SAME guard so non-Recompute rerenders can't sail past it.
  refreshAllocation();
  const strategyErr = validateStrategyAllocation();
  if (strategyErr) {
    setError(strategyErr);
    return;
  }
  // rerender() reads the form and dispatches to the single- or dual-scenario
  // render path based on the current comparison state. Wrap it in the busy
  // affordance: disable Recompute + show "Computing…" before the trajectory
  // fetch(es), and clear it in finally so it resets on success AND on error.
  recomputeInFlight = true;
  setRecomputeBusy(true);
  try {
    await rerender();
  } finally {
    recomputeInFlight = false;
    setRecomputeBusy(false);
  }
}

// UX REFRESH (ux1): the "Strategy & tuning" disclosure remembers its open/closed
// state across recomputes WITHIN the session, so active exploration isn't penalised
// by the fold snapping shut on every recompute. Default CLOSED on first load (no
// stored value). This is presentation-only state: it touches no setter, no input
// value, and no API call — it only restores + persists the <details open> flag via
// sessionStorage. Storage failures (private mode, disabled storage) degrade to the
// default-closed, non-persisted behaviour without throwing.
const STRATEGY_OPEN_KEY = "modeler.strategyOpen";
function wireStrategyDisclosure() {
  const el = document.getElementById("strategy-group");
  if (!el) return;
  let stored = null;
  try {
    stored = sessionStorage.getItem(STRATEGY_OPEN_KEY);
  } catch (_e) {
    stored = null;
  }
  // Only an explicit prior "true" opens it; absent/anything-else => closed default.
  el.open = stored === "true";
  el.addEventListener("toggle", () => {
    try {
      sessionStorage.setItem(STRATEGY_OPEN_KEY, el.open ? "true" : "false");
    } catch (_e) {
      /* storage unavailable — keep working without persistence */
    }
  });
}

// ---------- Dark-mode theme toggle (slice 2) ----------
//
// The anti-flash script in index.html already set document.documentElement
// .dataset.theme before first paint (from localStorage 'rm-theme', else the OS
// preference). These helpers reflect that state into the header button, flip it
// on click, and follow the OS while no manual choice is stored.
//
// localStorage key contract: 'dark' | 'light' = a manual choice; ABSENT = auto
// (track the OS). The anti-flash script READS it; only the click handler WRITES.
const THEME_KEY = "rm-theme";

// The theme actually applied to <html> right now ("dark" | "light"). The
// anti-flash script guarantees data-theme is one of these by first paint.
function resolvedTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

// Sync the toggle button's glyph + aria-label to the live theme. In LIGHT mode
// show a moon (☾ = "click for dark"); in DARK mode a sun (☀ = "click for light").
function syncThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const dark = resolvedTheme() === "dark";
  btn.textContent = dark ? "☀" : "☾";
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  btn.setAttribute("aria-label", label);
  btn.setAttribute("title", label);
}

// Click: flip to the opposite of the currently-resolved theme, persist the
// choice, resync the button, and RE-RENDER so charts re-read their CSS color
// tokens (Slice 1 made every chart color a render-time cssVar() read; a redraw
// is what swaps them). rerender() is the universal redraw — it rebuilds every
// Chart instance across single / overlay / side-by-side / stochastic, which the
// comparing-only renderFromCache() path does not. (Judgment call: see report.)
function onThemeToggle() {
  const next = resolvedTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_e) {
    /* storage unavailable — toggle still works for this session */
  }
  syncThemeToggle();
  rerender();
}

// Live OS reactivity: when the OS scheme flips AND the user has not made a
// manual choice (no stored 'rm-theme'), follow the OS and redraw. Once a manual
// choice is stored, this is a no-op (the stored value wins until cleared).
function wireThemeOsListener() {
  if (!window.matchMedia) return;
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = (e) => {
    let stored = null;
    try {
      stored = localStorage.getItem(THEME_KEY);
    } catch (_e) {
      stored = null;
    }
    if (stored === "dark" || stored === "light") return; // manual choice wins
    document.documentElement.dataset.theme = e.matches ? "dark" : "light";
    syncThemeToggle();
    rerender();
  };
  if (typeof mq.addEventListener === "function") {
    mq.addEventListener("change", handler);
  } else if (typeof mq.addListener === "function") {
    mq.addListener(handler); // older Safari
  }
}

function wireUp() {
  // Enter-in-a-field still submits the form (onRecompute calls preventDefault).
  document.getElementById("inputs-form").addEventListener("submit", onRecompute);
  // UX REFRESH (ux1): restore + persist the Strategy disclosure's open state.
  wireStrategyDisclosure();
  // The Recompute button now lives in the pinned action bar, OUTSIDE the <form>,
  // so it can no longer be a type=submit. Wire its click to the SAME recompute
  // handler (onRecompute reads the form and dispatches the render).
  const recomputeBtn = document.getElementById("recompute-btn");
  if (recomputeBtn) recomputeBtn.addEventListener("click", onRecompute);
  // Dark-mode slice 2: header theme toggle. Reflect the pre-paint theme into the
  // button, wire the click flip, and follow the OS while no manual choice exists.
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", onThemeToggle);
  syncThemeToggle();
  wireThemeOsListener();
  // Reset (rst1): the secondary action-bar button that restores the full
  // default-load state in place (confirm -> applyScenarioToForm(default blob)).
  const resetBtn = document.getElementById("reset-btn");
  if (resetBtn) resetBtn.addEventListener("click", resetToDefaults);
  // Tax-location enable checkbox (tb1b): CHECK pre-fills the default split from the
  // starting portfolio; UNCHECK clears the three inputs. No recompute (button-driven).
  const taxEnableBox = document.getElementById(TAX_LOCATION_ENABLE_ID);
  if (taxEnableBox) taxEnableBox.addEventListener("change", onTaxLocationEnableToggle);
  // Saved Scenarios (sv1): the "Scenarios ▾" popover control in the action bar.
  wireScenariosControl();
  document.getElementById("link-master").addEventListener("click", onMasterLink);
  // The Focus A | Focus B control collapses a comparison, keeping the chosen
  // scenario as the single baseline (Focus B copies B→A first; see onFocus).
  for (const btn of document.querySelectorAll("#focus-control .focus-btn")) {
    btn.addEventListener("click", () => onFocus(btn.dataset.focus));
  }
  // Slice 11b-2: the allocation section's own link/unlink toggle.
  const allocBtn = document.getElementById("allocation-link");
  if (allocBtn) allocBtn.addEventListener("click", toggleAllocationLink);
  // Slice 4b: the preset picker. Choosing a named portfolio populates the matrix
  // + recomputes (applyPreset); "Custom" clears the note without touching weights.
  const presetSel = document.getElementById("preset-select");
  if (presetSel) {
    presetSel.addEventListener("change", () => {
      if (presetSel.value === "") {
        showPresetNote(null);
        return;
      }
      applyPreset(parseInt(presetSel.value, 10));
    });
  }
  // gpp1: the glide-preset picker. Choosing a named glide fills the per-phase
  // glidepath weights (applyGlidePreset, switching to glidepath mode if needed) and
  // recomputes; it then resets to its placeholder (fire-once, like the portfolio
  // Preset dropdown). The placeholder option does nothing.
  renderGlidePresetOptions();
  const glidePresetSel = document.getElementById("glide-preset-select");
  if (glidePresetSel) {
    glidePresetSel.addEventListener("change", () => {
      if (glidePresetSel.value === "") return;
      applyGlidePreset(parseInt(glidePresetSel.value, 10));
    });
  }
  // Slice 10c: the view-toggle buttons each request their own viewMode.
  for (const btn of document.querySelectorAll(".view-toggle-btn")) {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  }
  // Return Variability (Build 1): the mode-toggle buttons (Deterministic |
  // Historical) each request their own simMode. Scope to the sim-mode toggle so
  // we don't double-bind the spending toggle's buttons (it reuses .mode-toggle-btn).
  for (const btn of document.querySelectorAll("#mode-toggle .mode-toggle-btn")) {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  }
  // Stress Test (stress-test design slice 1): the sub-selector picks which
  // synthetic-cohort stress to overlay. Changing it updates the active stress id
  // and re-runs (only meaningful in stress mode — the selector is hidden
  // otherwise). Initialize stressId from the control's current value so the two
  // can't drift.
  const stressSel = document.getElementById("stress-select");
  if (stressSel) {
    stressId = stressSel.value || stressId;
    stressSel.addEventListener("change", () => {
      stressId = stressSel.value;
      if (isStressMode()) rerender();
    });
  }
  // Lifecycle spending: the spending-shape toggle (Constant-real | Lifecycle).
  for (const btn of document.querySelectorAll("#spending-toggle .mode-toggle-btn")) {
    btn.addEventListener("click", () => setSpendingMode(btn.dataset.spendingMode));
  }
  // Buckets sourcing: the sourcing-shape toggle (Single | Buckets). Scoped to its
  // own group so it doesn't double-bind the other .mode-toggle-btn buttons.
  for (const btn of document.querySelectorAll("#sourcing-toggle .mode-toggle-btn")) {
    btn.addEventListener("click", () => setSourcingMode(btn.dataset.sourcingMode));
  }
  // Glidepath allocation: the Constant | Glidepath toggle. Scoped to its own group
  // so it doesn't double-bind the other .mode-toggle-btn buttons.
  for (const btn of document.querySelectorAll(
    "#allocation-mode-toggle .mode-toggle-btn",
  )) {
    btn.addEventListener("click", () => setAllocationMode(btn.dataset.allocationMode));
  }
  // Spending solver (slice 2): the confidence dial stops + the Solve button.
  for (const btn of document.querySelectorAll("#solve-dial .solve-dial-btn")) {
    btn.addEventListener("click", () => setConfidence(parseInt(btn.dataset.confidence, 10)));
  }
  const solveBtn = document.getElementById("solve-btn");
  if (solveBtn) solveBtn.addEventListener("click", onSolve);
  // Keep the read-only phase breakdown live as the user edits the go-go (annual
  // spending) input or the start age. Delegated on the form so it survives the
  // field rows being re-rendered on link/unlink. No-op in constant mode (the
  // breakdown is hidden there). Does NOT recompute the projection — Recompute does.
  const form = document.getElementById("inputs-form");
  if (form) {
    form.addEventListener("input", (e) => {
      const id = e.target && e.target.id;
      // SS claim-age slider (slice 2): keep each slider's live readout (selected
      // age · derived annual benefit · % of FRA) current. Moving a slider refreshes
      // its OWN readout; editing an FRA-amount input refreshes EVERY slider readout
      // (the dollar figure depends on the amount). No recompute (Recompute does
      // that) — the slider value is read on the next Recompute via readFormState,
      // exactly like every other field. Done first so it fires in every mode.
      if (e.target && e.target.dataset && e.target.dataset.ssSlider === "1") {
        refreshSsReadout(e.target);
      } else if (id && /_ss_fra_amount(-b)?$/.test(id)) {
        refreshAllSsReadouts();
      }
      // Glidepath: re-derive the phase matrices when an age that drives a boundary
      // changes (the GI start ages, horizon start/end), so the labels + matrix
      // count track the inflections live. No recompute (Recompute does that). Done
      // first so it fires regardless of spending mode.
      if (allocationMode === "glidepath") {
        const boundaryAgeIds = [
          aId("start_age"), aId("end_age"),
          aId("person_a_pension_start_age"), aId("person_b_pension_start_age"),
          aId("person_a_ss_claim_age"), aId("person_b_ss_claim_age"),
        ];
        if (boundaryAgeIds.includes(id)) renderGlidepathPhases();
      }
      // Tax-location (tax-buckets design tb1): keep the running-sum helper live as the
      // user types any of the three balances OR edits the starting portfolio (which
      // is the sum target). No recompute (Recompute does that) — purely the helper.
      // Done BEFORE the spending-mode short-circuit so it fires in every mode.
      if (
        id === TAX_TAXABLE_INPUT_ID ||
        id === TAX_DEFERRED_INPUT_ID ||
        id === TAX_ROTH_INPUT_ID ||
        id === aId("initial_portfolio")
      ) {
        updateTaxLocationSum();
      }
      // Live in every shape that shows a note: lifecycle (phase $ levels) and the
      // dynamic modes guardrails / fr_smooth (the start-spend in the note). No-op
      // in constant (no note).
      if (spendingMode !== "lifecycle" && !isDynamicSpending()) return;
      if (id === aId("annual_spending") || id === aId("start_age")) {
        updateSpendingBreakdown();
      }
    });
  }
}

// Initial load: every field starts linked, so we're NOT comparing — render a
// single scenario panel (panel A only; panel B hidden via .panels.is-single).
// Fetch the default scenario once, capture the resolved defaults, render it into
// panel A, then build the inputs section seeded with those defaults. Inputs are
// built AFTER the default scenario is known so every field renders with its
// correct starting value. updatePanelChrome() runs first so the single-column /
// "Projection"-title layout is in place before the chart draws.
async function init() {
  initModelAssumptions();
  initPlanFiles();
  wireUp();
  // Slice 8: register the single subscriber for every ComparisonState
  // transition. The subscribed function handles chrome refresh + row sync +
  // render dispatch — atomic, one notify per transition. Subscription happens
  // BEFORE any user interaction can fire a transition, but no init code
  // mutates compareState, so the first call only happens on first click.
  compareState.subscribe(onComparisonStateChanged);
  updateModeToggleSelection();   // Deterministic selected by default (Build 1)
  updateModeRegions();           // historical view hidden on load (Deterministic)
  updatePanelChrome();
  updateOutputMode();
  updateViewToggle();            // hidden on load (all linked => not comparing)
  updateViewToggleSelection();   // overlay selected by default
  updateAllocationCaption();     // hidden on load (allocation starts linked)
  const { payloadA } = await renderSingle(null);
  if (payloadA) {
    defaultScenario = payloadA.scenario;
    lastPayloads = { payloadA, payloadB: null };
    // Round-trip each class's per-class volatility + the correlation from the
    // echoed default scenario into the front-end model BEFORE buildAllocation()
    // reads them, so the vol inputs render with the correct seeded values (Bonds
    // 0.088, not the equity-default 0.174) and the correlation input shows +0.20.
    // Must run before buildAllocation() below.
    seedVolatilitiesFromScenario(defaultScenario);
    seedCorrelationFromScenario(defaultScenario);
  }
  // Slice 4b: load the canonical 12-class menu + the named presets BEFORE the
  // matrix is built, so the rows render with all 12 classes (legacy two at the
  // 60/40 canary, others at 0%) and the picker is populated. Both degrade
  // gracefully (a failed /api/classes keeps the 2-class fallback). Run after the
  // default-scenario seed so the legacy classes keep their echoed canary weights.
  await loadClassMenu(defaultScenario);
  await loadPresets();
  buildInputs();
  // SS claim-age readout (slice 2): buildInputs() renders the rows + seeds each
  // field from its default, but the live readout span can race the FRA-amount
  // field's presence at slider-build time, leaving it blank until the user nudges
  // the slider. Refresh every readout once here — after all field rows exist and
  // carry their defaults — so a fresh load shows the correct default effective
  // benefit (e.g. "Claim at 67 · $33,000/yr · 100% of FRA" for Person A).
  refreshAllSsReadouts();
  // Household editor (M5): off by default; built after the field rows so its
  // default people read the seeded horizon.
  initHouseholdEditor();
  // Slice 11b-1: build the editable allocation matrix + shared return inputs and
  // run the first live refresh (Total + blended-return line). Built here so all
  // input construction happens in one place on load; ASSET_CLASSES now carries the
  // full 12-class menu (loadClassMenu above).
  buildAllocation();
  // Glidepath allocation: constant selected by default (today's behavior); the
  // phase matrices + caption stay hidden until Glidepath is chosen. Run after
  // buildAllocation so the single-matrix seed is readable.
  updateAllocationModeView();
  // Lifecycle spending: constant-real selected by default (today's behavior);
  // breakdown hidden until Lifecycle is chosen. Run after buildInputs so the
  // breakdown can read the seeded annual-spending input.
  updateSpendingToggleSelection();
  updateSpendingBreakdown();
  // Buckets sourcing (bucket-strategy slice 1a/1b): single selected by default
  // (today's behavior); the buffer-survival readout + bucket config + bucket chart
  // stay hidden until Buckets is chosen. The matrices are rendered now (off the
  // loaded 12-class menu) so they're ready the moment Buckets is toggled on. Run
  // after buildAllocation so ASSET_CLASSES carries the full menu.
  updateSourcingToggleSelection();
  updateBufferSurvivalReadout(null);
  updateEffectiveEquityReadout(null);
  seedBucketWeights();
  renderBucketMatrices();
  updateSourcingModeView();
  updateBucketChartView(null);
  // Tax-location (tax-buckets design tb1): the three inputs start blank (feature off
  // — the canary single-pool default is preserved). Set the input gate (single by
  // default, so enabled) + the live sum helper (hidden while all blank), and keep
  // the tax-location chart torn down until a split is supplied + recomputed.
  updateTaxLocationGate();
  updateTaxLocationView(null);
  // Deterministic guardrails spending line (dsc1): default scenario is constant
  // spending, so the chart starts hidden; renderSingle() on first paint redraws it
  // only if guardrails is selected. Torn down here for the default-hidden start.
  updateDeterministicSpending(null);
  // Spending solver (slice 2): 80% dial stop selected by default; the control is
  // shown (all linked => not comparing) but its dial is hidden on load
  // (Deterministic default — no confidence). The readout starts empty.
  updateSolveDialSelection();
  updateSolveControl();
  updateMasterButton();
  updateLegend();
  // Chart explainers (ctx1): populate the "How to read this chart" expander on
  // first paint. init() seeds the first chart via renderSingle()/update* WITHOUT
  // routing through rerender(), so renderChartExplainers() (wired into rerender()
  // + renderFromCache()) would otherwise stay empty until the user's first mode
  // change/Recompute. Call it once here — by now defaultScenario + lastPayloads
  // are set, the form (buildInputs/buildAllocation) is built so readScenarios()
  // succeeds, and compareState/simMode/spendingMode globals are initialized — so
  // a fresh Deterministic load shows the paragraph + single-scenario param bullets
  // immediately.
  renderChartExplainers();
  // Saved Scenarios (sv1): prefetch the saved list so the popover opens warm
  // (it also refetches on open). Best-effort — a failed fetch is swallowed and
  // the popover degrades to the save-only row.
  prefetchScenarioList();
}

// Best-effort, non-blocking prefetch of the saved-scenario list at init so the
// first popover open is instant. The popover always refetches on open, so this
// is purely a warming step; failures are swallowed.
function prefetchScenarioList() {
  fetchScenarioList().catch(() => {});
}

// Bootstrap only in a real browser. Loaded into a node:vm sandbox (the
// GlidepathView test), there's no DOM, so skip init() and just expose the pure
// helpers attached to `window` above.
if (typeof document !== "undefined" && typeof document.getElementById === "function") {
  init();
}
