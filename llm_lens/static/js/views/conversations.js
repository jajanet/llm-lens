// Conversations view: list of all .jsonl files in a project.

import { state, invalidateProjectsCache, setViewMode, togglePreviewPosition, persistSelections } from "../state.js";
import { api } from "../api.js";
import { timeAgo, timeAbs, fmtSize, esc, escAttr, arrow, toast, renderStatsInline, renderStatsModalBody, fmtTokens, contextWindowFor } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal, showInfoModal } from "../modal.js";
import { navigate } from "../router.js";
import { renderOverviewBar, hydrateOverview } from "./projects.js";
import { convoScope, projectScope } from "../scopes.js";
import * as Tags from "../tag_components.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");
const PAGE = 30;

export async function show(folder) {
  const prevFolder = state.folder;
  state.view = "conversations";
  state.folder = folder;

  // Reset paging + filters on fresh navigation
  state.convoOffset = 0;
  state.convoItems = [];
  // Keep selection across accidental navigations while edit mode is
  // on; wipe when switching folders (selection is per-folder) or when
  // edit mode is off. localStorage mirrors this so hard reload works.
  if (!state.editMode || prevFolder !== folder) state.selected.clear();
  state.search = "";
  state.sort = "recent";
  state.desc = true;
  convoScope.setActiveFiltersLocal([]);
  state.tagMode = null;
  state.smartSelect = null;
  state.smartMatches = new Set();

  // Scope the overview to this folder. On scope change, drop cached data and
  // reset offset — the prior project's settings don't apply here.
  if (state.overviewScope !== folder) {
    state.overviewScope = folder;
    state.overview = null;
    state.overviewOffset = 0;
  }

  // Derive displayed path from cached projects (fetch if needed)
  await resolvePath(folder);
  renderBreadcrumb();

  app.innerHTML = '<div class="loading">Loading...</div>';
  await fetchPage(false);
  render();
  hydrateOverview();
  hydrateTags();
  hydrateProjectTags();
}

async function resolvePath(folder) {
  if (!state.projectsCache) state.projectsCache = await api.projects();
  const proj = state.projectsCache.find((p) => p.folder === folder);
  state.path = proj ? proj.path : folder;
}

export function renderBreadcrumb() {
  // Project-tag pills for the current folder. Always visible if the
  // project has any assigned tags; edit mode adds × for remove and a
  // "Tag this project" button. Mirrors the "Tag this convo" flow in
  // messages.js — pointed at the project-tag namespace instead.
  let tagsHtml = "";
  if (state.folder) {
    const assigned = projectScope.getAssignment(state.folder);
    const byId = new Map(projectScope.getLabels().map((l) => [l.id, l]));
    const pills = assigned.map((id) => {
      const label = byId.get(id);
      if (!label || !label.name) return "";
      if (state.editMode) {
        return `<span class="tag-pill tag-pill-sm tag-color-${label.color} tag-pill-removable" data-scope="projects" data-action="remove-project-tag" data-tag="${label.id}" title="Click to remove">${esc(label.name)} <span class="tag-x">×</span></span>`;
      }
      return `<span class="tag-pill tag-pill-sm tag-color-${label.color}">${esc(label.name)}</span>`;
    }).join("");
    const tagBtn = state.editMode
      ? `<button class="btn btn-sm bc-tag-btn" data-scope="projects" data-action="open-project-tag-popup" title="Tag this project">Tag this project</button>`
      : "";
    if (pills || tagBtn) {
      tagsHtml = `<span class="bc-tags">${pills}${tagBtn}</span>`;
    }
  }
  bc.innerHTML = `<a data-action="nav-projects">Projects</a> / <span class="bc-convo-name">${esc(state.path || "")}</span>${tagsHtml}`;
}

