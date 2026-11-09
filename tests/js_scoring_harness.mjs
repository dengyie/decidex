/**
 * Numerical scoring harness for the browser decision engine.
 *
 * Extracts the pure scoring functions (and their module-level constants)
 * out of tools/balatro_wasm/web/app.js and evaluates JSON test cases passed
 * on stdin: [{ name, fn, args }, ...] -> [{ name, ok, result|error }, ...].
 *
 * Extraction is per-definition (brace-walked), so browser-coupled module
 * scope (canvas/document lookups) never enters the sandbox.
 *
 * Usage: node tests/js_scoring_harness.mjs <path-to-app.js> < cases.json
 */
import { readFileSync } from "node:fs";

const appPath = process.argv[2];
if (!appPath) throw new Error("usage: node js_scoring_harness.mjs <app.js>");
const src = readFileSync(appPath, "utf8");

function extractDef(name) {
  let idx = src.indexOf("function " + name);
  if (idx < 0) idx = src.indexOf("const " + name + " =");
  if (idx < 0) throw new Error("definition not found: " + name);
  const brace = src.indexOf("{", idx);
  if (brace < 0) throw new Error("no body for: " + name);
  let depth = 0;
  let end = -1;
  for (let i = brace; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        end = i + 1;
        break;
      }
    }
  }
  if (end < 0) throw new Error("unbalanced body for: " + name);
  let def = src.slice(idx, end);
  if (src.slice(idx, idx + 6) === "const ") def += ";";
  return def;
}

const NEEDS = [
  "RANK_NUM", "RANK_VALUES", "POKER_HANDS", "POKER_HAND_TIERS",
  "JOKER_PILLAR_MAP", "PLANET_HAND_MAP",
  "isStraight", "getJokerFlags", "getScoringCards", "detectConstraints",
  "bannedHandTypeReason",
  "evaluateHandScore", "findBestPlays", "isProtectedHeldCard",
  "findBestDiscards", "optimizePlayWithJunkFilter", "arbitrateAction",
];

// Optional definitions: nice for debugging but not referenced by any
// function under test. Everything else in NEEDS is REQUIRED -- a missing
// required definition must fail loudly, never silently.
const OPTIONAL = new Set(["JOKER_PILLAR_MAP", "PLANET_HAND_MAP", "POKER_HAND_TIERS"]);

const parts = [];
for (const name of NEEDS) {
  try {
    parts.push(extractDef(name));
  } catch (err) {
    if (OPTIONAL.has(name)) {
      parts.push(`/* optional: ${name} */ void 0;`);
    } else {
      throw err;
    }
  }
}

const factory = new Function(parts.join("\n") + "\n" +
  "return { RANK_NUM, RANK_VALUES, POKER_HANDS, isStraight, getScoringCards, evaluateHandScore, findBestPlays, findBestDiscards, arbitrateAction, detectConstraints };");
const api = factory();

// stdout carries the JSON result only: decision-engine diagnostics (e.g. the
// pruned-search notice) must not corrupt it.
console.log = (...args) => process.stderr.write(args.join(" ") + "\n");

const cases = JSON.parse(readFileSync(0, "utf8"));
const out = [];
for (const c of cases) {
  try {
    const fn = api[c.fn];
    if (!fn) throw new Error("api has no function " + c.fn);
    out.push({ name: c.name, ok: true, result: fn(...c.args) });
  } catch (err) {
    out.push({ name: c.name, ok: false, error: String((err && err.message) || err) });
  }
}
process.stdout.write(JSON.stringify(out));
