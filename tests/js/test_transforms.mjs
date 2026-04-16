import { test } from "node:test";
import assert from "node:assert/strict";

const {
  scrub,
  normalizeWs,
  stripSwears,
  stripFiller,
  stripVerbosity,
  applyTransform,
} = await import("../../llm_lens/static/js/transforms.js");

test("scrub always returns a single period regardless of input", () => {
  assert.equal(scrub("anything"), ".");
  assert.equal(scrub(""), ".");
  assert.equal(scrub(), ".");
});

test("normalizeWs collapses inline runs and tightens blank lines", () => {
  const input = "hello    world\n\n\n\nnext   paragraph   ";
  assert.equal(normalizeWs(input), "hello world\n\nnext paragraph");
});

test("normalizeWs preserves indentation on space/tab-led lines", () => {
  const input = "    def foo():\n\treturn   1\n";
  assert.equal(normalizeWs(input), "    def foo():\n\treturn   1\n");
});

test("normalizeWs leaves fenced code blocks untouched", () => {
  const input = "prose  has    spaces\n```\na    b   c\n```\nafter   fence";
  const out = normalizeWs(input);
  assert.match(out, /prose has spaces/);
  assert.match(out, /a    b   c/);
  assert.match(out, /after fence/);
});

test("stripSwears removes plain word-bounded hits, case-insensitively", () => {
  assert.equal(stripSwears("oh damn it", ["damn"]), "oh it");
  assert.equal(stripSwears("Oh Damn it", ["damn"]), "Oh it");
});

test("stripSwears with stem (*) catches safe conjugations but spares unrelated words", () => {
  const words = ["fuck*", "ass*"];
  assert.equal(stripSwears("fuck fucks fucking fucker", words).trim(), "");
  assert.match(stripSwears("the assistant said", words), /assistant/);
  assert.match(stripSwears("assess the result", words), /assess/);
});

test("stripSwears cleans up double spaces and punctuation-adjacency", () => {
  assert.equal(stripSwears("this is damn good, damn it!", ["damn"]), "this is good, it!");
});

test("stripSwears returns input unchanged with empty list", () => {
  assert.equal(stripSwears("damn it", []), "damn it");
});

test("stripFiller removes phrases case-insensitively", () => {
  const phrases = ["You're absolutely right!", "I apologize for any confusion."];
  const input = "you're absolutely right! Let me try again. I apologize for any confusion.";
  const out = stripFiller(input, phrases);
  assert.equal(out, "Let me try again.");
});

test("stripFiller matches longest-first to avoid shorter-prefix wins", () => {
  const phrases = ["I apologize", "I apologize for any confusion."];
  const input = "I apologize for any confusion. please continue";
  const out = stripFiller(input, phrases);
  assert.equal(out, "please continue");
});

test("stripFiller returns input unchanged with empty phrase list", () => {
  assert.equal(stripFiller("hi", []), "hi");
});

test("stripVerbosity strips obviousness signalers word-bounded, case-insensitive", () => {
  const phrases = ["obviously", "clearly"];
  assert.equal(stripVerbosity("This is obviously broken", phrases), "This is broken");
  assert.equal(stripVerbosity("Clearly it works", phrases), "it works");
});



const { filterByWhitelist } = await import("../../llm_lens/static/js/transforms.js");

test("filterByWhitelist drops entries containing any whitelist phrase (case-insensitive substring)", () => {
  const list = ["let me help", "let me help you understand", "think step by step", "obviously"];
  const whitelist = ["Let me help"];
  const out = filterByWhitelist(list, whitelist);
  assert.deepEqual(out.sort(), ["obviously", "think step by step"].sort());
});

test("filterByWhitelist keeps list intact when whitelist is empty", () => {
  const list = ["a", "b", "c"];
  assert.deepEqual(filterByWhitelist(list, []), ["a", "b", "c"]);
  assert.deepEqual(filterByWhitelist(list, null), ["a", "b", "c"]);
});