async function fetchPage(append) {
  // Mode picks the source: active → live (paginated), archived → archive dir
  // (full list, no pagination). Each convo is tagged `archived: bool` so
  // per-card buttons can pick Arch/Unarch.
  if (state.mode === "archived") {
    let data;
    try {
      data = await api.archivedConversations(state.folder);
    } catch { data = { items: [], total: 0 }; }
    const items = (data.items || []).map((c) => ({ ...c, archived: true }));
    state.convoTotal = items.length;
    state.convoItems = items;
    hydrateNames(items);
    hydrateStats(items);
    return;
  }

  const data = await api.conversations(state.folder, {
    offset: state.convoOffset,
    limit: PAGE,
    sort: state.sort,
    desc: state.desc,
  });
  state.convoTotal = data.total;
  const liveItems = (data.items || []).map((c) => ({ ...c, archived: false }));
  state.convoItems = append ? state.convoItems.concat(liveItems) : liveItems;
  hydrateNames(liveItems);
  hydrateStats(liveItems);
}

// Fire-and-forget: asks the server for `/rename`-assigned titles for the just-
// loaded page, merges them into state so future re-renders keep them, and
// patches the rendered cells in place (no flicker, no lost caret/scroll).
async function hydrateNames(items) {
  if (!items.length) return;
  const ids = items.map((c) => c.id);
  let names;
  try {
    names = await api.conversationNames(state.folder, ids);
  } catch { return; }
  names = names || {};

  const byId = new Map(state.convoItems.map((c) => [c.id, c]));
  // Mark every requested id as hydrated so the fallback (id slice) can take
  // over for sessions that never got a `/rename`.
  for (const id of ids) {
    const c = byId.get(id);
    if (!c) continue;
    c.nameHydrated = true;
    if (names[id]) c.name = names[id];

    const display = c.name || c.id.slice(0, 8);
    const hasName = Boolean(c.name);
    // Patch the inner text span only — container (td/div) keeps the copy
    // button sibling. Dim class toggles on the container.
    for (const [containerSel, textSel, dimCls] of [
      [`tr[data-id="${CSS.escape(id)}"] .col-name`, ".col-name-text", "col-name-dim"],
      [`.card[data-id="${CSS.escape(id)}"] .card-name`, ".card-name-text", "card-name-dim"],
    ]) {
      const container = app.querySelector(containerSel);
      if (!container) continue;
      const textEl = container.querySelector(textSel);
      if (textEl) textEl.textContent = display;
      container.classList.remove("col-name-dim", "card-name-dim", "is-loading");
      if (!hasName) container.classList.add(dimCls);
    }
  }
}

// Per-convo stats: cards only. Fetches tokens/models/tool_uses/thinking/branch
// in one batched request and replaces the `.card-stats` placeholder content.
async function hydrateStats(items) {
  if (!items.length) return;
  const ids = items.map((c) => c.id);
  let stats;
  try {
    stats = await api.conversationStats(state.folder, ids);
  } catch { return; }
  stats = stats || {};

  const byId = new Map(state.convoItems.map((c) => [c.id, c]));
  for (const id of ids) {
    const c = byId.get(id);
    if (!c) continue;
    const s = stats[id];
    if (s) c.stats = s;
    c.statsHydrated = true;
  }
  // Re-render so cards and table rows both pick up hydrated stats uniformly.
  // Cheap for pages of ≤30 items and avoids the fragility of per-card
  // querySelector-innerHTML patches (which can miss if renderCards runs again
  // between fetch start and completion).
  if (state.smartSelect) recomputeSmartMatches();
  render();
}


// ── Tags ──────────────────────────────────────────────────────────────

async function hydrateTags() {
  try {
    await convoScope.refresh();
  } catch {
    convoScope.setLabelsLocal([]);
    convoScope.setAssignmentsLocal({});
  }
  render();
}

async function hydrateProjectTags() {
  // The project-tag namespace is shared across views. Fetching here
  // means jumping straight to `#/p/<folder>` (without going via the
  // projects list) still has data for the breadcrumb pills.
  try { await projectScope.refresh(); } catch { /* best-effort */ }
  renderBreadcrumb();
}



// ── Smart-select ──────────────────────────────────────────────────────

const SMART_PRESETS = [
  { key: "tools",     label: "Heavy tools",  min: 0, max: 200, step: 5,  defaultVal: 20 },
  { key: "thinking",  label: "Thinking",     min: 0, max: 100, step: 1,  defaultVal: 5 },
  { key: "expensive", label: "Expensive",    min: 0, max: 2000000, step: 50000, defaultVal: 200000 },
  { key: "edited",    label: "Edited",       min: 0, max: 0,   step: 0,  defaultVal: 0 },
];

