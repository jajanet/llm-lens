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

import { applyTransform } from "../transforms.js";

const WORD_LIST_KINDS = new Set(["remove_swears", "remove_filler"]);

function needsWordLists(kind) {
  return WORD_LIST_KINDS.has(kind);
}

async function ensureWordLists() {
  if (state.wordLists) return state.wordLists;
  try {
    state.wordLists = await api.getWordLists();
  } catch {
    state.wordLists = { swears: [], filler: [] };
  }
  return state.wordLists;
}

export async function show(folder, convoId) {
  state.view = "messages";
  state.folder = folder;
  state.convoId = convoId;
  state.convoName = null;
  state.msgSelected.clear();
  state.msgSearch = "";
  state.search = "";

  await resolvePath(folder);
  renderBreadcrumb();
  hydrateConvoName(folder, convoId);
  hydrateConvoTags();

  app.innerHTML = '<div class="loading">Loading...</div>';
  state.msgData = await api.messages(folder, convoId, { limit: PAGE_MSGS });
  state.msgOffset = state.msgData.offset;
  state.msgTotal = state.msgData.total;
  render();
}

async function hydrateConvoName(folder, convoId) {
  try {
    const names = await api.conversationNames(folder, [convoId]);
    if (state.convoId !== convoId) return;  // navigated away
    state.convoName = (names && names[convoId]) || null;
    renderBreadcrumb();
  } catch { /* leave fallback */ }
}

async function resolvePath(folder) {
  if (!state.projectsCache) state.projectsCache = await api.projects();
  const proj = state.projectsCache.find((p) => p.folder === folder);
  state.path = proj ? proj.path : folder;
}

function copyIconSvg() {
  return '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"></rect><path d="M3 10.5V3a1.5 1.5 0 0 1 1.5-1.5H11"></path></svg>';
}

function renderBreadcrumb() {
  const display = state.convoName || (state.convoId ? state.convoId.slice(0, 8) : "Conversation");
  const copyBtn = state.convoId
    ? `<button class="copy-id-btn copy-id-btn-inline" data-action="copy-resume" data-id="${escAttr(state.convoId)}" title="Copy 'claude --resume ${escAttr(state.convoId)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`
    : "";
  // Tag pills for this conversation. Edit mode adds × to remove and + to add.
  let tagsHtml = "";
  if (state.convoId && state.tagLabels && state.tagLabels.length) {
    const assigned = (state.tagAssignments || {})[state.convoId] || [];
    const pills = assigned.map((i) => {
      const label = state.tagLabels[i];
      if (!label || !label.name) return "";
      if (state.editMode) {
        return `<span class="tag-pill tag-pill-sm tag-color-${label.color} tag-pill-removable" data-action="remove-convo-tag" data-tag="${i}" title="Click to remove">${esc(label.name)} <span class="tag-x">×</span></span>`;
      }
      return `<span class="tag-pill tag-pill-sm tag-color-${label.color}">${esc(label.name)}</span>`;
    }).join("");
    const hasLabels = state.tagLabels.some((l) => l && l.name);
    const addBtn = state.editMode && hasLabels
      ? `<button class="bc-tag-add" data-action="open-tag-picker" title="Add tag">+</button>`
      : "";
    if (pills || addBtn) {
      tagsHtml = `<span class="bc-tags">${pills}${addBtn}</span>`;
    }
  }
  bc.innerHTML = `
    <a data-action="nav-projects">Projects</a> /
    <a data-action="nav-folder" data-folder="${escAttr(state.folder)}">${esc(state.path)}</a> /
    <span class="bc-convo-name">${esc(display)}</span>${copyBtn}${tagsHtml}
  `;
}


// Fetch tag labels + assignments for this project so the breadcrumb can show
// tag pills for the currently-open conversation and allow adding/removing.
export async function hydrateConvoTags() {
  try {
    const data = await api.getTags(state.folder);
    state.tagLabels = data.labels || [];
    state.tagAssignments = data.assignments || {};
  } catch {
    state.tagLabels = state.tagLabels || [];
    state.tagAssignments = state.tagAssignments || {};
  }
  renderBreadcrumb();
}

