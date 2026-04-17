// Projects view: list of all ~/.claude/projects folders.

import { state, invalidateProjectsCache, setViewMode } from "../state.js";
import { api } from "../api.js";
import { timeAgo, timeAbs, fmtSize, esc, escAttr, arrow, shortPath, renderStatsInline, renderTokenBars, fmtTokens, renderStatsModalBody, hlText } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal, showInfoModal } from "../modal.js";
import { navigate } from "../router.js";
import { projectScope } from "../scopes.js";
import * as Tags from "../tag_components.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");

export async function show() {
  state.view = "projects";
  state.folder = null;
  state.path = null;
  state.selected.clear();
  // Keep project selection across accidental nav while edit mode is
  // on; reset when exiting edit mode (done by toggleEditMode handler).
  if (!state.editMode) state.projectSelected = new Set();
  projectScope.setActiveFiltersLocal([]);
  state.sort = "recent";
  state.desc = true;
  state.search = "";

  // Overview scope switches from whatever to global (null). Drop cached data
  // so the "loading..." placeholder shows while hydrate repopulates.
  if (state.overviewScope !== null) {
    state.overviewScope = null;
    state.overview = null;
    state.overviewOffset = 0;
  }

  bc.innerHTML = "";

  if (!state.projectsCache) {
    app.innerHTML = '<div class="loading">Loading...</div>';
    state.projectsCache = await api.projects();
  }
  // Pull the global project-tag set + assignments so pills and the
  // tag bar render from real data rather than an empty stub.
  try { await projectScope.refresh(); } catch { /* best-effort */ }
  render();
  hydrateProjectStats(state.projectsCache);
  hydrateOverview();
}

function renderToolbar() {
  const extra = `
    <button class="btn ${state.viewMode === "list" ? "active" : ""}" data-action="set-view-mode" data-mode="list">&#9776;</button>
    <button class="btn ${state.viewMode === "grid" ? "active" : ""}" data-action="set-view-mode" data-mode="grid">&#9638;</button>
  `;
  configureToolbar({
    placeholder: "Filter projects...",
    searchValue: state.search,
    extraHtml: extra,
    onSearch: (v) => { state.search = v; render(); },
  });
}

function sortComparator() {
  const m = state.desc ? -1 : 1;
  switch (state.sort) {
    case "name":    return (a, b) => m * a.path.localeCompare(b.path);
    case "size":    return (a, b) => m * (a.total_size_kb - b.total_size_kb);
    case "convos":  return (a, b) => m * (a.conversation_count - b.conversation_count);
    default:        return (a, b) => m * (a.last_activity || "").localeCompare(b.last_activity || "");
  }
}

