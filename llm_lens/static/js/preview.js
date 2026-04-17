import { state, setPreviewEnabled, setPreviewView } from "./state.js";
import { applyTransform } from "./transforms.js";

const DIFF_SIZE_CAP = 8000;

function esc(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

export function diffWords(a, b) {
  if (a.length + b.length > DIFF_SIZE_CAP) return null;
  // Filter empties that split(/(\s+)/) produces at string boundaries — a
  // leading/trailing empty token otherwise shows up as a spurious del/ins.
  const aw = a.split(/(\s+)/).filter((s) => s.length > 0);
  const bw = b.split(/(\s+)/).filter((s) => s.length > 0);
  const m = aw.length, n = bw.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = aw[i - 1] === bw[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const ops = [];
  let i = m, j = n;
  while (i > 0 && j > 0) {
    if (aw[i - 1] === bw[j - 1]) { ops.unshift({ t: "eq", v: aw[i - 1] }); i--; j--; }
    else if (dp[i - 1][j] >= dp[i][j - 1]) { ops.unshift({ t: "del", v: aw[i - 1] }); i--; }
    else { ops.unshift({ t: "ins", v: bw[j - 1] }); j--; }
  }
  while (i > 0) { ops.unshift({ t: "del", v: aw[i - 1] }); i--; }
  while (j > 0) { ops.unshift({ t: "ins", v: bw[j - 1] }); j--; }
  return ops;
}

export function deltaOf(ops, before, after) {
  if (!ops) return { add: after.length, rem: before.length };
  let add = 0, rem = 0;
  for (const op of ops) {
    if (op.t === "ins") add += op.v.length;
    else if (op.t === "del") rem += op.v.length;
  }
  return { add, rem };
}

function renderInline(ops, before, after) {
  if (!ops) {
    return `<div class="preview-too-large">
      <div class="preview-half preview-before-block">${esc(before)}</div>
      <div class="preview-half preview-after-block">${esc(after)}</div>
    </div>`;
  }
  return ops.map((op) => {
    const v = esc(op.v);
    if (op.t === "eq") return v;
    if (op.t === "ins") return `<ins>${v}</ins>`;
    return `<del>${v}</del>`;
  }).join("");
}

function renderStacked(ops, before, after) {
  if (!ops) {
    return `<div class="preview-stacked">
      <div class="preview-before-block">${esc(before)}</div>
      <div class="preview-after-block">${esc(after)}</div>
    </div>`;
  }
  const beforeHtml = ops.map((op) => {
    if (op.t === "ins") return "";
    const v = esc(op.v);
    return op.t === "del" ? `<del>${v}</del>` : v;
  }).join("");
  const afterHtml = ops.map((op) => {
    if (op.t === "del") return "";
    const v = esc(op.v);
    return op.t === "ins" ? `<ins>${v}</ins>` : v;
  }).join("");
  return `<div class="preview-stacked">
    <div class="preview-before-block">${beforeHtml}</div>
    <div class="preview-after-block">${afterHtml}</div>
  </div>`;
}

function renderRow(row, view) {
  const body = view === "diff"
    ? renderStacked(row.ops, row.before, row.after)
    : `<div class="preview-inline">${renderInline(row.ops, row.before, row.after)}</div>`;
  const delta = `<span class="preview-delta">+${row.delta.add} / -${row.delta.rem}</span>`;
  const nonProseMark = row.nonProse
    ? ` <span class="preview-nonprose-mark" title="Non-prose: this message has tool_use / thinking blocks that will be collapsed. Stats preserved via tombstones; raw block contents are lost.">⚠</span>`
    : "";
  return `<div class="preview-row${row.nonProse ? " preview-row-nonprose" : ""}" data-uuid="${esc(row.uuid)}">
    <label class="preview-check">
      <input type="checkbox" data-preview-check data-uuid="${esc(row.uuid)}" checked>
      ${delta}${nonProseMark}
    </label>
    <div class="preview-body">${body}</div>
  </div>`;
}

export function computeRows(kind, candidates, opts) {
  const rows = [];
  for (const m of candidates) {
    const before = m.content || "";
    let after;
    try { after = applyTransform(kind, before, { ...opts, role: m.role }); }
    catch { continue; }
    if (after === before) continue;
    const ops = diffWords(before, after);
    rows.push({
      uuid: m.uuid, before, after, ops,
      delta: deltaOf(ops, before, after),
      // Backend-provided flags — true when editing this message will collapse
      // tool_use / thinking blocks (structural content lost; stats survive via
      // deleted-delta tombstones).
      nonProse: !!(m.has_tool_use || m.has_thinking),
    });
  }
  return rows;
}

/**
 * Show the preview modal. Returns a Promise resolving to:
 *   - { acceptedIds: Set<string>, byId: Map<string, {before, after}> }  when applied
 *   - null  when cancelled
 */
export function showPreviewModal({ kind, label, candidates, opts }) {
  const rows = computeRows(kind, candidates, opts);
  if (rows.length === 0) {
    return Promise.resolve({ acceptedIds: new Set(), byId: new Map(), empty: true });
  }

  return new Promise((resolve) => {
    const view = state.previewView;
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal preview-modal">
        <button class="modal-close" data-preview-close aria-label="Close">&times;</button>
        <h3 class="preview-title">${esc(label || kind)} — ${rows.length} message${rows.length === 1 ? "" : "s"} will change</h3>
        ${(() => {
          const n = rows.filter((r) => r.nonProse).length;
          if (!n) return "";
          return `<details class="preview-nonprose-banner" style="margin:8px 14px; padding:6px 8px; background:var(--warn-bg, #fff3cd); border:1px solid var(--warn-border, #d9b84a); border-radius:4px; font-size:12px">
            <summary style="cursor:pointer"><strong>⚠ ${n} of ${rows.length} message${rows.length === 1 ? "" : "s"} ${n === 1 ? "is" : "are"} non-prose</strong> — click for details</summary>
            <div style="margin-top:6px">Applying will collapse tool_use / thinking blocks in those messages to the transformed text. Stats (tool counts, bash command breakdowns, thinking, token slices, per-model) are preserved via deleted-delta tombstones, but the raw block contents are lost. Flagged rows below are marked with ⚠.</div>
          </details>`;
        })()}
        <div class="preview-topbar">
          <div class="preview-view-toggle" role="tablist">
            <button class="btn btn-sm ${view === "inline" ? "active" : ""}" data-preview-view="inline">Inline</button>
            <button class="btn btn-sm ${view === "diff" ? "active" : ""}" data-preview-view="diff">Diff</button>
          </div>
          <div class="preview-topbar-actions">
            <span class="split-btn">
              <button class="btn btn-sm" data-preview-select-all title="Toggle: select all rows / deselect all rows">Select all (${rows.length})</button>
              <button class="btn btn-sm split-arrow" data-preview-scope-menu title="Select by content type (prose / non-prose only)">▾</button>
            </span>
            <label class="preview-skip-check" title="Skip this modal the next time you run a transform. Re-enable from the transform menu.">
              <input type="checkbox" data-preview-skip>
              Don't preview next time
            </label>
          </div>
        </div>
        <div class="modal-body preview-body-scroll" data-preview-list>
          ${rows.map((r) => renderRow(r, view)).join("")}
        </div>
        <div class="modal-actions preview-bottombar">
          <button class="btn-cancel" data-preview-cancel>Cancel</button>
          <span class="preview-net-delta" data-preview-net aria-label="Net character delta across checked rows"></span>
          <button class="btn-confirm-delete" data-preview-apply-selected>Apply selected (${rows.length})</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const byId = new Map(rows.map((r) => [r.uuid, { before: r.before, after: r.after }]));
    const deltaById = new Map(rows.map((r) => [r.uuid, r.delta]));
    // Banner visibility follows currently-checked non-prose rows: unchecking
    // every non-prose row hides the warning (nothing destructive remains).
    const nonProseIds = new Set(rows.filter((r) => r.nonProse).map((r) => r.uuid));

    function finish(acceptedIds) {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(acceptedIds === null ? null : { acceptedIds, byId });
    }

    function collectChecked() {
      const out = new Set();
      overlay.querySelectorAll("[data-preview-check]:checked").forEach((el) => {
        out.add(el.dataset.uuid);
      });
      return out;
    }

    function updateNetDelta() {
      const checked = collectChecked();
      let add = 0, rem = 0;
      let checkedNonProse = 0;
      for (const id of checked) {
        const d = deltaById.get(id);
        if (d) { add += d.add; rem += d.rem; }
        if (nonProseIds.has(id)) checkedNonProse++;
      }
      const net = add - rem;
      const netStr = net === 0 ? "±0" : (net > 0 ? `+${net}` : `${net}`);
      const box = overlay.querySelector("[data-preview-net]");
      if (box) {
        box.textContent = `+${add} / -${rem} · net ${netStr}`;
        box.classList.toggle("preview-net-empty", checked.size === 0);
      }
      const banner = overlay.querySelector(".preview-nonprose-banner");
      if (banner) banner.style.display = checkedNonProse > 0 ? "" : "none";
      // Dynamic Select/Deselect label — matches the edit-convo toolbar pattern.
      const selBtn = overlay.querySelector("[data-preview-select-all]");
      if (selBtn) {
        const allChecked = checked.size === rows.length;
        selBtn.textContent = allChecked ? "Deselect all" : `Select all (${rows.length})`;
      }
      const applyBtn = overlay.querySelector("[data-preview-apply-selected]");
      if (applyBtn) {
        applyBtn.textContent = `Apply selected (${checked.size})`;
        applyBtn.disabled = checked.size === 0;
      }
    }

    function rerenderList() {
      const v = state.previewView;
      overlay.querySelectorAll("[data-preview-view]").forEach((b) => {
        b.classList.toggle("active", b.dataset.previewView === v);
      });
      const checked = collectChecked();
      const list = overlay.querySelector("[data-preview-list]");
      list.innerHTML = rows.map((r) => renderRow(r, v)).join("");
      if (checked.size !== rows.length) {
        list.querySelectorAll("[data-preview-check]").forEach((el) => {
          el.checked = checked.has(el.dataset.uuid);
        });
      }
      updateNetDelta();
    }

    function onKey(e) {
      if (e.key === "Escape") { e.stopPropagation(); finish(null); }
    }
    document.addEventListener("keydown", onKey, true);

    overlay.addEventListener("change", (e) => {
      const t = e.target;
      if (t.matches("[data-preview-skip]")) {
        // Checkbox flips the global preview setting immediately; modal stays
        // open so the user still chooses whether to commit the current batch.
        setPreviewEnabled(!t.checked);
        return;
      }
      if (t.matches("[data-preview-check]")) updateNetDelta();
    });

    overlay.addEventListener("click", (e) => {
      const t = e.target;
      if (t === overlay || t.matches("[data-preview-close]") || t.matches("[data-preview-cancel]")) {
        finish(null); return;
      }
      if (t.matches("[data-preview-view]")) {
        setPreviewView(t.dataset.previewView);
        rerenderList();
        return;
      }
      if (t.matches("[data-preview-apply-selected]")) {
        finish(collectChecked()); return;
      }
      if (t.matches("[data-preview-select-all]")) {
        const boxes = overlay.querySelectorAll("[data-preview-check]");
        const anyUnchecked = [...boxes].some((b) => !b.checked);
        boxes.forEach((b) => { b.checked = anyUnchecked; });
        updateNetDelta();
        return;
      }
      if (t.matches("[data-preview-scope-menu]")) {
        openScopeMenu(t);
        return;
      }
      if (t.matches("[data-preview-scope]")) {
        const scope = t.dataset.previewScope;  // "prose" | "non_prose"
        const isProse = scope === "prose";
        overlay.querySelectorAll("[data-preview-check]").forEach((b) => {
          const isNP = nonProseIds.has(b.dataset.uuid);
          b.checked = isProse ? !isNP : isNP;
        });
        closeScopeMenu();
        updateNetDelta();
        return;
      }
    });

    function closeScopeMenu() {
      const m = overlay.querySelector(".preview-scope-menu");
      if (m) m.remove();
    }
    function openScopeMenu(anchorEl) {
      closeScopeMenu();
      const proseN = rows.filter((r) => !r.nonProse).length;
      const nonProseN = rows.length - proseN;
      const menu = document.createElement("div");
      menu.className = "transform-menu preview-scope-menu";
      menu.innerHTML =
        `<button class="btn btn-sm transform-menu-item" data-preview-scope="prose" ${proseN ? "" : "disabled"}>Select prose only (${proseN})</button>` +
        `<button class="btn btn-sm transform-menu-item" data-preview-scope="non_prose" ${nonProseN ? "" : "disabled"}>Select non-prose only (${nonProseN})</button>`;
      const r = anchorEl.getBoundingClientRect();
      menu.style.position = "absolute";
      menu.style.top = `${r.bottom + window.scrollY + 4}px`;
      menu.style.left = `${r.left + window.scrollX}px`;
      document.body.appendChild(menu);
      const onDoc = (ev) => {
        if (!menu.contains(ev.target) && ev.target !== anchorEl) {
          closeScopeMenu();
          document.removeEventListener("click", onDoc, true);
        }
      };
      setTimeout(() => document.addEventListener("click", onDoc, true), 0);
    }

    updateNetDelta();
  });
}
