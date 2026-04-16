import { test } from "node:test";
import assert from "node:assert/strict";

const {
  scrub,
  normalizeWs,
  stripSwears,
  stripFiller,
  applyTransform,
} = await import("../../llm_lens/static/js/transforms.js");

// --- scrub ---------------------------------------------------------------

test("scrub always returns a single period regardless of input", () => {
  assert.equal(scrub("anything"), ".");
  assert.equal(scrub(""), ".");
  assert.equal(scrub(), ".");
});

// --- normalizeWs ---------------------------------------------------------

test("normalizeWs collapses inline runs and tightens blank lines", () => {
  const input = "hello    world\n\n\n\nnext   paragraph   ";
  assert.equal(normalizeWs(input), "hello world\n\nnext paragraph");
});

test("normalizeWs preserves indentation on space/tab-led lines", () => {
  const input = "    def foo():\n\treturn   1\n";
  // Indented lines: only rstrip, inner runs left alone.
  assert.equal(normalizeWs(input), "    def foo():\n\treturn   1\n");
});

test("normalizeWs leaves fenced code blocks untouched", () => {
  const input = "prose  has    spaces\n```\na    b   c\n```\nafter   fence";
  const out = normalizeWs(input);
  // Prose outside fence collapsed; inside fence untouched.
  assert.match(out, /prose has spaces/);
  assert.match(out, /a    b   c/);
  assert.match(out, /after fence/);
});

// --- stripSwears ---------------------------------------------------------

test("stripSwears removes plain word-bounded hits, case-insensitively", () => {
  assert.equal(stripSwears("oh damn it", ["damn"]), "oh it");
  assert.equal(stripSwears("Oh Damn it", ["damn"]), "Oh it");
});

test("stripSwears with stem (*) catches safe conjugations but spares unrelated words", () => {
  const words = ["fuck*", "ass*"];
  // Runs of removed words collapse to a single space (matches Python).
  assert.equal(stripSwears("fuck fucks fucking fucker", words).trim(), "");
  // `assistant` must survive — `ist`/`istant` aren't in the safe suffix set.
  assert.match(stripSwears("the assistant said", words), /assistant/);
  assert.match(stripSwears("assess the result", words), /assess/);
});

test("stripSwears cleans up double spaces and punctuation-adjacency", () => {
  assert.equal(stripSwears("this is damn good, damn it!", ["damn"]), "this is good, it!");
});

test("stripSwears returns input unchanged with empty list", () => {
  assert.equal(stripSwears("damn it", []), "damn it");
});

// --- stripFiller ---------------------------------------------------------

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
  // Longest (with period) must match first and consume the whole clause,
  // rather than the shorter "I apologize" leaving " for any confusion."
  // behind.
  assert.equal(out, "please continue");
});

test("stripFiller returns input unchanged with empty phrase list", () => {
  assert.equal(stripFiller("hi", []), "hi");
});

// --- applyTransform dispatch --------------------------------------------

test("applyTransform dispatches by kind and passes word lists through", () => {
  assert.equal(applyTransform("scrub", "anything"), ".");
  assert.equal(applyTransform("normalize_whitespace", "a   b"), "a b");
  // remove_swears doesn't rstrip — a trailing space after removal is expected.
  assert.equal(applyTransform("remove_swears", "oh damn", { swears: ["damn"] }), "oh ");
  // remove_filler does .trim() as its final step.
  assert.equal(
    applyTransform("remove_filler", "hi. thanks!", { filler: ["thanks!"] }),
    "hi."
  );
});

test("applyTransform throws on unknown kind", () => {
  assert.throws(() => applyTransform("nope", "x"), /Unknown transform kind/);
});
