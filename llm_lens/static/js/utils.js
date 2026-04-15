// Small pure utilities.

export function timeAgo(iso) {
  if (!iso) return "";
  const sec = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (sec < 60) return "now";
  if (sec < 3600) return Math.floor(sec / 60) + "m";
  if (sec < 86400) return Math.floor(sec / 3600) + "h";
  if (sec < 604800) return Math.floor(sec / 86400) + "d";
  return new Date(iso).toLocaleDateString();
}

export function timeAbs(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

export function fmtSize(kb) {
  return kb > 1024 ? (kb / 1024).toFixed(1) + " MB" : Math.round(kb) + " KB";
}

// Compact tokens: 12,345 -> "12.3k", 1,234,567 -> "1.2M". Real counts from
// message.usage, so "k" and "M" are decimal (1000), not binary.
export function fmtTokens(n) {
  if (n == null) return "";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}



// Anthropic Claude API pricing per 1M tokens (USD). Captured 2026-04-14 from
// https://claude.com/pricing. Cache writes assume the 5-minute TTL (default);
// 1-hour cache writes cost more but `message.usage.cache_creation_input_tokens`
// doesn't tell us the TTL, so we can't distinguish. Update this table when
// pricing or the API surface changes. Order matters — more-specific matchers
// (e.g. opus-4-6) come before more-general ones (opus-4).
const PRICING = [
  { match: /opus-4-6/,   input: 5,    output: 25,   cache_write: 6.25,  cache_read: 0.50 },
  { match: /sonnet-4-6/, input: 3,    output: 15,   cache_write: 3.75,  cache_read: 0.30 },
  { match: /haiku-4-5/,  input: 1,    output: 5,    cache_write: 1.25,  cache_read: 0.10 },
  { match: /opus-4-5/,   input: 5,    output: 25,   cache_write: 6.25,  cache_read: 0.50 },
  { match: /sonnet-4-5/, input: 3,    output: 15,   cache_write: 3.75,  cache_read: 0.30 },
  { match: /opus-4-1/,   input: 15,   output: 75,   cache_write: 18.75, cache_read: 1.50 },
  { match: /opus-4/,     input: 15,   output: 75,   cache_write: 18.75, cache_read: 1.50 },
  { match: /sonnet-4/,   input: 3,    output: 15,   cache_write: 3.75,  cache_read: 0.30 },
  { match: /haiku-3/,    input: 0.25, output: 1.25, cache_write: 0.30,  cache_read: 0.03 },
];

export const COST_ASSUMPTION_NOTE =
  "Cost assumes 5-minute cache writes (1-hour writes cost more; API usage doesn't distinguish).";
export const PRICING_CAPTURED = "2026-04-14";
export const PRICING_SOURCE_NOTE =
  `Pricing captured ${PRICING_CAPTURED} from claude.com/pricing — update PRICING in utils.js when rates change.`;

function priceFor(model) {
  if (!model) return null;
  for (const p of PRICING) if (p.match.test(model)) return p;
  return null;
}

// USD cost for a single (model, stats-bundle) pair. `stats` keys are the
// same as elsewhere — input_tokens / output_tokens / cache_read_tokens /
// cache_creation_tokens. Returns null if model isn't priced.
function costOf(stats, model) {
  const p = priceFor(model);
  if (!p || !stats) return null;
  const M = 1_000_000;
  return (
    (stats.input_tokens || 0) * p.input / M +
    (stats.output_tokens || 0) * p.output / M +
    (stats.cache_creation_tokens || 0) * p.cache_write / M +
    (stats.cache_read_tokens || 0) * p.cache_read / M
  );
}

// Cost across all models for one token-type (e.g. "input_tokens"). Summed
// using each model's rate, so at project/overview scope the number reflects
// the actual blend of models used.
function costByType(s, type) {
  const pm = (s && s.per_model) || {};
  const priceField = {
    input_tokens: "input",
    output_tokens: "output",
    cache_creation_tokens: "cache_write",
    cache_read_tokens: "cache_read",
  }[type];
  if (!priceField) return null;
  let total = 0;
  let anyPriced = false;
  for (const [m, mstats] of Object.entries(pm)) {
    const p = priceFor(m);
    if (!p) continue;
    total += (mstats[type] || 0) * p[priceField] / 1_000_000;
    anyPriced = true;
  }
  // Fallback: convo-level s with no per_model but a single known model.
  if (!anyPriced && s && s.models && s.models.length === 1) {
    const p = priceFor(s.models[0]);
    if (p) return (s[type] || 0) * p[priceField] / 1_000_000;
  }
  return anyPriced ? total : null;
}

function totalCost(s) {
  let t = 0;
  let any = false;
  for (const type of ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"]) {
    const c = costByType(s, type);
    if (c != null) { t += c; any = true; }
  }
  return any ? t : null;
}

export function fmtCost(c) {
  if (c == null) return "—";
  if (c === 0) return "$0";
  if (c < 0.01) return "< $0.01";
  if (c < 1) return `$${c.toFixed(3)}`;
  if (c < 100) return `$${c.toFixed(2)}`;
  return `$${Math.round(c).toLocaleString()}`;
}

// Strip the claude- prefix so "claude-sonnet-4-6" reads as "sonnet-4-6";
// keeps multi-model lists readable without losing disambiguation.
export function shortModel(m) {
  if (!m) return "";
  return m.replace(/^claude-/, "");
}


// Compact stat strip used in both conversations and projects card views.
// Accepts either shape:
//   convo: { input_tokens, cache_read_tokens, cache_creation_tokens, output_tokens,
//            tool_uses: {name: n}, thinking_count, git_branch, models: [...] }
//   project: same keys, plus optional `branches: [...]` (union across convos)
export function renderStatsInline(s, opts) {
  if (!s) return '<span class="stats-dim">no data</span>';
  opts = opts || {};
  const archived = (opts.includeArchived && s.archived_delta) ? s.archived_delta : null;
  const deleted  = (opts.includeDeleted  && s.deleted_delta)  ? s.deleted_delta  : null;

  const add = (k) => (s[k] || 0)
    + (archived ? (archived[k] || 0) : 0)
    + (deleted  ? (deleted[k]  || 0) : 0);
  const totalIn = add("input_tokens") + add("cache_read_tokens") + add("cache_creation_tokens");
  const sumTools = (obj) => Object.values(obj || {}).reduce((a, b) => a + b, 0);
  const toolTotal = sumTools(s.tool_uses)
                  + (archived ? sumTools(archived.tool_uses) : 0)
                  + (deleted  ? sumTools(deleted.tool_uses)  : 0);

  const parts = [];
  parts.push(`<span class="stats-pair"><span class="k">in</span> ${fmtTokens(totalIn)}</span>`);
  parts.push(`<span class="stats-pair"><span class="k">out</span> ${fmtTokens(add("output_tokens"))}</span>`);
  if (toolTotal) parts.push(`<span class="stats-pair"><span class="k">tools</span> ${toolTotal}</span>`);
  const think = add("thinking_count");
  if (think) parts.push(`<span class="stats-pair"><span class="k">think</span> ${think}</span>`);

  if (s.branches && s.branches.length) {
    const first = esc(s.branches[0]);
    const more = s.branches.length > 1 ? ` <span class="stats-more">+${s.branches.length - 1}</span>` : "";
    parts.push(`<span class="stats-pair"><span class="k">branch</span> ${first}${more}</span>`);
  } else if (s.git_branch) {
    parts.push(`<span class="stats-pair"><span class="k">branch</span> ${esc(s.git_branch)}</span>`);
  }

  if (s.models && s.models.length) {
    const first = esc(shortModel(s.models[0]));
    const more = s.models.length > 1 ? ` <span class="stats-more">+${s.models.length - 1}</span>` : "";
    parts.push(`<span class="stats-pair"><span class="k">model</span> ${first}${more}</span>`);
  }
  const inclBits = [archived && "archived", deleted && "deleted"].filter(Boolean);
  if (inclBits.length) {
    parts.push(`<span class="stats-pair stats-incl-deleted">(incl. ${inclBits.join(" + ")})</span>`);
  }
  return parts.join("");
}


// Inline-SVG bar chart of daily activity. `byDay` is `{YYYY-MM-DD: {tokens, ...}}`.
// For ranged views we zero-fill missing days so sparse activity doesn't collapse
// the x-axis. For "all", we only draw days that have data.
// Stacked-bar chart of token usage. `byPeriod` is `{bucketKey: {input_tokens,
// cache_read_tokens, cache_creation_tokens, output_tokens, tool_calls, convos}}`.
// `bucket` is one of: year, month, week, day, hour — drives x-axis formatting
// and zero-fill rules.
//
// Stack order (bottom→top): cache_read, cache_creation, input, output.
// Bottom-up reflects how "noisy" each band is (cache reads dominate height;
// output is the smallest but most meaningful — placed on top so it's always
// visible).
// Stacked-bar chart. Supports two modes:
//   mode="tokens": 4 segments — cache_read, cache_creation, input, output
//   mode="tools":  one segment per top-N tool name, rest lumped as "other"
// `bucket` drives axis formatting; `size` scales SVG height (compact vs expanded).
// `groupBy="model"` splits each period into up to 3 sub-bars, one per top model
// (by total across the visible window). Intra-group gap < inter-group gap so
// each period reads as a cluster.
// Merge each bucket's active/archived_delta/deleted_delta sub-stats into a
// single flat bucket according to the filter toggles. archived/deleted deltas
// don't carry per_model breakdowns, so model-grouped bars only reflect the
// "active" source — drop per_model when active is off to avoid misleading bars.
export function applyBucketFilters(byPeriod, f) {
  const TOK = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"];
  const out = {};
  for (const [k, b] of Object.entries(byPeriod || {})) {
    if (!b) continue;
    const a = b.archived_delta || {};
    const d = b.deleted_delta  || {};
    const merged = {};
    for (const t of TOK) {
      merged[t] = (f.active   ? (b[t] || 0) : 0)
                + (f.archived ? (a[t] || 0) : 0)
                + (f.deleted  ? (d[t] || 0) : 0);
    }
    const tools = {};
    const addTools = (obj) => {
      for (const [n, c] of Object.entries(obj || {})) tools[n] = (tools[n] || 0) + c;
    };
    if (f.active)   addTools(b.tool_uses);
    if (f.archived) addTools(a.tool_uses);
    if (f.deleted)  addTools(d.tool_uses);
    merged.tool_uses = tools;
    merged.tool_calls = Object.values(tools).reduce((s, v) => s + v, 0);
    merged.convos = b.convos || 0;
    merged.per_model = f.active ? (b.per_model || {}) : {};
    out[k] = merged;
  }
  return out;
}

export function renderTokenBars(byPeriod, range, bucket, mode, size, untilIso, groupBy, filters) {
  byPeriod = byPeriod || {};
  bucket = bucket || "day";
  mode = mode || "tokens";
  size = size || "compact";
  groupBy = groupBy || "none";
  const f = filters || { active: true, archived: false, deleted: false };
  byPeriod = applyBucketFilters(byPeriod, f);
  const anchor = untilIso ? new Date(untilIso) : new Date();
  // "Current" window: anchor is end-of-current-bucket (≥ now for offset=0).
  // At negative offsets the anchor has already passed — switch to absolute
  // labels so "Nh ago" doesn't read relative to a frozen past window.
  const isCurrent = !untilIso || anchor.getTime() >= Date.now() - 60_000;

  let keys = buildPeriodKeys(byPeriod, range, bucket, anchor);
  const totalOf = (b) => {
    if (!b) return 0;
    if (mode === "tools") return Object.values(b.tool_uses || {}).reduce((s, v) => s + v, 0);
    return (b.input_tokens || 0) + (b.cache_read_tokens || 0) +
           (b.cache_creation_tokens || 0) + (b.output_tokens || 0);
  };
  const nonEmpty = keys.filter((k) => totalOf(byPeriod[k]) > 0);
  if (nonEmpty.length === 0) {
    const metric = mode === "tools" ? "tool calls" : "tokens";
    // Wrap in `.overview-graph` so the redraw selector still finds this node
    // on the next nav — otherwise the empty state becomes a dead-end.
    return `<div class="overview-graph ov-size-${size}"><div class="overview-empty">no ${metric} in this range</div></div>`;
  }
  keys = nonEmpty;

  const SEGMENTS = mode === "tokens"
    ? [
        { key: "cache_read_tokens",     cls: "seg-cread",  label: "cache read"  },
        { key: "cache_creation_tokens", cls: "seg-cwrite", label: "cache write" },
        { key: "input_tokens",          cls: "seg-input",  label: "input"       },
        { key: "output_tokens",         cls: "seg-output", label: "output"      },
      ]
    : buildToolSegments(keys, byPeriod);

  const topModels = groupBy === "model" ? pickTopModels(keys, byPeriod, mode, 3) : [];
  const nSub = groupBy === "model" ? Math.max(1, topModels.length) : 1;

  // Scale height to the tallest single (sub-)bar, not the full-bucket total —
  // so grouped-model bars don't squash when one bucket has huge combined use.
  let maxV = 1;
  if (groupBy === "model" && topModels.length) {
    for (const k of keys) {
      const pm = (byPeriod[k] || {}).per_model || {};
      for (const m of topModels) {
        const v = totalOf(pm[m]);
        if (v > maxV) maxV = v;
      }
    }
  } else {
    for (const k of keys) {
      const v = totalOf(byPeriod[k]);
      if (v > maxV) maxV = v;
    }
  }

  const H = size === "expanded" ? 540 : 120;
  const W = 600, padL = 4, padR = 4, padT = 6, padB = 18;
  const innerH = H - padT - padB;
  const innerW = W - padL - padR;
  const groupW = innerW / keys.length;
  const interGroupGap = 1.5;
  const modelGap = groupBy === "model" ? 1 : 0;
  const subBarW = Math.max(1, (groupW - interGroupGap - (nSub - 1) * modelGap) / nSub);

  const drawSubBar = (stats, x, modelLabel, periodKey) => {
    stats = stats || {};
    let cumulative = 0;
    let segHtml = "";
    for (const seg of SEGMENTS) {
      const v = mode === "tools"
        ? (seg.key === "__other__" ? otherCount(stats, SEGMENTS) : (stats.tool_uses || {})[seg.key] || 0)
        : (stats[seg.key] || 0);
      if (v <= 0) continue;
      const hSeg = (v / maxV) * innerH;
      const y = padT + innerH - cumulative - hSeg;
      const fill = seg.fill ? ` style="fill:${seg.fill}"` : "";
      segHtml += `<rect class="ov-bar ${seg.cls}"${fill} x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${subBarW.toFixed(2)}" height="${hSeg.toFixed(2)}"></rect>`;
      cumulative += hSeg;
    }
    const tip = modelLabel
      ? formatModelTooltip(periodKey, modelLabel, stats, mode, bucket, SEGMENTS)
      : formatTooltip(periodKey, stats, mode, bucket, SEGMENTS);
    const hit = `<rect class="ov-hit" x="${x.toFixed(2)}" y="${padT}" width="${subBarW.toFixed(2)}" height="${innerH}" fill="transparent"></rect>`;
    return `<g class="ov-bar-group" data-tip="${escAttr(tip)}">${segHtml}${hit}</g>`;
  };

  const bars = keys.map((k, i) => {
    const groupX = padL + i * groupW;
    if (groupBy === "model" && topModels.length) {
      const pm = (byPeriod[k] || {}).per_model || {};
      let subs = "";
      for (let j = 0; j < topModels.length; j++) {
        const m = topModels[j];
        const x = groupX + j * (subBarW + modelGap) + 0.5;
        subs += drawSubBar(pm[m], x, shortModel(m), k);
      }
      return subs;
    }
    const x = groupX + 0.5;
    return drawSubBar(byPeriod[k] || {}, x, "", k);
  }).join("");

  const axisLabels = pickAxisLabels(keys, bucket, anchor, isCurrent);
  const labelsHtml = axisLabels.map(({ idx, text }) => {
    const x = padL + idx * groupW + groupW / 2;
    return `<text x="${x.toFixed(2)}" y="${(H - 4).toFixed(2)}" text-anchor="middle" class="ov-axis-label">${esc(text)}</text>`;
  }).join("");

  const maxLbl = `<text x="${padL}" y="${(padT + 8).toFixed(2)}" class="ov-axis-label ov-max-label">${esc(mode === "tools" ? String(maxV) : fmtTokens(maxV))}</text>`;

  const segLegend = SEGMENTS.slice().reverse().map((seg) => {
    const sw = seg.fill
      ? `<span class="ov-legend-sw" style="background:${seg.fill}"></span>`
      : `<span class="ov-legend-sw ${seg.cls}"></span>`;
    return `<span class="ov-legend-item">${sw}${esc(seg.label)}</span>`;
  }).join("");

  const modelLegend = (groupBy === "model" && topModels.length)
    ? `<div class="ov-legend ov-model-legend">${topModels.map((m, i) =>
        `<span class="ov-legend-item"><span class="ov-legend-pos">${i + 1}</span>${esc(shortModel(m))}</span>`
      ).join("")}</div>`
    : "";

  return `
    <div class="overview-graph ov-size-${size}${groupBy === "model" ? " ov-grouped-model" : ""}">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="overview-bars ov-size-${size}">
        ${maxLbl}${bars}${labelsHtml}
      </svg>
      ${modelLegend}
      <div class="ov-legend">${segLegend}</div>
      <div class="ov-tip" style="display:none"></div>
    </div>`;
}

// Top-N models by total (tokens or tool calls, matching `mode`) across the
// visible window. Stable ordering so sub-bar positions don't shuffle between
// re-renders of the same window.
function pickTopModels(keys, byPeriod, mode, n) {
  const totals = {};
  for (const k of keys) {
    const pm = (byPeriod[k] || {}).per_model || {};
    for (const [m, s] of Object.entries(pm)) {
      const v = mode === "tools"
        ? Object.values(s.tool_uses || {}).reduce((a, b) => a + b, 0)
        : (s.input_tokens || 0) + (s.output_tokens || 0) +
          (s.cache_read_tokens || 0) + (s.cache_creation_tokens || 0);
      totals[m] = (totals[m] || 0) + v;
    }
  }
  return Object.entries(totals)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([m]) => m);
}

