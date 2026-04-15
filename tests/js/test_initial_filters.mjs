// Tests for the load-time filters invariant — guards the "all three off in
// localStorage → empty graphs with no recovery" case reported after stale
// state from before the toggle-filter guard existed.
//
// Run: node --test tests/js/

import { test } from "node:test";
import assert from "node:assert/strict";

// state.js uses `localStorage` at module load to build its default `state`
// object. `computeInitialFilters` itself is pure (takes a storage param), but
// the surrounding module reads the global — so we shim globalThis.localStorage
// before importing.
globalThis.localStorage = {
  _data: {},
  getItem(k) { return k in this._data ? this._data[k] : null; },
  setItem(k, v) { this._data[k] = String(v); },
  removeItem(k) { delete this._data[k]; },
};
const { computeInitialFilters } = await import("../../llm_lens/static/js/state.js");

function mockStorage(initial = {}) {
  const data = { ...initial };
  return {
    _data: data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
}

test("fresh storage: active=true, others=false (no forced write since active is already on)", () => {
  const s = mockStorage();
  const f = computeInitialFilters(s);
  assert.deepEqual(f, { active: true, archived: false, deleted: false });
  assert.equal(s._data.filter_active, undefined, "no write needed — default already satisfies invariant");
});

test("filter_active=0, archived+deleted unset → active forced back on (invariant repair)", () => {
  const s = mockStorage({ filter_active: "0" });
  const f = computeInitialFilters(s);
  assert.equal(f.active, true);
  assert.equal(s._data.filter_active, "1", "persists the repair so next load stays valid");
});

test("active off, archived on → honored, no forcing", () => {
  const s = mockStorage({ filter_active: "0", filter_archived: "1" });
  const f = computeInitialFilters(s);
  assert.deepEqual(f, { active: false, archived: true, deleted: false });
  assert.equal(s._data.filter_active, "0", "active stays off since archived covers the invariant");
});

test("active off, deleted on → honored, no forcing", () => {
  const s = mockStorage({ filter_active: "0", filter_deleted: "1" });
  const f = computeInitialFilters(s);
  assert.deepEqual(f, { active: false, archived: false, deleted: true });
});

test("legacy showDeleted=1 migrates into deleted filter", () => {
  const s = mockStorage({ showDeleted: "1" });
  const f = computeInitialFilters(s);
  assert.equal(f.deleted, true);
});

test("all three explicitly off → active repaired back on", () => {
  const s = mockStorage({ filter_active: "0", filter_archived: "0", filter_deleted: "0" });
  const f = computeInitialFilters(s);
  assert.equal(f.active, true);
  assert.equal(f.archived, false);
  assert.equal(f.deleted, false);
  assert.equal(s._data.filter_active, "1");
});

test("all three explicitly on → all three on", () => {
  const s = mockStorage({ filter_active: "1", filter_archived: "1", filter_deleted: "1" });
  const f = computeInitialFilters(s);
  assert.deepEqual(f, { active: true, archived: true, deleted: true });
});
