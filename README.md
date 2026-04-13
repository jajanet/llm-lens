# llm-lens-web

![demo](https://raw.githubusercontent.com/jajanet/llm-lens/main/demo.gif)

A local, offline web UI for browsing and pruning the conversation history your LLM CLI has already written to disk. Currently supports [Claude Code](https://claude.ai/code), which saves every session as a `.jsonl` file under `~/.claude/projects/`.

**No API key. No auth. No agent.** This tool never talks to Anthropic's API, never invokes the `claude` CLI, and can't send new messages — it only reads and rewrites the JSONL files on your machine. Everything is local filesystem I/O.

The architecture is designed to accommodate other provider backends (OpenAI Codex CLI, Gemini CLI, etc.) — see [Extending to other providers](#extending-to-other-providers) — but today only Claude Code is implemented.

## Why this exists

Claude Code accumulates session history fast. After a few weeks of active use you can have hundreds of conversations spread across dozens of projects, with no built-in way to browse or prune them. This tool is a light browser-based UI for that directory.

A few read-only viewers already cover the browsing side well (see [Related tools](#related-tools) below). What this tool adds beyond browsing today is **basic message-level editing** — delete individual messages, extract a subset of messages into a new conversation, duplicate or bulk-delete conversations. These edits are still **best-effort**: Claude Code's `/resume` replay semantics aren't publicly documented, so a destructive edit can in some cases break resume of that conversation. See [Editing conversations: prefer non-destructive actions](#editing-conversations-prefer-non-destructive-actions) for the warning, and `docs/issue-1-diagnose.md` / `docs/issue-2-repair.md` / `docs/issue-3-synthesize.md` for the planned work to make editing robust (diagnose / repair / synthesize).

## Related tools

If all you need is a read-only browser, these cover that ground well:

- [d-kimuson/claude-code-viewer](https://github.com/d-kimuson/claude-code-viewer) — web UI that also runs Claude Code itself (needs an API key or the `claude` CLI)
- [jhlee0409/claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer) — multi-provider desktop app, session-level delete only
- [InDate/claude-log-viewer](https://github.com/InDate/claude-log-viewer) — read-only live viewer with usage analytics
- [matt1398/claude-devtools](https://github.com/matt1398/claude-devtools) — DevTools-style inspector (token attribution, compaction viz)
- [raine/claude-history](https://github.com/raine/claude-history) — fuzzy-search CLI

`llm-lens-web`'s positioning vs those: **offline-only** (no API key, no agent) and the only one that attempts **message-level editing** of existing conversations — with the caveats above.

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

### Editing conversations: prefer non-destructive actions

Claude Code's `/resume` replay semantics aren't publicly documented, so any destructive edit to a conversation `.jsonl` is best-effort. If you care about being able to `/resume` an edited conversation:

- **Prefer *Extract* (Edit mode → "Save to new convo")** over deleting messages. It writes a new conversation file and leaves the original untouched, so you can always fall back to the original if replay misbehaves.
- **If you must delete**, duplicate the conversation first (Conversations view → Dup), so the original is preserved.
- Deleting a whole conversation or project is fine — those just remove files and don't touch message chains.

The tool tries to keep edits "resume-safe" (re-links `parentUuid` chains, strips orphan `tool_use` / `tool_result` blocks), but there may be other undocumented invariants Claude Code's replay relies on.

---

## For users: running it

### Requirements

- Python 3.8+
- A browser
- At least one supported LLM CLI installed and used at least once (currently: Claude Code, which populates `~/.claude/projects/`)

### Install

The recommended way is [`pipx`](https://pipx.pypa.io/) or [`uv tool`](https://docs.astral.sh/uv/guides/tools/), which install CLI apps into isolated environments and put them on your `PATH`:

```bash
# with pipx
pipx install llm-lens-web

# or with uv
uv tool install llm-lens-web
```

Then run:

```bash
llm-lens-web
```

and open [http://localhost:5111](http://localhost:5111) in your browser.

To use a different port:

```bash
llm-lens-web 8080
```

The server binds to `0.0.0.0` so it is also reachable from other devices on your local network on whatever port you choose. It has no authentication — only run it on a trusted network.

### Alternative: plain pip

```bash
pip install llm-lens-web
llm-lens-web
```

### Upgrading / uninstalling

```bash
pipx upgrade llm-lens-web      # or: uv tool upgrade llm-lens-web
pipx uninstall llm-lens-web    # or: uv tool uninstall llm-lens-web
```

---

## For developers / contributors

### Project layout

```
pyproject.toml          Package metadata & dependencies
llm_lens/
  __init__.py           Flask backend — REST API + static file serving + main() entry point
  static/
    index.html          Single-page app shell
    css/styles.css      All styles (dark/light theme via CSS vars)
    js/
      main.js           Entry point: routing, theme, delegated click handler
      state.js          Shared mutable state + localStorage persistence
      api.js            Thin fetch wrappers for every backend endpoint
      router.js         Hash-based client-side router
      toolbar.js        Toolbar rendering helper
      modal.js          Confirm dialog
      utils.js          Formatting helpers (timeAgo, fmtSize, esc, etc.)
      views/
        projects.js     Projects list view
        conversations.js Conversations list view
        messages.js     Message thread view
```

No frontend build step. No bundler. No npm. The frontend is plain ES modules loaded directly by the browser.

### Running locally (editable install)

```bash
git clone <repo-url>
cd llm-lens
pip install -e .
LLM_LENS_DEBUG=1 llm-lens-web
```

`-e` (editable) installs the package so code edits take effect immediately. Setting `LLM_LENS_DEBUG=1` enables Flask's auto-reloader.

### Backend

`llm_lens/__init__.py` is a single-file Flask app. Key design notes:

- **`~/.claude/projects/` is the data source.** The path is hardcoded as `CLAUDE_PROJECTS_DIR`. Each subdirectory is a "project"; each `.jsonl` file inside is a conversation. This is the main provider-specific coupling today — see [Extending to other providers](#extending-to-other-providers).
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

- New backend endpoints go in `llm_lens/__init__.py` following the existing pattern (route → filesystem op → cache invalidation → JSON response).
- New frontend actions: add an entry to the `actions` map in `main.js`, implement the function in the relevant view file, and add a `data-action="..."` attribute to whatever HTML element triggers it.
- Styles are all in `static/css/styles.css` using CSS custom properties for theming. Dark theme is the default; `.light` on `<body>` overrides the vars.

---

## Extending to other providers

Today `llm-lens-web` only supports Claude Code. The codebase is single-provider but structured so a second provider is straightforward to add. The Claude-specific surface area is small:

- `CLAUDE_PROJECTS_DIR` — where to look on disk
- `_peek_jsonl_cached` / `_parse_messages_cached` — JSONL shape (`message.role`, content blocks `text`/`tool_use`/`tool_result`/`thinking`, `isSidechain`, `isMeta`, `file-history-snapshot`, `uuid`, `cwd`, `timestamp`)
- Mutation endpoints — line-level JSONL ops for delete/duplicate/extract

When adding a second provider, the recommended refactor is:

1. Introduce a `Provider` protocol with methods like `discover_projects()`, `list_conversations(project)`, `read_messages(convo)`, `delete_conversation(convo)`.
2. Move the existing Claude logic into `llm_lens/providers/claude_code.py` behind that interface.
3. Add the new provider as a sibling module.
4. Add a `:provider` segment to API routes (`/api/:provider/projects/...`) and a provider selector to the frontend.
5. Declare per-provider dependencies as `[project.optional-dependencies]` extras in `pyproject.toml`, e.g. `pip install llm-lens-web[codex]`.

Don't pre-build this abstraction before the second provider exists — extract it from two working implementations rather than guessing.
