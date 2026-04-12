// Projects view: list of all ~/.claude/projects folders.

import { state, invalidateProjectsCache, setViewMode } from "../state.js";
import { api } from "../api.js";
import { timeAgo, fmtSize, esc, escAttr, arrow, shortPath } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal } from "../modal.js";
import { navigate } from "../router.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");

export async function show() {
  state.view = "projects";
  state.folder = null;
  state.path = null;
  state.selected.clear();
  state.sort = "recent";
  state.desc = true;
  state.search = "";

  bc.innerHTML = "";

  if (!state.projectsCache) {
    app.innerHTML = '<div class="loading">Loading...</div>';
    state.projectsCache = await api.projects();
  }
  render();
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
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter((p) =>
      p.path.toLowerCase().includes(q) ||
      p.latest_preview.toLowerCase().includes(q)
    );
  }
  items.sort(sortComparator());

  if (!items.length) {
    app.innerHTML = '<div class="empty-state">No projects found</div>';
    return;
  }

  app.innerHTML = state.viewMode === "list" ? renderTable(items) : renderCards(items);
}

function renderTable(items) {
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th data-action="sort-projects" data-col="name">Project${arrow(state, "name")}</th>`;
  h += `<th data-action="sort-projects" data-col="convos" style="text-align:right">Convos${arrow(state, "convos")}</th>`;
  h += `<th data-action="sort-projects" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-projects" data-col="recent" style="text-align:right">Active${arrow(state, "recent")}</th>`;
  h += `<th style="width:40px"></th></tr></thead><tbody>`;

  for (const p of items) {
    const sp = shortPath(p.path);
    h += `
      <tr data-action="open-project" data-folder="${escAttr(p.folder)}" data-path="${escAttr(p.path)}">
        <td>
          <div style="font-weight:600;color:var(--heading);font-size:13px">${esc(sp)}</div>
          <div style="font-size:11px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:420px">${esc(p.latest_preview)}</div>
        </td>
        <td class="col-count">${p.conversation_count}</td>
        <td class="col-size">${fmtSize(p.total_size_kb)}</td>
        <td class="col-time">${timeAgo(p.last_activity)}</td>
        <td class="col-actions">
          <button class="btn-danger btn-sm" data-action="delete-project" data-folder="${escAttr(p.folder)}" data-name="${escAttr(sp)}">Del</button>
        </td>
      </tr>`;
  }
  return h + "</tbody></table></div>";
}

function renderCards(items) {
  let h = '<div class="card-grid">';
  for (const p of items) {
    const sp = shortPath(p.path);
    h += `
      <div class="card" data-action="open-project" data-folder="${escAttr(p.folder)}" data-path="${escAttr(p.path)}">
        <div class="card-title">${esc(sp)}</div>
        <div class="card-preview">${esc(p.latest_preview)}</div>
        <div class="card-footer">
          <span class="badge">${p.conversation_count} convos</span>
          <span class="badge">${fmtSize(p.total_size_kb)}</span>
          <span class="time-label">${timeAgo(p.last_activity)}</span>
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
    body: `Permanently delete ${esc(name)}?`,
    onConfirm: async () => {
      await api.deleteProject(folder);
      invalidateProjectsCache();
      show();
    },
  });
}
