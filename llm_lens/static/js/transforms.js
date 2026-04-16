// Pure text→text transforms. Run on the client; the result is sent to
// /edit, which writes it verbatim. Ports of the server-side helpers that
// used to live in llm_lens/__init__.py (_scrub_content, _normalize_ws,
// _strip_swears, _strip_filler). Behaviour should stay in sync with the
// Python regex semantics those tested against.

export const SCRUB_PLACEHOLDER = ".";

export function scrub() {
  return SCRUB_PLACEHOLDER;
}

// Collapse cosmetic whitespace without touching code-shaped content.
// - Lines inside a triple-backtick fenced block: left alone.
// - Lines starting with space/tab: rstrip only (preserve indentation).
// - Other prose lines: collapse inline [ \t]+ → single space, then rstrip.
// Then collapse 3+ newlines → 2.
export function normalizeWs(text) {
  if (!text) return text;
  const lines = text.split("\n");
  const out = [];
  let inFence = false;
  const WS = /[ \t]+/g;
  const RTRIM = /[ \t]+$/;
  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      inFence = !inFence;
      out.push(line);
      continue;
    }
    if (inFence) {
      out.push(line);
      continue;
    }
    if (line.length > 0 && (line[0] === " " || line[0] === "\t")) {
      out.push(line.replace(RTRIM, ""));
      continue;
    }
    out.push(line.replace(WS, " ").replace(RTRIM, ""));
  }
  return out.join("\n").replace(/\n{3,}/g, "\n\n");
}

// Mirrors Python _SAFE_STEM_SUFFIXES exactly.
const SAFE_STEM_SUFFIXES = ["", "s", "es", "ed", "er", "ers", "ing", "ings", "y", "ery", "ies"];

function reEscape(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function swearRegexParts(words) {
  const parts = [];
  for (const w of words || []) {
    if (typeof w !== "string" || !w.trim()) continue;
    if (w.endsWith("*") && w.length > 1) {
      const stem = reEscape(w.slice(0, -1));
      const suffixAlt = SAFE_STEM_SUFFIXES.map(reEscape).join("|");
      parts.push(`${stem}(?:${suffixAlt})`);
    } else {
      parts.push(reEscape(w));
    }
  }
  return parts;
}

export function stripSwears(text, words) {
  const parts = swearRegexParts(words);
  if (!text || !parts.length) return text;
  const pattern = new RegExp(`\\b(?:${parts.join("|")})\\b`, "gi");
  let out = text.replace(pattern, "");
  out = out.replace(/[ \t]{2,}/g, " ");
  out = out.replace(/\s+([,.!?;:])/g, "$1");
  return out;
}

export function stripFiller(text, phrases) {
  if (!text || !phrases || !phrases.length) return text;
  // Longest-first so "I apologize for any confusion." matches before a
  // shorter prefix would.
  const sorted = [...phrases].sort((a, b) => b.length - a.length);
  const pattern = new RegExp(sorted.map(reEscape).join("|"), "gi");
  let out = text.replace(pattern, "");
  out = out.replace(/[ \t]{2,}/g, " ");
  out = out.replace(/\n{3,}/g, "\n\n");
  out = out.replace(/\s+([,.!?;:])/g, "$1");
  return out.trim();
}

// Word-bounded phrase removal — same mechanism as stripSwears without
// the `*` stem syntax. Targets verbosity (obviousness signalers, meta-
// commentary) which is about token cost, not agent behavior.
export function stripVerbosity(text, phrases) {
  if (!text || !phrases || !phrases.length) return text;
  const sorted = [...phrases].sort((a, b) => b.length - a.length);
  const parts = sorted
    .filter((p) => typeof p === "string" && p.trim())
    .map(reEscape);
  if (!parts.length) return text;
  const pattern = new RegExp(`\\b(?:${parts.join("|")})\\b`, "gi");
  let out = text.replace(pattern, "");
  out = out.replace(/[ \t]{2,}/g, " ");
  out = out.replace(/\n{3,}/g, "\n\n");
  out = out.replace(/\s+([,.!?;:])/g, "$1");
  return out.trim();
}

export function applyTransform(kind, text, { swears = [], filler = [], verbosity = [] } = {}) {
  switch (kind) {
    case "scrub": return scrub();
    case "normalize_whitespace": return normalizeWs(text);
    case "remove_swears": return stripSwears(text, swears);
    case "remove_filler": return stripFiller(text, filler);
    case "remove_verbosity": return stripVerbosity(text, verbosity);
    // Priming = swears + drift phrases (same evidence base, different
    // register). One user-facing action chains both; the individual
    // kinds above stay callable for tests and internal use.
    case "remove_priming": return stripFiller(stripSwears(text, swears), filler);
    default: throw new Error(`Unknown transform kind: ${kind}`);
  }
}
