// Tests for preview.js pure helpers: diffWords, deltaOf, computeRows.
// The modal itself touches the DOM — that part is covered by frontend
// invariants in test_frontend_invariants.py. Here we cover:
//   - word-LCS correctness (ins/del/eq ops reconstruct the target)
//   - computeRows filters out messages that don't actually change
//   - oversize inputs short-circuit to null-ops with full-length delta
//
// state.js reads localStorage at module load, so stub it before importing
// preview.js (same pattern as test_exports.mjs).

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = {
  _data: {},
  getItem(k) { return k in this._data ? this._data[k] : null; },
  setItem(k, v) { this._data[k] = String(v); },
  removeItem(k) { delete this._data[k]; },
};

const {
  diffWords,
  deltaOf,
  computeRows,
} = await import("../../llm_lens/static/js/preview.js");

function reconstruct(ops) {
  let before = "", after = "";
  for (const op of ops) {
    if (op.t === "eq") { before += op.v; after += op.v; }
    else if (op.t === "del") before += op.v;
    else if (op.t === "ins") after += op.v;
  }
  return { before, after };
}

test("diffWords ops reconstruct both sides exactly", () => {
  const a = "the quick brown fox jumps over the lazy dog";
  const b = "the slow brown cat jumps over the lazy dog";
  const ops = diffWords(a, b);
  const r = reconstruct(ops);
  assert.equal(r.before, a);
  assert.equal(r.after, b);
});

test("diffWords marks identical strings as all eq", () => {
  const ops = diffWords("hello world", "hello world");
  assert.ok(ops.every((o) => o.t === "eq"));
});

test("diffWords handles pure insertion", () => {
  const ops = diffWords("", "new text");
  assert.ok(ops.every((o) => o.t === "ins"));
  assert.equal(reconstruct(ops).after, "new text");
});

test("diffWords handles pure deletion", () => {
  const ops = diffWords("gone", "");
  assert.ok(ops.every((o) => o.t === "del"));
  assert.equal(reconstruct(ops).before, "gone");
});

test("diffWords returns null past the size cap so callers can fall back", () => {
  const big = "x".repeat(5000);
  assert.equal(diffWords(big, big + " more"), null);
});

test("deltaOf counts only inserted and deleted characters", () => {
  const ops = [
    { t: "eq", v: "hello " },
    { t: "del", v: "big" },
    { t: "ins", v: "small" },
    { t: "eq", v: " world" },
  ];
  const d = deltaOf(ops, "hello big world", "hello small world");
  assert.equal(d.add, 5);
  assert.equal(d.rem, 3);
});

test("deltaOf without ops falls back to full before/after lengths", () => {
  const d = deltaOf(null, "abcd", "xyz");
  assert.equal(d.rem, 4);
  assert.equal(d.add, 3);
});

test("computeRows drops messages whose transform is a no-op", () => {
  const candidates = [
    { uuid: "a", content: "already .  " },
    { uuid: "b", content: "hello world" },
  ];
  const rows = computeRows("normalize_whitespace", candidates, {});
  // "hello world" has no collapsible whitespace — no-op, filtered out.
  // The other has trailing spaces that normalizeWs trims.
  const uuids = rows.map((r) => r.uuid);
  assert.deepEqual(uuids, ["a"]);
});

test("computeRows carries diff ops and delta for changed rows", () => {
  const rows = computeRows(
    "remove_swears",
    [{ uuid: "x", content: "oh damn it" }],
    { swears: ["damn"] },
  );
  assert.equal(rows.length, 1);
  const r = rows[0];
  assert.equal(r.before, "oh damn it");
  assert.notEqual(r.before, r.after);
  assert.ok(Array.isArray(r.ops));
  assert.ok(r.delta.rem > 0);
});

test("computeRows swallows transform errors rather than aborting the batch", () => {
  const rows = computeRows(
    "not-a-real-kind",
    [{ uuid: "x", content: "anything" }],
    {},
  );
  assert.deepEqual(rows, []);
});
