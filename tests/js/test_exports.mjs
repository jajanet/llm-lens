// Unit tests for the pure serializers in exports.js (serializeAsText and
// serializeAsJsonl). downloadBlob + ensureDownloadFields require DOM/API
// and are covered by frontend-invariant + backend-smoke tests instead.

import { test } from "node:test";
import assert from "node:assert/strict";

// state.js reads localStorage at module load. exports.js imports state.js.
globalThis.localStorage = {
  _data: {},
  getItem(k) { return k in this._data ? this._data[k] : null; },
  setItem(k, v) { this._data[k] = String(v); },
  removeItem(k) { delete this._data[k]; },
};

const {
  serializeAsText,
  serializeAsJsonl,
  EXPORT_FIELDS,
  REQUIRED_EXPORT_FIELDS,
} = await import("../../llm_lens/static/js/exports.js");

// --- serializeAsText ------------------------------------------------------

test("serializeAsText labels turns by role and joins with blank lines", () => {
  const out = serializeAsText([
    { role: "user", content: "hello" },
    { role: "assistant", content: "hi back" },
  ]);
  assert.equal(out, "User: hello\n\nAssistant: hi back");
});

test("serializeAsText strips inline tags, [Tool: …] markers, and [Tool Result]", () => {
  // Matches the stripping the original copySelected did so we don't leak
  // structural markers into plain text exports.
  const out = serializeAsText([
    { role: "assistant", content: "before<thinking>secret</thinking>after" },
    { role: "assistant", content: "ran [Tool: Bash:abc123] then [Tool Result] ok" },
  ]);
  assert.equal(out, "Assistant: beforesecretafter\n\nAssistant: ran  then  ok");
});

test("serializeAsText treats any non-user role as Assistant", () => {
  // Claude Code sometimes surfaces odd role values (system, tool, etc.).
  // The exporter normalizes to Assistant so the transcript stays binary.
  const out = serializeAsText([
    { role: "user", content: "q" },
    { role: "system", content: "sys" },
    { role: "tool", content: "result" },
  ]);
  assert.equal(out, "User: q\n\nAssistant: sys\n\nAssistant: result");
});

test("serializeAsText handles empty list and missing content", () => {
  assert.equal(serializeAsText([]), "");
  assert.equal(
    serializeAsText([{ role: "user" }, { role: "assistant", content: null }]),
    "User: \n\nAssistant: "
  );
});

// --- serializeAsJsonl -----------------------------------------------------

function line(obj) { return JSON.stringify(obj); }

test("serializeAsJsonl emits one JSON object per line plus trailing newline", () => {
  const fields = { uuid: true, role: true, content: true, timestamp: true };
  const out = serializeAsJsonl(
    [
      { uuid: "u1", role: "user", content: "hi", timestamp: "2026-04-16T00:00:00Z" },
      { uuid: "a1", role: "assistant", content: "yo", timestamp: "2026-04-16T00:00:01Z" },
    ],
    fields,
  );
  const want = [
    line({ uuid: "u1", role: "user", content: "hi", timestamp: "2026-04-16T00:00:00Z" }),
    line({ uuid: "a1", role: "assistant", content: "yo", timestamp: "2026-04-16T00:00:01Z" }),
    "",
  ].join("\n");
  assert.equal(out, want);
});

test("serializeAsJsonl omits fields the caller turned off", () => {
  const fields = { uuid: false, role: true, content: true, timestamp: false };
  const out = serializeAsJsonl(
    [{ uuid: "u1", role: "user", content: "hi", timestamp: "t" }],
    fields,
  );
  assert.equal(out, line({ role: "user", content: "hi" }) + "\n");
});

test("serializeAsJsonl forces role+content even if flags say false", () => {
  // Stale cached prefs could set role=false; the serializer must re-guard so
  // no JSONL line ever ships without structural fields (that'd be useless).
  const fields = Object.fromEntries(EXPORT_FIELDS.map((f) => [f, false]));
  const out = serializeAsJsonl(
    [{ uuid: "u1", role: "user", content: "hi", timestamp: "t", model: "m" }],
    fields,
  );
  const parsed = JSON.parse(out.trim());
  for (const req of REQUIRED_EXPORT_FIELDS) assert.ok(req in parsed, `missing ${req}`);
  assert.equal(parsed.role, "user");
  assert.equal(parsed.content, "hi");
  // Nothing else should have leaked through.
  assert.equal(Object.keys(parsed).length, REQUIRED_EXPORT_FIELDS.length);
});

test("serializeAsJsonl skips fields that are absent/null on the message", () => {
  // Don't emit `"model": null` when the source entry had no model set —
  // that changes the schema for downstream consumers. The field row is
  // simply absent.
  const fields = { uuid: true, role: true, content: true, model: true, usage: true };
  const out = serializeAsJsonl(
    [
      { uuid: "u1", role: "user", content: "hi" },  // no model/usage
      { uuid: "a1", role: "assistant", content: "yo",
        model: "claude-test", usage: { input_tokens: 5, output_tokens: 2 } },
    ],
    fields,
  );
  const [l1, l2] = out.trim().split("\n");
  const u = JSON.parse(l1);
  const a = JSON.parse(l2);
  assert.ok(!("model" in u));
  assert.ok(!("usage" in u));
  assert.equal(a.model, "claude-test");
  assert.deepEqual(a.usage, { input_tokens: 5, output_tokens: 2 });
});

test("serializeAsJsonl returns empty string for empty input (no trailing newline)", () => {
  const fields = Object.fromEntries(EXPORT_FIELDS.map((f) => [f, true]));
  assert.equal(serializeAsJsonl([], fields), "");
});

test("EXPORT_FIELDS and REQUIRED_EXPORT_FIELDS stay in sync with backend", () => {
  // Backend _ALL_DOWNLOAD_FIELDS = uuid, role, content, timestamp, commands,
  // model, usage. If that order changes, regenerate; if the set drifts, the
  // modal and serializer will ship different keys than the server accepts.
  assert.deepEqual(
    EXPORT_FIELDS,
    ["uuid", "role", "content", "timestamp", "commands", "model", "usage"],
  );
  assert.deepEqual(REQUIRED_EXPORT_FIELDS, ["role", "content"]);
});
