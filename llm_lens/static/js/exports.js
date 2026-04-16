// Export helpers: plain-text and JSONL serialization, download trigger, and
// cached download-field prefs fetched from the server.

import { api } from "./api.js";
import { state } from "./state.js";

// Match the server-side field list. `role` + `content` are always included;
// the modal disables their checkboxes (a message without them is useless to
// export), but we still guard here in case state drifts.
export const EXPORT_FIELDS = [
  "uuid",
  "role",
  "content",
  "timestamp",
  "commands",
  "model",
  "usage",
];
export const REQUIRED_EXPORT_FIELDS = ["role", "content"];

function cleanPlainBody(text) {
  // Matches copySelected's previous stripping: drop inline tags, Tool
  // markers, and [Tool Result] so the plain text is readable.
  return (text || "")
    .replace(/<[^>]+>/g, "")
    .replace(/\[Tool: [^\]]+\]/g, "")
    .replace(/\[Tool Result\]/g, "")
    .trim();
}

export function serializeAsText(msgs) {
  return msgs
    .map((m) => {
      const label = m.role === "user" ? "User" : "Assistant";
      return `${label}: ${cleanPlainBody(m.content)}`;
    })
    .join("\n\n");
}

export function serializeAsJsonl(msgs, fields) {
  const keep = new Set(EXPORT_FIELDS.filter((f) => fields[f] || REQUIRED_EXPORT_FIELDS.includes(f)));
  const lines = [];
  for (const m of msgs) {
    const obj = {};
    for (const f of EXPORT_FIELDS) {
      if (!keep.has(f)) continue;
      if (m[f] !== undefined && m[f] !== null) obj[f] = m[f];
    }
    lines.push(JSON.stringify(obj));
  }
  return lines.join("\n") + (lines.length ? "\n" : "");
}

export function downloadBlob(content, mime, filename) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function ensureDownloadFields() {
  if (!state.downloadFields) {
    state.downloadFields = await api.getDownloadFields();
  }
  return state.downloadFields;
}
