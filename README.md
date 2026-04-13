# llm-lens

![demo](https://raw.githubusercontent.com/jajanet/llm-lens/main/demo.gif)

A local web UI for browsing, searching, and managing the conversation history stored by LLM CLIs. Currently supports [Claude Code](https://claude.ai/code), which saves every session as a `.jsonl` file under `~/.claude/projects/`. `llm-lens` gives you a browser-based interface into that data so you don't have to dig through raw JSON on the command line.

The architecture is designed to accommodate other provider backends (OpenAI Codex CLI, Gemini CLI, etc.) — see [Extending to other providers](#extending-to-other-providers) — but today only Claude Code is implemented.

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
- A browser
- At least one supported LLM CLI installed and used at least once (currently: Claude Code, which populates `~/.claude/projects/`)

### Install

The recommended way is [`pipx`](https://pipx.pypa.io/) or [`uv tool`](https://docs.astral.sh/uv/guides/tools/), which install CLI apps into isolated environments and put them on your `PATH`:

```bash
# with pipx
pipx install llm-lens

# or with uv
uv tool install llm-lens
```

Then run:

```bash
llm-lens
```

and open [http://localhost:5111](http://localhost:5111) in your browser.

To use a different port:

```bash
llm-lens 8080
```

The server binds to `0.0.0.0` so it is also reachable from other devices on your local network on whatever port you choose. It has no authentication — only run it on a trusted network.

### Alternative: plain pip

```bash
pip install llm-lens
llm-lens
```

### Upgrading / uninstalling

```bash
pipx upgrade llm-lens      # or: uv tool upgrade llm-lens
pipx uninstall llm-lens    # or: uv tool uninstall llm-lens
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
LLM_LENS_DEBUG=1 llm-lens
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

Today `llm-lens` only supports Claude Code. The codebase is single-provider but structured so a second provider is straightforward to add. The Claude-specific surface area is small:

- `CLAUDE_PROJECTS_DIR` — where to look on disk
- `_peek_jsonl_cached` / `_parse_messages_cached` — JSONL shape (`message.role`, content blocks `text`/`tool_use`/`tool_result`/`thinking`, `isSidechain`, `isMeta`, `file-history-snapshot`, `uuid`, `cwd`, `timestamp`)
- Mutation endpoints — line-level JSONL ops for delete/duplicate/extract

When adding a second provider, the recommended refactor is:

1. Introduce a `Provider` protocol with methods like `discover_projects()`, `list_conversations(project)`, `read_messages(convo)`, `delete_conversation(convo)`.
2. Move the existing Claude logic into `llm_lens/providers/claude_code.py` behind that interface.
3. Add the new provider as a sibling module.
4. Add a `:provider` segment to API routes (`/api/:provider/projects/...`) and a provider selector to the frontend.
5. Declare per-provider dependencies as `[project.optional-dependencies]` extras in `pyproject.toml`, e.g. `pip install llm-lens[codex]`.

Don't pre-build this abstraction before the second provider exists — extract it from two working implementations rather than guessing.

---

## Publishing

`llm-lens` is distributed via [PyPI](https://pypi.org/). The release flow uses [build](https://pypa-build.readthedocs.io/) + [twine](https://twine.readthedocs.io/).

### One-time setup

1. Create accounts on [pypi.org](https://pypi.org/account/register/) and [test.pypi.org](https://test.pypi.org/account/register/).
2. Create an API token for each ([PyPI token docs](https://pypi.org/help/#apitoken)).
3. Install publishing tools:
   ```bash
   pip install --upgrade build twine
   ```

### Each release

1. Bump `version` in `pyproject.toml` (follow [semver](https://semver.org/)).
2. Commit and tag: `git tag v0.1.1 && git push --tags`.
3. Build the distributions:
   ```bash
   rm -rf dist/
   python -m build
   ```
   This produces `dist/llm_lens-X.Y.Z.tar.gz` (sdist) and `dist/llm_lens-X.Y.Z-py3-none-any.whl` (wheel).
4. Upload to TestPyPI first to smoke-test:
   ```bash
   twine upload --repository testpypi dist/*
   pipx install --index-url https://test.pypi.org/simple/ --pip-args="--extra-index-url https://pypi.org/simple/" llm-lens
   ```
5. Upload to real PyPI:
   ```bash
   twine upload dist/*
   ```

### Automating with GitHub Actions

Once stable, switch to [PyPI's trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) so releases happen from a GitHub Actions workflow on tag push — no API tokens stored in CI. See the [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish) action for a minimal template.

### Distribution beyond PyPI

Once `llm-lens` is on PyPI, it is automatically installable via `pipx` and `uv tool` with no extra work — those tools resolve from PyPI by default. Optional additional channels:

- **Homebrew** — submit a formula to [homebrew-core](https://docs.brew.sh/Adding-Software-to-Homebrew) or host your own tap (`brew tap janet/llm-lens`).
- **conda-forge** — submit a [feedstock](https://conda-forge.org/docs/maintainer/adding_pkgs/) for conda users.
- **GitHub Releases** — attach the built `.whl` and `.tar.gz` from `dist/` for direct download.

None are required; PyPI alone covers the vast majority of Python users.