// Remove a tag from the current conversation (clicked × on a pill in bc).
export async function removeConvoTag(tagIndex) {
  if (!state.convoId) return;
  const current = (state.tagAssignments[state.convoId] || []).filter((i) => i !== tagIndex);
  state.tagAssignments[state.convoId] = current;
  try {
    await api.assignTags(state.folder, state.convoId, current);
  } catch { /* best-effort */ }
  renderBreadcrumb();
}

// Show a small dropdown of the project's named tags next to the "+" button
// in the breadcrumb. Click one to toggle it on the current conversation.
export function openTagPicker(anchorEl) {
  document.querySelectorAll(".tag-picker").forEach((el) => el.remove());
  if (!state.convoId || !state.tagLabels) return;
  const assigned = new Set(state.tagAssignments[state.convoId] || []);
  const available = state.tagLabels.map((l, i) => ({ ...l, i })).filter((l) => l.name);
  if (!available.length) return;
  const pills = available.map((l) =>
    `<span class="tag-pill tag-pill-sm tag-color-${l.color}${assigned.has(l.i) ? " active" : ""}" data-action="pick-convo-tag" data-tag="${l.i}" title="${assigned.has(l.i) ? "Remove" : "Add"} '${escAttr(l.name)}'">${esc(l.name)}</span>`
  ).join("");
  const picker = document.createElement("div");
  picker.className = "tag-picker";
  picker.innerHTML = pills;
  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  picker.style.top = `${rect.bottom + 4 + window.scrollY}px`;
  picker.style.left = `${rect.left + window.scrollX}px`;
  // Close when clicking outside
  setTimeout(() => {
    const close = (e) => {
      if (!picker.contains(e.target)) {
        picker.remove();
        document.removeEventListener("click", close);
      }
    };
    document.addEventListener("click", close);
  }, 0);
}

export async function pickConvoTag(tagIndex) {
  if (!state.convoId) return;
  const current = new Set(state.tagAssignments[state.convoId] || []);
  if (current.has(tagIndex)) current.delete(tagIndex);
  else current.add(tagIndex);
  const next = [...current].sort();
  state.tagAssignments[state.convoId] = next;
  try {
    await api.assignTags(state.folder, state.convoId, next);
  } catch { /* best-effort */ }
  document.querySelectorAll(".tag-picker").forEach((el) => el.remove());
  renderBreadcrumb();
}

