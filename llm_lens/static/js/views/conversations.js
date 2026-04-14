// Conversations view: list of all .jsonl files in a project.

import { state, invalidateProjectsCache, setViewMode } from "../state.js";
import { api } from "../api.js";
import { timeAgo, timeAbs, fmtSize, esc, escAttr, arrow, toast, renderStatsInline, renderStatsModalBody } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal, showInfoModal } from "../modal.js";
import { navigate } from "../router.js";
import { renderOverviewBar, hydrateOverview } from "./projects.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");
const PAGE = 30;

export async function show(folder) {
  state.view = "conversations";
  state.folder = folder;

  // Reset paging + filters on fresh navigation
  state.convoOffset = 0;
  state.convoItems = [];
  state.selected.clear();
  state.search = "";
  state.sort = "recent";
  state.desc = true;

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
}

async function resolvePath(folder) {
  if (!state.projectsCache) state.projectsCache = await api.projects();
  const proj = state.projectsCache.find((p) => p.folder === folder);
  state.path = proj ? proj.path : folder;
}

function renderBreadcrumb() {
  bc.innerHTML = `<a data-action="nav-projects">Projects</a> / ${esc(state.path || "")}`;
}

async function fetchPage(append) {
  const data = await api.conversations(state.folder, {
    offset: state.convoOffset,
    limit: PAGE,
    sort: state.sort,
    desc: state.desc,
  });
  state.convoTotal = data.total;
  state.convoItems = append ? state.convoItems.concat(data.items) : data.items;
  hydrateNames(data.items);
  hydrateStats(data.items);
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
    for (const sel of [
      `tr[data-id="${CSS.escape(id)}"] .col-name-clickable`,
      `.card[data-id="${CSS.escape(id)}"] .card-name`,
    ]) {
      const el = app.querySelector(sel);
      if (!el) continue;
      el.textContent = display;
      el.classList.remove("col-name-dim", "card-name-dim", "is-loading");
      if (!hasName) el.classList.add(sel.includes("card") ? "card-name-dim" : "col-name-dim");
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

    const box = app.querySelector(`.card[data-id="${CSS.escape(id)}"] .card-stats`);
    if (box) box.innerHTML = renderStatsInline(c.stats);
  }
}


function renderToolbar() {
  const selN = state.selected.size;
  let extra = "";
  if (selN > 0) extra += `<button class="btn-danger" data-action="bulk-delete-convos">Delete ${selN}</button> `;
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

  let items = state.convoItems;
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter((c) =>
      c.preview.toLowerCase().includes(q) ||
      (c.name && c.name.toLowerCase().includes(q))
    );
  }

  const overviewHtml = renderOverviewBar();

  if (!items.length) {
    app.innerHTML = overviewHtml + '<div class="empty-state">No conversations</div>';
    return;
  }

  const body = state.viewMode === "list" ? renderTable(items) : renderCards(items);
  const more = state.convoItems.length < state.convoTotal
    ? `<div class="load-more-bar"><button class="btn" data-action="load-more-convos">Load more (${state.convoItems.length} / ${state.convoTotal})</button></div>`
    : "";
  app.innerHTML = overviewHtml + body + more;
}

function renderTable(items) {
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th class="col-check"><input type="checkbox" style="accent-color:var(--accent)" data-action="toggle-all-convos"></th>`;
  h += `<th class="col-name">Name</th>`;
  h += `<th data-action="sort-convos" data-col="recent">Preview${arrow(state, "recent")}</th>`;
  h += `<th data-action="sort-convos" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-convos" data-col="recent" style="text-align:right">Modified${arrow(state, "recent")}</th>`;
  h += `<th class="col-actions" style="width:80px"></th></tr></thead><tbody>`;

  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    const loading = !c.nameHydrated;
    const nameText = loading ? "loading..." : (c.name || c.id.slice(0, 8));
    const dimCls = (loading || !c.name) ? " col-name-dim" : "";
    const loadCls = loading ? " is-loading" : "";
    h += `
      <tr data-action="open-convo" data-id="${escAttr(c.id)}">
        <td class="col-check">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}">
        </td>
        <td class="col-name col-name-clickable${dimCls}${loadCls}" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'">${esc(nameText)}</td>
        <td class="col-preview">${esc(c.preview)}</td>
        <td class="col-size">${fmtSize(c.size_kb)}</td>
        <td class="col-time" title="${escAttr(timeAbs(c.last_modified))}">${timeAgo(c.last_modified)}</td>
        <td class="col-actions">
          <button class="btn btn-sm" data-action="duplicate-convo" data-id="${escAttr(c.id)}">Dup</button>
          <button class="btn-danger btn-sm" data-action="delete-convo" data-id="${escAttr(c.id)}">Del</button>
        </td>
      </tr>`;
  }
  return h + "</tbody></table></div>";
}