test("filterByWhitelist ignores empty/whitespace whitelist entries", () => {
  const list = ["keep this phrase"];
  assert.deepEqual(filterByWhitelist(list, ["", "   "]), ["keep this phrase"]);
});

test("applyTransform with whitelist blocks remove_filler", () => {
  const text = "You're absolutely right! Let me think step by step.";
  const out = applyTransform("remove_filler", text, {
    filler: ["You're absolutely right!", "Let me think step by step."],
    whitelist: ["absolutely right"],
  });
  assert.ok(out.toLowerCase().includes("absolutely right"));
  assert.ok(!out.toLowerCase().includes("step by step"));
});

test("applyTransform with whitelist blocks remove_verbosity", () => {
  const text = "Obviously this is fine. Clearly we should ship.";
  const out = applyTransform("remove_verbosity", text, {
    verbosity: ["obviously", "clearly"],
    whitelist: ["Obviously"],
  });
  assert.ok(out.toLowerCase().includes("obviously"));
  assert.ok(!out.toLowerCase().includes("clearly"));
});

test("applyTransform with whitelist blocks remove_swears", () => {
  const text = "this is shit and damn annoying";
  const out = applyTransform("remove_swears", text, {
    swears: ["shit*", "damn*"],
    whitelist: ["shit"],
  });
  assert.ok(out.includes("shit"));
  assert.ok(!out.includes("damn"));
});

test("applyTransform with whitelist blocks remove_custom_filter", () => {
  const text = "run the benchmark suite and let me check the logs";
  const out = applyTransform("remove_custom_filter", text, {
    custom_filter: ["run the benchmark suite", "let me check the logs"],
    whitelist: ["benchmark"],
  });
  assert.ok(out.toLowerCase().includes("benchmark"));
  assert.ok(!out.toLowerCase().includes("let me check the logs"));
});

test("remove_custom_filter with empty whitelist strips all matching phrases", () => {
  const text = "please let me check the logs again";
  const out = applyTransform("remove_custom_filter", text, {
    custom_filter: ["let me check the logs"],
    whitelist: [],
  });
  assert.ok(!out.toLowerCase().includes("let me check the logs"));
});

test("remove_custom_filter is case-insensitive", () => {
  const text = "LET ME check the logs here";
  const out = applyTransform("remove_custom_filter", text, {
    custom_filter: ["let me check the logs"],
    whitelist: [],
  });
  assert.ok(!out.toLowerCase().includes("let me check the logs"));
});



test("lowercase_user_text + role=user lowercases before stripping on remove_priming", () => {
  const text = "THIS IS SHIT AND ANNOYING";
  const out = applyTransform("remove_priming", text, {
    swears: ["shit*"],
    filler: [],
    lowercase_user_text: true,
    role: "user",
  });
  // Lowercased then stripped — "shit" gone, surrounding text lowercase.
  assert.ok(!out.toLowerCase().includes("shit"));
  assert.ok(!/[A-Z]/.test(out), `expected all-lowercase output, got: ${out}`);
});

test("lowercase_user_text does NOT lowercase when role != user", () => {
  const text = "THIS IS A SENTENCE";
  const out = applyTransform("remove_priming", text, {
    swears: [],
    filler: [],
    lowercase_user_text: true,
    role: "assistant",
  });
  // No change — the option only affects user-role text, and no filler
  // list entries match.
  assert.equal(out, text);
});

test("lowercase_user_text does NOT apply to other kinds like remove_swears alone", () => {
  const text = "DAMN THIS IS ANNOYING";
  const out = applyTransform("remove_swears", text, {
    swears: ["damn*"],
    lowercase_user_text: true,
    role: "user",
  });
  // remove_swears strips the swear but leaves the rest's case intact —
  // the lowercase option is scoped to remove_priming only.
  assert.ok(!out.includes("DAMN"));
  assert.ok(/[A-Z]/.test(out), `expected some uppercase to survive: ${out}`);
});

