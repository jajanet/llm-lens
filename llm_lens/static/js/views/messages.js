// Messages view: chat-style display of a single conversation.

import { state } from "../state.js";
import { api } from "../api.js";
import { esc, escAttr, highlightText, renderStatsModalBody, fmtTokens, contextWindowFor } from "../utils.js";
import { configureToolbar } from "../toolbar.js";
import { showConfirmModal, showInfoModal } from "../modal.js";
import { navigate } from "../router.js";

const app = document.getElementById("app");
const bc = document.getElementById("breadcrumb");
const PAGE_MSGS = 60;

import { applyTransform } from "../transforms.js";
import { showPreviewModal } from "../preview.js";
import {
  EXPORT_FIELDS,
  REQUIRED_EXPORT_FIELDS,
  ensureDownloadFields,
  serializeAsText,
  serializeAsJsonl,
  downloadBlob,
} from "../exports.js";

import { mountPillList, mountPillPairList } from "../pill_list.js";

const WORD_LIST_KINDS = new Set(["remove_swears", "remove_filler", "remove_verbosity", "remove_priming", "remove_custom_filter"]);

function needsWordLists(kind) {
  return WORD_LIST_KINDS.has(kind);
}

async function ensureWordLists() {
  if (state.wordLists) return state.wordLists;
  try {
    state.wordLists = await api.getWordLists();
  } catch {
    state.wordLists = { swears: [], filler: [], verbosity: [], custom_filter: [], whitelist: [], lowercase_user_text: false, abbreviations: [], apply_abbreviations: false, custom_filter_enabled: false, collapse_punct_repeats: false };
  }
  return state.wordLists;
}

export async function show(folder, convoId) {
  state.view = "messages";
  state.folder = folder;
  state.convoId = convoId;
  state.agentRunId = null;
  state.convoName = null;
  state.msgSelected.clear();
  state.msgSearch = "";
  state.search = "";

  await resolvePath(folder);
  renderBreadcrumb();
  hydrateConvoName(folder, convoId);
  hydrateConvoTags();

  // Warm the word lists cache so synchronous menu builders can read
  // flags like `custom_filter_enabled` without a round-trip.
  ensureWordLists();

  app.innerHTML = '<div class="loading">Loading...</div>';
  state.msgData = await api.messages(folder, convoId, { limit: PAGE_MSGS });
  state.msgOffset = state.msgData.offset;
  state.msgTotal = state.msgData.total;
  render();
}