function computeSmartMatches(preset, threshold) {
  const matches = new Set();
  for (const c of state.convoItems) {
    if (!c.stats) continue;
    const s = c.stats;
    switch (preset) {
      case "tools": {
        const total = Object.values(s.tool_uses || {}).reduce((a, b) => a + b, 0);
        if (total > threshold) matches.add(c.id);
        break;
      }
      case "thinking":
        if ((s.thinking_count || 0) > threshold) matches.add(c.id);
        break;
      case "expensive": {
        const total = (s.input_tokens || 0) + (s.output_tokens || 0);
        if (total > threshold) matches.add(c.id);
        break;
      }
      case "edited": {
        const dd = s.deleted_delta || {};
        if ((dd.messages_deleted || 0) > 0
            || (dd.messages_scrubbed || 0) > 0
            || (dd.messages_edited || 0) > 0) matches.add(c.id);
        break;
      }
    }
  }
  return matches;
}

function recomputeSmartMatches() {
  if (!state.smartSelect) { state.smartMatches = new Set(); return; }
  state.smartMatches = computeSmartMatches(state.smartSelect.preset, state.smartSelect.threshold);
}

export function toggleSmartSelect(presetKey) {
  if (state.smartSelect && state.smartSelect.preset === presetKey) {
    state.smartSelect = null;
    state.smartMatches = new Set();
  } else {
    const def = SMART_PRESETS.find((p) => p.key === presetKey);
    state.smartSelect = { preset: presetKey, threshold: def ? def.defaultVal : 0 };
    recomputeSmartMatches();
  }
  render();
}

export function setSmartThreshold(value) {
  if (!state.smartSelect) return;
  state.smartSelect.threshold = value;
  recomputeSmartMatches();
  render();
}

function renderSmartBar() {
  // Smart-select is a tagging/management tool — only visible in edit mode.
  if (!state.editMode) return "";

  let buttons = "";
  for (const p of SMART_PRESETS) {
    const isActive = state.smartSelect && state.smartSelect.preset === p.key;
    buttons += `<button class="smart-btn${isActive ? " active" : ""}" data-action="smart-select" data-preset="${p.key}">${p.label}</button>`;
  }

  let sliderHtml = "";
  if (state.smartSelect) {
    const p = SMART_PRESETS.find((x) => x.key === state.smartSelect.preset);
    if (p && p.max > 0) {
      const fmtVal = state.smartSelect.preset === "expensive"
        ? `${Math.round(state.smartSelect.threshold / 1000)}k tokens`
        : state.smartSelect.threshold;
      sliderHtml = `<input type="range" class="smart-slider" data-action="smart-threshold" min="${p.min}" max="${p.max}" step="${p.step}" value="${state.smartSelect.threshold}"> <span class="smart-count">&gt; ${fmtVal}</span>`;
    }
    sliderHtml += ` <span class="smart-count">${state.smartMatches.size} matching</span>`;
  }

  return `<div class="smart-bar"><span class="smart-bar-label">Select</span>${buttons}${sliderHtml}</div>`;
}


// renderFilterToggles removed — toggles live only in the overview bar.
function _unused_renderFilterToggles() { return ""; }

