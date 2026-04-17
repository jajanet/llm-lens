// Scope-agnostic tag UI.
//
// Every function in this module takes a `scope` as its first argument
// (see `./tag_scope.js`) and operates on whatever namespace that scope
// represents — per-project convo tags or the global project-tag set.
// Rendered DOM gets `data-scope="${scope.name}"` stamped on it so
// main.js's click dispatcher can look the scope back up from any
// descendant element (see `scopes.js :: scopeFor`).
//
// The split vs. scopes.js: this file is pure behavior (render + mutate
// through a scope); scopes.js owns the live singletons. New UI surfaces
// that want tags just import from here with a scope instance — they
// never touch `api`/`state` directly.

import { state } from "./state.js";
import { esc, escAttr, toast } from "./utils.js";

export const NUM_TAG_COLORS = 8;

// Pick a default color for a freshly-created tag: uniformly random
// from the colors no existing tag is using; if all 8 are in use,
// random from all 8.
export function pickDefaultColor(labels) {
  const used = new Set((labels || []).map((l) => l && l.color).filter((c) => typeof c === "number"));
  const unused = [];
  for (let i = 0; i < NUM_TAG_COLORS; i++) if (!used.has(i)) unused.push(i);
  const pool = unused.length ? unused : Array.from({ length: NUM_TAG_COLORS }, (_, i) => i);
  return pool[Math.floor(Math.random() * pool.length)];
}

// HTML for the 8-swatch color row inside an inline tag editor.
export function renderSwatchRow(selected) {
  let h = '<span class="tag-swatch-row">';
  for (let i = 0; i < NUM_TAG_COLORS; i++) {
    const sel = i === selected ? " selected" : "";
    h += `<button type="button" class="tag-swatch tag-swatch-${i}${sel}" data-color="${i}" aria-label="Color ${i + 1}"></button>`;
  }
  h += "</span>";
  return h;
}

// Pills rendered inline on a row/card (read-only filter pills).
export function renderTagPills(scope, key) {
  const ids = scope.getAssignment(key);
  const byId = new Map(scope.getLabels().map((l) => [l.id, l]));
  return ids.map((id) => {
    const label = byId.get(id);
    if (!label || !label.name) return "";
    return `<span class="tag-pill tag-pill-sm tag-color-${label.color}">${esc(label.name)}</span>`;
  }).join("");
}

// The labeled bar above a list (convos or projects). Edit mode adds
// a "+ Add tag" pill and a pencil-on-hover to open the inline editor.
export function renderTagBar(scope, { editMode, label = "Tags" } = {}) {
  const labels = scope.getLabels();
  const activeFilters = scope.getActiveFilters();

  if (!editMode) {
    if (!labels.length) return "";
    let pills = "";
    for (const l of labels) {
      if (!l || !l.name) continue;
      const isActive = activeFilters.includes(l.id);
      pills += `<span class="tag-pill tag-color-${l.color}${isActive ? " active" : ""}" data-scope="${scope.name}" data-action="toggle-tag-filter" data-tag="${l.id}" title="Filter by '${escAttr(l.name)}'">${esc(l.name)}</span>`;
    }
    return `<div class="tag-bar" data-scope="${scope.name}"><span class="tag-bar-label">${esc(label)}</span>${pills}</div>`;
  }

  let pills = "";
  for (const l of labels) {
    if (!l || !l.name) continue;
    const isActive = activeFilters.includes(l.id);
    pills += `<span class="tag-pill tag-pill-editable tag-color-${l.color}${isActive ? " active" : ""}" data-scope="${scope.name}" data-action="toggle-tag-filter" data-tag="${l.id}" title="Click body to filter">${esc(l.name)}<button class="tag-pencil" data-scope="${scope.name}" data-action="rename-tag-start" data-tag="${l.id}" title="Edit tag" aria-label="Edit tag">✎</button></span>`;
  }
  pills += `<span class="tag-pill tag-pill-empty" data-scope="${scope.name}" data-action="add-new-tag" title="Create a new tag">+ Add tag</span>`;
  return `<div class="tag-bar" data-scope="${scope.name}"><span class="tag-bar-label">${esc(label)}</span>${pills}</div>`;
}

// Flip a tag id in the scope's filter set and re-render via onChange.
export function toggleTagFilter(scope, tagId) {
  const current = [...scope.getActiveFilters()];
  const idx = current.indexOf(tagId);
  if (idx >= 0) current.splice(idx, 1);
  else current.push(tagId);
  scope.setActiveFiltersLocal(current);
  scope.onChange();
}

