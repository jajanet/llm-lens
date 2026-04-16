async function json(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

export const api = {
  projects: () =>
    json("/api/projects"),

  conversations: (folder, { offset = 0, limit = 30, sort = "recent", desc = true } = {}) =>
    json(`/api/projects/${folder}/conversations?offset=${offset}&limit=${limit}&sort=${sort}&desc=${desc ? 1 : 0}`),

  archivedConversations: (folder) =>
    json(`/api/projects/${folder}/archived`),

  messages: (folder, convoId, { offset, limit = 60 } = {}) => {
    const params = new URLSearchParams({ limit });
    if (offset != null) params.set("offset", offset);
    return json(`/api/projects/${folder}/conversations/${convoId}?${params}`);
  },

  agentRun: (folder, convoId, toolUseId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/agent/${encodeURIComponent(toolUseId)}`),

  deleteProject: (folder) =>
    json(`/api/projects/${folder}`, { method: "DELETE" }),

  deleteConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}`, { method: "DELETE" }),

  duplicateConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/duplicate`, { method: "POST" }),

  archiveConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/archive`, { method: "POST" }),

  unarchiveConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/unarchive`, { method: "POST" }),

  bulkArchive: (folder, ids) =>
    json(`/api/projects/${folder}/conversations/bulk-archive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),

  bulkUnarchive: (folder, ids) =>
    json(`/api/projects/${folder}/conversations/bulk-unarchive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),

  bulkDeleteConversations: (folder, ids) =>
    json(`/api/projects/${folder}/conversations/bulk-delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),

  conversationNames: (folder, ids) =>
    json(`/api/projects/${folder}/names`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),

  refreshCache: (folder) =>
    json(`/api/projects/${folder}/refresh-cache`, { method: "POST" }),

  conversationStats: (folder, ids) =>
    json(`/api/projects/${folder}/stats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),

  singleConversationStats: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/stats`),

  projectStats: (folders, tagsByFolder = null) =>
    json(`/api/projects/stats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tagsByFolder ? { folders, tags: tagsByFolder } : { folders }),
    }),

  overview: (range = "all", offset = 0, folder = null, tags = null) => {
    const params = new URLSearchParams({ range, offset: String(offset) });
    if (folder) params.set("folder", folder);
    if (folder && tags && tags.length) params.set("tags", tags.join(","));
    return json(`/api/overview?${params}`);
  },

  contextWindow: () =>
    json("/api/meta/context-window"),

  deleteMessage: (folder, convoId, msgUuid) =>
    json(`/api/projects/${folder}/conversations/${convoId}/messages/${msgUuid}`, { method: "DELETE" }),

  editMessage: (folder, convoId, msgUuid, text) =>
    json(`/api/projects/${folder}/conversations/${convoId}/messages/${msgUuid}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),

  getWordLists: () =>
    json("/api/word-lists"),

  getWordListDefaults: () =>
    json("/api/word-lists/defaults"),

  saveWordLists: (data) =>
    json("/api/word-lists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),

  scanCustomFilter: (folder, convoId, { min_length_chars, min_count, n_min, n_max }) =>
    json(`/api/projects/${folder}/conversations/${convoId}/custom-filter/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_length_chars, min_count, n_min, n_max }),
    }),


  getDownloadFields: () =>
    json("/api/download-fields"),

  saveDownloadFields: (data) =>
    json("/api/download-fields", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),

  rawConversationUrl: (folder, convoId) =>
    `/api/projects/${encodeURIComponent(folder)}/conversations/${encodeURIComponent(convoId)}/raw`,

  extractMessages: (folder, convoId, uuids) =>
    json(`/api/projects/${folder}/conversations/${convoId}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuids }),
    }),

  // ── Tags ──────────────────────────────────────────────────────────

  getTags: (folder) =>
    json(`/api/projects/${folder}/tags`),

  setTagLabels: (folder, labels) =>
    json(`/api/projects/${folder}/tags/labels`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ labels }),
    }),

  assignTags: (folder, convoId, tags) =>
    json(`/api/projects/${folder}/tags/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ convo_id: convoId, tags }),
    }),

  bulkAssignTag: (folder, ids, tag, add) =>
    json(`/api/projects/${folder}/tags/bulk-assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, tag, add }),
    }),
};
