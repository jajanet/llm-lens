// Messages view: chat-style display of a single conversation.

import { state } from "../state.js";
import { api } from "../api.js";
import { esc, escAttr, highlightText, renderStatsModalBody } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal, showInfoModal } from "../modal.js";
import { navigate } from "../router.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");
const PAGE_MSGS = 60;

export async function show(folder, convoId) {
  state.view = "messages";
  state.folder = folder;
  state.convoId = convoId;
  state.msgSelected.clear();
  state.msgSearch = "";
  state.search = "";

  await resolvePath(folder);
  renderBreadcrumb();

  app.innerHTML = '<div class="loading">Loading...</div>';
  state.msgData = await api.messages(folder, convoId, { limit: PAGE_MSGS });
  state.msgOffset = state.msgData.offset;
  state.msgTotal = state.msgData.total;
  render();
}

async function resolvePath(folder) {
  if (!state.projectsCache) state.projectsCache = await api.projects();
  const proj = state.projectsCache.find((p) => p.folder === folder);
  state.path = proj ? proj.path : folder;
}

function renderBreadcrumb() {
  bc.innerHTML = `
    <a data-action="nav-projects">Projects</a> /
    <a data-action="nav-folder" data-folder="${escAttr(state.folder)}">${esc(state.path)}</a> /
    Conversation
  `;
}

function renderToolbar() {
  const side = state.msgData?.sidechain || [];
  let extra = "";
  if (state.msgOffset > 0) {
    extra += `<button class="btn" data-action="load-earlier-msgs">Earlier (${state.msgOffset})</button> `;
  }
  if (side.length > 0) {
    extra += `<button class="btn ${state.showSide ? "active" : ""}" data-action="toggle-side">Side (${side.length})</button>`;
  }
  configureToolbar({
    placeholder: "Search messages...",
    searchValue: state.msgSearch,
    extraHtml: extra,
    onSearch: (v) => { state.msgSearch = v; render(); },
  });
}

export function render() {
  renderToolbar();

  const main = state.msgData?.main || [];
  const side = state.msgData?.sidechain || [];
  const q = state.msgSearch.toLowerCase();
  const filtered = q ? main.filter((m) => (m.content || "").toLowerCase().includes(q)) : main;

  let h = '<div class="convo-flex"><div class="chat-wrap">';
  h += renderChatMessages(filtered, q);
  h += "</div>";

  if (side.length > 0) {
    h += `<div class="convo-side ${state.showSide ? "" : "hidden"}"><h3>Side conversations</h3>`;
    for (const m of side) {
      const role = m.role === "user" ? "user" : "assistant";
      const c = processContent(m.content || "");
      if (c.html) h += `<div class="side-msg ${role}"><div class="msg-content">${c.html}</div></div>`;
    }
    h += "</div>";
  }
  h += "</div>";

  if (state.editMode && state.msgSelected.size > 0) {
    h += `
      <div class="sel-bar">
        <span>${state.msgSelected.size} selected</span>
        <button class="btn" data-action="copy-selected">Copy</button>
        <button class="btn" data-action="save-selected" title="Non-destructive: creates a new conversation, leaves this one intact. Preferred for curation.">Save to new convo</button>
        <button class="btn-danger" style="border-color:rgba(255,255,255,0.4);color:#fff" data-action="delete-selected" title="Rewrites original file in place. May break /resume in edge cases — duplicate the conversation first if you care about it.">Delete</button>
        <span style="flex:1"></span>
        <button class="btn" data-action="clear-selection">Clear</button>
      </div>
    `;
  }

  app.innerHTML = h;
}

function processContent(raw) {
  // Preserve thinking blocks as placeholders
  const thinkingBlocks = [];
  let c = raw.replace(/<thinking>([\s\S]*?)<\/thinking>/g, (_, inner) => {
    if (!inner.trim()) return "";
    const id = "t_" + Math.random().toString(36).slice(2, 8);
    const ph = `__THINK_${thinkingBlocks.length}__`;
    thinkingBlocks.push(
      `<span class="thinking-toggle" data-action="toggle-thinking" data-target="${id}">[thinking...]</span>` +
      `<div class="thinking-block" id="${id}" style="display:none">${esc(inner.trim())}</div>`
    );
    return ph;
  });

  const toolBadges = [];
  const toolNames = [];
  c = c.replace(/\[Tool: ([^\]]+)\]/g, (_, name) => {
    const ph = `__TOOL_${toolBadges.length}__`;
    toolBadges.push(`<span class="tool-badge">${esc(name)}</span>`);
    toolNames.push(name);
    return ph;
  });
  c = c.replace(/\[Tool Result\]/g, () => {
    const ph = `__TOOL_${toolBadges.length}__`;
    toolBadges.push('<span class="tool-badge">Result</span>');
    toolNames.push("Result");
    return ph;
  });

  c = c.replace(/\n{3,}/g, "\n\n").trim();

  const visible = c.replace(/__THINK_\d+__/g, "").replace(/__TOOL_\d+__/g, "").trim();
  const hasText = Boolean(visible);
  const hasTools = toolBadges.length > 0;
  if (!hasText && !thinkingBlocks.length && !hasTools) return { html: "", hasText: false, toolNames: [] };

  c = esc(c);
  thinkingBlocks.forEach((html, i) => { c = c.replace(`__THINK_${i}__`, html); });
  toolBadges.forEach((html, i) => { c = c.replace(`__TOOL_${i}__`, html); });
  c = c.replace(/\n/g, "<br>");
  return { html: c, hasText, toolNames };
}