function renderToolbar() {
  const selN = state.selected.size;
  const smartN = state.smartMatches.size;
  const assignPool = selN > 0 ? selN : smartN;
  let extra = "";
  if (selN > 0) {
    // Bulk actions depend on mode: Active selection → Archive+Delete,
    // Archived selection → Unarchive+Delete.
    if (state.mode === "archived") {
      extra += `<button class="btn" data-action="bulk-unarchive-convos">Unarchive ${selN}</button> `;
    } else {
      extra += `<button class="btn" data-action="bulk-archive-convos">Archive ${selN}</button> `;
    }
    extra += `<button class="btn-danger" data-action="bulk-delete-convos">Delete ${selN}</button> `;
  }
  // Edit-mode-only: assign tag to selected (or smart-matched).
  if (state.editMode && assignPool > 0) {
    const srcLabel = selN > 0 ? "selected" : "matching";
    extra += `<button class="btn" data-action="open-tag-assign-popup" title="Apply a tag to all ${srcLabel}">Tag ${assignPool} ${srcLabel}</button> `;
  }
  if (state.editMode) {
    // Action-phrased button: label is what clicking does, not current state.
    const nextIsLast = state.previewPosition === "first";
    const posLabel = nextIsLast ? "Show last message" : "Show first message";
    const posTitle = nextIsLast
      ? "Switch the preview column to show each convo's last user message (resume context)."
      : "Switch the preview column back to each convo's first user message (topic).";
    extra += `<button class="btn" data-action="toggle-preview-position" title="${posTitle}">${posLabel}</button>`;
  }
  extra += `<span style="display:inline-block; width:1px; height:22px; background:var(--border); margin:0 4px; vertical-align:middle"></span>`;
  extra += `<button class="btn ${state.viewMode === "list" ? "active" : ""}" data-action="set-view-mode" data-mode="list">&#9776;</button>`;
  extra += `<button class="btn ${state.viewMode === "grid" ? "active" : ""}" data-action="set-view-mode" data-mode="grid">&#9638;</button>`;

  configureToolbar({
    placeholder: "Filter conversations...",
    searchValue: state.search,
    extraHtml: extra,
    onSearch: (v) => { state.search = v; render(); },
  });
}

export function render() {
  renderToolbar();
  renderBreadcrumb();

  let items = state.convoItems;
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter((c) =>
      c.preview.toLowerCase().includes(q) ||
      (c.last_preview || "").toLowerCase().includes(q) ||
      (c.name && c.name.toLowerCase().includes(q))
    );
  }
  // Tag filter (OR logic)
  const activeFilters = convoScope.getActiveFilters();
  if (activeFilters.length) {
    items = items.filter((c) => {
      const assigned = convoScope.getAssignment(c.id);
      return assigned.some((t) => activeFilters.includes(t));
    });
  }

  const overviewHtml = renderOverviewBar();
  const smartBarHtml = renderSmartBar();
  const tagBarHtml = Tags.renderTagBar(convoScope, { editMode: state.editMode });
  const totalSize = items.reduce((s, c) => s + (c.size_kb || 0), 0);

  if (!items.length) {
    const empty = state.mode === "archived"
      ? "No archived conversations"
      : "No conversations";
    app.innerHTML = overviewHtml + smartBarHtml + tagBarHtml + `<div class="empty-state">${empty}</div>`;
    return;
  }

  const body = state.viewMode === "list" ? renderTable(items, totalSize) : renderCards(items, totalSize);
  // Pagination only applies in Active mode (archived set is fully fetched).
  const more = state.mode !== "archived" && state.convoItems.length < state.convoTotal
    ? `<div class="load-more-bar"><button class="btn" data-action="load-more-convos">Load more (${state.convoItems.length} / ${state.convoTotal})</button></div>`
    : "";
  app.innerHTML = overviewHtml + smartBarHtml + tagBarHtml + body + more;
}

function copyIconSvg() {
  return '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"></rect><path d="M3 10.5V3a1.5 1.5 0 0 1 1.5-1.5H11"></path></svg>';
}

function newTabIconSvg() {
  return '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4v4"></path><path d="M14 2 7.5 8.5"></path><path d="M12 9v4a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h4"></path></svg>';
}