test("lowercase_user_text off (default) doesn't lowercase even for user+priming", () => {
  const text = "THIS IS FINE";
  const out = applyTransform("remove_priming", text, {
    swears: [],
    filler: [],
    lowercase_user_text: false,
    role: "user",
  });
  assert.equal(out, text);
});



const { applyAbbreviations } = await import("../../llm_lens/static/js/transforms.js");

test("applyAbbreviations substitutes word-bounded case-insensitive", () => {
  const out = applyAbbreviations("You are welcome, you know", [
    { from: "you are", to: "ur" },
    { from: "you", to: "u" },
  ]);
  // Longest-first: "you are" matched first → "ur welcome", then "you" in
  // "you know" → "u know". Case-insensitive on match, literal case for replacement.
  assert.match(out, /ur welcome/i);
  assert.match(out, /u know/i);
});

test("applyAbbreviations longest-first prevents shorter-rule cannibalization", () => {
  // If "you" ran before "you're", "ur" would be produced first; pair list
  // sorting by from-length fixes this.
  const out = applyAbbreviations("you're right", [
    { from: "you", to: "u" },
    { from: "you're", to: "ur" },
  ]);
  assert.match(out, /ur right/i);
  assert.doesNotMatch(out, /u're right/i);
});

test("applyAbbreviations respects whitelist containment (skips pairs)", () => {
  const out = applyAbbreviations("please thanks", [
    { from: "please", to: "pls" },
    { from: "thanks", to: "ty" },
  ], ["please"]);
  // "please" is whitelisted → stays; "thanks" → "ty"
  assert.match(out, /please/);
  assert.doesNotMatch(out, /pls/);
  assert.match(out, /ty/);
});

test("applyAbbreviations handles punctuation-leading sources (w/ → with)", () => {
  const out = applyAbbreviations("run w/ flag w/o anything", [
    { from: "w/o", to: "without" },
    { from: "w/", to: "with" },
  ]);
  assert.match(out, /run with flag without anything/);
});

test("applyAbbreviations skips empty-from or malformed pairs", () => {
  const out = applyAbbreviations("some text", [
    { from: "", to: "x" },
    { from: "   ", to: "y" },
    null,
    { from: 42, to: "z" },
  ]);
  assert.equal(out, "some text");
});

test("applyTransform with apply_abbreviations=true runs subs on remove_verbosity output", () => {
  const out = applyTransform("remove_verbosity", "obviously you are right", {
    verbosity: ["obviously"],
    abbreviations: [{ from: "you are", to: "ur" }],
    apply_abbreviations: true,
  });
  // verbosity stripped, then abbreviations applied
  assert.doesNotMatch(out, /obviously/i);
  assert.match(out, /ur right/i);
});

test("applyTransform with apply_abbreviations=false leaves abbreviations untouched", () => {
  const out = applyTransform("remove_verbosity", "obviously you are right", {
    verbosity: ["obviously"],
    abbreviations: [{ from: "you are", to: "ur" }],
    apply_abbreviations: false,
  });
  assert.doesNotMatch(out, /obviously/i);
  assert.match(out, /you are right/);  // unchanged
});

test("applyTransform does NOT apply abbreviations on other kinds even if flag is on", () => {
  const out = applyTransform("remove_priming", "you are right", {
    swears: [],
    filler: [],
    abbreviations: [{ from: "you are", to: "ur" }],
    apply_abbreviations: true,
  });
  // apply_abbreviations is scoped to remove_verbosity
  assert.match(out, /you are right/);
});



const { collapsePunctRepeats } = await import("../../llm_lens/static/js/transforms.js");

test("collapsePunctRepeats: 3+ ? or ! collapses to one; 4+ dots collapse to ...", () => {
  assert.equal(collapsePunctRepeats("what???"), "what?");
  assert.equal(collapsePunctRepeats("hello!!!!"), "hello!");
  assert.equal(collapsePunctRepeats("wait.....?"), "wait...?");
  assert.equal(collapsePunctRepeats("long........."), "long...");
});

test("collapsePunctRepeats: preserves double (??, !!) and the 3-dot ellipsis", () => {
  // Rhetorical emphasis, not aggression — leave these alone.
  assert.equal(collapsePunctRepeats("huh?? ok!! fine..."), "huh?? ok!! fine...");
});

test("collapsePunctRepeats: fence-aware — code blocks untouched", () => {
  const input = [
    "aggressive??? out here",
    "```",
    "var x = 'hmmm???';",
    "```",
    "also bad!!!",
  ].join("\n");
  const out = collapsePunctRepeats(input);
  const lines = out.split("\n");
  assert.equal(lines[0], "aggressive? out here");
  assert.equal(lines[2], "var x = 'hmmm???';"); // untouched inside fence
  assert.equal(lines[4], "also bad!");
});

test("applyTransform with collapse_punct_repeats=true on remove_priming collapses after strip", () => {
  const out = applyTransform("remove_priming", "shit!!! this is broken???", {
    swears: ["shit*"],
    filler: [],
    collapse_punct_repeats: true,
  });
  assert.ok(!out.toLowerCase().includes("shit"));
  assert.doesNotMatch(out, /!{2,}/);
  assert.doesNotMatch(out, /\?{2,}/);
});

test("applyTransform with collapse_punct_repeats=false leaves punctuation alone", () => {
  const out = applyTransform("remove_priming", "broken???", {
    swears: [],
    filler: [],
    collapse_punct_repeats: false,
  });
  assert.equal(out, "broken???");
});

test("collapse_punct_repeats scoped to remove_priming — other kinds ignore the flag", () => {
  // normalize_whitespace doesn't honor the flag; only remove_priming does.
  const out = applyTransform("normalize_whitespace", "hmm???", { collapse_punct_repeats: true });
  assert.match(out, /hmm\?{3}/);
});

test("stripVerbosity respects word boundaries — no false positives on substrings", () => {
  const phrases = ["clearly"];
  // "unclearly" should NOT match because the start isn't a word boundary.
  assert.match(stripVerbosity("unclearly stated", phrases), /unclearly/);
});

test("stripVerbosity strips multi-word phrases", () => {
  const phrases = ["at the end of the day", "that's a great question"];
  const input = "at the end of the day, we ship. That's a great question though.";
  const out = stripVerbosity(input, phrases);
  assert.match(out, /we ship/);
  assert.doesNotMatch(out, /at the end of the day/i);
  assert.doesNotMatch(out, /that's a great question/i);
});

test("stripVerbosity returns input unchanged with empty phrase list", () => {
  assert.equal(stripVerbosity("obviously broken", []), "obviously broken");
});

test("applyTransform dispatches by kind and passes word lists through", () => {
  assert.equal(applyTransform("scrub", "anything"), ".");
  assert.equal(applyTransform("normalize_whitespace", "a   b"), "a b");
  assert.equal(applyTransform("remove_swears", "oh damn", { swears: ["damn"] }), "oh ");
  assert.equal(
    applyTransform("remove_filler", "hi. thanks!", { filler: ["thanks!"] }),
    "hi."
  );
  assert.equal(
    applyTransform("remove_verbosity", "this is obviously fine", { verbosity: ["obviously"] }),
    "this is fine"
  );
});

test("applyTransform remove_priming chains swears then drift phrases in one pass", () => {
  const input = "You're absolutely right! This is damn broken.";
  const out = applyTransform("remove_priming", input, {
    swears: ["damn"],
    filler: ["You're absolutely right!"],
  });
  assert.doesNotMatch(out, /damn/i);
  assert.doesNotMatch(out, /absolutely right/i);
  assert.match(out, /broken/);
});

test("applyTransform remove_priming is a no-op when both lists are empty", () => {
  const input = "hello world";
  assert.equal(applyTransform("remove_priming", input, { swears: [], filler: [] }), input);
});

test("applyTransform throws on unknown kind", () => {
  assert.throws(() => applyTransform("nope", "x"), /Unknown transform kind/);
});
