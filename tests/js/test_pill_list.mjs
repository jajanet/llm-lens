import { test } from "node:test";
import assert from "node:assert/strict";

// Minimal DOM shim sufficient for the pill_list component. Supports:
// innerHTML, classList, appendChild, insertBefore, removeChild,
// querySelector (class/id/tag), addEventListener, dataset, setAttribute,
// children.
let domCounter = 0;

function makeElement(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    parent: null,
    _id: null,
    _listeners: {},
    _classes: new Set(),
    _attrs: {},
    dataset: {},
    textContent: "",
    _innerHTML: "",
    type: "",
    placeholder: "",
    spellcheck: true,
    disabled: false,
    value: "",
    _domId: ++domCounter,
  };
  Object.defineProperty(el, "className", {
    get() { return [...el._classes].join(" "); },
    set(v) {
      el._classes = new Set();
      for (const c of String(v || "").split(/\s+/)) if (c) el._classes.add(c);
    },
  });
  el.classList = {
    add: (c) => el._classes.add(c),
    remove: (c) => el._classes.delete(c),
    contains: (c) => el._classes.has(c),
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._innerHTML; },
    set(v) {
      el._innerHTML = v;
      if (v === "") el.children = [];
    },
  });
  el.appendChild = (c) => {
    if (c.parent) c.parent.removeChild(c);
    c.parent = el;
    el.children.push(c);
    return c;
  };
  el.insertBefore = (c, ref) => {
    if (c.parent) c.parent.removeChild(c);
    c.parent = el;
    const idx = el.children.indexOf(ref);
    if (idx < 0) el.children.push(c);
    else el.children.splice(idx, 0, c);
    return c;
  };
  el.removeChild = (c) => {
    const idx = el.children.indexOf(c);
    if (idx >= 0) el.children.splice(idx, 1);
    c.parent = null;
    return c;
  };
  el.addEventListener = (ev, handler) => {
    (el._listeners[ev] ||= []).push(handler);
  };
  el.removeEventListener = (ev, handler) => {
    el._listeners[ev] = (el._listeners[ev] || []).filter((h) => h !== handler);
  };
  el.setAttribute = (k, v) => { el._attrs[k] = v; };
  el.getAttribute = (k) => el._attrs[k];
  el.focus = () => {};
  el.querySelector = (sel) => {
    const match = (node) => {
      if (!node.classList) return false;
      if (sel.startsWith(".")) return node._classes && node._classes.has(sel.slice(1));
      if (sel.startsWith("#")) return node._id === sel.slice(1);
      return node.tagName === sel.toUpperCase();
    };
    const walk = (n) => {
      for (const child of n.children) {
        if (match(child)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(el);
  };
  return el;
}

function fire(el, ev, data = {}) {
  for (const h of el._listeners[ev] || []) h({ target: el, preventDefault: () => {}, ...data });
}

globalThis.document = {
  createElement: makeElement,
  createTextNode: (t) => ({ textContent: t }),
};

const { mountPillList } = await import("../../llm_lens/static/js/pill_list.js");

function pillsWrap(container) { return container.querySelector(".pill-list-pills"); }
function pillNodes(container) {
  return pillsWrap(container).children.filter((c) => c._classes && c._classes.has("pill"));
}
function pillLabels(container) {
  return pillNodes(container).map((p) => {
    const label = p.children.find((c) => c._classes && c._classes.has("pill-label"));
    return label ? label.textContent : "";
  });
}
function getInput(container) { return container.querySelector(".pill-input"); }
function getAddBtn(container) { return container.querySelector(".pill-add-btn"); }
function clickRemove(container, pillIndex) {
  const pill = pillNodes(container)[pillIndex];
  const x = pill.children.find((c) => c._classes && c._classes.has("pill-remove"));
  fire(x, "click");
}

test("mountPillList renders initial entries as pills", () => {
  const container = makeElement("div");
  mountPillList(container, { entries: ["one", "two", "three"] });
  assert.equal(pillNodes(container).length, 3);
  assert.deepEqual(pillLabels(container), ["one", "two", "three"]);
});

test("input + add button live inline inside pill-list-pills (chip-input layout)", () => {
  const container = makeElement("div");
  mountPillList(container, { entries: ["one"] });
  const wrap = pillsWrap(container);
  // Order: pill, input, addBtn — input/button are the last two children,
  // pill sits before them so adding more pills visibly appends in front
  // of the input.
  const last = wrap.children[wrap.children.length - 1];
  const secondLast = wrap.children[wrap.children.length - 2];
  assert.ok(last._classes.has("pill-add-btn"));
  assert.ok(secondLast._classes.has("pill-input"));
});

test("Enter on input commits a new pill, pill appears before input", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: ["one"] });
  const input = getInput(container);
  input.value = "two";
  fire(input, "keydown", { key: "Enter" });
  assert.deepEqual(m.getEntries(), ["one", "two"]);
  // Input is still the second-to-last child.
  const wrap = pillsWrap(container);
  const secondLast = wrap.children[wrap.children.length - 2];
  assert.ok(secondLast._classes.has("pill-input"));
});

test("Plus button commits a new pill", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: [] });
  const input = getInput(container);
  input.value = "via button";
  fire(getAddBtn(container), "click");
  assert.deepEqual(m.getEntries(), ["via button"]);
});