function renderToolbar() {
  const side = state.msgData?.sidechain || [];
  let extra = "";
  if (state.msgOffset > 0) {
    extra += `<button class="btn" data-action="load-earlier-msgs">Earlier (${state.msgOffset})</button> `;
  }
  if (side.length > 0) {
    extra += `<button class="btn ${state.showSide ? "active" : ""}" data-action="toggle-side">Side (${side.length})</button> `;
  }
  extra += `<button class="btn ${state.showWhitespace ? "active" : ""}" data-action="toggle-whitespace" title="Show invisible characters: spaces as · and tabs as →">Whitespace</button>`;
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
  if (state.editMode && filtered.length > 0) {
    const selectable = filtered.filter((m) => !!m.uuid);
    const allSelected = selectable.length > 0 &&
      selectable.every((m) => state.msgSelected.has(m.uuid));
    const label = allSelected ? "Deselect all" : `Select all (${selectable.length})`;
    h += `<div class="chat-select-all"><button class="btn btn-sm" data-action="toggle-all-msgs">${label}</button></div>`;
  }
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
        <span class="split-btn">
          <button class="btn" data-action="bulk-transform" data-kind="scrub" title="Scrub text on selected prose-only messages. Non-prose messages are skipped.">Scrub</button>
          <button class="btn split-arrow" data-action="open-bulk-transform-menu" title="More text transforms">▾</button>
        </span>
        <button class="btn-danger" style="border-color:rgba(255,255,255,0.4);color:#fff" data-action="delete-selected" title="Rewrites original file in place. May break /resume in edge cases — duplicate the conversation first if you care about it.">Delete</button>
        <span style="flex:1"></span>
        <button class="btn" data-action="clear-selection">Clear</button>
      </div>
    `;
  }

  app.innerHTML = h;
}

function processContent(raw, commands) {
  const cmdById = {};
  for (const c of commands || []) cmdById[c.id] = c.command;

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
  // Marker shapes emitted by the backend parser:
  //   [Tool: Read]            (legacy / no id)
  //   [Tool: Bash:tool_use_id]
  c = c.replace(/\[Tool: ([^:\]]+)(?::([^\]]+))?\]/g, (_, name, tid) => {
    const ph = `__TOOL_${toolBadges.length}__`;
    let badge;
    if (name === "Bash" && tid && cmdById[tid]) {
      const raw = cmdById[tid];
      const oneLine = raw.replace(/\n+/g, " ↵ ");
      const preview = oneLine.length > 80 ? oneLine.slice(0, 80) + "…" : oneLine;
      badge =
        `<span class="tool-badge tool-badge-bash">${esc(name)}</span>` +
        `<code class="bash-cmd-preview">${maskSecrets(preview)}</code>` +
        `<details class="bash-cmd-full"><summary>show full</summary><pre>${maskSecrets(raw)}</pre></details>`;
    } else {
      badge = `<span class="tool-badge">${esc(name)}</span>`;
    }
    toolBadges.push(badge);
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
  const hasThinking = thinkingBlocks.length > 0;
  if (!hasText && !hasThinking && !hasTools) return { html: "", hasText: false, toolNames: [], hasThinking: false };

  c = esc(c);
  if (state.showWhitespace) {
    c = c.replace(/ /g, '<span class="ws-dot">·</span>')
         .replace(/\t/g, '<span class="ws-tab">→</span>');
  }
  thinkingBlocks.forEach((html, i) => { c = c.replace(`__THINK_${i}__`, html); });
  toolBadges.forEach((html, i) => { c = c.replace(`__TOOL_${i}__`, html); });
  c = c.replace(/\n/g, "<br>");
  return { html: c, hasText, toolNames, hasThinking };
}


// Patterns for known sensitive substrings. Conservative: only match
// strings that are obviously credentials. Anything that matches gets
// replaced with a clickable "reveal" span; the original is preserved in
// data-secret so the user can opt to see it.
const SECRET_PATTERNS = [
  /sk-ant-[A-Za-z0-9_\-]{20,}/g,
  /sk-[A-Za-z0-9]{32,}/g,
  /ghp_[A-Za-z0-9]{30,}/g,
  /gho_[A-Za-z0-9]{30,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /xox[bpoa]-[A-Za-z0-9\-]{10,}/g,
  /AKIA[A-Z0-9]{16}/g,
  /AIza[A-Za-z0-9_\-]{35}/g,
  /Bearer\s+[A-Za-z0-9._\-]{16,}/g,
  // env-style: NAME_KEY=value, NAME_TOKEN=value, etc.
  /\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)\s*=\s*\S+/g,
  // URL with embedded password: scheme://user:pass@host
  /\b[a-z]+:\/\/[^\s:@]+:[^\s@]+@\S+/g,
];

export function maskSecrets(text) {
  if (!text) return "";
  // We need to escape first, then walk the escaped string looking for
  // matches. But patterns include literal characters that survive escape
  // (alphanumerics, hyphens, underscores). So escape first, then run
  // patterns on the escaped string — same matches work.
  let out = esc(text);
  for (const re of SECRET_PATTERNS) {
    out = out.replace(re, (m) =>
      `<span class="secret-mask" data-action="reveal-secret" data-secret="${esc(m).replace(/"/g, "&quot;")}" title="Click to reveal">[sensitive]</span>`
    );
  }
  return out;
}

