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

export function fmtSize(kb) {
  return kb > 1024 ? (kb / 1024).toFixed(1) + " MB" : Math.round(kb) + " KB";
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

export function highlightText(html, query) {
  if (!query) return html;
  const re = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
  return html.replace(/>([^<]*)</g, (m, text) =>
    ">" + text.replace(re, '<span class="highlight">$1</span>') + "<"
  );
}