function renderChatMessages(msgs, query) {
  // First pass: process each message and compute {rendered, hasText, toolNames}.
  const processed = [];
  for (const m of msgs) {
    const c = processContent(m.content || "");
    if (!c.html) continue;
    const finalHtml = query ? highlightText(c.html, query) : c.html;
    processed.push({ m, html: finalHtml, hasText: c.hasText, toolNames: c.toolNames });
  }

  // Second pass: coalesce consecutive tool-only messages into groups. A match
  // in the query forces a group open so search hits stay visible.
  const qLower = (query || "").toLowerCase();
  const groups = [];
  let i = 0;
  while (i < processed.length) {
    const p = processed[i];
    const isToolOnly = !p.hasText && p.toolNames.length > 0;
    if (!isToolOnly) {
      groups.push({ kind: "msg", items: [p] });
      i++;
      continue;
    }
    // Collect run of tool-only messages
    const run = [];
    let forceOpen = false;
    while (i < processed.length) {
      const q = processed[i];
      const qToolOnly = !q.hasText && q.toolNames.length > 0;
      if (!qToolOnly) break;
      run.push(q);
      if (qLower && (q.m.content || "").toLowerCase().includes(qLower)) forceOpen = true;
      i++;
    }
    if (run.length === 1) {
      // Don't bother grouping a single tool bubble — render it directly
      // in the compact-tool style via the normal path below.
      groups.push({ kind: "msg", items: run, compact: true });
    } else {
      groups.push({ kind: "group", items: run, forceOpen });
    }
  }

  let h = "";
  for (const g of groups) {
    if (g.kind === "group") {
      const firstUuid = g.items[0]?.m?.uuid || "";
      const expanded = g.forceOpen || expandedGroups.has(firstUuid);
      h += renderToolGroup(g.items, firstUuid, expanded);
    } else {
      for (const p of g.items) h += renderSingleMsg(p, g.compact);
    }
  }
  return h;
}


// Tracked per module so collapse/expand survives re-renders without plumbing
// new fields through state.js. Keyed on the first message's uuid in a group.
const expandedGroups = new Set();

function renderSingleMsg(p, compact) {
  const { m, html: c } = p;
  const role = m.role === "user" ? "user" : "assistant";
  const ck = state.msgSelected.has(m.uuid) ? "checked" : "";
  const checkHtml = m.uuid
    ? `<input type="checkbox" class="chat-check" ${ck} data-action="toggle-msg-sel" data-uuid="${escAttr(m.uuid)}">`
    : "";
  const actionsHtml = m.uuid
    ? `<span class="msg-actions-row">
        <button class="btn btn-sm" data-action="copy-msg" data-uuid="${escAttr(m.uuid)}" title="Copy">Copy</button>
        <button class="btn-danger btn-sm btn-del-msg" data-action="delete-msg" data-uuid="${escAttr(m.uuid)}" title="Delete this message (rewrites file — may break /resume). Prefer Edit mode → Save to new convo for curation.">x</button>
      </span>`
    : "";
  const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : "";
  const bubbleCls = compact ? "chat-bubble tool-bubble" : "chat-bubble";
  const metaHtml = compact
    ? ""
    : `<div class="chat-meta"><span class="role-lbl">${role}</span><span>${ts}</span></div>`;
  return `<div class="chat-msg ${role}${compact ? " compact" : ""}">${checkHtml}<div class="${bubbleCls}">${actionsHtml}${metaHtml}<div class="msg-content">${c}</div></div></div>`;
}

function renderToolGroup(items, groupId, expanded) {
  const names = items.flatMap((p) => p.toolNames);
  const preview = names.slice(0, 6).map(esc).join(" · ");
  const more = names.length > 6 ? ` …+${names.length - 6}` : "";
  const caret = expanded ? "▾" : "▸";
  const summary = `<div class="tool-group-summary" data-action="toggle-tool-group" data-group-id="${escAttr(groupId)}">
    <span class="tool-caret">${caret}</span>
    <span class="tool-group-label">${items.length} tool calls</span>
    <span class="tool-group-names">— ${preview}${more}</span>
  </div>`;

  if (!expanded) {
    return `<div class="tool-group collapsed">${summary}</div>`;
  }
  const inner = items.map((p) => renderSingleMsg(p, true)).join("");
  return `<div class="tool-group expanded">${summary}${inner}</div>`;
}