function renderChatMessages(msgs, query) {
  const processed = [];
  for (const m of msgs) {
    const c = processContent(m.content || "", m.commands);
    if (!c.html) continue;
    const finalHtml = query ? highlightText(c.html, query) : c.html;
    processed.push({ m, html: finalHtml, hasText: c.hasText, toolNames: c.toolNames, hasThinking: c.hasThinking });
  }

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
  const { m, html: c, hasText, toolNames, hasThinking } = p;
  const role = m.role === "user" ? "user" : "assistant";
  const ck = state.msgSelected.has(m.uuid) ? "checked" : "";
  const checkHtml = m.uuid
    ? `<input type="checkbox" class="chat-check" ${ck} data-action="toggle-msg-sel" data-uuid="${escAttr(m.uuid)}">`
    : "";
  // proseOnly matches backend `_is_prose_only`: text blocks only. Tool and
  // thinking blocks carry structural meaning and must not be editable.
  const proseOnly = m.uuid && hasText && !(toolNames && toolNames.length) && !hasThinking;
  const transformBtn = proseOnly
    ? `<span class="split-btn">
        <button class="btn btn-sm" data-action="edit-msg" data-uuid="${escAttr(m.uuid)}" title="Edit this message's text in place. Preserves usage/stats and resume chain.">Edit</button>
        <button class="btn btn-sm split-arrow" data-action="open-transform-menu" data-uuid="${escAttr(m.uuid)}" title="More text transforms">▾</button>
      </span>`
    : "";
  const actionsHtml = m.uuid
    ? `<span class="msg-actions-row">
        <button class="btn btn-sm" data-action="copy-msg" data-uuid="${escAttr(m.uuid)}" title="Copy">Copy</button>
        ${transformBtn}
        <button class="btn-danger btn-sm btn-del-msg" data-action="delete-msg" data-uuid="${escAttr(m.uuid)}" title="Delete this message (rewrites file — may break /resume). Prefer Edit mode → Save to new convo for curation.">x</button>
      </span>`
    : "";
  const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : "";
  const bubbleCls = compact ? "chat-bubble tool-bubble" : "chat-bubble";
  const bubbleUuidAttr = m.uuid ? ` data-uuid="${escAttr(m.uuid)}"` : "";
  const metaHtml = compact
    ? ""
    : `<div class="chat-meta"><span class="role-lbl">${role}</span><span>${ts}</span></div>`;
  return `<div class="chat-msg ${role}${compact ? " compact" : ""}">${checkHtml}<div class="${bubbleCls}"${bubbleUuidAttr}>${actionsHtml}${metaHtml}<div class="msg-content">${c}</div></div></div>`;
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


export function toggleWhitespace() {
  state.showWhitespace = !state.showWhitespace;
  render();
}

export function toggleMsgSel(uuid) {
  if (state.msgSelected.has(uuid)) state.msgSelected.delete(uuid);
  else state.msgSelected.add(uuid);
  render();
}


export function toggleAllMsgs() {
  const main = state.msgData?.main || [];
  const q = state.msgSearch.toLowerCase();
  const filtered = q ? main.filter((m) => (m.content || "").toLowerCase().includes(q)) : main;
  const ids = filtered.filter((m) => !!m.uuid).map((m) => m.uuid);
  const allSelected = ids.length > 0 && ids.every((id) => state.msgSelected.has(id));
  if (allSelected) {
    for (const id of ids) state.msgSelected.delete(id);
  } else {
    for (const id of ids) state.msgSelected.add(id);
  }
  render();
}

export function openBulkTransformMenu(anchorEl) {
  const existing = document.querySelector(".transform-menu");
  if (existing) {
    const wasBulk = existing.dataset.bulk === "1";
    existing.remove();
    if (wasBulk) return;
  }
  const menu = document.createElement("div");
  menu.className = "transform-menu";
  menu.dataset.bulk = "1";
  const items = Object.entries(TRANSFORM_LABELS)
    .map(([kind, label]) =>
      `<button class="btn btn-sm transform-menu-item" data-action="bulk-transform" data-kind="${kind}">${label}</button>`
    ).join("");
  menu.innerHTML = items +
    `<div class="transform-menu-sep"></div>` +
    `<button class="btn btn-sm transform-menu-item" data-action="open-word-lists">Curate word lists…</button>`;
  const rect = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  menu.style.top = `${rect.bottom + window.scrollY + 2}px`;
  menu.style.left = `${rect.left + window.scrollX}px`;
  document.body.appendChild(menu);
  setTimeout(() => {
    const handler = (ev) => {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener("click", handler, true);
      }
    };
    document.addEventListener("click", handler, true);
  }, 0);
}


