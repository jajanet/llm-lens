# Claude Session Browser

A local web UI for browsing, searching, and managing the conversation history stored by the [Claude Code](https://claude.ai/code) CLI. Claude Code saves every session as a `.jsonl` file under `~/.claude/projects/`. This tool gives you a browser-based interface into that data so you don't have to dig through raw JSON on the command line.

## Why this exists

Claude Code accumulates session history fast. After a few weeks of active use you can have hundreds of conversations spread across dozens of projects, with no built-in way to browse, search, or prune them. This tool solves that — it reads the same directory Claude Code writes to and gives you a real UI to manage it.

## What it does

Three-level navigation mirroring Claude Code's own storage layout:

- **Projects** — one entry per folder in `~/.claude/projects/`, showing conversation count, total size, and a preview of the most recent message. Sortable by name, activity, size, or conversation count. List or card layout.
- **Conversations** — all `.jsonl` sessions within a project, paginated and sortable. Each shows a preview of the first user message, file size, and last-modified time.
- **Messages** — chat-style view of a single conversation. Renders tool calls and tool results as inline badges. Thinking blocks are collapsed by default and expandable. Paginated so large conversations load quickly.

**Things you can do:**

| Action | Where |
|---|---|
| Filter/search | All three levels, client-side |
| Sort by column | Projects + Conversations views |
| Toggle list / card layout | Projects + Conversations views |
| Delete a project | Projects view |
| Delete a conversation | Conversations view |
| Duplicate a conversation | Conversations view |
| Bulk-delete conversations | Conversations view (checkbox select) |
| Copy a message to clipboard | Messages view |
| Delete a single message | Messages view |
| Select messages and copy them | Messages view (Edit mode) |
| Extract selected messages to a new conversation | Messages view (Edit mode) |
| Bulk-delete selected messages | Messages view (Edit mode) |
| View sidechain (sub-agent) messages | Messages view |
| Dark / light theme | Header toggle, persisted in `localStorage` |

**What it doesn't do:** it has no write path back to Claude Code. Deleting or editing sessions here does not affect any running Claude Code process — it only modifies the files on disk.

---

## For users: running it

### Requirements

- Python 3.8+
- [Flask](https://flask.palletsprojects.com/) (`pip install flask`)
- A browser
- Claude Code installed and having been used at least once (so `~/.claude/projects/` exists)

### Start the server

```bash
python app.py
```

Then open [http://localhost:5111](http://localhost:5111) in your browser.

To use a different port:

```bash
python app.py 8080
```

The server binds to `0.0.0.0` so it is also reachable from other devices on your local network on whatever port you choose. It has no authentication — only run it on a trusted network.

### Using a virtual environment (recommended)

```bash
python -m venv .venv
source .venv/bin/activate
pip install flask
python app.py
```

---

## For developers / contributors

### Project layout

```
app.py                  Flask backend — REST API + static file serving
static/
  index.html            Single-page app shell
  css/styles.css        All styles (dark/light theme via CSS vars)
  js/
    main.js             Entry point: routing, theme, delegated click handler
    state.js            Shared mutable state + localStorage persistence
    api.js              Thin fetch wrappers for every backend endpoint
    router.js           Hash-based client-side router
    toolbar.js          Toolbar rendering helper
    modal.js            Confirm dialog
    utils.js            Formatting helpers (timeAgo, fmtSize, esc, etc.)
    views/
      projects.js       Projects list view
      conversations.js  Conversations list view
      messages.js       Message thread view
```

No build step. No bundler. No npm. The frontend is plain ES modules loaded directly by the browser.

### Running locally

```bash
pip install flask
python app.py
```

Flask's built-in dev server runs with `debug=True`, so it reloads on backend changes automatically. Frontend changes take effect on page refresh.

### Backend

`app.py` is a single-file Flask app. Key design notes:

- **`~/.claude/projects/` is the data source.** The path is hardcoded as `CLAUDE_PROJECTS_DIR`. Each subdirectory is a "project"; each `.jsonl` file inside is a conversation.
- **LRU cache with file-stat invalidation.** `_peek_jsonl_cached` and `_parse_messages_cached` are `@lru_cache` functions keyed on `(filepath, mtime, size)`. Any write to a file (delete, duplicate, extract) calls `_invalidate_cache_for()` which does a full cache clear. This keeps reads fast without serving stale data.
- **Pagination everywhere.** Projects, conversations, and messages are all paginated. The conversations endpoint supports server-side sort by recency and size; sort-by-message-count loads all files and sorts in Python (unavoidable since line counts require reading each file).
- **Mutations are plain filesystem ops.** Delete = `unlink`. Duplicate = `shutil.copy2`. Extract = filtered line-by-line copy to a new UUID-named file. No database.

### Frontend

The frontend is a small hand-rolled SPA:

- **Hash router** (`router.js`) — three routes: `/`, `/p/:folder`, `/p/:folder/c/:convoId`.
- **Delegated click handler** (`main.js`) — all interactive elements carry `data-action="..."`. One top-level listener on `document.body` dispatches to an actions map. No inline handlers anywhere.
- **Edit mode** — a global toggle that adds `edit-mode` to `<body>`. Message checkboxes and a floating selection bar appear only in this mode.
- **Content rendering** — `processContent()` in `messages.js` handles thinking blocks (`<thinking>...</thinking>`) and tool call markers (`[Tool: name]`, `[Tool Result]`) before HTML-escaping the rest. Thinking blocks become collapsible toggles. Tool calls become styled badges.

### API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/projects` | All projects with metadata |
| `GET` | `/api/projects/:folder/conversations` | Paginated conversations (`offset`, `limit`, `sort`, `desc`) |
| `GET` | `/api/projects/:folder/conversations/:id` | Paginated messages (`offset`, `limit`) |
| `DELETE` | `/api/projects/:folder/conversations/:id` | Delete a conversation |
| `POST` | `/api/projects/:folder/conversations/:id/duplicate` | Duplicate a conversation |
| `DELETE` | `/api/projects/:folder/conversations/:id/messages/:uuid` | Delete a single message |
| `POST` | `/api/projects/:folder/conversations/:id/extract` | Create new conversation from selected message UUIDs |
| `POST` | `/api/projects/:folder/conversations/bulk-delete` | Delete multiple conversations |
| `DELETE` | `/api/projects/:folder` | Delete an entire project |

All mutation endpoints invalidate the in-process LRU cache and return `{"ok": true}` on success.

### Adding features

- New backend endpoints go in `app.py` following the existing pattern (route → filesystem op → cache invalidation → JSON response).
- New frontend actions: add an entry to the `actions` map in `main.js`, implement the function in the relevant view file, and add a `data-action="..."` attribute to whatever HTML element triggers it.
- Styles are all in `static/css/styles.css` using CSS custom properties for theming. Dark theme is the default; `.light` on `<body>` overrides the vars.
