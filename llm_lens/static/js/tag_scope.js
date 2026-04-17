// A TagScope abstracts "where do tags live for this surface?" so the
// generic tag components (tag bar, inline editor, assign popup) can
// operate on either the per-project convo-tag namespace or the global
// project-tag namespace without branching internally.
//
// Scope surface (both `makeConvoScope` and `makeProjectScope` return
// an object with exactly these fields):
//
//   name                    → "convos" | "projects" — stamped on rendered
//                             elements as `data-scope` so the click
//                             dispatcher can resolve which scope a
//                             given button belongs to.
//   onChange()              → re-render hook set by the owning view
//                             (mutable; main.js binds after import).
//   getLabels()             → label list backing the scope
//   getAssignments()        → full {key: [tag_id,...]} map
//   getAssignment(key)      → tag ids for one key (convo id or folder)
//   getActiveFilters()      → tag ids currently filtering the view
//   setLabelsLocal/setAssignmentsLocal/setActiveFiltersLocal
//                           → local (optimistic) setters, no server call
//   setLabels(labels)       → persist + server-side scrub of removed ids
//   setAssignment(key, ids) → persist one key's tag set
//   bulkAssign(keys, id, add) → persist a single add/remove across keys
//   refresh()               → re-fetch authoritative state from server
//
// Callers do optimistic local updates (via *Local helpers) and fire
// the server call; on failure they can roll back by calling refresh().

import { state } from "./state.js";
import { api } from "./api.js";

export function makeConvoScope(options = {}) {
  const scope = {
    name: "convos",
    onChange: options.onChange || (() => {}),

    // reads
    getLabels: () => state.tagLabels || [],
    getAssignments: () => state.tagAssignments || {},
    getAssignment: (key) => (state.tagAssignments || {})[key] || [],
    getActiveFilters: () => state.activeTagFilters || [],

    // local (optimistic) writes
    setLabelsLocal: (labels) => { state.tagLabels = labels; },
    setAssignmentsLocal: (map) => { state.tagAssignments = map; },
    setActiveFiltersLocal: (ids) => { state.activeTagFilters = ids; },

    // server writes
    setLabels: (labels) => api.setTagLabels(state.folder, labels),
    setAssignment: (key, ids) => api.assignTags(state.folder, key, ids),
    bulkAssign: (keys, tagId, add) => api.bulkAssignTag(state.folder, keys, tagId, add),

    // round-trip refresh: overwrites local state with authoritative server state
    refresh: async () => {
      const data = await api.getTags(state.folder);
      state.tagLabels = data.labels || [];
      state.tagAssignments = data.assignments || {};
    },

    // Pool of keys the tag-assign popup acts on when caller didn't
    // supply an explicit ids list. Convo view uses the selection set,
    // with smart-match as a fallback.
    defaultIds: () => state.selected.size > 0
      ? [...state.selected]
      : [...state.smartMatches],
  };
  return scope;
}

export function makeProjectScope(options = {}) {
  const scope = {
    name: "projects",
    onChange: options.onChange || (() => {}),

    getLabels: () => state.projectTagLabels || [],
    getAssignments: () => state.projectTagAssignments || {},
    getAssignment: (key) => (state.projectTagAssignments || {})[key] || [],
    getActiveFilters: () => state.projectActiveTagFilters || [],

    setLabelsLocal: (labels) => { state.projectTagLabels = labels; },
    setAssignmentsLocal: (map) => { state.projectTagAssignments = map; },
    setActiveFiltersLocal: (ids) => { state.projectActiveTagFilters = ids; },

    setLabels: (labels) => api.setProjectTagLabels(labels),
    setAssignment: (key, ids) => api.assignProjectTags(key, ids),
    bulkAssign: (keys, tagId, add) => api.bulkAssignProjectTag(keys, tagId, add),

    refresh: async () => {
      const data = await api.getProjectTags();
      state.projectTagLabels = data.labels || [];
      state.projectTagAssignments = data.assignments || {};
    },

    // Projects view uses a selection set (`projectSelected`) just like
    // the convo view — keeps the bulk-tag flow identical across views.
    defaultIds: () => state.projectSelected
      ? [...state.projectSelected]
      : [],
  };
  return scope;
}