// Toggle a tag on a single key (convo id or folder), persist, re-render.
export async function toggleKeyTag(scope, key, tagId) {
  const current = scope.getAssignment(key);
  const next = current.includes(tagId)
    ? current.filter((i) => i !== tagId)
    : [...current, tagId].sort();
  const all = { ...scope.getAssignments(), [key]: next };
  if (!next.length) delete all[key];
  scope.setAssignmentsLocal(all);
  try { await scope.setAssignment(key, next); } catch { /* best-effort */ }
  scope.onChange();
}

// Open the inline editor over an existing tag pill (tagId set) or the
// "+ Add tag" pill (tagId null). Mousedown-preventDefault on swatches
// keeps the input focused so blur→commit sees the right color.
export function openTagEditor(scope, anchorEl, { tagId, initialName = "", initialColor } = {}) {
  const root = anchorEl.closest(".tag-bar") || document.body;
  const labels = scope.getLabels();
  const existing = tagId != null ? labels.find((l) => l && l.id === tagId) : null;
  const startName = existing ? existing.name : initialName;
  const startColor = existing
    ? existing.color
    : (typeof initialColor === "number" ? initialColor : pickDefaultColor(labels));

  let selectedColor = startColor;
  const editorHtml =
    `<span class="tag-edit-row" data-scope="${scope.name}" data-tag-edit="${tagId ?? ""}">` +
      `<input class="tag-rename-input" value="${escAttr(startName)}" placeholder="Tag name..." maxlength="30">` +
      renderSwatchRow(selectedColor) +
    `</span>`;
  anchorEl.outerHTML = editorHtml;
  const row = root.querySelector(`.tag-edit-row[data-scope="${scope.name}"][data-tag-edit="${tagId ?? ""}"]`);
  if (!row) return;
  const input = row.querySelector(".tag-rename-input");
  input.focus();
  input.select();

  row.addEventListener("mousedown", (e) => {
    const sw = e.target.closest(".tag-swatch");
    if (!sw) return;
    e.preventDefault();
    e.stopPropagation();
    selectedColor = parseInt(sw.dataset.color, 10);
    row.querySelectorAll(".tag-swatch").forEach((el) => el.classList.remove("selected"));
    sw.classList.add("selected");
  });

  let committed = false;
  const commit = async () => {
    if (committed) return;
    committed = true;
    const name = input.value.trim().slice(0, 30);
    const current = scope.getLabels();

    if (!name) {
      if (tagId == null) { scope.onChange(); return; }
      // Existing tag → delete. Optimistic local scrub; server mirrors.
      const next = current.filter((l) => l.id !== tagId);
      scope.setLabelsLocal(next);
      const assignments = { ...scope.getAssignments() };
      for (const k of Object.keys(assignments)) {
        const filtered = (assignments[k] || []).filter((i) => i !== tagId);
        if (filtered.length) assignments[k] = filtered;
        else delete assignments[k];
      }
      scope.setAssignmentsLocal(assignments);
      scope.setActiveFiltersLocal(scope.getActiveFilters().filter((i) => i !== tagId));
      try { await scope.setLabels(next); } catch { /* best-effort */ }
      scope.onChange();
      return;
    }

    if (tagId == null) {
      const draft = [...current, { name, color: selectedColor }];
      try {
        await scope.setLabels(draft);
        await scope.refresh();
      } catch { /* best-effort */ }
      scope.onChange();
      return;
    }

    const next = current.map((l) => l.id === tagId ? { ...l, name, color: selectedColor } : l);
    scope.setLabelsLocal(next);
    try { await scope.setLabels(next); } catch { /* best-effort */ }
    scope.onChange();
  };

  input.addEventListener("blur", () => { setTimeout(commit, 0); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { committed = true; scope.onChange(); }
  });
}

export function renameTag(scope, tagId) {
  const el = document.querySelector(
    `.tag-bar[data-scope="${scope.name}"] [data-tag="${tagId}"]`,
  );
  if (!el) return;
  openTagEditor(scope, el, { tagId });
}

export function addNewTag(scope) {
  const el = document.querySelector(
    `.tag-bar[data-scope="${scope.name}"] [data-action="add-new-tag"]`,
  );
  if (!el) return;
  openTagEditor(scope, el, { tagId: null });
}

// ── Tag-assign popup (bulk apply or create+apply in one step) ───────

let _tagPopupCtx = null;