export function render() {
  renderToolbar();

  let items = [...(state.projectsCache || [])];
  // Mode filter: active mode drops archive-only projects; archived mode drops
  // active-only projects. A project with both kinds appears in either mode.
  items = items.filter((p) => state.mode === "archived"
    ? (p.archived_count || 0) > 0
    : (p.conversation_count || 0) > 0);
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter((p) =>
      p.path.toLowerCase().includes(q) ||
      p.latest_preview.toLowerCase().includes(q)
    );
  }
  items.sort(sortComparator());

  // Project-tag filter (OR). Same semantics as convo-tag filtering.
  const activeFilters = projectScope.getActiveFilters();
  if (activeFilters.length) {
    items = items.filter((p) => {
      const assigned = projectScope.getAssignment(p.folder);
      return assigned.some((t) => activeFilters.includes(t));
    });
  }

  const overviewHtml = renderOverviewBar();
  const tagBarHtml = Tags.renderTagBar(projectScope, {
    editMode: state.editMode,
    label: "Project tags",
  });
  const totalSize = items.reduce((s, p) => s + (p.total_size_kb || 0), 0);

  // Bulk-tag action bar — inlined into the tag-bar row in edit mode
  // so "N selected / Tag N / Clear" sits on the right of the same
  // line as the tag pills, instead of stacking on its own line.
  const selN = state.projectSelected ? state.projectSelected.size : 0;
  const bulkHtml = (state.editMode && selN > 0)
    ? `<div class="tag-bar-bulk"><span class="tag-bar-sel-count">${selN} selected</span>
         <button class="btn btn-sm" data-scope="projects" data-action="open-tag-assign-popup" title="Apply a tag to selected projects">Tag ${selN} selected</button>
         <button class="btn btn-sm" data-action="clear-project-selection">Clear</button>
       </div>`
    : "";

  // When the tag-bar has content, wrap both in a flex row; otherwise
  // fall back to just the bulk bar (edge case: edit mode on but render
  // happened before tags hydrated).
  const tagRowHtml = tagBarHtml
    ? (bulkHtml ? `<div class="tag-row">${tagBarHtml}${bulkHtml}</div>` : tagBarHtml)
    : bulkHtml;

  if (!items.length) {
    const empty = state.mode === "archived"
      ? "No archived projects"
      : "No projects found";
    app.innerHTML = overviewHtml + tagRowHtml + `<div class="empty-state">${empty}</div>`;
    return;
  }

  const grid = state.viewMode === "list" ? renderTable(items, totalSize) : renderCards(items, totalSize);
  app.innerHTML = overviewHtml + tagRowHtml + grid;
}

export function renderOverviewBar() {
  const ranges = [["all", "all time"], ["year", "year"], ["month", "month"], ["week", "week"], ["day", "day"]];
  const modes  = [["tokens", "data: tokens"], ["tools", "data: tool uses"]];
  const sizes  = [["compact", "view: compact"], ["expanded", "view: expanded"]];
  const groups = [["none", "group: combined"], ["model", "group: by model"]];

  const opt = (v, label, cur) => `<option value="${v}"${v === cur ? " selected" : ""}>${label}</option>`;
  const rangeSel = ranges.map(([v, l]) => opt(v, l, state.overviewRange)).join("");
  const modeSel  = modes.map(([v, l]) => opt(v, l, state.overviewMode)).join("");
  const sizeSel  = sizes.map(([v, l]) => opt(v, l, state.overviewSize)).join("");
  const groupSel = groups.map(([v, l]) => opt(v, l, state.overviewGroupBy)).join("");

  const statsInner = state.overview
    ? renderStatsInline(state.overview.totals, {
        includeArchived: state.filters.archived,
        includeDeleted:  state.filters.deleted,
        planContextWindow: state.planContextWindow,
      })
    : '<span class="stats-dim is-loading">loading...</span>';

  const convoLine = state.overview
    ? `<div class="overview-meta">${state.overview.convo_count} convos${state.overview.archived_count ? ` · ${state.overview.archived_count} archived` : ""} · ${esc(windowLabel())} · each bar = 1 ${esc(state.overview.bucket)}</div>`
    : "";

  const graphInner = state.overview
    ? renderTokenBars(state.overview.by_period, state.overviewRange, state.overview.bucket, state.overviewMode, state.overviewSize, state.overview.until, state.overviewGroupBy, state.filters)
    : '<div class="overview-graph"><div class="stats-dim is-loading">loading...</div></div>';

  const navBtns = state.overviewRange === "all" ? "" : `
    <button class="btn btn-sm" data-action="overview-nav-prev" title="Previous ${esc(state.overviewRange)}">&lsaquo;</button>
    <button class="btn btn-sm" data-action="overview-nav-next" title="Next ${esc(state.overviewRange)}" ${state.overviewOffset >= 0 ? "disabled" : ""}>&rsaquo;</button>
  `;

  const f = state.filters;
  const tog = (k, label, title) => `
    <label class="filter-toggle" title="${title}">
      <input type="checkbox" data-action="toggle-filter" data-filter="${k}" ${f[k] ? "checked" : ""}>
      ${label}
    </span>`;
  const filterToggles = `<div class="filter-toggles">
    ${tog("active",   "active",   "Include live conversations")}
    ${tog("archived", "archived", "Include archived conversations + their stats")}
    ${tog("deleted",  "deleted",  "Include stats from deleted conversations and messages")}
  </div>`;

  return `
    <div class="overview-bar">
      <div class="overview-header">
        <span class="overview-title">Overview</span>
        <span class="overview-period">${esc(periodLabel())}</span>
        <span style="flex:1"></span>
        ${filterToggles}
        ${navBtns}
        <select class="overview-range" data-action="set-overview-range">${rangeSel}</select>
        <select class="overview-range" data-action="set-overview-mode">${modeSel}</select>
        <select class="overview-range" data-action="set-overview-group">${groupSel}</select>
        <select class="overview-range" data-action="set-overview-size">${sizeSel}</select>
      </div>
      <div class="overview-stats">${statsInner}</div>
      ${convoLine}
      ${graphInner}
    </div>`;
}

