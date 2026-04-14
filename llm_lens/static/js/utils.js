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
export function renderStatsInline(s) {
  if (!s) return '<span class="stats-dim">no data</span>';
  const totalIn = (s.input_tokens || 0) + (s.cache_read_tokens || 0) + (s.cache_creation_tokens || 0);
  const toolTotal = Object.values(s.tool_uses || {}).reduce((a, b) => a + b, 0);

  const parts = [];
  parts.push(`<span class="stats-pair"><span class="k">in</span> ${fmtTokens(totalIn)}</span>`);
  parts.push(`<span class="stats-pair"><span class="k">out</span> ${fmtTokens(s.output_tokens || 0)}</span>`);
  if (toolTotal) parts.push(`<span class="stats-pair"><span class="k">tools</span> ${toolTotal}</span>`);
  if (s.thinking_count) parts.push(`<span class="stats-pair"><span class="k">think</span> ${s.thinking_count}</span>`);

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
export function renderTokenBars(byPeriod, range, bucket, mode, size, untilIso) {
  byPeriod = byPeriod || {};
  bucket = bucket || "day";
  mode = mode || "tokens";
  size = size || "compact";
  const anchor = untilIso ? new Date(untilIso) : new Date();
  // "Current" window: anchor is within a minute of now (offset=0 case).
  // Past windows use absolute time labels instead of relative "Nh ago".
  const isCurrent = !untilIso || Math.abs(Date.now() - anchor.getTime()) < 60_000;

  let keys = buildPeriodKeys(byPeriod, range, bucket, anchor);
  const totalOf = (k) => {
    const b = byPeriod[k] || {};
    if (mode === "tools") return Object.values(b.tool_uses || {}).reduce((s, v) => s + v, 0);
    return (b.input_tokens || 0) + (b.cache_read_tokens || 0) +
           (b.cache_creation_tokens || 0) + (b.output_tokens || 0);
  };
  const nonEmpty = keys.filter((k) => totalOf(k) > 0);
  if (nonEmpty.length === 0) {
    const metric = mode === "tools" ? "tool calls" : "tokens";
    return `<div class="overview-empty">no ${metric} in this range</div>`;
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

  const totals = keys.map(totalOf);
  const maxV = Math.max(...totals, 1);

  const H = size === "expanded" ? 540 : 120;
  const W = 600, padL = 4, padR = 4, padT = 6, padB = 18;
  const innerH = H - padT - padB;
  const innerW = W - padL - padR;
  const barW = innerW / keys.length;

  const bars = keys.map((k, i) => {
    const b = byPeriod[k] || {};
    const x = padL + i * barW + 0.5;
    const w = Math.max(1, barW - 1.5);

    let cumulative = 0;
    let segHtml = "";
    for (const seg of SEGMENTS) {
      const v = mode === "tools"
        ? (seg.key === "__other__" ? otherCount(b, SEGMENTS) : (b.tool_uses || {})[seg.key] || 0)
        : (b[seg.key] || 0);
      if (v <= 0) continue;
      const hSeg = (v / maxV) * innerH;
      const y = padT + innerH - cumulative - hSeg;
      const fill = seg.fill ? ` style="fill:${seg.fill}"` : "";
      segHtml += `<rect class="ov-bar ${seg.cls}"${fill} x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${w.toFixed(2)}" height="${hSeg.toFixed(2)}"></rect>`;
      cumulative += hSeg;
    }

    const tip = formatTooltip(k, b, mode);
    const hit = `<rect class="ov-hit" x="${x.toFixed(2)}" y="${padT}" width="${w.toFixed(2)}" height="${innerH}" fill="transparent"></rect>`;
    return `<g class="ov-bar-group" data-tip="${escAttr(tip)}">${segHtml}${hit}</g>`;
  }).join("");

  const axisLabels = pickAxisLabels(keys, bucket, anchor, isCurrent);
  const labelsHtml = axisLabels.map(({ idx, text }) => {
    const x = padL + idx * barW + barW / 2;
    return `<text x="${x.toFixed(2)}" y="${(H - 4).toFixed(2)}" text-anchor="middle" class="ov-axis-label">${esc(text)}</text>`;
  }).join("");

  const maxLbl = `<text x="${padL}" y="${(padT + 8).toFixed(2)}" class="ov-axis-label ov-max-label">${esc(mode === "tools" ? String(maxV) : fmtTokens(maxV))}</text>`;

  const legend = SEGMENTS.slice().reverse().map((seg) => {
    const sw = seg.fill
      ? `<span class="ov-legend-sw" style="background:${seg.fill}"></span>`
      : `<span class="ov-legend-sw ${seg.cls}"></span>`;
    return `<span class="ov-legend-item">${sw}${esc(seg.label)}</span>`;
  }).join("");

  return `
    <div class="overview-graph ov-size-${size}">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="overview-bars ov-size-${size}">
        ${maxLbl}${bars}${labelsHtml}
      </svg>
      <div class="ov-legend">${legend}</div>
      <div class="ov-tip" style="display:none"></div>
    </div>`;
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
function formatTooltip(key, b, mode) {
  const hdr = `<div class="ov-tip-hdr">${esc(key)}</div>`;
  const row = (label, val, cls) =>
    `<div class="ov-tip-row ${cls || ""}"><span class="ov-tip-k">${label}</span><span class="ov-tip-v">${val}</span></div>`;

  if (mode === "tools") {
    const entries = Object.entries(b.tool_uses || {}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((n, [, c]) => n + c, 0);
    const rows = entries.length
      ? entries.map(([n, c]) => row(esc(n), c)).join("")
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
  const now = anchor || new Date();
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
  const now = anchor || new Date();
  const fmt = (k) => {
    if (bucket === "hour") {
      const [ymd, hr] = k.split(" ");
      const [y, m, d] = ymd.split("-").map(Number);
      const then = new Date(y, m - 1, d, parseInt(hr, 10));
      if (isCurrent !== false) {
        const diffH = Math.round((now - then) / 3600000);
        return diffH <= 0 ? "now" : `${diffH}h ago`;
      }
      return `${hr}:00`;
    }
    if (bucket === "day")  return k.slice(5);
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
export function renderStatsModalBody(s) {
  if (!s) return '<div class="stats-dim">No data.</div>';
  const totalIn = (s.input_tokens || 0) + (s.cache_read_tokens || 0) + (s.cache_creation_tokens || 0);
  const toolEntries = Object.entries(s.tool_uses || {}).sort((a, b) => b[1] - a[1]);
  const toolTotal = toolEntries.reduce((n, [, c]) => n + c, 0);

  const tokenRows = [
    ["Context tokens (cumulative)", fmtTokens(totalIn)],
    ['<span class="stats-note">summed per turn — cache reads inflate this</span>', ""],
    ["&nbsp;&nbsp;&nbsp;&nbsp;direct input", fmtTokens(s.input_tokens || 0)],
    ["&nbsp;&nbsp;&nbsp;&nbsp;cache read", fmtTokens(s.cache_read_tokens || 0)],
    ["&nbsp;&nbsp;&nbsp;&nbsp;cache write", fmtTokens(s.cache_creation_tokens || 0)],
    ["Output tokens", fmtTokens(s.output_tokens || 0)],
  ];

  const branchLine = (() => {
    if (s.branches && s.branches.length) {
      return s.branches.map((b) => `<code>${esc(b)}</code>`).join(", ");
    }
    if (s.git_branch) return `<code>${esc(s.git_branch)}</code>`;
    return '<span class="stats-dim">none</span>';
  })();

  const sessionRows = [
    ["Tool-use blocks (total)", String(toolTotal)],
    ["Thinking blocks", String(s.thinking_count || 0)],
    [s.branches ? "Git branches seen" : "Git branch", branchLine],
    ["Model(s) used", (s.models && s.models.length)
      ? s.models.map((m) => `<code>${esc(shortModel(m))}</code>`).join(", ")
      : '<span class="stats-dim">none</span>'],
  ];

  const makeTable = (rows) => {
    const trs = rows.map(([k, v]) =>
      `<tr><td class="stats-k">${k}</td><td class="stats-v">${v}</td></tr>`
    ).join("");
    return `<table class="stats-table">${trs}</table>`;
  };

  let html = "";
  html += `<details class="stats-section" open><summary>Tokens</summary>${makeTable(tokenRows)}</details>`;

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