// === Actions ===

export async function loadEarlier() {
  const newOffset = Math.max(0, state.msgOffset - PAGE_MSGS);
  const limit = state.msgOffset - newOffset;
  if (limit <= 0) return;
  const earlier = await api.messages(state.folder, state.convoId, { offset: newOffset, limit });
  state.msgData.main = earlier.main.concat(state.msgData.main);
  state.msgOffset = newOffset;
  render();
}

export function toggleSide() {
  state.showSide = !state.showSide;
  render();
}

export function toggleMsgSel(uuid) {
  if (state.msgSelected.has(uuid)) state.msgSelected.delete(uuid);
  else state.msgSelected.add(uuid);
  render();
}

export function clearSelection() {
  state.msgSelected.clear();
  render();
}

export async function copyMsg(uuid) {
  const m = (state.msgData?.main || []).find((m) => m.uuid === uuid);
  if (!m) return;
  const text = (m.content || "")
    .replace(/<[^>]+>/g, "")
    .replace(/\[Tool: [^\]]+\]/g, "")
    .replace(/\[Tool Result\]/g, "")
    .trim();
  await navigator.clipboard.writeText(text);
}

export function deleteMsg(uuid) {
  showConfirmModal({
    title: "Delete message?",
    body: `Rewrites the original conversation file in place. This may break
      <code>/resume</code> for this conversation — Claude Code's replay
      semantics aren't publicly documented.
      <br><br><strong>Prefer Edit mode → "Save to new convo"</strong> to curate
      non-destructively, or duplicate this conversation first if you care
      about preserving it.`,
    onConfirm: async () => {
      await api.deleteMessage(state.folder, state.convoId, uuid);
      show(state.folder, state.convoId);
    },
  });
}

export async function copySelected() {
  const main = state.msgData?.main || [];
  const texts = main
    .filter((m) => state.msgSelected.has(m.uuid))
    .map((m) => {
      const label = m.role === "user" ? "User" : "Assistant";
      const body = (m.content || "")
        .replace(/<[^>]+>/g, "")
        .replace(/\[Tool: [^\]]+\]/g, "")
        .replace(/\[Tool Result\]/g, "")
        .trim();
      return `${label}: ${body}`;
    });
  await navigator.clipboard.writeText(texts.join("\n\n"));
  state.msgSelected.clear();
  render();
}

export async function saveSelected() {
  const uuids = [...state.msgSelected];
  if (!uuids.length) return;
  const data = await api.extractMessages(state.folder, state.convoId, uuids);
  state.msgSelected.clear();
  if (data.new_id) {
    navigate(`/p/${encodeURIComponent(state.folder)}/c/${encodeURIComponent(data.new_id)}`);
  }
}

export function deleteSelected() {
  const uuids = [...state.msgSelected];
  if (!uuids.length) return;
  showConfirmModal({
    title: `Delete ${uuids.length} messages?`,
    body: `Rewrites the original conversation file in place. Cannot be undone,
      and may break <code>/resume</code> for this conversation — Claude Code's
      replay semantics aren't publicly documented.
      <br><br><strong>Prefer "Save to new convo"</strong> (non-destructive,
      creates a copy), or duplicate this conversation first if you care about
      preserving it.`,
    onConfirm: async () => {
      for (const u of uuids) {
        await api.deleteMessage(state.folder, state.convoId, u);
      }
      state.msgSelected.clear();
      show(state.folder, state.convoId);
    },
  });
}


// Stats modal for the currently-open conversation. Fetches the detailed stats
// endpoint (same shape as the batch one used on the list view) and renders
// a structured table rather than the compact inline chip strip.
export async function showStats() {
  if (!state.folder || !state.convoId) return;
  showInfoModal({ title: "Conversation stats", body: '<div class="stats-loading">loading...</div>' });
  let s;
  try {
    s = await api.singleConversationStats(state.folder, state.convoId);
  } catch {
    const box = document.querySelector(".modal .modal-body");
    if (box) box.innerHTML = '<div class="stats-dim">Failed to load stats.</div>';
    return;
  }
  const box = document.querySelector(".modal .modal-body");
  if (box) box.innerHTML = renderStatsModalBody(s);
}


export function toggleToolGroup(groupId) {
  if (expandedGroups.has(groupId)) expandedGroups.delete(groupId);
  else expandedGroups.add(groupId);
  render();
}