function renderTable(items, totalSize) {
  const pct = (kb) => totalSize > 0 ? `${(kb / totalSize * 100).toFixed(1)}%` : "";
  const allSel = items.length > 0 && items.every((c) => state.selected.has(c.id));
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th class="col-check"><input type="checkbox" style="accent-color:var(--accent)" ${allSel ? "checked" : ""} data-action="toggle-all-convos"></th>`;
  h += `<th class="col-name">Name</th>`;
  h += `<th data-action="sort-convos" data-col="recent">Preview${arrow(state, "recent")}</th>`;
  h += `<th data-action="sort-convos" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-convos" data-col="context" style="text-align:right" title="Context at last turn (how close to /compact)">Ctx${arrow(state, "context")}</th>`;
  h += `<th data-action="sort-convos" data-col="recent" style="text-align:right">Modified${arrow(state, "recent")}</th>`;
  h += `<th class="col-actions" style="width:120px"></th></tr></thead><tbody>`;

  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    const loading = !c.nameHydrated;
    const nameText = loading ? "loading..." : (c.name || c.id.slice(0, 8));
    const dimCls = (loading || !c.name) ? " col-name-dim" : "";
    const loadCls = loading ? " is-loading" : "";
    const rowCls = c.archived ? " row-archived" : "";
    const smartCls = state.smartMatches.has(c.id) ? " smart-match" : "";
    const archBtn = c.archived
      ? `<button class="btn btn-sm" data-action="unarchive-convo" data-id="${escAttr(c.id)}" title="Restore to ~/.claude/projects/">Unarch</button>`
      : `<button class="btn btn-sm" data-action="archive-convo" data-id="${escAttr(c.id)}" title="Removes from Claude Code's /resume list. Restore anytime from the Archived filter.">Arch</button>`;
    const archBadge = c.archived ? `<span class="badge badge-archived">archived</span> ` : "";
    const tagPills = Tags.renderTagPills(convoScope, c.id);
    const copyBtn = `<button class="copy-id-btn" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`;
    const convoHref = `#/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(c.id)}`;
    const openNewTabBtn = `<a class="open-new-tab-btn" data-action="noop-anchor" href="${convoHref}" target="_blank" rel="noopener" title="Open in new tab" aria-label="Open in new tab">${newTabIconSvg()}</a>`;
    const agentPill = c.agent ? `<span class="badge badge-agent" title="Agent: ${escAttr(c.agent)}">@${esc(c.agent)}</span> ` : "";

    // Context cell: only render a value once stats have hydrated.
    let ctxCell = '<span class="stats-dim">—</span>';
    if (c.statsHydrated && c.stats) {
      const s = c.stats;
      const ctxTokens = (s.last_context_input_tokens || 0)
                      + (s.last_context_cache_creation_tokens || 0)
                      + (s.last_context_cache_read_tokens || 0);
      if (ctxTokens) {
        const win = contextWindowFor(s.last_model_for_context, ctxTokens, state.planContextWindow);
        const ctxPct = Math.round(ctxTokens / win * 100);
        ctxCell = `${fmtTokens(ctxTokens)} <span class="stats-pct">(${ctxPct}%)</span>`;
      }
    }

    h += `
      <tr class="${rowCls}${smartCls}" data-action="open-convo" data-id="${escAttr(c.id)}">
        <td class="col-check">
          <span class="check-hit" data-action="toggle-convo-sel" data-id="${escAttr(c.id)}"><input type="checkbox" class="item-check" ${ck} tabindex="-1"></span>
        </td>
        <td class="col-name${dimCls}${loadCls}"><div class="col-name-wrap"><span class="col-name-text">${archBadge}${agentPill}${esc(nameText)}</span>${tagPills ? ` ${tagPills}` : ""}${copyBtn}${openNewTabBtn}</div></td>
        <td class="col-preview">${esc(state.previewPosition === "last" ? (c.last_preview || c.preview) : c.preview)}</td>
        <td class="col-size" style="text-align:right">${fmtSize(c.size_kb)} <span class="stats-pct">(${pct(c.size_kb)})</span></td>
        <td class="col-ctx" style="text-align:right">${ctxCell}</td>
        <td class="col-time" style="text-align:right" title="${escAttr(timeAbs(c.last_modified))}">${timeAgo(c.last_modified)}</td>
        <td class="col-actions">
          ${archBtn}
          <button class="btn btn-sm" data-action="duplicate-convo" data-id="${escAttr(c.id)}">Dup</button>
          <button class="btn-danger btn-sm" data-action="delete-convo" data-id="${escAttr(c.id)}">Del</button>
        </td>
      </tr>`;
  }
  return h + "</tbody></table></div>";
}