export function openTagAssignPopup(scope, anchorEl, opts = {}) {
  document.querySelectorAll(".tag-assign-popup").forEach((el) => el.remove());

  const ids = opts.ids ? [...opts.ids] : scope.defaultIds();
  if (!ids.length) return;

  const labels = scope.getLabels();
  const named = labels.filter((l) => l && l.name);
  const poolLabel = opts.poolLabel || `${ids.length}`;
  const defaultColor = pickDefaultColor(labels);
  _tagPopupCtx = {
    scope,
    ids,
    onDone: opts.onDone || null,
    selectedColor: defaultColor,
  };

  const pillsHtml = named.length
    ? named.map((l) =>
        `<div class="tag-assign-row" data-scope="${scope.name}" data-action="apply-existing-tag" data-tag="${l.id}"><span class="tag-pill tag-pill-sm tag-color-${l.color}">${esc(l.name)}</span><span class="tag-assign-hint">apply to ${poolLabel}</span></div>`
      ).join("")
    : `<div class="tag-assign-empty">No tags yet — create one below.</div>`;

  const createHtml =
    `<div class="tag-assign-create">
       <input type="text" class="tag-assign-new-input" placeholder="Create new tag..." maxlength="30">
       ${renderSwatchRow(defaultColor)}
       <button class="btn btn-sm" data-scope="${scope.name}" data-action="create-and-assign-tag">Create &amp; apply</button>
     </div>`;

  const popup = document.createElement("div");
  popup.className = "tag-assign-popup";
  popup.dataset.scope = scope.name;
  popup.innerHTML = `<div class="tag-assign-list">${pillsHtml}</div>${createHtml}`;
  document.body.appendChild(popup);

  const rect = anchorEl.getBoundingClientRect();
  popup.style.top = `${rect.bottom + 4 + window.scrollY}px`;
  popup.style.left = `${rect.left + window.scrollX}px`;

  const input = popup.querySelector(".tag-assign-new-input");
  if (input && !named.length) input.focus();

  popup.addEventListener("mousedown", (e) => {
    const sw = e.target.closest(".tag-swatch");
    if (!sw) return;
    e.preventDefault();
    e.stopPropagation();
    const c = parseInt(sw.dataset.color, 10);
    if (!_tagPopupCtx) return;
    _tagPopupCtx.selectedColor = c;
    popup.querySelectorAll(".tag-swatch").forEach((el) => el.classList.remove("selected"));
    sw.classList.add("selected");
  });

  setTimeout(() => {
    const close = (e) => {
      if (!popup.contains(e.target) && e.target !== anchorEl) {
        popup.remove();
        _tagPopupCtx = null;
        document.removeEventListener("click", close);
      }
    };
    document.addEventListener("click", close);
  }, 0);
}

export async function applyExistingTag(tagId) {
  const ctx = _tagPopupCtx;
  if (!ctx) return;
  const { scope, ids } = ctx;
  if (!ids.length) return;
  try { await scope.bulkAssign(ids, tagId, true); }
  catch { toast("Tag assign failed"); return; }
  const all = { ...scope.getAssignments() };
  for (const id of ids) {
    const cur = new Set(all[id] || []);
    cur.add(tagId);
    all[id] = [...cur].sort();
  }
  scope.setAssignmentsLocal(all);
  document.querySelectorAll(".tag-assign-popup").forEach((el) => el.remove());
  _tagPopupCtx = null;
  if (ctx.onDone) ctx.onDone();
  else scope.onChange();
  toast(`Tagged ${ids.length}`);
}

export async function createAndAssignTag() {
  const popup = document.querySelector(".tag-assign-popup");
  const input = popup && popup.querySelector(".tag-assign-new-input");
  const name = input ? input.value.trim().slice(0, 30) : "";
  if (!name) { if (input) input.focus(); return; }
  const ctx = _tagPopupCtx;
  if (!ctx) return;
  const { scope, ids } = ctx;
  if (!ids.length) return;
  const color = typeof ctx.selectedColor === "number"
    ? ctx.selectedColor
    : pickDefaultColor(scope.getLabels());

  const draft = [...scope.getLabels(), { name, color }];
  let newId = null;
  try {
    await scope.setLabels(draft);
    const priorIds = new Set(scope.getLabels().map((l) => l.id));
    await scope.refresh();
    const created = scope.getLabels().find((l) => !priorIds.has(l.id));
    newId = created ? created.id : null;
  } catch { toast("Create + apply failed"); return; }

  if (newId == null) { toast("Created, but couldn't find new id to apply"); scope.onChange(); return; }

  try { await scope.bulkAssign(ids, newId, true); }
  catch { toast("Tag assign failed"); return; }
  const all = { ...scope.getAssignments() };
  for (const id of ids) {
    const cur = new Set(all[id] || []);
    cur.add(newId);
    all[id] = [...cur].sort();
  }
  scope.setAssignmentsLocal(all);
  document.querySelectorAll(".tag-assign-popup").forEach((el) => el.remove());
  _tagPopupCtx = null;
  if (ctx.onDone) ctx.onDone();
  else scope.onChange();
  toast(`Tagged ${ids.length} with '${name}'`);
}

// Suppress "state is defined but never used" linter hints: even though
// this module doesn't touch state directly, importing it keeps the
// dependency explicit if we ever add scope-agnostic state reads.
void state;