function formatModelTooltip(key, model, s, mode, bucket, segments) {
  const hdr = `<div class="ov-tip-hdr">${esc(model)} · ${esc(formatBucketHeader(key, bucket))}</div>`;
  const row = (label, val, cls) =>
    `<div class="ov-tip-row ${cls || ""}"><span class="ov-tip-k">${label}</span><span class="ov-tip-v">${val}</span></div>`;
  const swatch = (seg) => seg
    ? (seg.fill
        ? `<span class="seg-swatch" style="background:${seg.fill}"></span>`
        : `<span class="seg-swatch ${seg.cls}"></span>`)
    : "";

  if (mode === "tools") {
    const segByKey = new Map();
    let otherSeg = null;
    for (const seg of segments || []) {
      if (seg.key === "__other__") otherSeg = seg;
      else segByKey.set(seg.key, seg);
    }
    const entries = Object.entries(s.tool_uses || {}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((n, [, c]) => n + c, 0);
    const rows = entries.length
      ? entries.map(([n, c]) => row(swatch(segByKey.get(n) || otherSeg) + esc(n), c)).join("")
      : row('<span class="stats-dim">no tools</span>', "");
    return hdr + rows + row("total", total, "ov-tip-total");
  }

  const totalTokens = (s.input_tokens || 0) + (s.cache_read_tokens || 0) +
                      (s.cache_creation_tokens || 0) + (s.output_tokens || 0);
  return hdr +
    row('<span class="seg-swatch seg-output"></span>output', fmtTokens(s.output_tokens || 0)) +
    row('<span class="seg-swatch seg-input"></span>input', fmtTokens(s.input_tokens || 0)) +
    row('<span class="seg-swatch seg-cwrite"></span>cache write', fmtTokens(s.cache_creation_tokens || 0)) +
    row('<span class="seg-swatch seg-cread"></span>cache read', fmtTokens(s.cache_read_tokens || 0)) +
    row("total", fmtTokens(totalTokens), "ov-tip-total");
}

// Pick top-N tools across the whole range; anything outside lumps into "other".
// Colors come from a fixed palette — deterministic per rank so the legend reads
// the same way each render.
const TOOL_PALETTE = ["#58a6ff", "#f78166", "#3fb950", "#d2a8ff", "#f1e05a", "#ff7b72"];
function buildToolSegments(keys, byPeriod) {
  const totals = {};
  for (const k of keys) {
    for (const [name, count] of Object.entries((byPeriod[k] || {}).tool_uses || {})) {
      totals[name] = (totals[name] || 0) + count;
    }
  }
  const sorted = Object.entries(totals).sort((a, b) => b[1] - a[1]);
  const topN = sorted.slice(0, TOOL_PALETTE.length);
  const rest = sorted.slice(TOOL_PALETTE.length);
  const segs = topN.map(([name], i) => ({
    key: name, cls: `seg-tool-${i}`, fill: TOOL_PALETTE[i], label: name,
  }));
  if (rest.length) {
    segs.push({ key: "__other__", cls: "seg-tool-other", fill: "var(--text3)", label: `other (${rest.length})` });
  }
  // Reverse so top-N end up drawn last (on top of stack).
  return segs.reverse();
}

function otherCount(bucket, segs) {
  const named = new Set(segs.filter((s) => s.key !== "__other__").map((s) => s.key));
  let n = 0;
  for (const [name, count] of Object.entries(bucket.tool_uses || {})) {
    if (!named.has(name)) n += count;
  }
  return n;
}

// Returns an HTML snippet (line-separated <div>s). Placed inside a tooltip
// div on hover. Tool names / bucket keys are esc()'d since they come from
// user data (JSONL content).
// Human-readable header for a bucket key — differs by granularity so e.g.
// an hour tooltip says "Mon 3 PM · Apr 14" instead of the raw ISO-ish key.
function formatBucketHeader(key, bucket) {
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  if (bucket === "hour") {
    const [ymd, hr] = key.split(" ");
    const [y, m, d] = ymd.split("-").map(Number);
    const dt = new Date(y, m - 1, d, parseInt(hr, 10));
    const h = dt.getHours();
    const hhLabel = h === 0 ? "12 AM" : h < 12 ? `${h} AM` : h === 12 ? "12 PM" : `${h - 12} PM`;
    return `${DOW[dt.getDay()]} ${hhLabel} · ${MON[m - 1]} ${d}`;
  }
  if (bucket === "day") {
    const [y, m, d] = key.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    return `${DOW[dt.getDay()]} · ${MON[m - 1]} ${d}`;
  }
  if (bucket === "week") {
    const [y, w] = key.split("-W");
    return `Week ${parseInt(w, 10)} · ${y}`;
  }
  if (bucket === "month") {
    const [y, m] = key.split("-");
    return `${MON[parseInt(m, 10) - 1]} ${y}`;
  }
  if (bucket === "year") return key;
  return key;
}

function formatTooltip(key, b, mode, bucket, segments) {
  const hdr = `<div class="ov-tip-hdr">${esc(formatBucketHeader(key, bucket))}</div>`;
  const row = (label, val, cls) =>
    `<div class="ov-tip-row ${cls || ""}"><span class="ov-tip-k">${label}</span><span class="ov-tip-v">${val}</span></div>`;
  const swatch = (seg) => seg
    ? (seg.fill
        ? `<span class="seg-swatch" style="background:${seg.fill}"></span>`
        : `<span class="seg-swatch ${seg.cls}"></span>`)
    : "";

  if (mode === "tools") {
    const segByKey = new Map();
    let otherSeg = null;
    for (const seg of segments || []) {
      if (seg.key === "__other__") otherSeg = seg;
      else segByKey.set(seg.key, seg);
    }
    const entries = Object.entries(b.tool_uses || {}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((n, [, c]) => n + c, 0);
    const rows = entries.length
      ? entries.map(([n, c]) => row(swatch(segByKey.get(n) || otherSeg) + esc(n), c)).join("")
      : row('<span class="stats-dim">no tools</span>', "");
    return hdr + rows +
      row("total", total, "ov-tip-total") +
      row('<span class="stats-dim">convos</span>', (b.convos || 0));
  }

  const totalTokens = (b.input_tokens || 0) + (b.cache_read_tokens || 0) +
                      (b.cache_creation_tokens || 0) + (b.output_tokens || 0);
  return hdr +
    row('<span class="seg-swatch seg-output"></span>output', fmtTokens(b.output_tokens || 0)) +
    row('<span class="seg-swatch seg-input"></span>input', fmtTokens(b.input_tokens || 0)) +
    row('<span class="seg-swatch seg-cwrite"></span>cache write', fmtTokens(b.cache_creation_tokens || 0)) +
    row('<span class="seg-swatch seg-cread"></span>cache read', fmtTokens(b.cache_read_tokens || 0)) +
    row("total", fmtTokens(totalTokens), "ov-tip-total") +
    row('<span class="stats-dim">tool calls · convos</span>', `${b.tool_calls || 0} · ${b.convos || 0}`);
}

// Zero-fill period keys so bars are evenly spaced even when some buckets are
// empty. Returns the ordered list of keys to render as bars.
// Zero-fill period keys so bars are evenly spaced even when some buckets are
// empty. `anchor` is the right edge of the window (defaults to now for
// offset=0). Shifting the anchor is how prev/next nav works.
function buildPeriodKeys(byPeriod, range, bucket, anchor) {
  // Anchor is the *exclusive* upper bound of the window (end of the last
  // included bucket). Step back 1ms so local-time getters land inside that
  // last bucket rather than the one after it.
  const now = anchor
    ? new Date(anchor.getTime() - 1)
    : new Date(Date.now() - 1);
  const pad2 = (n) => String(n).padStart(2, "0");

  if (bucket === "hour") {
    const keys = [];
    for (let i = 23; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 3600 * 1000);
      keys.push(`${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}`);
    }
    return keys;
  }
  if (bucket === "day") {
    const n = range === "month" ? 30 : 7;
    const keys = [];
    for (let i = n - 1; i >= 0; i--) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      keys.push(`${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`);
    }
    return keys;
  }
  if (bucket === "week") {
    const keys = [];
    for (let i = 4; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 7 * 86400 * 1000);
      const { year, week } = isoWeek(d);
      keys.push(`${year}-W${pad2(week)}`);
    }
    return Array.from(new Set(keys));
  }
  if (bucket === "month") {
    const keys = [];
    for (let i = 11; i >= 0; i--) {
      const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      keys.push(`${d.getFullYear()}-${pad2(d.getMonth() + 1)}`);
    }
    return keys;
  }
  if (bucket === "year") {
    const presentYears = Object.keys(byPeriod).sort();
    if (!presentYears.length) return [String(now.getFullYear())];
    const minY = parseInt(presentYears[0], 10);
    const maxY = parseInt(presentYears[presentYears.length - 1], 10);
    const keys = [];
    for (let y = minY; y <= maxY; y++) keys.push(String(y));
    return keys;
  }
  return Object.keys(byPeriod).sort();
}