function renderTable(items, totalSize) {
  const pct = (kb) => totalSize > 0 ? `${(kb / totalSize * 100).toFixed(1)}%` : "";
  // Header "select all" checkbox reflects current selection: checked
  // iff every visible project is in `projectSelected`, else unchecked.
  const allSel = items.length > 0
    && state.projectSelected
    && items.every((p) => state.projectSelected.has(p.folder));
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th class="col-check"><input type="checkbox" style="accent-color:var(--accent)" ${allSel ? "checked" : ""} data-action="toggle-all-projects"></th>`;
  h += `<th data-action="sort-projects" data-col="name">Project${arrow(state, "name")}</th>`;
  h += `<th data-action="sort-projects" data-col="convos" style="text-align:right">Convos${arrow(state, "convos")}</th>`;
  h += `<th data-action="sort-projects" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-projects" data-col="recent" style="text-align:right">Active${arrow(state, "recent")}</th>`;
  h += `<th style="width:40px"></th></tr></thead><tbody>`;

  for (const p of items) {
    const sp = shortPath(p.path);
    const selected = state.projectSelected && state.projectSelected.has(p.folder);
    const ck = selected ? "checked" : "";
    const tagPills = Tags.renderTagPills(projectScope, p.folder);
    h += `
      <tr data-action="open-project" data-folder="${escAttr(p.folder)}" data-path="${escAttr(p.path)}">
        <td class="col-check"><span class="check-hit" data-action="toggle-project-sel" data-folder="${escAttr(p.folder)}"><input type="checkbox" class="item-check" ${ck} tabindex="-1"></span></td>
        <td>
          <div style="font-weight:600;color:var(--heading);font-size:13px">${hlText(sp, state.search)}${tagPills ? " " + tagPills : ""}</div>
          <div style="font-size:11px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:420px">${hlText(p.latest_preview, state.search)}</div>
        </td>
        <td class="col-count">${p.conversation_count}</td>
        <td class="col-size">${fmtSize(p.total_size_kb)} <span class="stats-pct">(${pct(p.total_size_kb)})</span></td>
        <td class="col-time" title="${escAttr(timeAbs(p.last_activity))}">${timeAgo(p.last_activity)}</td>
        <td class="col-actions">
          <button class="btn-danger btn-sm" data-action="delete-project" data-folder="${escAttr(p.folder)}" data-name="${escAttr(sp)}">Del</button>
        </td>
      </tr>`;
  }
  return h + "</tbody></table></div>";
}