function renderCards(items, totalSize) {
  const pct = (kb) => totalSize > 0 ? `${(kb / totalSize * 100).toFixed(1)}%` : "";
  let h = '<div class="card-grid">';
  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    const loading = !c.nameHydrated;
    const nameText = loading ? "loading..." : (c.name || c.id.slice(0, 8));
    const dimCls = (loading || !c.name) ? " card-name-dim" : "";
    const loadCls = loading ? " is-loading" : "";
    const archCls = c.archived ? " card-archived" : "";
    const smartCls = state.smartMatches.has(c.id) ? " smart-match" : "";
    const statsInner = c.statsHydrated
      ? renderStatsInline(c.stats, {
          includeArchived: state.filters.archived,
          includeDeleted: state.filters.deleted,
        })
      : '<span class="stats-dim is-loading">loading...</span>';

    // Context badge: sits next to the size badge in the footer. Only rendered
    // once stats have hydrated AND the convo has at least one main-thread
    // assistant turn (otherwise ctxTokens is 0).
    let ctxBadge = "";
    if (c.statsHydrated && c.stats) {
      const s = c.stats;
      const ctxTokens = (s.last_context_input_tokens || 0)
                      + (s.last_context_cache_creation_tokens || 0)
                      + (s.last_context_cache_read_tokens || 0);
      if (ctxTokens) {
        const win = contextWindowFor(s.last_model_for_context, ctxTokens, state.planContextWindow);
        const ctxPct = Math.round(ctxTokens / win * 100);
        ctxBadge = `<span class="badge" title="Context at last turn (how close to /compact): ${fmtTokens(ctxTokens)} of ${fmtTokens(win)}">ctx ${fmtTokens(ctxTokens)} <span class="stats-pct">(${ctxPct}%)</span></span>`;
      }
    }

    const agentBadge = c.agent ? `<span class="badge badge-agent" title="Agent: ${escAttr(c.agent)}">@${esc(c.agent)}</span>` : "";

    const archBtn = c.archived
      ? `<button class="btn btn-sm" data-action="unarchive-convo" data-id="${escAttr(c.id)}" title="Restore to ~/.claude/projects/">Unarch</button>`
      : `<button class="btn btn-sm" data-action="archive-convo" data-id="${escAttr(c.id)}" title="Removes from Claude Code's /resume list. Restore anytime from the Archived filter.">Arch</button>`;
    const archBadge = c.archived ? `<span class="badge badge-archived">archived</span>` : "";
    const tagPills = Tags.renderTagPills(convoScope, c.id);
    const copyBtn = `<button class="copy-id-btn" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`;
    const convoHref = `#/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(c.id)}`;
    const openNewTabBtn = `<a class="open-new-tab-btn" data-action="noop-anchor" href="${convoHref}" target="_blank" rel="noopener" title="Open in new tab" aria-label="Open in new tab">${newTabIconSvg()}</a>`;
    h += `
      <div class="card${archCls}${smartCls}" data-action="open-convo" data-id="${escAttr(c.id)}">
        <div class="card-name${dimCls}${loadCls}"><span class="card-name-text">${esc(nameText)}</span>${copyBtn}${openNewTabBtn}</div>
        <div style="display:flex;align-items:start;gap:8px">
          <span class="check-hit" data-action="toggle-convo-sel" data-id="${escAttr(c.id)}"><input type="checkbox" class="item-check" ${ck} tabindex="-1"></span>
          <div class="card-preview" style="flex:1;-webkit-line-clamp:4">${esc(state.previewPosition === "last" ? (c.last_preview || c.preview) : c.preview)}</div>
        </div>
        <div class="card-stats">${statsInner}</div>
        <div class="card-footer">
          ${archBadge}${tagPills ? ` ${tagPills}` : ""}
          <span class="badge">${fmtSize(c.size_kb)} <span class="stats-pct">(${pct(c.size_kb)})</span></span>
          ${ctxBadge}
          ${agentBadge}
          <span class="time-label" title="${escAttr(timeAbs(c.last_modified))}">${timeAgo(c.last_modified)}</span>
          <span style="flex:1"></span>
          ${archBtn}
          <button class="btn btn-sm" data-action="duplicate-convo" data-id="${escAttr(c.id)}">Dup</button>
          <button class="btn-danger btn-sm" data-action="delete-convo" data-id="${escAttr(c.id)}">Del</button>
        </div>
      </div>`;
  }
  return h + "</div>";
}

