import { test } from "node:test";
import assert from "node:assert/strict";

const { highlightText, hlText } = await import("../../llm_lens/static/js/utils.js");

// ── highlightText ────────────────────────────────────────────────────
// Regression guard: the tag-boundary regex used to fail silently on
// tag-less HTML, so plain user messages (no tool badges, no thinking
// blocks) never got their matches wrapped while assistant messages did.

test("highlightText wraps matches in pure text (no tags)", () => {
  const out = highlightText("hello needle world", "needle");
  assert.match(out, /<span class="highlight">needle<\/span>/);
  assert.equal(out, 'hello <span class="highlight">needle</span> world');
});

test("highlightText still works when HTML has tag boundaries", () => {
  const input = '<span class="tool-badge">Read</span> some text with needle inside';
  const out = highlightText(input, "needle");
  assert.match(out, /<span class="highlight">needle<\/span>/);
  // Pre-existing span classes must remain intact.
  assert.match(out, /class="tool-badge"/);
});

test("highlightText is case-insensitive", () => {
  assert.match(highlightText("HELLO NEEDLE", "needle"), /<span class="highlight">NEEDLE<\/span>/);
});

test("highlightText does not alter HTML tag names or attribute values", () => {
  // The regex operates only on text between `>` and `<`. Attribute
  // values (needle inside `class="needle-badge"`) must stay untouched
  // or we'd corrupt the markup.
  const input = '<span class="needle-badge">clean</span>';
  const out = highlightText(input, "needle");
  assert.equal(out, input);
});

test("highlightText is a no-op on empty query", () => {
  assert.equal(highlightText("needle", ""), "needle");
  assert.equal(highlightText("needle", undefined), "needle");
});

test("highlightText handles query with regex metacharacters safely", () => {
  const out = highlightText("path/to/file.js", "file.js");
  assert.match(out, /<span class="highlight">file\.js<\/span>/);
  // Dot must not match any char — "fileXjs" should NOT highlight.
  const out2 = highlightText("fileXjs", "file.js");
  assert.doesNotMatch(out2, /<span class="highlight">/);
});

// hlText is DOM-backed (uses `esc` which touches document) — its
// regression would be caught by the frontend invariant tests instead
// of here.
void hlText;