function renderCards(items, totalSize) {
  const pct = (kb) => totalSize > 0 ? `${(kb / totalSize * 100).toFixed(1)}%` : "";
  let h = '<div class="card-grid">';
  for (const p of items) {
    const sp = shortPath(p.path);
    const statsInner = p.statsHydrated
      ? renderStatsInline(p.stats, {
          includeArchived: state.filters.archived,
          includeDeleted:  state.filters.deleted,
          planContextWindow: state.planContextWindow,
        })
      : '<span class="stats-dim is-loading">loading...</span>';
    const archBadge = p.archived_count
      ? `<span class="badge${state.filters.archived ? "" : " badge-muted"}">${p.archived_count} archived</span>`
      : "";
    const selected = state.projectSelected && state.projectSelected.has(p.folder);
    const ck = selected ? "checked" : "";
    const tagPills = Tags.renderTagPills(projectScope, p.folder);
    h += `
      <div class="card" data-action="open-project" data-folder="${escAttr(p.folder)}" data-path="${escAttr(p.path)}">
        <div class="card-title">${hlText(sp, state.search)}</div>
        ${tagPills ? `<div class="card-tagpills">${tagPills}</div>` : ""}
        <div style="display:flex;align-items:start;gap:8px">
          <span class="check-hit" data-action="toggle-project-sel" data-folder="${escAttr(p.folder)}"><input type="checkbox" class="item-check" ${ck} tabindex="-1"></span>
          <div class="card-preview" style="flex:1">${hlText(p.latest_preview, state.search)}</div>
        </div>
        <div class="card-stats">${statsInner}</div>
        <div class="card-footer">
          <span class="badge">${p.conversation_count} convos</span>
          ${archBadge}
          <span class="badge">${fmtSize(p.total_size_kb)} <span class="stats-pct">(${pct(p.total_size_kb)})</span></span>
          <span class="time-label" title="${escAttr(timeAbs(p.last_activity))}">${timeAgo(p.last_activity)}</span>
          <span style="flex:1"></span>
          <button class="btn-danger btn-sm" data-action="delete-project" data-folder="${escAttr(p.folder)}" data-name="${escAttr(sp)}">Del</button>
        </div>
      </div>`;
  }
  return h + "</div>";
}

// === Actions invoked via event delegation ===

export function sortBy(col) {
  if (state.sort === col) state.desc = !state.desc;
  else { state.sort = col; state.desc = true; }
  render();
}

export function setMode(mode) {
  setViewMode(mode);
  render();
}

export function openProject(folder, path) {
  state.path = path;  // pass along for display; router will take it from there
  navigate(`/p/${encodeURIComponent(folder)}`);
}

export function deleteProject(folder, name) {
  showConfirmModal({
    title: "Delete project?",
    body: `Permanently deletes <strong>${esc(name)}</strong> and every
      conversation inside it from <code>~/.claude/projects/</code>.
      <strong>Cannot be undone</strong>, and none of these conversations will
      be resumable after.
      <br><br>There's no project-level archive — if any conversations matter,
      open the project and <strong>Archive</strong> them individually first.`,
    onConfirm: async () => {
      await api.deleteProject(folder);
      invalidateProjectsCache();
      show();
    },
  });
}


// Fire-and-forget aggregation: asks the server to sum per-convo stats for
// each requested folder. Patches `.card-stats` cells in place once returned;
// also merges onto the in-memory projectsCache so re-renders (filter/sort)
// retain the numbers without re-fetching.
async function hydrateProjectStats(projects) {
  if (!projects || !projects.length) return;
  const folders = projects.map((p) => p.folder);
  let stats;
  try {
    stats = await api.projectStats(folders);
  } catch { return; }
  stats = stats || {};

  const byFolder = new Map((state.projectsCache || []).map((p) => [p.folder, p]));
  for (const folder of folders) {
    const p = byFolder.get(folder);
    if (!p) continue;
    if (stats[folder]) p.stats = stats[folder];
    p.statsHydrated = true;

    const box = app.querySelector(`.card[data-folder="${CSS.escape(folder)}"] .card-stats`);
    if (box) box.innerHTML = renderStatsInline(p.stats, {
      includeArchived: state.filters.archived,
      includeDeleted:  state.filters.deleted,
      planContextWindow: state.planContextWindow,
    });
  }
}


