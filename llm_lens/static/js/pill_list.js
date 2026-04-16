// Pill / token list component used by the Curate word lists modal.
// All pills + the commit input live in a single flex-wrapping container
// so the input sits inline, right after the last pill (chip-input style).
//
// Deletion: × click on a pill OR backspace-on-empty-input removes last.
// Commit: Enter in input OR "+" button click.
// Paste with newlines splits into multiple pills.
// Dedupe on commit is case-insensitive (preserves typed case).
//
// mountPillList(container, { entries, onRemove, placeholder })
//   - container: DOM element to mount into (contents are replaced)
//   - entries: initial array of strings
//   - onRemove: optional callback fired after a pill is removed
//     (receives the removed entry string). Used by custom_filter to
//     move deleted entries into the whitelist mount.
//   - placeholder: optional text for the input placeholder
//
// Returns { getEntries, addEntry }:
//   - getEntries(): current entries (trimmed, deduped case-insensitively)
//   - addEntry(text): programmatically append an entry

export function mountPillList(container, { entries = [], onRemove = null, placeholder = "add + Enter" } = {}) {
  const state = {
    entries: dedupePreserveCase(
      (entries || [])
        .filter((e) => typeof e === "string" && e.trim())
        .map((e) => e.trim())
    ),
  };

  container.innerHTML = "";
  container.classList.add("pill-list");

  const wrap = document.createElement("div");
  wrap.className = "pill-list-pills";
  container.appendChild(wrap);

  const input = document.createElement("input");
  input.type = "text";
  input.className = "pill-input";
  input.placeholder = placeholder;
  input.spellcheck = false;

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn btn-sm pill-add-btn";
  addBtn.textContent = "+";
  addBtn.setAttribute("aria-label", "Add entry");

  function makePill(entry) {
    const pill = document.createElement("span");
    pill.className = "pill";
    const label = document.createElement("span");
    label.className = "pill-label";
    label.textContent = entry;
    pill.appendChild(label);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "pill-remove";
    x.setAttribute("aria-label", `Remove ${entry}`);
    x.textContent = "×";
    x.addEventListener("click", () => removeEntry(entry));
    pill.appendChild(x);
    return pill;
  }

  // Render pills in front of the input (input + addBtn stay as the last
  // two children of wrap so adding another pill visibly appends before
  // the input).
  function renderAll() {
    // Remove existing pill nodes, keep input + addBtn as anchors.
    const toRemove = [];
    for (const child of Array.from(wrap.children || [])) {
      if (child.classList && child.classList.contains("pill")) toRemove.push(child);
    }
    for (const c of toRemove) wrap.removeChild(c);
    for (const entry of state.entries) {
      wrap.insertBefore(makePill(entry), input);
    }
  }

  function commit(rawText) {
    if (!rawText) return;
    // Paste handling: split on newlines so bulk paste makes N pills.
    const parts = rawText.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    let changed = false;
    for (const p of parts) {
      const lower = p.toLowerCase();
      if (state.entries.some((e) => e.toLowerCase() === lower)) continue;
      state.entries.push(p);
      changed = true;
    }
    if (changed) renderAll();
  }

  function removeEntry(entry) {
    const lower = entry.toLowerCase();
    const before = state.entries.length;
    state.entries = state.entries.filter((e) => e.toLowerCase() !== lower);
    if (state.entries.length < before) {
      renderAll();
      if (onRemove) onRemove(entry);
    }
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit(input.value);
      input.value = "";
    } else if (e.key === "Backspace" && input.value === "" && state.entries.length > 0) {
      e.preventDefault();
      const last = state.entries[state.entries.length - 1];
      removeEntry(last);
    }
  });

  input.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData)?.getData?.("text") ?? "";
    if (!text.includes("\n")) return; // single-line paste lands in input normally
    e.preventDefault();
    commit(text);
    input.value = "";
  });

  addBtn.addEventListener("click", () => {
    commit(input.value);
    input.value = "";
    input.focus();
  });

  // Seed wrap with input + addBtn, then render initial pills before input.
  wrap.appendChild(input);
  wrap.appendChild(addBtn);
  renderAll();

  return {
    getEntries: () => [...state.entries],
    addEntry: (text) => {
      if (typeof text !== "string" || !text.trim()) return;
      commit(text.trim());
    },
  };
}