// === Actions ===

export function sortBy(col) {
  if (state.sort === col) state.desc = !state.desc;
  else { state.sort = col; state.desc = true; }
  state.convoOffset = 0;
  state.convoItems = [];
  refreshAndRender();
}

export async function refreshAndRender() {
  app.innerHTML = '<div class="loading">Loading...</div>';
  await fetchPage(false);
  render();
  hydrateTags();
}

export async function refreshCache() {
  await api.refreshCache(state.folder);
  state.convoOffset = 0;
  state.convoItems = [];
  await refreshAndRender();
}


// Opens a modal with the current project's aggregated stats — same shape as
// the overview modal, just filtered to this one folder. Uses the existing
// projectStats endpoint.
export async function openProjectStats() {
  if (!state.folder) return;
  showInfoModal({ title: "Project stats", body: '<div class="stats-loading">loading...</div>' });
  const convoFilters = convoScope.getActiveFilters();
  const tagsByFolder = convoFilters.length
    ? { [state.folder]: convoFilters }
    : null;
  let data;
  try {
    data = await api.projectStats([state.folder], tagsByFolder);
  } catch {
    const box = document.querySelector(".modal .modal-body");
    if (box) box.innerHTML = '<div class="stats-dim">Failed to load stats.</div>';
    return;
  }
  const stats = data[state.folder];
  const box = document.querySelector(".modal .modal-body");
  if (box) box.innerHTML = renderStatsModalBody(stats, { filters: { ...state.filters } });
}


// Project-level stats modal: sum-aggregates all convos in the current folder
// via the same endpoint used to hydrate the landing cards.
export async function showStats() {
  if (!state.folder) return;
  showInfoModal({ title: `Project stats — ${esc(state.path || state.folder)}`, body: '<div class="stats-loading">loading...</div>' });
  // Pass tag filter through so the stats modal reflects whatever's active.
  const convoFilters = convoScope.getActiveFilters();
  const tagsByFolder = convoFilters.length
    ? { [state.folder]: convoFilters }
    : null;
  let all;
  try {
    all = await api.projectStats([state.folder], tagsByFolder);
  } catch {
    const box = document.querySelector(".modal .modal-body");
    if (box) box.innerHTML = '<div class="stats-dim">Failed to load stats.</div>';
    return;
  }
  const s = (all || {})[state.folder];
  const box = document.querySelector(".modal .modal-body");
  if (box) box.innerHTML = renderStatsModalBody(s, { filters: { ...state.filters } });
}

export async function copyResume(id) {
  // Sync state read — never await before the copy attempt, browsers gate
  // clipboard writes on the live user-activation token from the click and
  // an intermediate await discards it.
  const path = state.path || null;
  // Wrap with `cd` so the command works from any cwd. Claude Code's /resume
  // only finds the convo when the shell's cwd maps to the project folder
  // it lives in.
  const cmd = path
    ? `cd "${path}" && claude --resume ${id}`
    : `claude --resume ${id}`;

  // Try modern clipboard API first; fall back to the legacy textarea +
  // execCommand path, which works in non-secure contexts (raw IPs, file://,
  // http on a non-localhost host) where `navigator.clipboard` is unavailable
  // or rejected by the browser.
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(cmd);
      ok = true;
    }
  } catch { /* fall through */ }
  if (!ok) {
    try {
      const ta = document.createElement("textarea");
      ta.value = cmd;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch { /* still failed */ }
  }
  toast(ok ? `Copied: ${cmd}` : "Copy failed");
}

export async function loadMore() {
  state.convoOffset = state.convoItems.length;
  await fetchPage(true);
  render();
}

export function setMode(mode) {
  setViewMode(mode);
  render();
}

export function toggleSel(id) {
  if (state.selected.has(id)) state.selected.delete(id);
  else state.selected.add(id);
  persistSelections();
  render();
}

