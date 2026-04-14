// All server calls live here.

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

  messages: (folder, convoId, { offset, limit = 60 } = {}) => {
    const params = new URLSearchParams({ limit });
    if (offset != null) params.set("offset", offset);
    return json(`/api/projects/${folder}/conversations/${convoId}?${params}`);
  },

  deleteProject: (folder) =>
    json(`/api/projects/${folder}`, { method: "DELETE" }),

  deleteConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}`, { method: "DELETE" }),

  duplicateConversation: (folder, convoId) =>
    json(`/api/projects/${folder}/conversations/${convoId}/duplicate`, { method: "POST" }),

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

  projectStats: (folders) =>
    json(`/api/projects/stats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders }),
    }),

  overview: (range = "all", offset = 0, folder = null) => {
    const params = new URLSearchParams({ range, offset: String(offset) });
    if (folder) params.set("folder", folder);
    return json(`/api/overview?${params}`);
  },

  deleteMessage: (folder, convoId, msgUuid) =>
    json(`/api/projects/${folder}/conversations/${convoId}/messages/${msgUuid}`, { method: "DELETE" }),

  extractMessages: (folder, convoId, uuids) =>
    json(`/api/projects/${folder}/conversations/${convoId}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuids }),
    }),
};