// Pair variant: same chip-input shape but each entry is `{from, to}`.
// Pills render as "from → to" with ×. Input is a two-field row
// (from-input, to-input) + "+" button; Enter commits from either field.
// Paste with newlines: each line is split on "→" / "->" / "=>" to form
// pairs; lines without an arrow are ignored.
//
// mountPillPairList(container, { pairs, onRemove, fromPlaceholder, toPlaceholder })
// Returns { getPairs, addPair }.
export function mountPillPairList(container, {
  pairs = [],
  onRemove = null,
  fromPlaceholder = "from",
  toPlaceholder = "to",
} = {}) {
  const state = {
    pairs: dedupePairsPreserveCase(
      (pairs || []).filter((p) => p && typeof p.from === "string" && typeof p.to === "string" && p.from.trim())
        .map((p) => ({ from: p.from.trim(), to: p.to })),
    ),
  };

  container.innerHTML = "";
  container.classList.add("pill-list");

  const wrap = document.createElement("div");
  wrap.className = "pill-list-pills";
  container.appendChild(wrap);

  const fromInput = document.createElement("input");
  fromInput.type = "text";
  fromInput.className = "pill-input pill-input-from";
  fromInput.placeholder = fromPlaceholder;
  fromInput.spellcheck = false;

  const arrow = document.createElement("span");
  arrow.className = "pill-pair-arrow";
  arrow.textContent = "→";

  const toInput = document.createElement("input");
  toInput.type = "text";
  toInput.className = "pill-input pill-input-to";
  toInput.placeholder = toPlaceholder;
  toInput.spellcheck = false;

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn btn-sm pill-add-btn";
  addBtn.textContent = "+";
  addBtn.setAttribute("aria-label", "Add pair");

  function makePill(pair) {
    const pill = document.createElement("span");
    pill.className = "pill pill-pair";
    const label = document.createElement("span");
    label.className = "pill-label";
    label.textContent = `${pair.from} → ${pair.to}`;
    pill.appendChild(label);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "pill-remove";
    x.setAttribute("aria-label", `Remove ${pair.from}`);
    x.textContent = "×";
    x.addEventListener("click", () => removePair(pair.from));
    pill.appendChild(x);
    return pill;
  }

  function renderAll() {
    const toRemove = [];
    for (const child of Array.from(wrap.children || [])) {
      if (child.classList && child.classList.contains("pill")) toRemove.push(child);
    }
    for (const c of toRemove) wrap.removeChild(c);
    for (const pair of state.pairs) {
      wrap.insertBefore(makePill(pair), fromInput);
    }
  }

  function commitOne(fromRaw, toRaw) {
    const from = (fromRaw || "").trim();
    const to = (toRaw || "").trim();
    if (!from) return false;
    const lower = from.toLowerCase();
    if (state.pairs.some((p) => p.from.toLowerCase() === lower)) return false;
    state.pairs.push({ from, to });
    return true;
  }

  function commitFromInputs() {
    if (commitOne(fromInput.value, toInput.value)) {
      renderAll();
    }
    fromInput.value = "";
    toInput.value = "";
  }

  function removePair(fromKey) {
    const lower = fromKey.toLowerCase();
    const before = state.pairs.length;
    const removed = state.pairs.find((p) => p.from.toLowerCase() === lower);
    state.pairs = state.pairs.filter((p) => p.from.toLowerCase() !== lower);
    if (state.pairs.length < before) {
      renderAll();
      if (onRemove && removed) onRemove(removed);
    }
  }

  function commitPasted(raw) {
    if (!raw) return;
    const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    let changed = false;
    for (const line of lines) {
      const parts = line.split(/\s*(?:→|->|=>)\s*/);
      if (parts.length < 2) continue;
      if (commitOne(parts[0], parts.slice(1).join(" → "))) changed = true;
    }
    if (changed) renderAll();
  }

  const onKey = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitFromInputs();
    } else if (e.key === "Backspace" && e.target === fromInput && fromInput.value === "" && toInput.value === "" && state.pairs.length > 0) {
      e.preventDefault();
      const last = state.pairs[state.pairs.length - 1];
      removePair(last.from);
    }
  };
  fromInput.addEventListener("keydown", onKey);
  toInput.addEventListener("keydown", onKey);

  fromInput.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData)?.getData?.("text") ?? "";
    if (!text.includes("\n") || !/→|->|=>/.test(text)) return;
    e.preventDefault();
    commitPasted(text);
  });

  addBtn.addEventListener("click", () => {
    commitFromInputs();
    fromInput.focus();
  });

  wrap.appendChild(fromInput);
  wrap.appendChild(arrow);
  wrap.appendChild(toInput);
  wrap.appendChild(addBtn);
  renderAll();

  return {
    getPairs: () => state.pairs.map((p) => ({ from: p.from, to: p.to })),
    addPair: (pair) => {
      if (!pair || typeof pair.from !== "string" || typeof pair.to !== "string") return;
      if (commitOne(pair.from, pair.to)) renderAll();
    },
  };
}

function dedupePairsPreserveCase(arr) {
  const seen = new Set();
  const out = [];
  for (const p of arr) {
    const lower = p.from.toLowerCase();
    if (seen.has(lower)) continue;
    seen.add(lower);
    out.push(p);
  }
  return out;
}

function dedupePreserveCase(arr) {
  const seen = new Set();
  const out = [];
  for (const s of arr) {
    const lower = s.toLowerCase();
    if (seen.has(lower)) continue;
    seen.add(lower);
    out.push(s);
  }
  return out;
}