test("× click removes the pill and fires onRemove", () => {
  const container = makeElement("div");
  const removed = [];
  const m = mountPillList(container, {
    entries: ["a", "b", "c"],
    onRemove: (e) => removed.push(e),
  });
  clickRemove(container, 1);
  assert.deepEqual(m.getEntries(), ["a", "c"]);
  assert.deepEqual(removed, ["b"]);
});

test("Backspace on empty input removes the last pill and fires onRemove", () => {
  const container = makeElement("div");
  const removed = [];
  const m = mountPillList(container, {
    entries: ["a", "b", "c"],
    onRemove: (e) => removed.push(e),
  });
  const input = getInput(container);
  input.value = "";
  fire(input, "keydown", { key: "Backspace" });
  assert.deepEqual(m.getEntries(), ["a", "b"]);
  assert.deepEqual(removed, ["c"]);
});

test("Backspace on non-empty input does NOT remove a pill", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: ["a", "b"] });
  const input = getInput(container);
  input.value = "typing";
  fire(input, "keydown", { key: "Backspace" });
  assert.deepEqual(m.getEntries(), ["a", "b"]);
});

test("Case-insensitive dedupe: adding 'Benchmark' when 'benchmark' exists is a no-op", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: ["benchmark"] });
  const input = getInput(container);
  input.value = "Benchmark";
  fire(input, "keydown", { key: "Enter" });
  assert.deepEqual(m.getEntries(), ["benchmark"]);
});

test("Case is preserved as typed (matching is case-insensitive elsewhere)", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: [] });
  const input = getInput(container);
  input.value = "Benchmark";
  fire(input, "keydown", { key: "Enter" });
  assert.deepEqual(m.getEntries(), ["Benchmark"]);
});

test("Paste with newlines commits multiple pills", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: [] });
  const input = getInput(container);
  fire(input, "paste", {
    clipboardData: { getData: () => "one\ntwo\nthree" },
  });
  assert.deepEqual(m.getEntries(), ["one", "two", "three"]);
});

test("getEntries returns a copy — mutations don't leak into the component state", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: ["a", "b"] });
  const entries = m.getEntries();
  entries.push("c");
  assert.deepEqual(m.getEntries(), ["a", "b"]);
});

test("addEntry programmatic add works (used by custom_filter → whitelist move)", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: [] });
  m.addEntry("programmatic");
  assert.deepEqual(m.getEntries(), ["programmatic"]);
});

test("Trims whitespace on commit", () => {
  const container = makeElement("div");
  const m = mountPillList(container, { entries: [] });
  const input = getInput(container);
  input.value = "   padded entry   ";
  fire(input, "keydown", { key: "Enter" });
  assert.deepEqual(m.getEntries(), ["padded entry"]);
});



const { mountPillPairList } = await import("../../llm_lens/static/js/pill_list.js");

function pairLabels(container) {
  return pillNodes(container).map((p) => {
    const label = p.children.find((c) => c._classes && c._classes.has("pill-label"));
    return label ? label.textContent : "";
  });
}

test("mountPillPairList renders initial pairs as 'from → to' pills", () => {
  const container = makeElement("div");
  mountPillPairList(container, { pairs: [{ from: "w/", to: "with" }, { from: "ty", to: "thank you" }] });
  assert.deepEqual(pairLabels(container), ["w/ → with", "ty → thank you"]);
});

test("mountPillPairList Enter in from-input with to-input filled commits pair", () => {
  const container = makeElement("div");
  const m = mountPillPairList(container, { pairs: [] });
  const fromInput = container.querySelector(".pill-input-from");
  const toInput = container.querySelector(".pill-input-to");
  fromInput.value = "w/o";
  toInput.value = "without";
  fire(fromInput, "keydown", { key: "Enter" });
  assert.deepEqual(m.getPairs(), [{ from: "w/o", to: "without" }]);
});

test("mountPillPairList × click removes the pair and fires onRemove", () => {
  const container = makeElement("div");
  const removed = [];
  const m = mountPillPairList(container, {
    pairs: [{ from: "a", to: "b" }, { from: "c", to: "d" }],
    onRemove: (p) => removed.push(p),
  });
  const pill = pillNodes(container)[0];
  const x = pill.children.find((c) => c._classes.has("pill-remove"));
  fire(x, "click");
  assert.deepEqual(m.getPairs(), [{ from: "c", to: "d" }]);
  assert.deepEqual(removed, [{ from: "a", to: "b" }]);
});

test("mountPillPairList case-insensitive dedupe on commit", () => {
  const container = makeElement("div");
  const m = mountPillPairList(container, { pairs: [{ from: "you", to: "u" }] });
  const fromInput = container.querySelector(".pill-input-from");
  const toInput = container.querySelector(".pill-input-to");
  fromInput.value = "YOU";
  toInput.value = "u2";
  fire(fromInput, "keydown", { key: "Enter" });
  assert.deepEqual(m.getPairs(), [{ from: "you", to: "u" }]);
});

test("mountPillPairList paste with newline-delimited 'from → to' lines commits multiple pairs", () => {
  const container = makeElement("div");
  const m = mountPillPairList(container, { pairs: [] });
  const fromInput = container.querySelector(".pill-input-from");
  fire(fromInput, "paste", {
    clipboardData: { getData: () => "foo → bar\nbaz -> qux\nnoarrow" },
  });
  assert.deepEqual(m.getPairs(), [
    { from: "foo", to: "bar" },
    { from: "baz", to: "qux" },
  ]);
});
