// Tests for applyBucketFilters — the per-bucket merger that feeds the
// overview graph. Regression coverage for "graph renders empty when filters
// are toggled" and "graph ignores archived/deleted deltas".
//
// Run: node --test tests/js/

import { test } from "node:test";
import assert from "node:assert/strict";

import { applyBucketFilters } from "../../llm_lens/static/js/utils.js";

const ALL_ON  = { active: true,  archived: true,  deleted: true  };
const ACTIVE  = { active: true,  archived: false, deleted: false };
const ARCH    = { active: false, archived: true,  deleted: false };
const DEL     = { active: false, archived: false, deleted: true  };
const NONE    = { active: false, archived: false, deleted: false };

function mkBucket({ active = {}, archived = {}, deleted = {}, per_model = {}, convos = 0 } = {}) {
  return {
    input_tokens: active.input_tokens || 0,
    output_tokens: active.output_tokens || 0,
    cache_read_tokens: active.cache_read_tokens || 0,
    cache_creation_tokens: active.cache_creation_tokens || 0,
    tool_uses: active.tool_uses || {},
    convos,
    per_model,
    archived_delta: {
      input_tokens: archived.input_tokens || 0,
      output_tokens: archived.output_tokens || 0,
      cache_read_tokens: archived.cache_read_tokens || 0,
      cache_creation_tokens: archived.cache_creation_tokens || 0,
      tool_uses: archived.tool_uses || {},
    },
    deleted_delta: {
      input_tokens: deleted.input_tokens || 0,
      output_tokens: deleted.output_tokens || 0,
      cache_read_tokens: deleted.cache_read_tokens || 0,
      cache_creation_tokens: deleted.cache_creation_tokens || 0,
      tool_uses: deleted.tool_uses || {},
    },
  };
}

test("active-only: tokens match raw bucket (no regression on default graph)", () => {
  const byPeriod = {
    "2026-04-14": mkBucket({
      active: { input_tokens: 100, output_tokens: 50, cache_read_tokens: 10, cache_creation_tokens: 5 },
      archived: { input_tokens: 999 },
      deleted: { input_tokens: 777 },
    }),
  };
  const out = applyBucketFilters(byPeriod, ACTIVE);
  assert.equal(out["2026-04-14"].input_tokens, 100);
  assert.equal(out["2026-04-14"].output_tokens, 50);
  assert.equal(out["2026-04-14"].cache_read_tokens, 10);
  assert.equal(out["2026-04-14"].cache_creation_tokens, 5);
});

test("archived-only: reads archived_delta, ignores active + deleted", () => {
  const byPeriod = {
    k: mkBucket({
      active: { input_tokens: 100 },
      archived: { input_tokens: 200, output_tokens: 30 },
      deleted: { input_tokens: 400 },
    }),
  };
  const out = applyBucketFilters(byPeriod, ARCH);
  assert.equal(out.k.input_tokens, 200);
  assert.equal(out.k.output_tokens, 30);
});

test("deleted-only: reads deleted_delta, ignores active + archived", () => {
  const byPeriod = {
    k: mkBucket({
      active: { input_tokens: 100 },
      archived: { input_tokens: 200 },
      deleted: { input_tokens: 400, cache_read_tokens: 5 },
    }),
  };
  const out = applyBucketFilters(byPeriod, DEL);
  assert.equal(out.k.input_tokens, 400);
  assert.equal(out.k.cache_read_tokens, 5);
});

test("all-on: sums every source for every token type", () => {
  const byPeriod = {
    k: mkBucket({
      active:   { input_tokens: 1, output_tokens: 2, cache_read_tokens: 3, cache_creation_tokens: 4 },
      archived: { input_tokens: 10, output_tokens: 20, cache_read_tokens: 30, cache_creation_tokens: 40 },
      deleted:  { input_tokens: 100, output_tokens: 200, cache_read_tokens: 300, cache_creation_tokens: 400 },
    }),
  };
  const out = applyBucketFilters(byPeriod, ALL_ON);
  assert.equal(out.k.input_tokens, 111);
  assert.equal(out.k.output_tokens, 222);
  assert.equal(out.k.cache_read_tokens, 333);
  assert.equal(out.k.cache_creation_tokens, 444);
});

test("all-off: every token + tool count is zero (this is the empty-graph state)", () => {
  const byPeriod = {
    k: mkBucket({
      active: { input_tokens: 100, tool_uses: { Read: 5 } },
    }),
  };
  const out = applyBucketFilters(byPeriod, NONE);
  assert.equal(out.k.input_tokens, 0);
  assert.equal(out.k.tool_calls, 0);
  assert.deepEqual(out.k.tool_uses, {});
});

test("tool_uses merged across enabled sources, dropped for disabled", () => {
  const byPeriod = {
    k: mkBucket({
      active:   { tool_uses: { Read: 5, Edit: 2 } },
      archived: { tool_uses: { Read: 3, Write: 1 } },
      deleted:  { tool_uses: { Edit: 100 } },
    }),
  };
  const out = applyBucketFilters(byPeriod, { active: true, archived: true, deleted: false });
  assert.deepEqual(out.k.tool_uses, { Read: 8, Edit: 2, Write: 1 });
  assert.equal(out.k.tool_calls, 11);
});

test("per_model preserved only when active is on (deltas lack model breakdown)", () => {
  const pm = { "claude-opus": { input_tokens: 50 } };
  const byPeriod = { k: mkBucket({ per_model: pm }) };
  assert.deepEqual(applyBucketFilters(byPeriod, ACTIVE).k.per_model, pm);
  assert.deepEqual(applyBucketFilters(byPeriod, ARCH).k.per_model, {});
  assert.deepEqual(applyBucketFilters(byPeriod, DEL).k.per_model, {});
});

test("missing archived_delta/deleted_delta treated as zero (no crash)", () => {
  const byPeriod = {
    k: { input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_creation_tokens: 0, tool_uses: {}, convos: 1, per_model: {} },
  };
  const out = applyBucketFilters(byPeriod, ALL_ON);
  assert.equal(out.k.input_tokens, 10);
  assert.equal(out.k.output_tokens, 5);
});

test("empty byPeriod returns empty out", () => {
  assert.deepEqual(applyBucketFilters({}, ACTIVE), {});
  assert.deepEqual(applyBucketFilters(null, ACTIVE), {});
  assert.deepEqual(applyBucketFilters(undefined, ACTIVE), {});
});

test("falsy bucket value skipped, not copied as-is", () => {
  const out = applyBucketFilters({ a: null, b: undefined, c: mkBucket({ active: { input_tokens: 7 } }) }, ACTIVE);
  assert.equal(Object.keys(out).length, 1);
  assert.equal(out.c.input_tokens, 7);
});