// Fire-and-forget overview: totals + per-day buckets across every project,
// filtered by state.overviewRange. DOM-patches the overview bar in place so
// changing range doesn't disturb the projects grid or its scroll.
// Exported so conversations.js (scoped per-project overview) can reuse the
// same hydrate-and-patch machinery. Reads state.overviewScope to pick the
// right backend filter.
let overviewReqId = 0;

export async function hydrateOverview() {
  // Bump a request counter so out-of-order responses (fast prev/next clicks)
  // can't overwrite newer state with older window data.
  const myReq = ++overviewReqId;
  // Tag filter only applies when viewing a single project (tag assignments
  // are per-folder).
  const tags = state.overviewScope && state.activeTagFilters && state.activeTagFilters.length
    ? state.activeTagFilters
    : null;
  let data;
  try {
    data = await api.overview(state.overviewRange, state.overviewOffset, state.overviewScope, tags);
  } catch { return; }
  if (myReq !== overviewReqId) return;
  state.overview = data;

  const bar = app.querySelector(".overview-bar");
  if (!bar) return;
  const stats = bar.querySelector(".overview-stats");
  if (stats) stats.innerHTML = renderStatsInline(data.totals, {
    includeArchived: state.filters.archived,
    includeDeleted:  state.filters.deleted,
    planContextWindow: state.planContextWindow,
  });
  const periodEl = bar.querySelector(".overview-period");
  if (periodEl) periodEl.textContent = periodLabel();
  const oldMeta = bar.querySelector(".overview-meta");
  const metaHtml = `<div class="overview-meta">${data.convo_count} convos${data.archived_count ? ` · ${data.archived_count} archived` : ""} · ${esc(windowLabel())} · each bar = 1 ${esc(data.bucket)}</div>`;
  if (oldMeta) oldMeta.outerHTML = metaHtml;
  const header = bar.querySelector(".overview-header");
  if (header) {
    const nextBtn = header.querySelector('[data-action="overview-nav-next"]');
    if (nextBtn) {
      if (state.overviewOffset >= 0) nextBtn.setAttribute("disabled", "");
      else nextBtn.removeAttribute("disabled");
    }
  }
  redrawOverviewGraph();
}

// Format a human label for the current window — e.g. "this week", "1 week ago",
// "Apr 7 – Apr 13". Uses the server-provided since/until ISO strings.
function windowLabel() {
  const ov = state.overview;
  const range = state.overviewRange;
  const offset = state.overviewOffset;
  if (range === "all") return "all time";

  // Relative word ("this week", "2 months ago") plus concrete date range
  // pulled from the server's since/until — gives the user an anchor.
  let relative;
  if (offset === 0) relative = `this ${range}`;
  else {
    const n = Math.abs(offset);
    const unit = range + (n > 1 ? "s" : "");
    relative = offset < 0 ? `${n} ${unit} ago` : `${n} ${unit} from now`;
  }

  if (!ov || !ov.since || !ov.until) return relative;
  const since = new Date(ov.since);
  // Use (until - 1ms) so a window ending at midnight displays the previous
  // day, not the first sliver of the next one.
  const until = new Date(new Date(ov.until).getTime() - 1);

  const fmtMonthDay = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const fmtMonth = (d) => d.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  let concrete;
  if (range === "day") {
    concrete = since.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } else if (range === "year") {
    concrete = String(since.getFullYear());
  } else if (range === "month") {
    concrete = fmtMonth(since);
  } else {
    // week: "Apr 8 – Apr 14" (or cross-month)
    concrete = `${fmtMonthDay(since)} – ${fmtMonthDay(until)}`;
  }
  return `${relative} (${concrete})`;
}


