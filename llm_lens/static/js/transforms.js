// Pure text→text transforms. Run on the client; the result is sent to
// /edit, which writes it verbatim. Ports of the server-side helpers that
// used to live in llm_lens/__init__.py (_redact_content, _normalize_ws,
// _strip_swears, _strip_filler). Behaviour should stay in sync with the
// Python regex semantics those tested against.

export const REDACT_PLACEHOLDER = ".";

export function redact() {
  return REDACT_PLACEHOLDER;
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

// Returns entries whose text does NOT contain any whitelist entry as a
// case-insensitive substring. "let me help" in whitelist blocks both
// "let me help" and "let me help you understand" in a trigger list.
// Non-string / empty-after-trim whitelist entries are ignored.
export function filterByWhitelist(list, whitelist) {
  if (!list || !list.length) return [];
  const wl = (whitelist || [])
    .filter((w) => typeof w === "string" && w.trim())
    .map((w) => w.toLowerCase());
  if (!wl.length) return [...list];
  return list.filter((entry) => {
    if (typeof entry !== "string" || !entry.trim()) return false;
    const lower = entry.toLowerCase();
    return !wl.some((w) => lower.includes(w));
  });
}

// Abbreviation substitution — word-bounded, case-insensitive, longest-
// `from`-first so multi-word rules ("you are" → "ur") run before
// shorter-prefix rules ("you" → "u") and don't get chopped up. Pairs
// whose `from` contains any whitelist phrase (case-insensitive
// substring) are skipped, same posture as the other remove-* filters.
// Punctuation-leading/trailing sources (e.g. "w/", "i.e.") get a
// conditional word boundary — only applied on the side where the
// source starts/ends with a word character.
// Collapse aggressive-repeat punctuation ("???", "!!!", "......") down
// to single marks — priming cleanup, not token cleanup. Thresholds are
// deliberately conservative: "??" and "!!" are common rhetorical
// emphasis that doesn't read as aggressive, so leave those alone. 3+
// "?" or "!" and 4+ "." get flattened. The 3-dot ellipsis is preserved
// because it's intentional punctuation.
// Fence-aware: skips lines inside ```...``` blocks so code and string
// literals aren't mangled.
export function collapsePunctRepeats(text) {
  if (!text) return text;
  const lines = text.split("\n");
  const out = [];
  let inFence = false;
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
    let s = line;
    s = s.replace(/\?{3,}/g, "?");
    s = s.replace(/!{3,}/g, "!");
    s = s.replace(/\.{4,}/g, "...");
    out.push(s);
  }
  return out.join("\n");
}

export function applyAbbreviations(text, pairs, whitelist = []) {
  if (!text || !pairs || !pairs.length) return text;
  const wl = (whitelist || [])
    .filter((w) => typeof w === "string" && w.trim())
    .map((w) => w.toLowerCase());
  const active = pairs.filter((p) => {
    if (!p || typeof p.from !== "string" || typeof p.to !== "string") return false;
    if (!p.from.trim()) return false;
    if (!wl.length) return true;
    const lower = p.from.toLowerCase();
    return !wl.some((w) => lower.includes(w));
  });
  if (!active.length) return text;
  const sorted = [...active].sort((a, b) => b.from.length - a.from.length);
  let out = text;
  for (const p of sorted) {
    const firstCh = p.from[0];
    const lastCh = p.from[p.from.length - 1];
    const startB = /\w/.test(firstCh) ? "\\b" : "";
    const endB = /\w/.test(lastCh) ? "\\b" : "";
    const pattern = new RegExp(`${startB}${reEscape(p.from)}${endB}`, "gi");
    out = out.replace(pattern, p.to);
  }
  return out;
}

export function applyTransform(kind, text, {
  swears = [],
  filler = [],
  verbosity = [],
  custom_filter = [],
  whitelist = [],
  lowercase_user_text = false,
  role = null,
  abbreviations = [],
  apply_abbreviations = false,
  collapse_punct_repeats = false,
} = {}) {
  // Global whitelist is honored by every remove_* transform — any entry
  // containing a whitelist phrase (case-insensitive substring) is
  // dropped from the trigger list before matching. Redact and normalize
  // don't consult it; they don't operate on curated lists.
  const fSwears = filterByWhitelist(swears, whitelist);
  const fFiller = filterByWhitelist(filler, whitelist);
  const fVerbosity = filterByWhitelist(verbosity, whitelist);
  const fCustom = filterByWhitelist(custom_filter, whitelist);

  // Optional: drop user-role priming by lowercasing before the regex
  // strip. Only applies to remove_priming on user-role messages —
  // capslock-rant reduction, per the feature brief.
  let primingInput = text;
  if (lowercase_user_text && kind === "remove_priming" && role === "user") {
    primingInput = (text || "").toLowerCase();
  }

  switch (kind) {
    case "redact": return redact();
    case "normalize_whitespace": return normalizeWs(text);
    case "remove_swears": return stripSwears(text, fSwears);
    case "remove_filler": return stripFiller(text, fFiller);
    case "remove_verbosity": {
      const stripped = stripVerbosity(text, fVerbosity);
      // Optional modifier: when the user toggles "apply abbreviations"
      // in the curation modal, run the abbreviation substitution pass
      // after the verbosity strip. Pairs honor the global whitelist —
      // any pair whose `from` contains a whitelisted phrase is skipped.
      return apply_abbreviations ? applyAbbreviations(stripped, abbreviations, whitelist) : stripped;
    }
    // Priming = swears + drift phrases (same evidence base, different
    // register). One user-facing action chains both; the individual
    // kinds above stay callable for tests and internal use.
    case "remove_priming": {
      let out = stripFiller(stripSwears(primingInput, fSwears), fFiller);
      // Optional modifier: collapse aggressive-repeat punctuation
      // (!!!/???/.....) — same priming motivation, structural mechanism.
      if (collapse_punct_repeats) out = collapsePunctRepeats(out);
      return out;
    }
    case "remove_custom_filter": return stripFiller(text, fCustom);
    default: throw new Error(`Unknown transform kind: ${kind}`);
  }
}