function isoWeek(d) {
  const dt = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const dayNum = dt.getUTCDay() || 7;
  dt.setUTCDate(dt.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(dt.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((dt - yearStart) / 86400000 + 1) / 7);
  return { year: dt.getUTCFullYear(), week };
}

// Pick ~5 x-axis labels so we don't over-crowd. Always include first and last.
// Pick ~5 x-axis labels so we don't over-crowd. Always include first and last.
// `anchor` used for relative-time format on hour bucket when showing current
// window; for past windows we fall back to absolute clock times.
function pickAxisLabels(keys, bucket, anchor, isCurrent) {
  // Mirror buildPeriodKeys's 1ms step-back so relative-time labels line up
  // with the exclusive-upper anchor convention.
  const now = anchor ? new Date(anchor.getTime() - 1) : new Date();
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const fmt = (k) => {
    if (bucket === "hour") {
      const [ymd, hr] = k.split(" ");
      const [y, m, d] = ymd.split("-").map(Number);
      const then = new Date(y, m - 1, d, parseInt(hr, 10));
      if (isCurrent !== false) {
        const diffH = Math.floor((now - then) / 3600000);
        return diffH <= 0 ? "now" : `${diffH}h ago`;
      }
      return `${hr}:00`;
    }
    if (bucket === "day") {
      const [y, m, d] = k.split("-").map(Number);
      const dt = new Date(y, m - 1, d);
      return `${DOW[dt.getDay()]} ${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    }
    if (bucket === "week") return "Week " + k.split("-W")[1];
    if (bucket === "month") {
      const [, m] = k.split("-");
      return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(m, 10) - 1] || m;
    }
    if (bucket === "year") return k;
    return k;
  };
  const n = keys.length;
  if (n <= 6) return keys.map((k, idx) => ({ idx, text: fmt(k) }));
  const picks = new Set([0, n - 1, Math.floor(n / 2), Math.floor(n / 4), Math.floor(3 * n / 4)]);
  return [...picks].sort((a, b) => a - b).map((idx) => ({ idx, text: fmt(keys[idx]) }));
}

// Detailed stats view for modals. Uses <details>/<summary> so sections are
// collapsible without extra JS. Handles both per-convo shape (single
// git_branch) and project-aggregate shape (branches[] union).
export function renderStatsModalBody(s, opts) {
  if (!s) return '<div class="stats-dim">No data.</div>';
  opts = opts || {};
  // Modal respects whatever the overview-bar toggles are currently set to.
  // No inline toggle UI inside the modal — single source of truth lives in
  // the overview bar.
  const f = opts.filters || { active: true, archived: false, deleted: false };

  const perModel = s.per_model || {};
  const modelList = Object.keys(perModel)
    .filter((m) => {
      const p = perModel[m] || {};
      const hasTok = (p.input_tokens || 0) + (p.output_tokens || 0) +
                     (p.cache_read_tokens || 0) + (p.cache_creation_tokens || 0) > 0;
      const hasTool = Object.keys(p.tool_uses || {}).length > 0;
      return hasTok || hasTool;
    })
    .sort((a, b) => totalTokens(perModel[b]) - totalTokens(perModel[a]));
  const canSplit = modelList.length >= 2;

  const viewToggleHtml = canSplit ? `
    <div class="stats-view-toggle" role="radiogroup">
      <label><input type="radio" name="stats-view" value="combined" data-action="set-stats-view" checked> Combined</label>
      <label><input type="radio" name="stats-view" value="by-model" data-action="set-stats-view"> By model</label>
    </div>` : "";

  let html = `<div class="stats-modal">${viewToggleHtml}`;
  html += `<div class="stats-view stats-view-combined">${renderStatsCombined(s, { ...opts, filters: f })}</div>`;
  if (canSplit) {
    html += `<div class="stats-view stats-view-by-model" hidden>${renderStatsByModel(s, { ...opts, filters: f }, modelList)}</div>`;
  }
  html += `</div>`;
  return html;
}

function totalTokens(p) {
  if (!p) return 0;
  return (p.input_tokens || 0) + (p.output_tokens || 0) +
         (p.cache_read_tokens || 0) + (p.cache_creation_tokens || 0);
}

function renderStatsCombined(s, opts) {
  opts = opts || {};
  const f = opts.filters || { active: true, archived: false, deleted: false };
  const a = s.archived_delta || {};
  const d = s.deleted_delta  || {};
  // Build an "effective" stats object that respects the three toggles for
  // the aggregated tables. Archived/Deleted breakdowns below stay from raw.
  const sum = (k) => (f.active ? (s[k] || 0) : 0)
                   + (f.archived ? (a[k] || 0) : 0)
                   + (f.deleted  ? (d[k] || 0) : 0);
  const mergeToolUses = () => {
    const out = {};
    const add = (obj) => { for (const [n, c] of Object.entries(obj || {})) out[n] = (out[n] || 0) + c; };
    if (f.active) add(s.tool_uses);
    if (f.archived) add(a.tool_uses);
    if (f.deleted) add(d.tool_uses);
    return out;
  };
  const eff = {
    input_tokens: sum("input_tokens"),
    output_tokens: sum("output_tokens"),
    cache_read_tokens: sum("cache_read_tokens"),
    cache_creation_tokens: sum("cache_creation_tokens"),
    thinking_count: sum("thinking_count"),
    tool_uses: mergeToolUses(),
  };

  const totalIn = eff.input_tokens + eff.cache_read_tokens + eff.cache_creation_tokens;
  const toolEntries = Object.entries(eff.tool_uses).sort((a, b) => b[1] - a[1]);
  const toolTotal = toolEntries.reduce((n, [, c]) => n + c, 0);

  // Cost: computed on effective numbers using the same priceFor logic by
  // hand-building a stat-shaped object that costByType can read.
  const costCarrier = { ...eff, models: s.models, per_model: s.per_model };
  const costIn  = costByType(costCarrier, "input_tokens");
  const costCR  = costByType(costCarrier, "cache_read_tokens");
  const costCW  = costByType(costCarrier, "cache_creation_tokens");
  const costOut = costByType(costCarrier, "output_tokens");
  const costTot = totalCost(costCarrier);
  const totalInCost = [costIn, costCR, costCW].reduce((a, v) => a + (v || 0), 0);
  const anyPriced = costTot != null;

  const tokenRows3 = [
    ["Context tokens (cumulative)", fmtTokens(totalIn), anyPriced ? fmtCost(totalInCost) : "—"],
    ['<span class="stats-note">summed per turn — cache reads inflate this</span>', "", ""],
    ["&nbsp;&nbsp;&nbsp;&nbsp;direct input", fmtTokens(eff.input_tokens), fmtCost(costIn)],
    ["&nbsp;&nbsp;&nbsp;&nbsp;cache read", fmtTokens(eff.cache_read_tokens), fmtCost(costCR)],
    ['&nbsp;&nbsp;&nbsp;&nbsp;cache write<sup>1</sup>', fmtTokens(eff.cache_creation_tokens), fmtCost(costCW)],
    ["Output tokens", fmtTokens(eff.output_tokens), fmtCost(costOut)],
  ];

  const branchLine = (() => {
    if (s.branches && s.branches.length) {
      return s.branches.map((b) => `<code>${esc(b)}</code>`).join(", ");
    }
    if (s.git_branch) return `<code>${esc(s.git_branch)}</code>`;
    return '<span class="stats-dim">none</span>';
  })();

  const sessionRows = [];
  if (opts.convoCount != null) sessionRows.push(["Conversations", String(opts.convoCount)]);
  if (opts.archivedCount != null) sessionRows.push(["Archived conversations", String(opts.archivedCount)]);
  sessionRows.push(
    ["Tool-use blocks (total)", String(toolTotal)],
    ["Thinking blocks", String(eff.thinking_count)],
    [s.branches ? "Git branches seen" : "Git branch", branchLine],
    ["Model(s) used", (s.models && s.models.length)
      ? s.models.map((m) => `<code>${esc(shortModel(m))}</code>`).join(", ")
      : '<span class="stats-dim">none</span>'],
  );

  const makeTable = (rows) => {
    const trs = rows.map(([k, v]) =>
      `<tr><td class="stats-k">${k}</td><td class="stats-v">${v}</td></tr>`
    ).join("");
    return `<table class="stats-table">${trs}</table>`;
  };
  const makeTable3 = (rows) => {
    const trs = rows.map(([k, v, c]) =>
      `<tr><td class="stats-k">${k}</td><td class="stats-v">${v}</td><td class="stats-c">${c}</td></tr>`
    ).join("");
    const totalTr = anyPriced
      ? `<tr class="stats-total"><td class="stats-k">Total cost<sup>2</sup></td><td class="stats-v"></td><td class="stats-c">${fmtCost(costTot)}</td></tr>`
      : "";
    const note = `<tr><td colspan="3" class="stats-note stats-note-row">
      <sup>1</sup> ${esc(COST_ASSUMPTION_NOTE)}<br>
      <sup>2</sup> ${esc(PRICING_SOURCE_NOTE)}
    </td></tr>`;
    return `<table class="stats-table stats-tokens-3col">${trs}${totalTr}${note}</table>`;
  };

  const deltaSection = (delta, label, cls) => {
    if (!delta) return "";
    const tEntries = Object.entries(delta.tool_uses || {}).sort((a, b) => b[1] - a[1]);
    const tTotal = tEntries.reduce((n, [, c]) => n + c, 0);
    const inTok = (delta.input_tokens || 0) + (delta.cache_read_tokens || 0) + (delta.cache_creation_tokens || 0);
    const hasContent = tTotal || inTok || (delta.output_tokens || 0) || (delta.messages_deleted || 0);
    if (!hasContent) return "";
    const rows = [];
    if (delta.messages_deleted) rows.push(["Messages deleted", String(delta.messages_deleted)]);
    rows.push(
      ["Input tokens (cumulative)", fmtTokens(inTok)],
      ["Output tokens", fmtTokens(delta.output_tokens || 0)],
      ["Tool-use blocks", String(tTotal)],
      ["Thinking blocks", String(delta.thinking_count || 0)],
    );
    let body = makeTable(rows);
    if (tEntries.length) {
      const trs = tEntries.map(([name, c]) =>
        `<tr><td class="stats-k"><code>${esc(name)}</code></td><td class="stats-v">${c}</td></tr>`
      ).join("");
      body += `<table class="stats-table stats-deleted-tools">${trs}</table>`;
    }
    return `<details class="stats-section ${cls}"><summary>${label}</summary>${body}</details>`;
  };

  let html = "";
  html += `<details class="stats-section" open><summary>Tokens</summary>${makeTable3(tokenRows3)}</details>`;

  if (toolEntries.length) {
    const toolRows = toolEntries.map(([name, c]) =>
      `<tr><td class="stats-k"><code>${esc(name)}</code></td><td class="stats-v">${c}</td></tr>`
    ).join("");
    html += `<details class="stats-section" open><summary>Tool-use breakdown (${toolTotal})</summary>
      <table class="stats-table">${toolRows}
        <tr class="stats-total"><td class="stats-k">total</td><td class="stats-v">${toolTotal}</td></tr>
      </table></details>`;
  }

  html += `<details class="stats-section" open><summary>Session info</summary>${makeTable(sessionRows)}</details>`;
  // Always-visible breakdowns (content-driven). These don't depend on the
  // toggle flags — they're the raw archived/deleted numbers for reference.
  html += deltaSection(s.archived_delta, "Archived content", "stats-section-archived");
  html += deltaSection(s.deleted_delta,  "Deleted content",  "stats-section-deleted");
  return html;
}

function renderStatsByModel(s, opts, modelList) {
  const pm = s.per_model || {};
  // Tokens matrix: rows = models, cols = [direct in, cache read, cache write, output, total].
  const tokHead = `<tr>
      <th class="stats-k">Model</th>
      <th class="stats-v">direct in</th>
      <th class="stats-v">cache read</th>
      <th class="stats-v">cache write</th>
      <th class="stats-v">output</th>
      <th class="stats-v">total</th>
    </tr>`;
  const tokRows = modelList.map((m) => {
    const p = pm[m] || {};
    const inT = p.input_tokens || 0;
    const crT = p.cache_read_tokens || 0;
    const cwT = p.cache_creation_tokens || 0;
    const out = p.output_tokens || 0;
    const tot = inT + crT + cwT + out;
    return `<tr>
      <td class="stats-k"><code>${esc(shortModel(m))}</code></td>
      <td class="stats-v">${fmtTokens(inT)}</td>
      <td class="stats-v">${fmtTokens(crT)}</td>
      <td class="stats-v">${fmtTokens(cwT)}</td>
      <td class="stats-v">${fmtTokens(out)}</td>
      <td class="stats-v stats-total-col">${fmtTokens(tot)}</td>
    </tr>`;
  }).join("");

  // Cost matrix: rows = models, cols = [input, cache read, cache write*, output, total].
  // Asterisk on cache-write header ties to the 5-min TTL assumption footnote.
  const anyUnpriced = modelList.some((m) => !priceFor(m));
  const costHead = `<tr>
      <th class="stats-k">Model</th>
      <th class="stats-v">input</th>
      <th class="stats-v">cache read</th>
      <th class="stats-v">cache write<sup>1</sup></th>
      <th class="stats-v">output</th>
      <th class="stats-v">total</th>
    </tr>`;
  let costTotalSum = 0;
  let costAny = false;
  const costRows = modelList.map((m) => {
    const p = priceFor(m);
    const stats = pm[m] || {};
    if (!p) {
      return `<tr>
        <td class="stats-k"><code>${esc(shortModel(m))}</code><sup>3</sup></td>
        <td class="stats-v" colspan="5"><span class="stats-dim">no published price</span></td>
      </tr>`;
    }
    const cIn = (stats.input_tokens || 0) * p.input / 1_000_000;
    const cCR = (stats.cache_read_tokens || 0) * p.cache_read / 1_000_000;
    const cCW = (stats.cache_creation_tokens || 0) * p.cache_write / 1_000_000;
    const cOut = (stats.output_tokens || 0) * p.output / 1_000_000;
    const cTot = cIn + cCR + cCW + cOut;
    costTotalSum += cTot;
    costAny = true;
    return `<tr>
      <td class="stats-k"><code>${esc(shortModel(m))}</code></td>
      <td class="stats-v">${fmtCost(cIn)}</td>
      <td class="stats-v">${fmtCost(cCR)}</td>
      <td class="stats-v">${fmtCost(cCW)}</td>
      <td class="stats-v">${fmtCost(cOut)}</td>
      <td class="stats-v stats-total-col">${fmtCost(cTot)}</td>
    </tr>`;
  }).join("");
  const costTotalRow = costAny ? `<tr class="stats-total">
      <td class="stats-k">total<sup>2</sup></td>
      <td class="stats-v" colspan="4"></td>
      <td class="stats-v stats-total-col">${fmtCost(costTotalSum)}</td>
    </tr>` : "";
  const costFootnotes = `<tr><td colspan="6" class="stats-note stats-note-row">
      <sup>1</sup> ${esc(COST_ASSUMPTION_NOTE)}<br>
      <sup>2</sup> ${esc(PRICING_SOURCE_NOTE)}
      ${anyUnpriced ? '<br><sup>3</sup> no published price — excluded from total.' : ""}
    </td></tr>`;

  // Tool matrix: rows = tool names (union), cols = each model + total.
  const toolNames = new Set();
  for (const m of modelList) {
    for (const name of Object.keys((pm[m] || {}).tool_uses || {})) toolNames.add(name);
  }
  const toolCounts = [...toolNames].map((name) => {
    const perM = modelList.map((m) => ((pm[m] || {}).tool_uses || {})[name] || 0);
    const total = perM.reduce((a, b) => a + b, 0);
    return { name, perM, total };
  }).sort((a, b) => b.total - a.total);

  let html = "";
  html += `<details class="stats-section" open><summary>Tokens by model</summary>
    <table class="stats-table stats-matrix">${tokHead}${tokRows}</table></details>`;

  html += `<details class="stats-section" open><summary>Cost by model</summary>
    <table class="stats-table stats-matrix">${costHead}${costRows}${costTotalRow}${costFootnotes}</table></details>`;

  if (toolCounts.length) {
    const toolHead = `<tr>
        <th class="stats-k">Tool</th>
        ${modelList.map((m) => `<th class="stats-v"><code>${esc(shortModel(m))}</code></th>`).join("")}
        <th class="stats-v">total</th>
      </tr>`;
    const toolRows = toolCounts.map(({ name, perM, total }) => `<tr>
        <td class="stats-k"><code>${esc(name)}</code></td>
        ${perM.map((c) => `<td class="stats-v">${c || ""}</td>`).join("")}
        <td class="stats-v stats-total-col">${total}</td>
      </tr>`).join("");
    const totalRow = `<tr class="stats-total">
        <td class="stats-k">total</td>
        ${modelList.map((m) => {
          const t = Object.values((pm[m] || {}).tool_uses || {}).reduce((a, b) => a + b, 0);
          return `<td class="stats-v">${t || ""}</td>`;
        }).join("")}
        <td class="stats-v stats-total-col">${toolCounts.reduce((a, t) => a + t.total, 0)}</td>
      </tr>`;
    html += `<details class="stats-section" open><summary>Tool-use by model</summary>
      <table class="stats-table stats-matrix">${toolHead}${toolRows}${totalRow}</table></details>`;
  }

  // Session info: same as combined (not per-model).
  const branchLine = (() => {
    if (s.branches && s.branches.length) return s.branches.map((b) => `<code>${esc(b)}</code>`).join(", ");
    if (s.git_branch) return `<code>${esc(s.git_branch)}</code>`;
    return '<span class="stats-dim">none</span>';
  })();
  const sessionRows = [];
  if (opts.convoCount != null) sessionRows.push(["Conversations", String(opts.convoCount)]);
  sessionRows.push(
    ["Thinking blocks", String(s.thinking_count || 0)],
    [s.branches ? "Git branches seen" : "Git branch", branchLine],
  );
  const trs = sessionRows.map(([k, v]) =>
    `<tr><td class="stats-k">${k}</td><td class="stats-v">${v}</td></tr>`
  ).join("");
  html += `<details class="stats-section" open><summary>Session info</summary>
    <table class="stats-table">${trs}</table></details>`;
  return html;
}

export function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

export function arrow(state, col) {
  if (state.sort !== col) return "";
  return state.desc ? " \u25BE" : " \u25B4";
}

export function shortPath(path) {
  return path.replace(/^\/Users\/[^/]+\//, "~/");
}

export function escAttr(s) {
  return (s ?? "").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Lightweight toast: one element at a time, fades in/out, ~2s.
export function toast(msg) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 1800);
}

export function highlightText(html, query) {
  if (!query) return html;
  const re = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
  return html.replace(/>([^<]*)</g, (m, text) =>
    ">" + text.replace(re, '<span class="highlight">$1</span>') + "<"
  );
}