export function revealSecret(el) {
  if (!el || !el.dataset) return;
  const original = el.dataset.secret || "";
  if (!original) return;
  // Swap the mask span for a plain text node containing the original.
  // Keep behavior local — no re-render, no network call. Once revealed,
  // stays revealed until the view re-renders.
  const replacement = document.createElement("span");
  replacement.className = "secret-revealed";
  replacement.textContent = original;
  replacement.title = "Revealed — scroll away or reload to re-hide";
  el.replaceWith(replacement);
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
      <br><br><strong>Prefer Scrub</strong> if you only want to redact the
      text — it leaves <code>usage</code> and the chain intact, so it's
      strictly less invasive than delete.
      <br>Or use Edit mode → "Save to new convo" to curate non-destructively.`,
    onConfirm: async () => {
      await api.deleteMessage(state.folder, state.convoId, uuid);
      show(state.folder, state.convoId);
    },
  });
}


export const TRANSFORM_LABELS = {
  scrub: "Scrub (replace text with \".\")",
  normalize_whitespace: "Normalize whitespace",
  remove_swears: "Remove swears",
  remove_filler: "Remove filler / drift phrases",
};

const TRANSFORM_CONFIRM = {
  scrub: `Replaces this message's text content with "." in place. Leaves
    <code>usage</code>, <code>uuid</code>, and <code>parentUuid</code>
    alone, so token stats and the resume chain are preserved. Only works
    on prose-only messages. Like other destructive edits, this may still
    affect <code>/resume</code> in undocumented ways.`,
  normalize_whitespace: `Collapses runs of spaces/tabs into a single space
    and 3+ consecutive newlines into a double newline. Leaves
    <code>usage</code> and chain alone. Prose-only messages.`,
  remove_swears: `Strips swear words listed in your word list from the
    message text. Matches are word-bounded — bare "ass" won't blow up
    "assistant". Stems with a trailing <code>*</code> (e.g.
    <code>fuck*</code>) catch a closed list of conjugations
    (fuck/fucks/fucker/fucking/...). Use <em>Curate word lists</em> to
    edit. Leaves <code>usage</code> and chain alone.`,
  remove_filler: `Removes sycophancy / drift phrases (e.g. "You're absolutely
    right!", "Let me think step by step.") from the message text. Phrases
    are matched exactly, case-insensitive. Use <em>Curate word lists</em>
    to edit. Leaves <code>usage</code> and chain alone.`,
};

export async function transformMsg(uuid, kind = "scrub") {
  const body = TRANSFORM_CONFIRM[kind];
  if (!body) {
    alert(`Unknown transform: ${kind}`);
    return;
  }
  showConfirmModal({
    title: `${TRANSFORM_LABELS[kind]}?`,
    body,
    onConfirm: async () => {
      const m = (state.msgData?.main || []).find((x) => x.uuid === uuid);
      if (!m) return;
      const lists = needsWordLists(kind) ? await ensureWordLists() : {};
      const newText = applyTransform(kind, m.content || "", lists);
      try {
        await api.editMessage(state.folder, state.convoId, uuid, newText);
      } catch (e) {
        alert(`Transform failed: ${e.message}`);
        return;
      }
      show(state.folder, state.convoId);
    },
  });
}


function cssUuid(uuid) {
  if (window.CSS && typeof CSS.escape === "function") return CSS.escape(uuid);
  return String(uuid).replace(/["\\]/g, "\\$&");
}

function autosizeEditTa(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.max(80, ta.scrollHeight + 4) + "px";
}

export function editMsg(uuid) {
  const menu = document.querySelector(".transform-menu");
  if (menu) menu.remove();
  const m = (state.msgData?.main || []).find((x) => x.uuid === uuid);
  if (!m) return;
  const bubble = document.querySelector(`.chat-bubble[data-uuid="${cssUuid(uuid)}"]`);
  if (!bubble || bubble.classList.contains("editing")) return;
  const content = bubble.querySelector(".msg-content");
  const actions = bubble.querySelector(".msg-actions-row");
  if (!content) return;

  bubble.classList.add("editing");
  bubble._origContent = content.innerHTML;
  if (actions) bubble._origActions = actions.innerHTML;

  content.innerHTML =
    `<textarea class="msg-edit-ta" spellcheck="false"></textarea>` +
    `<div class="msg-edit-hint">Rewrites the file in place. Preserves usage/stats and resume chain — same caveats as scrub.</div>`;
  const ta = content.querySelector("textarea");
  ta.value = m.content || "";
  if (actions) {
    actions.innerHTML =
      `<button class="btn btn-sm" data-action="save-edit-msg" data-uuid="${escAttr(uuid)}">Save</button>` +
      `<button class="btn btn-sm" data-action="cancel-edit-msg" data-uuid="${escAttr(uuid)}">Cancel</button>`;
  }
  ta.focus();
  autosizeEditTa(ta);
  ta.addEventListener("input", () => autosizeEditTa(ta));
}

export async function saveEditMsg(uuid) {
  const bubble = document.querySelector(`.chat-bubble[data-uuid="${cssUuid(uuid)}"]`);
  if (!bubble) return;
  const ta = bubble.querySelector(".msg-edit-ta");
  if (!ta) return;
  const text = ta.value;
  try {
    await api.editMessage(state.folder, state.convoId, uuid, text);
  } catch (e) {
    alert(`Edit failed: ${e.message}`);
    return;
  }
  show(state.folder, state.convoId);
}

export function cancelEditMsg(uuid) {
  const bubble = document.querySelector(`.chat-bubble[data-uuid="${cssUuid(uuid)}"]`);
  if (!bubble) return;
  const content = bubble.querySelector(".msg-content");
  const actions = bubble.querySelector(".msg-actions-row");
  if (content && bubble._origContent != null) content.innerHTML = bubble._origContent;
  if (actions && bubble._origActions != null) actions.innerHTML = bubble._origActions;
  bubble.classList.remove("editing");
  delete bubble._origContent;
  delete bubble._origActions;
}

export function openTransformMenu(uuid, anchorEl) {
  const existing = document.querySelector(".transform-menu");
  if (existing) {
    const forSame = existing.dataset.uuid === uuid;
    existing.remove();
    if (forSame) return;
  }
  const menu = document.createElement("div");
  menu.className = "transform-menu";
  menu.dataset.uuid = uuid;
  const editItem =
    `<button class="btn btn-sm transform-menu-item" data-action="edit-msg" data-uuid="${uuid}">Edit (rewrite text)</button>`;
  const items = Object.entries(TRANSFORM_LABELS)
    .map(([kind, label]) =>
      `<button class="btn btn-sm transform-menu-item" data-action="transform-msg" data-uuid="${uuid}" data-kind="${kind}">${label}</button>`
    ).join("");
  menu.innerHTML = editItem + items +
    `<div class="transform-menu-sep"></div>` +
    `<button class="btn btn-sm transform-menu-item" data-action="open-word-lists">Curate word lists…</button>`;
  const rect = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  menu.style.top = `${rect.bottom + window.scrollY + 2}px`;
  menu.style.left = `${rect.left + window.scrollX}px`;
  document.body.appendChild(menu);
  setTimeout(() => {
    const handler = (ev) => {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener("click", handler, true);
      }
    };
    document.addEventListener("click", handler, true);
  }, 0);
}


export async function openWordListsModal() {
  let current, defaults;
  try {
    [current, defaults] = await Promise.all([
      api.getWordLists(),
      api.getWordListDefaults(),
    ]);
  } catch (e) {
    alert(`Couldn't load word lists: ${e.message}`);
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <h3>Curate word lists</h3>
      <p style="font-size:13px; color:var(--text2)">
        Edit the lists used by <em>Remove swears</em> and <em>Remove filler</em>.
        One entry per line. For swears, append <code>*</code> to a stem to
        catch conjugations safely (e.g. <code>fuck*</code> matches
        fuck/fucks/fucker/fucking). Bare words match exactly with word
        boundaries — <code>ass</code> won't blow up <code>assistant</code>.
        Phrases are matched as exact substrings, case-insensitive.
      </p>
      <div class="word-lists-grid">
        <div>
          <label><strong>Swear words</strong> <span style="color:var(--text3); font-weight:normal">(${current.swears.length} entries)</span></label>
          <textarea id="wl-swears" rows="14" spellcheck="false">${current.swears.join("\n")}</textarea>
          <button class="btn btn-sm" data-reset="swears">Reset to defaults (${defaults.swears.length})</button>
        </div>
        <div>
          <label><strong>Filler / drift phrases</strong> <span style="color:var(--text3); font-weight:normal">(${current.filler.length} entries)</span></label>
          <textarea id="wl-filler" rows="14" spellcheck="false">${current.filler.join("\n")}</textarea>
          <button class="btn btn-sm" data-reset="filler">Reset to defaults (${defaults.filler.length})</button>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn-cancel" data-modal-cancel>Cancel</button>
        <button class="btn-confirm-delete" data-modal-save>Save</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const swearsTa = overlay.querySelector("#wl-swears");
  const fillerTa = overlay.querySelector("#wl-filler");
  const close = () => overlay.remove();

  overlay.addEventListener("click", async (e) => {
    if (e.target === overlay || e.target.matches("[data-modal-cancel]")) {
      close();
    } else if (e.target.matches("[data-reset]")) {
      const which = e.target.dataset.reset;
      if (which === "swears") swearsTa.value = defaults.swears.join("\n");
      else if (which === "filler") fillerTa.value = defaults.filler.join("\n");
    } else if (e.target.matches("[data-modal-save]")) {
      const payload = {
        swears: swearsTa.value.split("\n").map((s) => s.trim()).filter(Boolean),
        filler: fillerTa.value.split("\n").map((s) => s.trim()).filter(Boolean),
      };
      try {
        await api.saveWordLists(payload);
      } catch (err) {
        alert(`Save failed: ${err.message}`);
        return;
      }
      // Invalidate cached lists so the next transform fetches the fresh copy.
      state.wordLists = null;
      close();
    }
  });
}

export async function bulkTransform(kind = "scrub") {
  const ids = Array.from(state.msgSelected);
  if (!ids.length) return;
  const label = TRANSFORM_LABELS[kind] || kind;
  if (!confirm(`${label} for ${ids.length} selected message(s)? Non-prose messages will be skipped.`)) return;
  const lists = needsWordLists(kind) ? await ensureWordLists() : {};
  const byId = new Map((state.msgData?.main || []).map((m) => [m.uuid, m]));
  let ok = 0, skipped = 0, errored = 0;
  for (const id of ids) {
    const m = byId.get(id);
    if (!m) { errored++; continue; }
    const newText = applyTransform(kind, m.content || "", lists);
    try {
      await api.editMessage(state.folder, state.convoId, id, newText);
      ok++;
    } catch (e) {
      if ((e.message || "").startsWith("400")) skipped++;
      else errored++;
    }
  }
  state.msgSelected.clear();
  if (errored) alert(`Applied to ${ok}. Skipped ${skipped} (non-prose). ${errored} failed — check server logs.`);
  else if (skipped) alert(`Applied to ${ok}. Skipped ${skipped} (non-prose).`);
  show(state.folder, state.convoId);
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
      <br><br><strong>Prefer Scrub</strong> on these messages if you only
      want to redact text — it preserves <code>usage</code> and the chain.
      <br>Or use "Save to new convo" (non-destructive, creates a copy).`,
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
  if (box) box.innerHTML = renderStatsModalBody(s, { filters: { ...state.filters } });
}


export function toggleToolGroup(groupId) {
  if (expandedGroups.has(groupId)) expandedGroups.delete(groupId);
  else expandedGroups.add(groupId);
  render();
}