// Compact period label that sits next to "Overview" in the bar header and
// updates on every nav/range change. Examples:
//   all time / 2026 / April 2026 / Week 15, 2026 / Apr 14, 2026
function periodLabel() {
  const ov = state.overview;
  const range = state.overviewRange;
  if (range === "all") return "all-time";
  if (!ov || !ov.since) {
    // fallback while data is loading — keeps label stable instead of blank
    return range;
  }
  const since = new Date(ov.since);
  if (range === "year")  return String(since.getFullYear());
  if (range === "month") return since.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  if (range === "week") {
    const w = isoWeekNumber(since);
    return `Week ${w}, ${since.getFullYear()}`;
  }
  if (range === "day") {
    return since.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  return range;
}

function isoWeekNumber(d) {
  const dt = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const dayNum = dt.getUTCDay() || 7;
  dt.setUTCDate(dt.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(dt.getUTCFullYear(), 0, 1));
  return Math.ceil(((dt - yearStart) / 86400000 + 1) / 7);
}

// Re-draw only the graph (for mode/size toggles). Doesn't re-fetch.
function redrawOverviewGraph() {
  if (!state.overview) return;
  const bar = app.querySelector(".overview-bar");
  if (!bar) return;
  const oldGraph = bar.querySelector(".overview-graph");
  if (oldGraph) oldGraph.outerHTML = renderTokenBars(
    state.overview.by_period, state.overviewRange, state.overview.bucket,
    state.overviewMode, state.overviewSize, state.overview.until,
    state.overviewGroupBy, state.filters,
  );
}

export function setOverviewRange(range) {
  if (range === state.overviewRange) return;
  state.overviewRange = range;
  state.overviewOffset = 0;
  state.overview = null;
  // DOM-patch the overview bar in place so this works from both projects and
  // conversations views (they each own their own app.innerHTML).
  const bar = app.querySelector(".overview-bar");
  if (bar) bar.outerHTML = renderOverviewBar();
  hydrateOverview();
}

// Page back/forward one unit of the current range (prev week, next month, …).
// Disallowed for "all" range and for offsets > 0 (no future data).
export function navOverview(delta) {
  if (state.overviewRange === "all") return;
  const next = state.overviewOffset + delta;
  if (next > 0) return;
  state.overviewOffset = next;
  state.overview = null;
  const bar = app.querySelector(".overview-bar");
  if (bar) {
    bar.querySelector(".overview-stats").innerHTML = '<span class="stats-dim is-loading">loading...</span>';
    const meta = bar.querySelector(".overview-meta");
    if (meta) meta.remove();
    const graph = bar.querySelector(".overview-graph");
    if (graph) graph.outerHTML = '<div class="overview-graph"><div class="stats-dim is-loading">loading...</div></div>';
  }
  hydrateOverview();
}


// Mode (tokens vs tool uses) and size (compact vs expanded) don't change the
// payload — just how we draw it. Skip the re-fetch.
export function setOverviewMode(mode) {
  if (mode === state.overviewMode) return;
  state.overviewMode = mode;
  redrawOverviewGraph();
}

export function setOverviewSize(sz) {
  if (sz === state.overviewSize) return;
  state.overviewSize = sz;
  redrawOverviewGraph();
}

export function setOverviewGroupBy(gb) {
  if (gb === state.overviewGroupBy) return;
  state.overviewGroupBy = gb;
  redrawOverviewGraph();
}


// Open a structured stats modal for the current overview. Uses the already-
// hydrated totals (if available); otherwise nudges the user that it's loading.
export function openOverviewStats() {
  if (!state.overview) {
    showInfoModal({ title: "Stats", body: '<div class="stats-loading">loading... try again in a moment</div>' });
    return;
  }
  const scopeLabel = state.overviewScope ? "Project stats" : "Overview stats";
  showInfoModal({
    title: `${scopeLabel} — ${state.overviewRange}`,
    body: renderStatsModalBody(state.overview.totals, {
      convoCount: state.overview.convo_count,
      archivedCount: state.overview.archived_count,
      filters: { ...state.filters },
    }),
  });
}


