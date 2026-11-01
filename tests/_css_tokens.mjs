// _css_tokens.mjs — shared test helper for the color-token refactor (dark-mode
// slice 1). Color now lives in web/styles.css as CSS custom properties; the
// chart code in app.js / chart_view.js READS those tokens at render time via
//
//   getComputedStyle(document.documentElement).getPropertyValue(name)
//
// The JS tests run those modules in a node:vm sandbox that has no real DOM, so
// we inject a `getComputedStyle` stub backed by the ACTUAL token values parsed
// from styles.css. This keeps the tests asserting the real rendered colors
// (drift-proof: edit a token, the test reads the new value) without a browser.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STYLES_PATH = path.resolve(__dirname, "../web/styles.css");

// Parse the `:root { ... }` custom-property declarations into a name→value map.
// Only the first :root block is read (the token table lives there).
export function loadCssTokens() {
  const css = fs.readFileSync(STYLES_PATH, "utf8");
  const rootStart = css.indexOf(":root");
  const open = css.indexOf("{", rootStart);
  const close = css.indexOf("}", open);
  const body = css.slice(open + 1, close);
  const tokens = {};
  const re = /(--[\w-]+)\s*:\s*([^;]+);/g;
  let m;
  while ((m = re.exec(body)) !== null) {
    tokens[m[1].trim()] = m[2].trim();
  }
  return tokens;
}

// A getComputedStyle stub whose getPropertyValue(name) returns the parsed token
// value (or "" for unknown names, matching the browser's behavior). Shape mirrors
// what cssVar() touches: getComputedStyle(el).getPropertyValue(name).
export function makeGetComputedStyle(tokens = loadCssTokens()) {
  return function getComputedStyle() {
    return {
      getPropertyValue: (name) =>
        Object.prototype.hasOwnProperty.call(tokens, name) ? tokens[name] : "",
    };
  };
}
