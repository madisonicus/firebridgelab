// Browser check (Playwright). Not part of the automated test suite.
//
// Runs against a running server at BASE_URL (default http://127.0.0.1:8000),
// e.g. `uvicorn api.main:app --port 8000` from the repo root with data/cache
// populated. Install the browser driver once with
// `npm install playwright && npx playwright install chromium`.
// Screenshots go to SHOT_DIR (default /tmp/<script>_shots).
//
// Popover gate (tb1c) — the durable guard against the recurring "ⓘ popover pops
// under the main pane / off-screen / collapses to a sliver" bug.
//
// Positioning for every ⓘ info popover is handled by CONTAINER-LEVEL CSS rules in
// styles.css (`.inputs-section .info-popover` / `.content-region .info-popover`),
// wrapper-agnostic on purpose. This script proves that contract holds for every
// popover that is ACTUALLY VISIBLE in a given UI state — and, because it iterates
// all `.info` triggers and drives the app through the modes that reveal the
// sidebar's mode-specific popovers (glidepath / buckets / guardrails), it covers
// the popovers that have historically clipped. Run it after adding any ⓘ.
//
// Key correctness rule: a popover only "clips" when it is DISPLAYED. A trigger that
// is hidden because its mode/section is inactive has zero area and nothing to clip —
// so we SKIP it (per state), we do NOT fail it. A trigger that IS visible but whose
// popover renders off-screen, occluded, or as a <5px sliver is a real FAIL.
//

import pw from "playwright";
const { chromium } = pw;

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

const browser = await chromium.launch({ headless: true });
// Wide viewport: at >=1100px the sidebar is a 300px overflow-clipping grid column —
// the exact condition the sidebar clip-fix targets. This is where the bug bites.
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await ctx.newPage();

const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(`console.error: ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

const sleep = (ms) => page.waitForTimeout(ms);
const seen = new Set();   // labels already PASSED (dedup across states)
let failures = 0;

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.waitForSelector("#inputs-form", { timeout: 15000 });

// Open every non-`.info` <details> (Strategy & tuning fold, per-table drill-downs)
// so popovers nested inside collapsed disclosures are laid out and measurable.
async function openAllDisclosures() {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll("details")) {
      if (!d.classList.contains("info")) d.open = true;
    }
  });
  await sleep(300);
}

// Test every CURRENTLY-VISIBLE popover not already passed. Hidden triggers (wrong
// mode / collapsed section) are skipped, not failed.
async function testVisible(stateLabel) {
  const n = await page.$$eval(".info", (els) => els.length);
  for (let i = 0; i < n; i++) {
    // Step 1: open only this trigger; if its section/mode is inactive (display:none
    // → offsetParent null, or zero-area glyph) skip; otherwise scroll it into view so
    // the popover is measured where a user would actually see it (not below the fold
    // of an all-disclosures-expanded page — that's a scroll artifact, not a clip).
    const pre = await page.evaluate((idx) => {
      const infos = [...document.querySelectorAll(".info")];
      infos.forEach((d, j) => { d.open = j === idx; });
      const info = infos[idx];
      if (!info) return { state: "missing" };
      const pop = info.querySelector(".info-popover");
      if (!pop) return { state: "nopop" };
      const sum = info.querySelector("summary") || info;
      const label =
        (sum.getAttribute && (sum.getAttribute("aria-label") || sum.title)) ||
        (pop.textContent || "").trim().slice(0, 46) ||
        (info.closest("[id]") && info.closest("[id]").id) || `#${idx}`;
      const tr = sum.getBoundingClientRect();
      const triggerRendered = info.offsetParent !== null && tr.width > 0 && tr.height > 0;
      if (!triggerRendered) { info.open = false; return { state: "hidden", label }; }
      sum.scrollIntoView({ block: "center", inline: "center" });
      return { state: "visible", label };
    }, i);

    if (pre.state !== "visible") continue;          // hidden/missing → skip silently
    if (seen.has(pre.label)) continue;
    await sleep(120);                                // let the scroll settle

    // Step 2: measure the now-on-screen popover.
    const res = await page.evaluate((idx) => {
      const info = [...document.querySelectorAll(".info")][idx];
      const pop = info.querySelector(".info-popover");
      const r = pop.getBoundingClientRect();
      const vw = window.innerWidth, vh = window.innerHeight;
      const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
      const hit = document.elementFromPoint(cx, cy);
      info.open = false;
      return {
        rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
        vw, vh,
        hitIsPopover: hit === pop || (hit && pop.contains(hit)),
        hitTag: hit ? hit.tagName.toLowerCase() : null,
        hitClass: hit ? String(hit.className) : null,
      };
    }, i);
    res.label = pre.label;
    if (seen.has(res.label)) continue;              // already passed in an earlier state
    const r = res.rect;
    const inViewport = r.left >= -0.5 && r.top >= -0.5 && r.right <= res.vw + 0.5 && r.bottom <= res.vh + 0.5;
    const hasArea = r.width > 4 && r.height > 4;     // guards the sv1b sliver regression
    const ok = inViewport && hasArea && res.hitIsPopover;
    if (ok) {
      seen.add(res.label);
      console.log(`PASS [${stateLabel}]: ${res.label} — rect[${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.right)},${Math.round(r.bottom)}]`);
    } else {
      failures++;
      console.log(`FAIL [${stateLabel}]: ${res.label} — inViewport=${inViewport} hasArea=${hasArea} hitIsPopover=${res.hitIsPopover} ` +
        `rect[${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.right)},${Math.round(r.bottom)}] hit=<${res.hitTag} class="${res.hitClass}">`);
    }
  }
}

const click = async (sel) => { try { await page.locator(sel).first().click({ timeout: 3000 }); await sleep(400); } catch {} };

// --- State 1: default (deterministic / constant / single), table drill-down open ---
await openAllDisclosures();
await testVisible("default");

// --- State 2: glidepath allocation (reveals #glidepath-caption ⓘ) ---
await click("#strategy-group > summary");
await click('#allocation-mode-toggle [data-allocation-mode="glidepath"]');
await openAllDisclosures();
await testVisible("glidepath");

// --- State 3: buckets sourcing (reveals bucket-years-hint / bucket-caption ⓘ) ---
await click('#sourcing-toggle [data-sourcing-mode="buckets"]');
await openAllDisclosures();
await testVisible("buckets");

// --- State 4: guardrails spending + historical lens (reveals satiation / spending-fan ⓘ) ---
await click('#sourcing-toggle [data-sourcing-mode="single"]');
await click('#spending-toggle [data-spending-mode="guardrails"]');
await click('#mode-toggle [data-mode="historical"]');
await sleep(3500);
await openAllDisclosures();
await testVisible("guardrails+historical");

console.log(`\n${seen.size} distinct popover(s) tested + on-screen + un-occluded; ${failures} failure(s).`);
if (errors.length) {
  console.log("CONSOLE/PAGE ERRORS:");
  for (const e of errors) console.log("  " + e);
  failures++;
}
process.exitCode = failures ? 1 : 0;
console.log(failures ? "RESULT: FAIL" : "RESULT: PASS");
await browser.close();