export async function showAgent(folder, convoId, toolUseId) {
  // Agent-run view: scoped to messages of one subagent run within a parent
  // conversation. Loads from /agent/<tool_use_id> which returns the same
  // shape as api_conversation, so the existing render path just works.
  state.view = "messages";
  state.folder = folder;
  state.convoId = convoId;
  state.agentRunId = toolUseId;
  state.msgSelected.clear();
  state.msgSearch = "";
  state.search = "";

  await resolvePath(folder);
  renderBreadcrumb();
  hydrateConvoName(folder, convoId);

  app.innerHTML = '<div class="loading">Loading agent run…</div>';
  try {
    state.msgData = await api.agentRun(folder, convoId, toolUseId);
  } catch (e) {
    app.innerHTML = '<div class="error">Agent run not found.</div>';
    return;
  }
  state.msgOffset = 0;
  state.msgTotal = state.msgData.total;
  renderBreadcrumb();
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

export function renderBreadcrumb() {
  const display = state.convoName || (state.convoId ? state.convoId.slice(0, 8) : "Conversation");
  const copyBtn = state.convoId
    ? `<button class="copy-id-btn copy-id-btn-inline" data-action="copy-resume" data-id="${escAttr(state.convoId)}" title="Copy 'claude --resume ${escAttr(state.convoId)}'" aria-label="Copy resume command">${copyIconSvg()}</button>`
    : "";

  // Agent + context badges: same shape as the card/table views in the
  // project page, sourced from api_conversation's `header` field. Agent
  // badge hides when the session never set one; ctx badge hides when the
  // convo has no main-thread assistant turn yet.
  const header = state.msgData?.header || {};
  const agentBadge = header.agent
    ? `<span class="badge badge-agent" title="Agent: ${escAttr(header.agent)}">@${esc(header.agent)}</span>`
    : "";
  let ctxBadge = "";
  const ctxTokens = (header.last_context_input_tokens || 0)
                  + (header.last_context_cache_creation_tokens || 0)
                  + (header.last_context_cache_read_tokens || 0);
  if (ctxTokens) {
    const win = contextWindowFor(header.last_model_for_context, ctxTokens, state.planContextWindow);
    const ctxPct = Math.round(ctxTokens / win * 100);
    ctxBadge = `<span class="badge" title="Context at last turn (how close to /compact): ${fmtTokens(ctxTokens)} of ${fmtTokens(win)}">ctx ${fmtTokens(ctxTokens)} <span class="stats-pct">(${ctxPct}%)</span></span>`;
  }
  const headerBadges = (agentBadge || ctxBadge)
    ? `<span class="bc-header-badges">${agentBadge}${ctxBadge}</span>`
    : "";

  // Tag pills for this conversation. Edit mode adds × to remove and a
  // "Tag this convo" button (always shown, even with zero tags — the
  // popup supports inline label creation, same flow as the project-view
  // "Tag N selected" button).
  let tagsHtml = "";
  if (state.convoId) {
    const assigned = (state.tagAssignments || {})[state.convoId] || [];
    const byId = new Map((state.tagLabels || []).map((l) => [l.id, l]));
    const pills = assigned.map((id) => {
      const label = byId.get(id);
      if (!label || !label.name) return "";
      if (state.editMode) {
        return `<span class="tag-pill tag-pill-sm tag-color-${label.color} tag-pill-removable" data-action="remove-convo-tag" data-tag="${label.id}" title="Click to remove">${esc(label.name)} <span class="tag-x">×</span></span>`;
      }
      return `<span class="tag-pill tag-pill-sm tag-color-${label.color}">${esc(label.name)}</span>`;
    }).join("");
    const tagBtn = state.editMode
      ? `<button class="btn btn-sm bc-tag-btn" data-action="open-convo-tag-popup" title="Tag this conversation">Tag this convo</button>`
      : "";
    if (pills || tagBtn) {
      tagsHtml = `<span class="bc-tags">${pills}${tagBtn}</span>`;
    }
  }

  // Agent segment: the parent-convo name becomes clickable, and a new leaf
  // shows "Agent: <name>". Tag pills + header badges hide in agent view
  // since they're parent-convo concerns, not per-run.
  let convoSeg;
  if (state.agentRunId) {
    convoSeg =
      `<a data-action="nav-convo">${esc(display)}</a>${copyBtn} /` +
      ` <span class="bc-agent-name">Agent: ${esc(state.msgData?.agent_name || "agent")}</span>`;
    tagsHtml = "";
  } else {
    convoSeg = `<span class="bc-convo-name">${esc(display)}</span>${copyBtn}${headerBadges}${tagsHtml}`;
  }

  bc.innerHTML = `
    <a data-action="nav-projects">Projects</a> /
    <a data-action="nav-folder" data-folder="${escAttr(state.folder)}">${esc(state.path)}</a> /
    ${convoSeg}
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
export async function removeConvoTag(tagId) {
  if (!state.convoId) return;
  const current = (state.tagAssignments[state.convoId] || []).filter((i) => i !== tagId);
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
  const available = state.tagLabels.filter((l) => l && l.name);
  if (!available.length) return;
  const pills = available.map((l) =>
    `<span class="tag-pill tag-pill-sm tag-color-${l.color}${assigned.has(l.id) ? " active" : ""}" data-action="pick-convo-tag" data-tag="${l.id}" title="${assigned.has(l.id) ? "Remove" : "Add"} '${escAttr(l.name)}'">${esc(l.name)}</span>`
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

export async function pickConvoTag(tagId) {
  if (!state.convoId) return;
  const current = new Set(state.tagAssignments[state.convoId] || []);
  if (current.has(tagId)) current.delete(tagId);
  else current.add(tagId);
  const next = [...current].sort();
  state.tagAssignments[state.convoId] = next;
  try {
    await api.assignTags(state.folder, state.convoId, next);
  } catch { /* best-effort */ }
  document.querySelectorAll(".tag-picker").forEach((el) => el.remove());
  renderBreadcrumb();
}

function renderToolbar() {
  let extra = "";
  // While a search-triggered full-load is in flight, show an inline
  // spinner chip so the user sees that old messages are being pulled
  // (otherwise they type, see only the last-60-window matches, and
  // think the search is broken until the fetch lands).
  if (state.msgSearchLoading) {
    extra += `<span class="msg-search-loading">searching all messages…</span> `;
  }
  // "Earlier" only applies to parent-convo paging; agent runs are loaded
  // whole by the /agent/<run_id> endpoint. Hidden during an active
  // search because the search flow pulls the full set anyway — leaving
  // the button would let the user kick off a redundant fetch that then
  // also resets pagination counters in a confusing way.
  if (!state.agentRunId && state.msgOffset > 0 && !state.msgSearch) {
    extra += `<button class="btn" data-action="load-earlier-msgs">Earlier (${state.msgOffset})</button> `;
  }
  // Agent-run index — comprehensive list (anchored + standalone). Anchored
  // runs also have inline `→` markers in the transcript; this button is
  // the only way to reach standalone ones.
  const runs = (!state.agentRunId && state.msgData?.agent_runs) || [];
  if (runs.length > 0) {
    extra += `<button class="btn" data-action="open-agents-menu" title="List all subagents spawned by this conversation">Subagents (${runs.length})</button> `;
  }
  extra += `<button class="btn ${state.showWhitespace ? "active" : ""}" data-action="toggle-whitespace" title="Show invisible characters: spaces as · and tabs as →">Whitespace</button>`;
  configureToolbar({
    placeholder: "Search messages...",
    searchValue: state.msgSearch,
    extraHtml: extra,
    onSearch: (v) => {
      const prev = state.msgSearch;
      state.msgSearch = v;
      // Clearing the search box: we loaded all messages to make search
      // work, which left msgOffset=0 and Earlier hidden. Restore the
      // original windowed view (last PAGE_MSGS + Earlier button) so the
      // UI returns to its pre-search shape. The backend LRU keeps the
      // parse warm so reloading the window is in-memory fast.
      if (prev && !v) restoreWindowedView();
      scheduleMsgSearchLoad(v);
      render();
    },
  });
}

// Re-slices the locally held messages back into the last-PAGE_MSGS
// window and restores the Earlier offset. Purely client-side — we
// already have the messages from the search-triggered full load, so
// there's no network round-trip here.
function restoreWindowedView() {
  if (state.agentRunId) return;
  const all = state.msgData?.main || [];
  const total = state.msgTotal || all.length;
  if (all.length <= PAGE_MSGS) return;
  const start = Math.max(0, all.length - PAGE_MSGS);
  state.msgData.main = all.slice(start);
  state.msgOffset = Math.max(0, total - PAGE_MSGS);
}


// When the user searches, ensure every message in the convo is loaded —
// default pagination only has the last 60, so earlier matches would be
// invisible. Debounced so we don't re-fetch per keystroke; skipped on
// agent-run view (those load whole already) and when we already have
// the full set.
let _msgSearchDebounce = null;
let _msgSearchSeq = 0;
function scheduleMsgSearchLoad(query) {
  if (!query) return;
  if (state.agentRunId) return;
  const loaded = (state.msgData?.main || []).length;
  const total = state.msgTotal || 0;
  if (total <= loaded) return;
  // Kick off immediately — there's nothing to debounce. The fetch is
  // idempotent (subsequent calls bail at `total <= loaded`) and we want
  // old matches on screen as fast as possible. Set a flag so the toolbar
  // can show "searching all messages…" while we wait.
  const folder = state.folder;
  const convoId = state.convoId;
  const seq = ++_msgSearchSeq;
  state.msgSearchLoading = true;
  render();
  (async () => {
    let data;
    try {
      data = await api.messages(folder, convoId, { offset: 0, limit: total });
    } catch {
      state.msgSearchLoading = false;
      render();
      return;
    }
    if (seq !== _msgSearchSeq || folder !== state.folder || convoId !== state.convoId) {
      state.msgSearchLoading = false;
      return;
    }
    state.msgData.main = data.main || [];
    state.msgOffset = 0;
    state.msgSearchLoading = false;
    render();
  })();
}

export function render() {
  renderToolbar();
  renderBreadcrumb();

  const main = state.msgData?.main || [];
  const q = state.msgSearch.toLowerCase();
  const filtered = q ? main.filter((m) => (m.content || "").toLowerCase().includes(q)) : main;

  let h = '<div class="chat-wrap">';
  if (state.editMode && filtered.length > 0) {
    const selectable = filtered.filter((m) => !!m.uuid);
    const allSelected = selectable.length > 0 &&
      selectable.every((m) => state.msgSelected.has(m.uuid));
    const label = allSelected ? "Deselect all" : `Select all (${selectable.length})`;
    const proseN = selectable.filter((m) => !m.has_tool_use && !m.has_thinking).length;
    const nonProseN = selectable.length - proseN;
    h += `<div class="chat-select-all">
      <span class="split-btn">
        <button class="btn btn-sm" data-action="toggle-all-msgs">${label}</button>
        <button class="btn btn-sm split-arrow" data-action="open-select-scope-menu" title="Select by content type (prose-only / non-prose only)">▾</button>
      </span>
    </div>`;
  }
  h += renderChatMessages(filtered, q);
  h += "</div>";

  if (state.editMode && state.msgSelected.size > 0) {
    h += `
      <div class="sel-bar">
        <span>${state.msgSelected.size} selected</span>
        <button class="btn" data-action="open-export-menu" title="Copy, download, or extract the selected messages.">Export/Extract ▾</button>
        <span class="split-btn">
          <button class="btn" data-action="bulk-transform" data-kind="redact" title="Redact text on selected messages. Non-prose messages have their tool_use / thinking blocks collapsed; stats are preserved via tombstones but raw block contents are lost.">Redact</button>
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

  // New-format agent runs surface a `→ <agent-name>` link next to their
  // parent-side anchor — the Agent/Task `tool_use` block they spawned
  // from. `anchor_tool_use_id` may be null for standalone subagents; those
  // runs only appear in the toolbar list, not inline. Multiple runs may
  // share an anchor (one Agent invocation fans out to several subagents),
  // so we collect them as a list per id, not a single entry.
  const runsByAnchorToolUseId = {};
  for (const r of (state.msgData?.agent_runs || [])) {
    if (r.source === "subagent" && r.anchor_tool_use_id) {
      (runsByAnchorToolUseId[r.anchor_tool_use_id] ||= []).push(r);
    }
  }

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
    const anchored = tid ? (runsByAnchorToolUseId[tid] || []) : [];
    for (const run of anchored) {
      badge += ` <a class="agent-link" data-action="open-agent" data-run-id="${escAttr(run.run_id)}" title="Open subagent transcript (${run.message_count} message${run.message_count === 1 ? "" : "s"})">→ ${esc(run.name)}</a>`;
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

  // System / slash-command / queue-operation markers emitted by the backend
  // for entries that previously didn't surface (top-level `content`, no
  // `message` object). Each becomes a small pill in the transcript.
  //   [Slash: /btw]       → ⎇ /btw
  //   [SlashOut] text     → stdout pill + text flows after
  //   [SlashErr] text     → stderr pill + text flows after
  //   [Queued] text       → queued pill + text flows after
  //   [Compacted] text    → compacted marker
  //   [Away] text         → away-summary marker
  //   [Info] text         → info marker
  //   [Scheduled] text    → scheduled-task marker
  c = c.replace(/\[(Slash|SlashOut|SlashErr|Queued|Compacted|Away|Info|Scheduled|System)(?::\s*([^\]]+))?\]/g, (_, kind, payload) => {
    const ph = `__TOOL_${toolBadges.length}__`;
    const labels = {
      Slash: payload || "/",
      SlashOut: "stdout",
      SlashErr: "stderr",
      Queued: "queued",
      Compacted: "compacted",
      Away: "away summary",
      Info: "info",
      Scheduled: "scheduled",
      System: "system",
    };
    const cls = `tool-badge tool-badge-${kind.toLowerCase()}`;
    toolBadges.push(`<span class="${cls}">${esc(labels[kind])}</span>`);
    toolNames.push(kind);
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
  const { m, html: c, toolNames, hasThinking } = p;
  const role = m.role === "user" ? "user" : "assistant";
  const ck = state.msgSelected.has(m.uuid) ? "checked" : "";
  const checkHtml = m.uuid
    ? `<input type="checkbox" class="chat-check" ${ck} data-action="toggle-msg-sel" data-uuid="${escAttr(m.uuid)}">`
    : "";
  const canEdit = !!m.uuid;
  // Non-prose = has tool_use or thinking blocks. Editing collapses them to a
  // single text block: stats are preserved via tombstones (tool_uses,
  // commands, thinking_count, turn-token slices, per_model) but the raw
  // block contents (bash command strings, tool inputs, thinking text) are
  // overwritten permanently.
  const isNonProse = !!((toolNames && toolNames.length) || hasThinking);
  const editTitle = isNonProse
    ? "Edit — collapses tool_use / thinking blocks to plain text. Stats preserved via tombstones; raw block contents are overwritten permanently."
    : "Edit this message's text in place. Preserves usage/stats and resume chain.";
  const transformBtn = canEdit
    ? `<span class="split-btn">
        <button class="btn btn-sm" data-action="edit-msg" data-uuid="${escAttr(m.uuid)}" data-non-prose="${isNonProse ? "1" : "0"}" title="${editTitle}">Edit${isNonProse ? " ⚠" : ""}</button>
        <button class="btn btn-sm split-arrow" data-action="open-transform-menu" data-uuid="${escAttr(m.uuid)}" title="More text transforms">▾</button>
      </span>`
    : "";
  const delTitle = isNonProse
    ? "Delete — removes the entry entirely. Stats preserved via tombstones (tool counts, bash breakdowns, thinking, token slices); raw block contents are gone forever. May break /resume."
    : "Delete this message (rewrites file — may break /resume). Prefer Edit mode → Save to new convo for curation.";
  const actionsHtml = m.uuid
    ? `<span class="msg-actions-row">
        <button class="btn btn-sm" data-action="copy-msg" data-uuid="${escAttr(m.uuid)}" title="Copy">Copy</button>
        ${transformBtn}
        <button class="btn-danger btn-sm btn-del-msg" data-action="delete-msg" data-uuid="${escAttr(m.uuid)}" data-non-prose="${isNonProse ? "1" : "0"}" title="${delTitle}">${isNonProse ? "x ⚠" : "x"}</button>
      </span>`
    : "";
  const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : "";
  const bubbleCls = compact ? "chat-bubble tool-bubble" : "chat-bubble";
  const bubbleUuidAttr = m.uuid ? ` data-uuid="${escAttr(m.uuid)}"` : "";
  const metaHtml = compact
    ? ""
    : `<div class="chat-meta"><span class="role-lbl">${role}</span><span>${ts}</span></div>`;

  // Inline agent-run marker: old-format clusters have no Task tool_use to
  // attach to, so we anchor on the main message whose uuid matches the
  // cluster's anchor_uuid. A small "legacy" tag flags the format.
  let agentInline = "";
  if (m.uuid && state.msgData?.agent_runs) {
    const matches = state.msgData.agent_runs.filter(
      (r) => r.source === "inline" && r.anchor_uuid === m.uuid
    );
    for (const r of matches) {
      agentInline += ` <a class="agent-link agent-link-inline" data-action="open-agent" data-run-id="${escAttr(r.run_id)}" title="Open inline agent transcript (${r.message_count} message${r.message_count === 1 ? "" : "s"})">→ ${esc(r.name)} <span class="agent-src-tag">legacy</span></a>`;
    }
  }

  return `<div class="chat-msg ${role}${compact ? " compact" : ""}">${checkHtml}<div class="${bubbleCls}"${bubbleUuidAttr}>${actionsHtml}${metaHtml}<div class="msg-content">${c}${agentInline}</div></div></div>`;
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

export function toggleWhitespace() {
  state.showWhitespace = !state.showWhitespace;
  render();
}


export function openAgentsMenu(anchorEl) {
  // Toggle: re-click closes. Also close any other transform-style menus.
  const existing = document.querySelector(".transform-menu[data-agents='1']");
  if (existing) { existing.remove(); return; }
  document.querySelectorAll(".transform-menu").forEach((el) => el.remove());

  const runs = state.msgData?.agent_runs || [];
  if (runs.length === 0) return;

  const menu = document.createElement("div");
  menu.className = "transform-menu";
  menu.dataset.agents = "1";
  // Sort: anchored subagents first (they have an inline marker too), then
  // standalone, then inline-legacy. Within each group, order by first_ts.
  const rank = (r) => r.source === "subagent" && r.anchor_tool_use_id ? 0
                   : r.source === "subagent" ? 1
                   : 2;
  const sorted = [...runs].sort((a, b) => rank(a) - rank(b) || (a.first_ts || "").localeCompare(b.first_ts || ""));
  menu.innerHTML = sorted.map((r) => {
    const srcLabel = r.source === "inline" ? "legacy"
                   : r.anchor_tool_use_id ? "" : "standalone";
    const tag = srcLabel ? ` <span class="agent-src-tag">${srcLabel}</span>` : "";
    return `<button class="btn btn-sm transform-menu-item" data-action="open-agent" data-run-id="${escAttr(r.run_id)}" title="${esc(r.name)} — ${r.message_count} message${r.message_count === 1 ? "" : "s"}">${esc(r.name)} <span class="agent-src-tag">${r.message_count}</span>${tag}</button>`;
  }).join("");

  const rect = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  menu.style.left = `${rect.left + window.scrollX}px`;
  menu.style.top = `${rect.bottom + window.scrollY + 2}px`;
  document.body.appendChild(menu);
  setTimeout(() => {
    const handler = (ev) => {
      if (!menu.contains(ev.target) && ev.target !== anchorEl) {
        menu.remove();
        document.removeEventListener("click", handler, true);
      }
    };
    document.addEventListener("click", handler, true);
  }, 0);
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

// Select all messages whose shape matches `scope`:
//   "prose"     — neither has_tool_use nor has_thinking
//   "non_prose" — has_tool_use or has_thinking
// Additive: adds to the current selection rather than replacing, so users
// can stack "prose then non-prose" or intersect with a search filter.
export function selectByScope(scope) {
  const main = state.msgData?.main || [];
  const q = state.msgSearch.toLowerCase();
  const filtered = q ? main.filter((m) => (m.content || "").toLowerCase().includes(q)) : main;
  const isProse = (m) => !m.has_tool_use && !m.has_thinking;
  const picks = filtered
    .filter((m) => !!m.uuid)
    .filter((m) => scope === "prose" ? isProse(m) : !isProse(m));
  for (const m of picks) state.msgSelected.add(m.uuid);
  closeSelectScopeMenu();
  render();
}

function closeSelectScopeMenu() {
  const existing = document.querySelector(".select-scope-menu");
  if (existing) existing.remove();
}

export function openSelectScopeMenu(anchorEl) {
  const existing = document.querySelector(".select-scope-menu");
  if (existing) { existing.remove(); return; }
  const main = state.msgData?.main || [];
  const q = state.msgSearch.toLowerCase();
  const filtered = q ? main.filter((m) => (m.content || "").toLowerCase().includes(q)) : main;
  const selectable = filtered.filter((m) => !!m.uuid);
  const proseN = selectable.filter((m) => !m.has_tool_use && !m.has_thinking).length;
  const nonProseN = selectable.length - proseN;

  const menu = document.createElement("div");
  menu.className = "transform-menu select-scope-menu";
  menu.innerHTML =
    `<button class="btn btn-sm transform-menu-item" data-action="select-scope" data-scope="prose" ${proseN ? "" : "disabled"}>Select prose only (${proseN})</button>` +
    `<button class="btn btn-sm transform-menu-item" data-action="select-scope" data-scope="non_prose" ${nonProseN ? "" : "disabled"}>Select non-prose only (${nonProseN})</button>`;
  const r = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  menu.style.top = `${r.bottom + window.scrollY + 4}px`;
  menu.style.left = `${r.left + window.scrollX}px`;
  document.body.appendChild(menu);

  const onDocClick = (e) => {
    if (!menu.contains(e.target) && e.target !== anchorEl) {
      closeSelectScopeMenu();
      document.removeEventListener("click", onDocClick, true);
    }
  };
  setTimeout(() => document.addEventListener("click", onDocClick, true), 0);
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
  const items = visibleTransformEntries()
    .map(([kind, label]) =>
      `<button class="btn btn-sm transform-menu-item" data-action="bulk-transform" data-kind="${kind}">${label}</button>`
    ).join("");
  const previewOn = state.previewEnabled;
  const toggleLabel = previewOn ? "Turn off preview edits" : "Turn on preview edits";
  menu.innerHTML = items +
    `<div class="transform-menu-sep"></div>` +
    `<button class="btn btn-sm transform-menu-item transform-menu-toggle" data-action="toggle-preview" title="Show a review modal before applying any edit. Toggle any time from this menu or from inside the preview modal.">${toggleLabel}</button>` +
    `<button class="btn btn-sm transform-menu-item" data-action="open-word-lists">Curate word lists…</button>`;
  const rect = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  // Surface above the anchor — sel-bar is at viewport bottom, a
  // downward-opening menu gets clipped by the page edge.
  menu.style.left = `${rect.left + window.scrollX}px`;
  menu.style.visibility = "hidden";
  document.body.appendChild(menu);
  menu.style.top = `${rect.top + window.scrollY - menu.offsetHeight - 2}px`;
  menu.style.visibility = "";
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


export function openExportMenu(anchorEl) {
  const existing = document.querySelector(".transform-menu[data-export='1']");
  if (existing) {
    existing.remove();
    return;
  }
  // Close any other open transform menus
  document.querySelectorAll(".transform-menu").forEach((el) => el.remove());

  const menu = document.createElement("div");
  menu.className = "transform-menu";
  menu.dataset.export = "1";
  menu.innerHTML = [
    `<button class="btn btn-sm transform-menu-item" data-action="copy-selected" title="Copy selected messages as plain text (Role: body, blank line between).">Copy plain to clipboard</button>`,
    `<button class="btn btn-sm transform-menu-item" data-action="copy-selected-jsonl" title="Copy selected messages as JSONL using the current JSONL fields.">Copy JSONL to clipboard</button>`,
    `<button class="btn btn-sm transform-menu-item" data-action="download-selected-jsonl" title="Download selected messages as a .jsonl file using the current JSONL fields.">Download JSONL</button>`,
    `<div class="transform-menu-sep"></div>`,
    `<button class="btn btn-sm transform-menu-item" data-action="save-selected" title="Writes a NEW .jsonl on disk: a fresh conversation containing only the selected messages. parentUuid chain is remapped and orphan tool blocks are stripped.">Extract to new conversation</button>`,
    `<div class="transform-menu-sep"></div>`,
    `<button class="btn btn-sm transform-menu-item" data-action="open-jsonl-fields" title="Pick which fields are included in JSONL exports (Copy JSONL / Download JSONL).">JSONL fields…</button>`,
  ].join("");
  const rect = anchorEl.getBoundingClientRect();
  menu.style.position = "absolute";
  // Anchor to the top of the button and render ABOVE it — the sel-bar sits
  // at the bottom of the viewport, so a downward-opening menu gets clipped.
  menu.style.left = `${rect.left + window.scrollX}px`;
  menu.style.visibility = "hidden";
  document.body.appendChild(menu);
  menu.style.top = `${rect.top + window.scrollY - menu.offsetHeight - 2}px`;
  menu.style.visibility = "";
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

export async function openJsonlFieldsModal() {
  let current;
  try {
    current = await ensureDownloadFields();
  } catch (e) {
    alert(`Couldn't load JSONL fields: ${e.message}`);
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  const fieldRows = EXPORT_FIELDS.map((f) => {
    const required = REQUIRED_EXPORT_FIELDS.includes(f);
    const checked = required || current[f] ? "checked" : "";
    const disabled = required ? "disabled" : "";
    const hint = required ? " <span style=\"color:var(--text3); font-weight:normal\">(required)</span>" : "";
    return `<label class="jsonl-field-row"><input type="checkbox" data-field="${f}" ${checked} ${disabled}> <code>${f}</code>${hint}</label>`;
  }).join("");
  overlay.innerHTML = `
    <div class="modal">
      <h3>JSONL fields</h3>
      <p style="font-size:13px; color:var(--text2)">
        Which fields to include when exporting messages as JSONL
        (Copy JSONL / Download JSONL). <code>role</code> and
        <code>content</code> are always included. For the full unmodified
        source file, use <em>Download raw convo</em> in the page toolbar.
      </p>
      <div class="jsonl-fields-list">${fieldRows}</div>
      <div class="modal-actions">
        <button class="btn btn-sm" data-select-all>Select all</button>
        <span style="flex:1"></span>
        <button class="btn-cancel" data-modal-cancel>Cancel</button>
        <button class="btn-confirm-delete" data-modal-save>Save</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.addEventListener("click", async (e) => {
    if (e.target === overlay || e.target.matches("[data-modal-cancel]")) {
      close();
    } else if (e.target.matches("[data-select-all]")) {
      overlay.querySelectorAll("input[type=checkbox][data-field]").forEach((cb) => {
        if (!cb.disabled) cb.checked = true;
      });
    } else if (e.target.matches("[data-modal-save]")) {
      const payload = {};
      overlay.querySelectorAll("input[type=checkbox][data-field]").forEach((cb) => {
        payload[cb.dataset.field] = cb.checked;
      });
      try {
        state.downloadFields = await api.saveDownloadFields(payload);
      } catch (err) {
        alert(`Save failed: ${err.message}`);
        return;
      }
      close();
    }
  });
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



// True when the message has tool_use or thinking blocks (structural data
// that a text-only edit/redact/delete destroys). Backend sets these flags
// explicitly in _format_entry_message so the frontend doesn't have to
// re-parse the flattened display content.
function _isNonProseMsg(m) {
  return !!(m && (m.has_tool_use || m.has_thinking));
}

function _countNonProseIn(uuids) {
  const byId = new Map((state.msgData?.main || []).map((m) => [m.uuid, m]));
  let n = 0;
  for (const u of uuids) if (_isNonProseMsg(byId.get(u))) n++;
  return n;
}

function _nonProseWarningBlock(n) {
  if (!n) return "";
  return `<div style="margin-top:10px; padding:6px 8px; background:var(--warn-bg, #fff3cd); border:1px solid var(--warn-border, #d9b84a); border-radius:4px; font-size:12px">
    <strong>⚠ ${n} non-prose message${n === 1 ? "" : "s"} in selection.</strong>
    These have tool_use / thinking blocks. Stats (tool counts, bash command breakdowns, thinking counts, token slices, per-model) are preserved via deleted-delta tombstones, but the raw block contents (bash command strings, tool inputs, thinking text) are gone forever.
  </div>`;
}

export function deleteMsg(uuid) {
  const btn = document.querySelector(`[data-action="delete-msg"][data-uuid="${cssUuid(uuid)}"]`);
  const isNonProse = btn && btn.dataset.nonProse === "1";
  const nonProseWarning = isNonProse
    ? `<div style="margin-top:10px; padding:6px 8px; background:var(--warn-bg, #fff3cd); border:1px solid var(--warn-border, #d9b84a); border-radius:4px; font-size:12px">
         <strong>⚠ Non-prose message.</strong> This message has tool_use / thinking blocks.
         Stats (tool counts, bash command breakdowns, thinking counts, token slices, per-model breakdowns)
         are preserved via deleted-delta tombstones, but the raw block contents
         (bash command strings, tool inputs, thinking text) are gone forever.
       </div>`
    : "";
  showConfirmModal({
    title: "Delete message?",
    body: `Rewrites the original conversation file in place. This may break
      <code>/resume</code> for this conversation — Claude Code's replay
      semantics aren't publicly documented.
      <br><br><strong>Prefer Redact</strong> if you only want to redact the
      text — it leaves <code>usage</code> and the chain intact, so it's
      strictly less invasive than delete.
      <br>Or use Edit mode → "Save to new convo" to curate non-destructively.${nonProseWarning}`,
    onConfirm: async () => {
      await api.deleteMessage(state.folder, state.convoId, uuid);
      show(state.folder, state.convoId);
    },
  });
}


export const TRANSFORM_LABELS = {
  redact: "Redact (replace text with \".\")",
  normalize_whitespace: "Normalize whitespace",
  remove_verbosity: "Remove verbosity",
  remove_priming: "Remove priming language",
  remove_custom_filter: "Remove custom filter",
};

const TRANSFORM_CONFIRM = {
  redact: `Replaces this message's text content with "." in place. Leaves
    <code>usage</code>, <code>uuid</code>, and <code>parentUuid</code>
    alone, so token stats and the resume chain are preserved. Only works
    on prose-only messages. Like other destructive edits, this may still
    affect <code>/resume</code> in undocumented ways.`,
  normalize_whitespace: `Collapses runs of spaces/tabs into a single space
    and 3+ consecutive newlines into a double newline. Leaves
    <code>usage</code> and chain alone. Prose-only messages.`,
  remove_priming: `Strips priming language from the message — words and
    phrases in prior turns that degrade the next turn's output. Two
    sub-lists applied in sequence: <strong>swears</strong> (emotionally
    charged words, word-bounded, <code>*</code> stem syntax for
    conjugations) and <strong>drift phrases</strong> (sycophancy /
    meta-commentary like "You're absolutely right!", matched exactly
    case-insensitive). Evidence base:
    <a href="https://dafmulder.substack.com/p/i-ran-1950-experiments-to-find-out" target="_blank" rel="noopener">Mulder's 1,950 experiments</a>
    and
    <a href="https://www.reddit.com/r/ClaudeAI/comments/1skmgef/emotional_priming_changes_claudes_code_more_than/" target="_blank" rel="noopener">community replication</a>.
    Use <em>Curate word lists</em> to edit either sub-list. Leaves
    <code>usage</code> and chain alone.`,
  remove_verbosity: `Separate from priming — this doesn't change agent
    behavior, it just reclaims tokens. Strips obviousness signalers
    ("obviously", "clearly", "of course") and meta-commentary phrases
    ("that's a great question", "at the end of the day") matched
    word-bounded, case-insensitive. Default list is conservative; add
    sincerity markers, intensifiers, or hedges via
    <em>Curate word lists</em> if you want them gone too. Test for any
    candidate: remove it, and if the sentence still makes sense it was
    filler. Leaves <code>usage</code> and chain alone.`,
  remove_custom_filter: `Strips phrases from your <strong>custom filter
    list</strong> — repeated text you've derived from your own cache via
    <em>Curate word lists → Calculate from cache</em>, plus anything
    you've added by hand. Catches session-boundary boilerplate, repeated
    scaffolding, and per-convo filler that the curated lists don't.
    Matched case-insensitive exact substring. Every remove-* transform
    honors the global <strong>whitelist</strong>, so entries containing
    any whitelisted phrase are skipped. Leaves <code>usage</code> and
    chain alone.`,
};


// Filters the transform menu to only show entries the user has opted into.
// `remove_custom_filter` is hidden unless `custom_filter_enabled` is on in
// the curation modal — the feature is experimental enough that we don't
// want to clutter the split-button menu by default.
function visibleTransformEntries() {
  const enabled = state.wordLists?.custom_filter_enabled === true;
  return Object.entries(TRANSFORM_LABELS).filter(([kind]) => {
    if (kind === "remove_custom_filter" && !enabled) return false;
    return true;
  });
}

export async function transformMsg(uuid, kind = "redact") {
  const body = TRANSFORM_CONFIRM[kind];
  if (!body) {
    alert(`Unknown transform: ${kind}`);
    return;
  }
  const m = (state.msgData?.main || []).find((x) => x.uuid === uuid);
  if (!m) return;
  const lists = needsWordLists(kind) ? await ensureWordLists() : {};

  if (kind === "remove_custom_filter" && !(lists.custom_filter || []).length) {
    alert("Custom filter list is empty. Open Curate word lists → Calculate from cache, or add entries manually.");
    return;
  }

  const doApply = async (newText) => {
    try {
      await api.editMessage(state.folder, state.convoId, uuid, newText);
    } catch (e) {
      alert(`Transform failed: ${e.message}`);
      return;
    }
    show(state.folder, state.convoId);
  };

  if (state.previewEnabled) {
    const res = await showPreviewModal({
      kind,
      label: TRANSFORM_LABELS[kind],
      candidates: [{ uuid, content: m.content || "", role: m.role }],
      opts: lists,
    });
    if (!res || !res.acceptedIds || res.acceptedIds.size === 0) return;
    const entry = res.byId.get(uuid);
    if (!entry) return;
    await doApply(entry.after);
    return;
  }

  // Preview disabled — fall back to the original confirmation flow so a
  // destructive one-shot isn't applied without any prompt.
  showConfirmModal({
    title: `${TRANSFORM_LABELS[kind]}?`,
    body,
    onConfirm: () => doApply(applyTransform(kind, m.content || "", { ...lists, role: m.role })),
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

  // The edit button carries data-non-prose="1" when the message has
  // tool_use or thinking blocks. In that case, saving collapses those
  // blocks to plain text — stats survive via tombstones but raw block
  // contents (bash commands, tool inputs, thinking text) are lost.
  const editBtn = bubble.querySelector('[data-action="edit-msg"]');
  const isNonProse = editBtn && editBtn.dataset.nonProse === "1";

  bubble.classList.add("editing");
  bubble._origContent = content.innerHTML;
  if (actions) bubble._origActions = actions.innerHTML;

  const warning = isNonProse
    ? `<div class="msg-edit-warning" style="margin-bottom:6px; padding:6px 8px; background:var(--warn-bg, #fff3cd); border:1px solid var(--warn-border, #d9b84a); border-radius:4px; font-size:12px">
         <strong>⚠ Non-prose message.</strong> Saving will collapse tool_use / thinking blocks to this text. Stats (tool counts, bash command breakdowns, thinking counts, token slices) are preserved via deleted-delta tombstones, but the raw block contents (bash command strings, tool inputs, thinking text) will be overwritten permanently.
       </div>`
    : "";
  content.innerHTML =
    warning +
    `<textarea class="msg-edit-ta" spellcheck="false"></textarea>` +
    `<div class="msg-edit-hint">Rewrites the file in place. Preserves usage/stats and resume chain — same caveats as redact.</div>`;
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
  const items = visibleTransformEntries()
    .map(([kind, label]) =>
      `<button class="btn btn-sm transform-menu-item" data-action="transform-msg" data-uuid="${uuid}" data-kind="${kind}">${label}</button>`
    ).join("");
  const previewOn = state.previewEnabled;
  const toggleLabel = previewOn ? "Turn off preview edits" : "Turn on preview edits";
  menu.innerHTML = editItem + items +
    `<div class="transform-menu-sep"></div>` +
    `<button class="btn btn-sm transform-menu-item transform-menu-toggle" data-action="toggle-preview" title="Show a review modal before applying any edit. Toggle any time from this menu or from inside the preview modal.">${toggleLabel}</button>` +
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

  // Back-compat for cached lists that predate later categories.
  current.verbosity = current.verbosity || [];
  current.custom_filter = current.custom_filter || [];
  current.whitelist = current.whitelist || [];
  current.lowercase_user_text = current.lowercase_user_text === true;
  current.abbreviations = Array.isArray(current.abbreviations) ? current.abbreviations : [];
  current.apply_abbreviations = current.apply_abbreviations === true;
  current.custom_filter_enabled = current.custom_filter_enabled === true;
  current.collapse_punct_repeats = current.collapse_punct_repeats === true;
  defaults.verbosity = defaults.verbosity || [];
  defaults.custom_filter = defaults.custom_filter || [];
  defaults.whitelist = defaults.whitelist || [];
  defaults.abbreviations = Array.isArray(defaults.abbreviations) ? defaults.abbreviations : [];

  const folder = state.folder;
  const convoId = state.convoId;
  const msgTotal = state.msgData?.total ?? null;
  const canScan = Boolean(folder && convoId);

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal wordlists-modal" style="max-width:720px; width:90vw">
      <h3>Curate word lists</h3>
      <p style="font-size:12px; color:var(--text3); margin:0 0 10px 0">
        Each list is a set of entries matched case-insensitively. The
        <strong>whitelist</strong> is global — entries containing any
        whitelisted phrase are skipped by every remove-* transform.
      </p>

      <div class="modal-body wordlists-modal-body">
        <details class="stats-section" open style="margin-bottom:8px">
          <summary><strong>Priming language</strong> <span style="color:var(--text3); font-weight:normal" id="priming-count-badge">(${current.swears.length} swears + ${current.filler.length} drift phrases)</span></summary>
          <div style="padding:8px 4px 4px">
            <p style="font-size:12px; color:var(--text2); margin:0 0 8px 0">
              Emotionally charged words and sycophancy in prior turns degrade
              the next turn's output — see
              <a href="https://dafmulder.substack.com/p/i-ran-1950-experiments-to-find-out" target="_blank" rel="noopener">Mulder's 1,950 experiments</a>
              and
              <a href="https://www.reddit.com/r/ClaudeAI/comments/1skmgef/emotional_priming_changes_claudes_code_more_than/" target="_blank" rel="noopener">community replication</a>
              for the evidence base. Two sub-lists with different match
              mechanisms; both are stripped in one pass when you hit
              <em>Remove priming language</em>.
            </p>

            <label style="display:inline-flex; align-items:center; gap:6px; margin:0 0 10px 0; font-size:12px; color:var(--text2)">
              <input type="checkbox" id="wl-lowercase-user-text" ${current.lowercase_user_text ? "checked" : ""}>
              <span>Also lowercase user-role text on <em>Remove priming language</em> <span style="color:var(--text3)">— for capslock-rant reduction. Off by default. Only applies to messages with role=user, only during the combined <code>Remove priming language</code> action.</span></span>
            </label>

            <label style="display:inline-flex; align-items:center; gap:6px; margin:0 0 10px 0; font-size:12px; color:var(--text2)">
              <input type="checkbox" id="wl-collapse-punct" ${current.collapse_punct_repeats ? "checked" : ""}>
              <span>Also collapse aggressive-repeat punctuation on <em>Remove priming language</em> <span style="color:var(--text3)">— <code>!!!</code>/<code>???</code>/<code>.....</code> flatten to single marks (<code>.</code>-runs of 4+ flatten to the 3-dot ellipsis, preserving intentional <code>...</code>). Tone-reduction motivation, not token savings; minor token win only on very long runs. Off by default. Fence-aware: skips lines inside <code>\`\`\`</code> blocks.</span></span>
            </label>

            <label style="display:block; margin-top:6px"><strong>Swears</strong> <span style="color:var(--text3); font-weight:normal">— word-bounded, case-insensitive. Append <code>*</code> to a stem for conjugations (<code>fuck*</code> → fuck/fucks/fucker/fucking); bare words match exactly so <code>ass</code> won't blow up <code>assistant</code>.</span></label>
            <div id="wl-swears"></div>
            <button class="btn btn-sm" data-reset="swears" style="margin-top:6px">Reset swears to defaults (${defaults.swears.length})</button>

            <label style="display:block; margin-top:10px"><strong>Drift phrases</strong> <span style="color:var(--text3); font-weight:normal">— sycophancy / meta-commentary ("You're absolutely right!", "Let me think step by step."). Exact substring match, case-insensitive.</span></label>
            <div id="wl-filler"></div>
            <button class="btn btn-sm" data-reset="filler" style="margin-top:6px">Reset drift phrases to defaults (${defaults.filler.length})</button>
          </div>
        </details>

        <details class="stats-section" open style="margin-bottom:8px">
          <summary><strong>Verbosity</strong> <span style="color:var(--text3); font-weight:normal" id="verbosity-count-badge">(${current.verbosity.length} entries)</span></summary>
          <div style="padding:8px 4px 4px">
            <p style="font-size:12px; color:var(--text2); margin:0 0 6px 0">
              Not about agent behavior — just token cost and readability.
              Strips obviousness signalers ("obviously", "clearly", "of
              course") and meta-commentary phrases ("that's a great
              question", "at the end of the day"). Word-bounded,
              case-insensitive. Default list is conservative; add sincerity
              markers, intensifiers, or hedges yourself if you want them
              gone too. Test for any candidate: remove it — if the sentence
              still makes sense, it was filler.
            </p>
            <div id="wl-verbosity"></div>
            <button class="btn btn-sm" data-reset="verbosity" style="margin-top:6px">Reset to defaults (${defaults.verbosity.length})</button>

            <details class="stats-section" style="margin-top:12px">
              <summary style="font-size:12px"><strong>Abbreviation substitutions</strong> <span style="color:var(--text3); font-weight:normal">(${current.abbreviations.length} pairs) — experimental</span></summary>
              <div style="padding:8px 4px 4px">
                <label style="display:inline-flex; align-items:flex-start; gap:6px; margin:0 0 10px 0; font-size:12px; color:var(--text2)">
                  <input type="checkbox" id="wl-apply-abbreviations" ${current.apply_abbreviations ? "checked" : ""}>
                  <span>Also apply abbreviation substitutions on <em>Remove verbosity</em> <span style="color:var(--text3)">— off by default. When on, each pair below is applied word-bounded and case-insensitively after the verbosity strip. Pairs whose <code>from</code> contains a whitelisted phrase are skipped.</span></span>
                </label>
                <p style="font-size:12px; color:var(--text2); margin:0 0 8px 0">
                  Two groups of pairs: (1) token-savers that <em>de-abbreviate</em> shorthand that tokenizes worse than the spelled-out word
                  (<code>i.e.</code>→<code>ie</code>, <code>w/</code>→<code>with</code>, <code>smth</code>→<code>something</code>, …) and (2) token-neutral
                  substitutions that save characters on disk (<code>you</code>→<code>u</code>, <code>please</code>→<code>pls</code>, …). Verified
                  empirically against OpenAI's
                  <a href="https://github.com/openai/tiktoken" target="_blank" rel="noopener">tiktoken</a>
                  (<code>o200k_base</code>); try a pair against Claude's actual tokenizer at
                  <a href="https://claude-tokenizer.vercel.app/" target="_blank" rel="noopener">claude-tokenizer.vercel.app</a>.
                </p>
                <p style="font-size:11px; color:var(--text3); margin:0 0 8px 0">
                  <strong>Caveats:</strong> Claude's BPE isn't publicly available; tiktoken is the closest public proxy and the <em>direction</em> (which rewrite saves) generally holds, but exact magnitudes can differ. Case-insensitive matching substitutes with the literal <code>to</code> — "You're" becomes "ur" (lowercase), which may look odd mid-sentence. Enabling this rewrites on-disk text; <code>usage</code> and the chain are still preserved.
                </p>
                <div id="wl-abbreviations"></div>
                <button class="btn btn-sm" data-reset="abbreviations" style="margin-top:6px">Reset to defaults (${defaults.abbreviations.length})</button>
              </div>
            </details>
          </div>
        </details>

        <details class="stats-section" style="margin-bottom:8px">
          <summary><strong>Whitelist</strong> <span style="color:var(--text3); font-weight:normal" id="wl-count-badge">(${current.whitelist.length} entries, applies to all filters)</span></summary>
          <div style="padding:8px 4px 4px">
            <p style="font-size:12px; color:var(--text2); margin:0 0 8px 0">
              Global "never redact" list. Any entry in any remove-* list that
              contains a whitelisted phrase (case-insensitive substring) is
              skipped at transform time. Ships with a curated seed of SWE /
              tool / code terms; edit freely.
            </p>
            <div id="wl-whitelist"></div>
            <div style="display:flex; gap:6px; margin-top:6px; flex-wrap:wrap">
              <button class="btn btn-sm" data-reset="whitelist">Reset to defaults (${defaults.whitelist.length})</button>
              <button class="btn btn-sm" data-prune-whitelist>Prune other lists against whitelist</button>
            </div>
          </div>
        </details>

        <details class="stats-section" style="margin-bottom:8px">
          <summary><strong>Custom filter</strong> <span style="color:var(--text3); font-weight:normal" id="cf-count-badge">(${current.custom_filter.length} entries) — experimental</span></summary>
          <div style="padding:8px 4px 4px">
            <p style="font-size:12px; color:var(--text2); margin:0 0 8px 0">
              Scans this conversation for repeated phrases you might want
              stripped. Usefulness depends heavily on the convo — short /
              clean convos produce little signal. Candidates land in an
              editable pill list that's saved globally. Click <code>×</code>
              on a pill to move it to the whitelist.
            </p>
            <label style="display:inline-flex; align-items:flex-start; gap:6px; margin:0 0 10px 0; font-size:12px; color:var(--text2)">
              <input type="checkbox" id="wl-custom-filter-enabled" ${current.custom_filter_enabled ? "checked" : ""}>
              <span>Show <em>Remove custom filter</em> in the transform menu <span style="color:var(--text3)">— off by default. Flip on once you've curated entries worth applying across convos.</span></span>
            </label>
            <div class="cf-thresholds" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:0 0 8px 0; font-size:12px; color:var(--text2)">
              <label style="display:inline-flex; align-items:center; gap:4px">min words <input type="number" id="cf-n-min" value="1" min="1" max="10" style="width:48px"></label>
              <label style="display:inline-flex; align-items:center; gap:4px">max words <input type="number" id="cf-n-max" value="3" min="1" max="10" style="width:48px"></label>
              <label style="display:inline-flex; align-items:center; gap:4px">min length (chars) <input type="number" id="cf-min-length" value="6" min="1" style="width:56px"></label>
              <label style="display:inline-flex; align-items:center; gap:4px">min occurrences <input type="number" id="cf-min-count" value="3" min="2" style="width:56px"></label>
            </div>
            <div style="font-size:12px; color:var(--text3); margin:0 0 8px 0" id="cf-scan-scope">${canScan ? `Scan this conversation${msgTotal != null ? ` (${msgTotal} message${msgTotal === 1 ? "" : "s"})` : ""}` : "Open a conversation to enable scanning"}</div>
            <div id="wl-custom-filter"></div>
            <button class="btn btn-sm" data-calc="custom-filter" style="margin-top:6px" ${canScan ? "" : "disabled"}>Calculate from this conversation</button>
          </div>
        </details>
      </div>

      <div class="modal-actions">
        <button class="btn-cancel" data-modal-cancel>Cancel</button>
        <button class="btn-confirm-delete" data-modal-save>Save</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const whitelistMount = mountPillList(
    overlay.querySelector("#wl-whitelist"),
    { entries: current.whitelist, placeholder: "add phrase + Enter" },
  );
  const customFilterMount = mountPillList(
    overlay.querySelector("#wl-custom-filter"),
    {
      entries: current.custom_filter,
      placeholder: "add phrase + Enter (or Calculate from this conversation)",
      onRemove: (entry) => whitelistMount.addEntry(entry),
    },
  );
  const swearsMount = mountPillList(
    overlay.querySelector("#wl-swears"),
    { entries: current.swears, placeholder: "add word + Enter (*stem OK)" },
  );
  const fillerMount = mountPillList(
    overlay.querySelector("#wl-filler"),
    { entries: current.filler, placeholder: "add phrase + Enter" },
  );
  const verbosityMount = mountPillList(
    overlay.querySelector("#wl-verbosity"),
    { entries: current.verbosity, placeholder: "add word + Enter" },
  );
  const abbreviationsMount = mountPillPairList(
    overlay.querySelector("#wl-abbreviations"),
    { pairs: current.abbreviations, fromPlaceholder: "from", toPlaceholder: "to" },
  );

  const mounts = {
    swears: swearsMount,
    filler: fillerMount,
    verbosity: verbosityMount,
    custom_filter: customFilterMount,
    whitelist: whitelistMount,
    abbreviations: abbreviationsMount,
  };

  const close = () => overlay.remove();

  overlay.addEventListener("click", async (e) => {
    if (e.target === overlay || e.target.matches("[data-modal-cancel]")) {
      close();
      return;
    }
    if (e.target.matches("[data-reset]")) {
      const which = e.target.dataset.reset;
      const container = overlay.querySelector(`#wl-${which.replace("_", "-")}`);
      if (!container) return;
      if (which === "abbreviations") {
        mounts.abbreviations = mountPillPairList(container, {
          pairs: defaults.abbreviations || [],
          fromPlaceholder: "from",
          toPlaceholder: "to",
        });
        return;
      }
      const newMount = mountPillList(container, {
        entries: defaults[which] || [],
        placeholder: container.querySelector(".pill-input")?.placeholder || "add + Enter",
        onRemove: which === "custom_filter" ? (entry) => mounts.whitelist.addEntry(entry) : undefined,
      });
      mounts[which] = newMount;
      return;
    }
    if (e.target.matches("[data-prune-whitelist]")) {
      const wl = mounts.whitelist.getEntries().map((s) => s.toLowerCase()).filter(Boolean);
      if (!wl.length) return;
      let removed = 0;
      for (const key of ["swears", "filler", "verbosity", "custom_filter"]) {
        const current_entries = mounts[key].getEntries();
        const kept = current_entries.filter((entry) => {
          const lower = entry.toLowerCase();
          return !wl.some((w) => lower.includes(w));
        });
        removed += current_entries.length - kept.length;
        if (kept.length !== current_entries.length) {
          const container = overlay.querySelector(`#wl-${key.replace("_", "-")}`);
          if (container) {
            mounts[key] = mountPillList(container, {
              entries: kept,
              placeholder: container.querySelector(".pill-input")?.placeholder || "add + Enter",
              onRemove: key === "custom_filter" ? (entry) => mounts.whitelist.addEntry(entry) : undefined,
            });
          }
        }
      }
      alert(`Pruned ${removed} entr${removed === 1 ? "y" : "ies"} matching the whitelist.`);
      return;
    }
    if (e.target.matches("[data-calc='custom-filter']")) {
      if (!canScan) {
        alert("Open a conversation first to scan it.");
        return;
      }
      const btn = e.target;
      const scopeLabel = overlay.querySelector("#cf-scan-scope");
      const min_length_chars = Math.max(1, parseInt(overlay.querySelector("#cf-min-length").value, 10) || 6);
      const min_count = Math.max(2, parseInt(overlay.querySelector("#cf-min-count").value, 10) || 3);
      const n_min_raw = parseInt(overlay.querySelector("#cf-n-min").value, 10) || 1;
      const n_max_raw = parseInt(overlay.querySelector("#cf-n-max").value, 10) || 3;
      const n_min = Math.max(1, Math.min(10, n_min_raw));
      const n_max = Math.max(n_min, Math.min(10, n_max_raw));
      if (mounts.custom_filter.getEntries().length > 0) {
        if (!confirm("Replace current custom filter list with fresh scan results? Entries not in the whitelist will be lost.")) return;
      }
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Scanning… 0s";
      const t0 = performance.now();
      const tick = setInterval(() => {
        const s = Math.floor((performance.now() - t0) / 1000);
        btn.textContent = `Scanning… ${s}s`;
      }, 1000);
      try {
        const res = await api.scanCustomFilter(folder, convoId, { min_length_chars, min_count, n_min, n_max });
        scopeLabel.textContent = `Derived from ${res.msg_count} message${res.msg_count === 1 ? "" : "s"} — ${(res.candidates || []).length} candidate${(res.candidates || []).length === 1 ? "" : "s"}`;
        const container = overlay.querySelector("#wl-custom-filter");
        mounts.custom_filter = mountPillList(container, {
          entries: res.candidates || [],
          placeholder: "add phrase + Enter (or Calculate from this conversation)",
          onRemove: (entry) => mounts.whitelist.addEntry(entry),
        });
      } catch (err) {
        alert(`Scan failed: ${err.message}`);
      } finally {
        clearInterval(tick);
        btn.textContent = originalText;
        btn.disabled = false;
      }
      return;
    }
    if (e.target.matches("[data-modal-save]")) {
      const lowercaseCheckbox = overlay.querySelector("#wl-lowercase-user-text");
      const applyAbbrevCheckbox = overlay.querySelector("#wl-apply-abbreviations");
      const customFilterEnabledCheckbox = overlay.querySelector("#wl-custom-filter-enabled");
      const collapsePunctCheckbox = overlay.querySelector("#wl-collapse-punct");
      const payload = {
        swears: mounts.swears.getEntries(),
        filler: mounts.filler.getEntries(),
        verbosity: mounts.verbosity.getEntries(),
        custom_filter: mounts.custom_filter.getEntries(),
        whitelist: mounts.whitelist.getEntries(),
        lowercase_user_text: !!(lowercaseCheckbox && lowercaseCheckbox.checked),
        abbreviations: mounts.abbreviations.getPairs(),
        apply_abbreviations: !!(applyAbbrevCheckbox && applyAbbrevCheckbox.checked),
        custom_filter_enabled: !!(customFilterEnabledCheckbox && customFilterEnabledCheckbox.checked),
        collapse_punct_repeats: !!(collapsePunctCheckbox && collapsePunctCheckbox.checked),
      };
      try {
        const saved = await api.saveWordLists(payload);
        state.wordLists = saved || null;
      } catch (err) {
        alert(`Save failed: ${err.message}`);
        return;
      }
      close();
    }
  });
}

export async function bulkTransform(kind = "redact") {
  const ids = Array.from(state.msgSelected);
  if (!ids.length) return;
  const label = TRANSFORM_LABELS[kind] || kind;
  const lists = needsWordLists(kind) ? await ensureWordLists() : {};
  const byId = new Map((state.msgData?.main || []).map((m) => [m.uuid, m]));

  if (kind === "remove_custom_filter" && !(lists.custom_filter || []).length) {
    alert("Custom filter list is empty. Open Curate word lists → Calculate from cache, or add entries manually.");
    return;
  }

  // accepted: Map<uuid, newText>
  let accepted;
  if (state.previewEnabled) {
    const candidates = ids
      .map((id) => byId.get(id))
      .filter(Boolean)
      .map((m) => ({
        uuid: m.uuid, content: m.content || "", role: m.role,
        has_tool_use: !!m.has_tool_use, has_thinking: !!m.has_thinking,
      }));
    const res = await showPreviewModal({ kind, label, candidates, opts: lists });
    if (!res) return;
    if (res.empty) {
      alert(`${label}: nothing would change for the selected messages.`);
      return;
    }
    accepted = new Map();
    for (const id of res.acceptedIds) {
      const entry = res.byId.get(id);
      if (entry) accepted.set(id, entry.after);
    }
    if (accepted.size === 0) return;
  } else {
    const nonProseN = _countNonProseIn(ids);
    const nonProseNote = nonProseN
      ? `\n\n⚠ ${nonProseN} of the selected message${nonProseN === 1 ? " is" : "s are"} non-prose (tool_use / thinking blocks). Those blocks will be collapsed to the transformed text. Stats are preserved via deleted-delta tombstones, but raw block contents are lost.`
      : "";
    if (!confirm(`${label} for ${ids.length} selected message(s)?${nonProseNote}`)) return;
    accepted = new Map();
    for (const id of ids) {
      const m = byId.get(id);
      if (!m) continue;
      accepted.set(id, applyTransform(kind, m.content || "", { ...lists, role: m.role }));
    }
  }

  let ok = 0, errored = 0;
  for (const [id, newText] of accepted) {
    try {
      await api.editMessage(state.folder, state.convoId, id, newText);
      ok++;
    } catch (e) {
      errored++;
    }
  }
  state.msgSelected.clear();
  if (errored) alert(`Applied to ${ok}. ${errored} failed — check server logs.`);
  show(state.folder, state.convoId);
}

export async function copySelected() {
  const main = state.msgData?.main || [];
  const selected = main.filter((m) => state.msgSelected.has(m.uuid));
  const text = serializeAsText(selected);
  await navigator.clipboard.writeText(text);
  state.msgSelected.clear();
  render();
}

export async function copySelectedJsonl() {
  const main = state.msgData?.main || [];
  const selected = main.filter((m) => state.msgSelected.has(m.uuid));
  const fields = await ensureDownloadFields();
  const jsonl = serializeAsJsonl(selected, fields);
  await navigator.clipboard.writeText(jsonl);
  state.msgSelected.clear();
  render();
}

export async function downloadSelectedJsonl() {
  const main = state.msgData?.main || [];
  const selected = main.filter((m) => state.msgSelected.has(m.uuid));
  if (!selected.length) return;
  const fields = await ensureDownloadFields();
  const jsonl = serializeAsJsonl(selected, fields);
  const base = (state.convoId || "conversation").slice(0, 8);
  downloadBlob(jsonl, "application/x-ndjson", `${base}_selection.jsonl`);
  state.msgSelected.clear();
  render();
}

export function downloadRawConvo() {
  if (!state.folder || !state.convoId) return;
  const a = document.createElement("a");
  a.href = api.rawConversationUrl(state.folder, state.convoId);
  a.download = `${state.convoId}.jsonl`;
  document.body.appendChild(a);
  a.click();
  a.remove();
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
  const nonProseN = _countNonProseIn(uuids);
  showConfirmModal({
    title: `Delete ${uuids.length} messages?`,
    body: `Rewrites the original conversation file in place. Cannot be undone,
      and may break <code>/resume</code> for this conversation — Claude Code's
      replay semantics aren't publicly documented.
      <br><br><strong>Prefer Redact</strong> on these messages if you only
      want to redact text — it preserves <code>usage</code> and the chain.
      <br>Or use "Save to new convo" (non-destructive, creates a copy).${_nonProseWarningBlock(nonProseN)}`,
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


