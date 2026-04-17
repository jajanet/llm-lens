// Central registry of live TagScope singletons.
//
// One instance per namespace. The scope's onChange hook is bound by
// main.js once the views are imported (we can't do it here without
// creating a circular dependency). Anything that needs to operate on
// tags imports a scope from here:
//
//   import { convoScope } from "./scopes.js";
//
// And the click-action dispatcher resolves "which scope did this
// click belong to?" via the `data-scope` attribute stamped onto
// rendered elements by the tag components.

import { makeConvoScope, makeProjectScope } from "./tag_scope.js";

export const convoScope = makeConvoScope();
export const projectScope = makeProjectScope();

// Look up the scope an event belongs to based on its DOM ancestry.
// Defaults to `convoScope` so unmarked elements (older code paths)
// keep their current behavior.
export function scopeFor(el) {
  const marker = el.closest && el.closest("[data-scope]");
  const name = marker ? marker.dataset.scope : null;
  return name === "projects" ? projectScope : convoScope;
}

// Returns whichever scope matches a name. Useful for tag components
// that were invoked programmatically (no DOM anchor).
export function scopeByName(name) {
  return name === "projects" ? projectScope : convoScope;
}