export function toggleAll() {
  const visible = state.convoItems || [];
  const allSelected = visible.length > 0 && visible.every((c) => state.selected.has(c.id));
  if (allSelected) for (const c of visible) state.selected.delete(c.id);
  else for (const c of visible) state.selected.add(c.id);
  persistSelections();
  render();
}

export function openConvo(id) {
  navigate(`/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(id)}`);
}

export function deleteConvo(id) {
  const item = state.convoItems.find((c) => c.id === id);
  const archived = !!(item && item.archived);
  showConfirmModal({
    title: "Delete conversation?",
    body: archived
      ? `Permanently deletes this archived conversation. Stats will still
         show under the <em>deleted</em> filter, but the content is gone.
         <strong>Cannot be undone.</strong>`
      : `Permanently deletes the <code>.jsonl</code> file from
         <code>~/.claude/projects/</code>. <strong>Cannot be undone</strong>, and
         you won't be able to <code>/resume</code> it after.
         <br><br>Click <em>Arch</em> to keep the content reversibly, or <em>Dup &amp; Delete</em> to keep a backup copy.`,
    onConfirm: async () => {
      await api.deleteConversation(state.folder, id);
      invalidateProjectsCache();
      await refreshAndRender();
    },
    onDuplicate: archived ? undefined : async () => {
      await api.duplicateConversation(state.folder, id);
      await api.deleteConversation(state.folder, id);
      invalidateProjectsCache();
      await refreshAndRender();
    },
  });
}



export async function archiveConvo(id) {
  try {
    await api.archiveConversation(state.folder, id);
  } catch (e) {
    toast("Archive failed");
    return;
  }
  // Ensure archived filter is on so the user can see where it went.
  if (!state.filters.archived) {
    // Silently flip it on — no setFilter call because we don't want to toggle
    // off active. Persist directly.
    state.filters.archived = true;
    localStorage.setItem("filter_archived", "1");
  }
  invalidateProjectsCache();
  await refreshAndRender();
  toast("Archived");
}


export function unarchiveConvo(id) {
  (async () => {
    try {
      await api.unarchiveConversation(state.folder, id);
    } catch (e) {
      toast("Unarchive failed — a live convo may exist at that path");
      return;
    }
    invalidateProjectsCache();
    await refreshAndRender();
    toast("Restored");
  })();
}

export async function duplicateConvo(id) {
  await api.duplicateConversation(state.folder, id);
  invalidateProjectsCache();
  await refreshAndRender();
}

export function bulkDelete() {
  const ids = [...state.selected];
  if (!ids.length) return;
  showConfirmModal({
    title: `Delete ${ids.length} conversations?`,
    body: `Permanently deletes ${ids.length} <code>.jsonl</code> files from
      <code>~/.claude/projects/</code>. <strong>Cannot be undone</strong>, and
      you won't be able to <code>/resume</code> any of them after.
      <br><br><strong>Prefer Archive</strong> to remove them from
      Claude Code's <code>/resume</code> list reversibly — content stays on
      disk and can be restored from the Archived filter.`,
    onConfirm: async () => {
      await api.bulkDeleteConversations(state.folder, ids);
      state.selected.clear();
      invalidateProjectsCache();
      await refreshAndRender();
    },
  });
}



export function bulkArchive() {
  const ids = [...state.selected];
  if (!ids.length) return;
  (async () => {
    try {
      await api.bulkArchive(state.folder, ids);
    } catch {
      toast("Bulk archive failed");
      return;
    }
    state.selected.clear();
    invalidateProjectsCache();
    await refreshAndRender();
    toast(`Archived ${ids.length}`);
  })();
}


export function bulkUnarchive() {
  const ids = [...state.selected];
  if (!ids.length) return;
  (async () => {
    let res;
    try {
      res = await api.bulkUnarchive(state.folder, ids);
    } catch {
      toast("Bulk unarchive failed");
      return;
    }
    state.selected.clear();
    invalidateProjectsCache();
    await refreshAndRender();
    const skipped = (res && res.skipped && res.skipped.length) || 0;
    toast(skipped
      ? `Restored ${ids.length - skipped}; ${skipped} skipped (live file exists)`
      : `Restored ${ids.length}`);
  })();
}


export function togglePreview() {
  togglePreviewPosition();
  render();
}
