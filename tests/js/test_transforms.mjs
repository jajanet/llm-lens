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