function renderCards(items) {
  let h = '<div class="card-grid">';
  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    const loading = !c.nameHydrated;
    const nameText = loading ? "loading..." : (c.name || c.id.slice(0, 8));
    const dimCls = (loading || !c.name) ? " card-name-dim" : "";
    const loadCls = loading ? " is-loading" : "";
    const statsInner = c.statsHydrated
      ? renderStatsInline(c.stats)
      : '<span class="stats-dim is-loading">loading...</span>';
    h += `
      <div class="card" data-action="open-convo" data-id="${escAttr(c.id)}">
        <div class="card-name col-name-clickable${dimCls}${loadCls}" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'">${esc(nameText)}</div>
        <div style="display:flex;align-items:start;gap:8px">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}" style="margin-top:2px">
          <div class="card-preview" style="flex:1;-webkit-line-clamp:4">${esc(c.preview)}</div>
        </div>
        <div class="card-stats">${statsInner}</div>
        <div class="card-footer">
          <span class="badge">${fmtSize(c.size_kb)}</span>
          <span class="time-label" title="${escAttr(timeAbs(c.last_modified))}">${timeAgo(c.last_modified)}</span>
          <span style="flex:1"></span>
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

async function refreshAndRender() {
  app.innerHTML = '<div class="loading">Loading...</div>';
  await fetchPage(false);
  render();
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
  let data;
  try {
    data = await api.projectStats([state.folder]);
  } catch {
    const box = document.querySelector(".modal .modal-body");
    if (box) box.innerHTML = '<div class="stats-dim">Failed to load stats.</div>';
    return;
  }
  const stats = data[state.folder];
  const box = document.querySelector(".modal .modal-body");
  if (box) box.innerHTML = renderStatsModalBody(stats);
}


// Project-level stats modal: sum-aggregates all convos in the current folder
// via the same endpoint used to hydrate the landing cards.
export async function showStats() {
  if (!state.folder) return;
  showInfoModal({ title: `Project stats — ${esc(state.path || state.folder)}`, body: '<div class="stats-loading">loading...</div>' });
  let all;
  try {
    all = await api.projectStats([state.folder]);
  } catch {
    const box = document.querySelector(".modal .modal-body");
    if (box) box.innerHTML = '<div class="stats-dim">Failed to load stats.</div>';
    return;
  }
  const s = (all || {})[state.folder];
  const box = document.querySelector(".modal .modal-body");
  if (box) box.innerHTML = renderStatsModalBody(s);
}

export async function copyResume(id) {
  const cmd = `claude --resume ${id}`;
  try {
    await navigator.clipboard.writeText(cmd);
    toast(`Copied: ${cmd}`);
  } catch {
    toast("Copy failed");
  }
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
  render();
}

export function toggleAll(checked) {
  for (const c of state.convoItems) {
    if (checked) state.selected.add(c.id);
    else state.selected.delete(c.id);
  }
  render();
}

export function openConvo(id) {
  navigate(`/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(id)}`);
}

export function deleteConvo(id) {
  showConfirmModal({
    title: "Delete conversation?",
    body: `Permanently deletes the <code>.jsonl</code> file from
      <code>~/.claude/projects/</code>. <strong>Cannot be undone</strong>, and
      you won't be able to <code>/resume</code> it after.
      <br><br>Click <em>Dup</em> first if you want a backup.`,
    onConfirm: async () => {
      await api.deleteConversation(state.folder, id);
      invalidateProjectsCache();
      await refreshAndRender();
    },
  });
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
      you won't be able to <code>/resume</code> any of them after.`,
    onConfirm: async () => {
      await api.bulkDeleteConversations(state.folder, ids);
      state.selected.clear();
      invalidateProjectsCache();
      await refreshAndRender();
    },
  });
}
