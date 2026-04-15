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

    const box = app.querySelector(`.card[data-id="${CSS.escape(id)}"] .card-stats`);
    if (box) box.innerHTML = renderStatsInline(c.stats, {
      includeArchived: state.filters.archived,
      includeDeleted: state.filters.deleted,
    });
  }
}


// renderFilterToggles removed — toggles live only in the overview bar.
function _unused_renderFilterToggles() { return ""; }

function renderToolbar() {
  const selN = state.selected.size;
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
  // Filter toggles (active/archived/deleted) live only in the overview bar
  // — one source of truth, like the range selector.
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
    const empty = state.mode === "archived"
      ? "No archived conversations"
      : "No conversations";
    app.innerHTML = overviewHtml + `<div class="empty-state">${empty}</div>`;
    return;
  }

  const body = state.viewMode === "list" ? renderTable(items) : renderCards(items);
  // Pagination only applies in Active mode (archived set is fully fetched).
  const more = state.mode !== "archived" && state.convoItems.length < state.convoTotal
    ? `<div class="load-more-bar"><button class="btn" data-action="load-more-convos">Load more (${state.convoItems.length} / ${state.convoTotal})</button></div>`
    : "";
  app.innerHTML = overviewHtml + body + more;
}

function copyIconSvg() {
  return '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"></rect><path d="M3 10.5V3a1.5 1.5 0 0 1 1.5-1.5H11"></path></svg>';
}

function renderTable(items) {
  let h = '<div class="tbl-wrap"><table class="tbl"><thead><tr>';
  h += `<th class="col-check"><input type="checkbox" style="accent-color:var(--accent)" data-action="toggle-all-convos"></th>`;
  h += `<th class="col-name">Name</th>`;
  h += `<th data-action="sort-convos" data-col="recent">Preview${arrow(state, "recent")}</th>`;
  h += `<th data-action="sort-convos" data-col="size" style="text-align:right">Size${arrow(state, "size")}</th>`;
  h += `<th data-action="sort-convos" data-col="recent" style="text-align:right">Modified${arrow(state, "recent")}</th>`;
  h += `<th class="col-actions" style="width:120px"></th></tr></thead><tbody>`;

  for (const c of items) {
    const ck = state.selected.has(c.id) ? "checked" : "";
    const loading = !c.nameHydrated;
    const nameText = loading ? "loading..." : (c.name || c.id.slice(0, 8));
    const dimCls = (loading || !c.name) ? " col-name-dim" : "";
    const loadCls = loading ? " is-loading" : "";
    const rowCls = c.archived ? " row-archived" : "";
    const archBtn = c.archived
      ? `<button class="btn btn-sm" data-action="unarchive-convo" data-id="${escAttr(c.id)}" title="Restore to ~/.claude/projects/">Unarch</button>`
      : `<button class="btn btn-sm" data-action="archive-convo" data-id="${escAttr(c.id)}" title="Removes from Claude Code's /resume list. Restore anytime from the Archived filter.">Arch</button>`;
    const archBadge = c.archived ? `<span class="badge badge-archived">archived</span> ` : "";
    const copyBtn = `<button class="copy-id-btn" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`;
    h += `
      <tr class="${rowCls}" data-action="open-convo" data-id="${escAttr(c.id)}">
        <td class="col-check">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}">
        </td>
        <td class="col-name${dimCls}${loadCls}"><div class="col-name-wrap"><span class="col-name-text">${archBadge}${esc(nameText)}</span>${copyBtn}</div></td>
        <td class="col-preview">${esc(c.preview)}</td>
        <td class="col-size">${fmtSize(c.size_kb)}</td>
        <td class="col-time" title="${escAttr(timeAbs(c.last_modified))}">${timeAgo(c.last_modified)}</td>
        <td class="col-actions">
          ${archBtn}
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
    const archCls = c.archived ? " card-archived" : "";
    const statsInner = c.statsHydrated
      ? renderStatsInline(c.stats, { includeArchived: state.filters.archived, includeDeleted: state.filters.deleted })
      : '<span class="stats-dim is-loading">loading...</span>';
    const archBtn = c.archived
      ? `<button class="btn btn-sm" data-action="unarchive-convo" data-id="${escAttr(c.id)}" title="Restore to ~/.claude/projects/">Unarch</button>`
      : `<button class="btn btn-sm" data-action="archive-convo" data-id="${escAttr(c.id)}" title="Removes from Claude Code's /resume list. Restore anytime from the Archived filter.">Arch</button>`;
    const archBadge = c.archived ? `<span class="badge badge-archived">archived</span>` : "";
    const copyBtn = `<button class="copy-id-btn" data-action="copy-resume" data-id="${escAttr(c.id)}" title="Copy 'claude --resume ${escAttr(c.id)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`;
    h += `
      <div class="card${archCls}" data-action="open-convo" data-id="${escAttr(c.id)}">
        <div class="card-name${dimCls}${loadCls}"><span class="card-name-text">${esc(nameText)}</span>${copyBtn}</div>
        <div style="display:flex;align-items:start;gap:8px">
          <input type="checkbox" class="item-check" ${ck} data-action="toggle-convo-sel" data-id="${escAttr(c.id)}" style="margin-top:2px">
          <div class="card-preview" style="flex:1;-webkit-line-clamp:4">${esc(c.preview)}</div>
        </div>
        <div class="card-stats">${statsInner}</div>
        <div class="card-footer">
          ${archBadge}
          <span class="badge">${fmtSize(c.size_kb)}</span>
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
  if (box) box.innerHTML = renderStatsModalBody(stats, { filters: { ...state.filters } });
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
         <br><br>Click <em>Arch</em> to keep the content reversibly, or <em>Dup</em> for a backup.`,
    onConfirm: async () => {
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
