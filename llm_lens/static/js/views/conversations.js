// Conversations view: list of all .jsonl files in a project.

import { state, invalidateProjectsCache, setViewMode } from "../state.js";
import { api } from "../api.js";
import { timeAgo, fmtSize, esc, escAttr, arrow } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal } from "../modal.js";
import { navigate } from "../router.js";

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

  // Derive displayed path from cached projects (fetch if needed)
  await resolvePath(folder);
  renderBreadcrumb();

  app.innerHTML = '<div class="loading">Loading...</div>';
  await fetchPage(false);
  render();
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
    items = items.filter((c) => c.preview.toLowerCase().includes(q));
  }

  if (!items.length) {
    app.innerHTML = '<div class="empty-state">No conversations</div>';
    return;
  }

  const body = state.viewMode === "list" ? renderTable(items) : renderCards(items);
  const more = state.convoItems.length < state.convoTotal
    ? `<div class="load-more-bar"><button class="btn" data-action="load-more-convos">Load more (${state.convoItems.length} / ${state.convoTotal})</button></div>`
    : "";
  app.innerHTML = body + more;
}

function renderTable(items) {
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th class="col-check"><input type="checkbox" style="accent-color:var(--accent)" data-action="toggle-all-convos"></th>`;
  h += `<th data-action="sort-convos" data-col="recent">Preview${arrow(state, "recent")}</th>`;
  h += `<th data-action="sort-convos" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-convos" data-col="recent" style="text-align:right">Modified${arrow(state, "recent")}</th>`;
  h += `<th style="width:80px"></th></tr></thead><tbody>`;

  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    h += `
      <tr data-action="open-convo" data-id="${escAttr(c.id)}">
        <td class="col-check">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}">
        </td>
        <td class="col-preview">${esc(c.preview)}</td>
        <td class="col-size">${fmtSize(c.size_kb)}</td>
        <td class="col-time">${timeAgo(c.last_modified)}</td>
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
    h += `
      <div class="card" data-action="open-convo" data-id="${escAttr(c.id)}">
        <div style="display:flex;align-items:start;gap:8px">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}" style="margin-top:2px">
          <div class="card-preview" style="flex:1;-webkit-line-clamp:4">${esc(c.preview)}</div>
        </div>
        <div class="card-footer">
          <span class="badge">${fmtSize(c.size_kb)}</span>
          <span class="time-label">${timeAgo(c.last_modified)}</span>
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
