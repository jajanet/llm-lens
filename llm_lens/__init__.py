#!/usr/bin/env python3
"""llm-lens-web: web UI for browsing/editing/deleting LLM CLI conversation history.

Currently Claude Code only. Provider-specific bits are confined to:
  - CLAUDE_PROJECTS_DIR (storage location)
  - _peek_jsonl_cached / _parse_messages_cached (JSONL format parsers)
  - the mutation endpoints (line-level JSONL ops)
When adding a second provider, extract these behind a Provider protocol.
"""

import json
import os
import re
import sys
import shutil
import uuid as uuid_mod
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone, timedelta


def _mtime_iso(mtime: float) -> str:
    """Convert a POSIX mtime (UTC epoch) to an ISO-8601 UTC string.

    Using `datetime.fromtimestamp(mtime)` (no tz) gives a naive *local* time;
    suffixing `Z` would lie about the zone and cause the JS side to shift
    by the server's UTC offset a second time. Always anchor to UTC.
    """
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")

from flask import Flask, jsonify, request, send_from_directory, send_file

from . import peek_cache
from . import tag_store

app = Flask(__name__, static_folder="static")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


# Archive root — mirrors CLAUDE_PROJECTS_DIR. Archived convos keep their
# <folder>/<id>.jsonl layout so every existing scan helper (`_stats`,
# `_peek`, etc.) works unchanged when handed an archive path.
ARCHIVE_ROOT = Path.home() / ".cache" / "llm-lens" / "archive"


def _archive_folder(folder: str) -> Path:
    return ARCHIVE_ROOT / folder


def _archive_path(folder: str, convo_id: str) -> Path:
    return ARCHIVE_ROOT / folder / f"{convo_id}.jsonl"


def _move_preserving_mtime(src: Path, dst: Path):
    """Move a file (or directory) preserving mtime across devices.

    `shutil.move` can fall back to copy+remove when crossing filesystems and
    may touch mtime. We use copy2/copytree (mtime-preserving) + unlink/rmtree
    explicitly so archive→unarchive is always round-trippable.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
        shutil.rmtree(src)
    else:
        shutil.copy2(src, dst)
        src.unlink()

# ---------------------------------------------------------------------------
# LRU cache – keyed on (path, mtime, size) so edits/new files invalidate
# ---------------------------------------------------------------------------

def _tail_user_preview(filepath: Path) -> str | None:
    """Return the last non-meta user message's text preview, or None.

    Reads the file's tail and parses lines backwards, expanding the read
    window progressively until a prose user message is found or we've
    covered the whole file. Expanding widens because Claude Code convos
    often end with long assistant turns / tool_results that push the last
    user message far back from EOF.

    Stops at the first user message with prose text (skips tool_result-only
    entries and `<local-command>` artifacts).
    """
    try:
        size = filepath.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    def _extract(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text") or "")
            return "".join(parts)
        return ""

    # Progressive tail reads: start small, double until we find a user
    # message or we've read the whole file. Cap at 4MB to keep worst-case
    # bounded for the 99th-percentile large convo files.
    windows = [256_000, 1_000_000, 4_000_000]
    for window in windows:
        try:
            with open(filepath, "rb") as fh:
                start = max(0, size - window)
                fh.seek(start)
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        lines = chunk.splitlines()
        # Drop the (likely truncated) first line unless we read from byte 0.
        if start > 0 and lines:
            lines = lines[1:]

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("isMeta"):
                continue
            msg = entry.get("message") or {}
            if msg.get("role") != "user":
                continue
            text = _extract(msg.get("content"))
            if not text or text.startswith("<local-command"):
                continue
            return re.sub(r"<[^>]+>", "", text[:200]).strip()

        # Already covered whole file — no user message found; stop expanding.
        if start == 0:
            return None

    return None


@lru_cache(maxsize=512)
def _peek_jsonl_cached(filepath_str: str, mtime: float, size: int) -> dict:
    """Cached peek: only re-reads when file changes.

    Returns `preview` (first user message's text) and `last_preview` (last
    user message's text, via tail read). Handles both string and
    list-of-blocks content shapes — list shape happens after in-place edits
    (`_replace_content` writes `[{"type":"text","text":...}]`) and in
    subagent flows; without both-shape handling the project list silently
    showed "(empty)".
    """
    filepath = Path(filepath_str)
    cwd = None
    preview = None
    first_ts = None
    agent = None

    def _extract_user_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text") or "")
            return "".join(parts)
        return ""

    with open(filepath, "r") as fh:
        for i, line in enumerate(fh):
            if i >= 30:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "file-history-snapshot":
                continue
            if entry.get("type") == "agent-setting":
                val = entry.get("agentSetting")
                if val:
                    agent = val
                continue
            if not cwd and entry.get("cwd"):
                cwd = entry["cwd"]
            if not first_ts and entry.get("timestamp"):
                first_ts = entry["timestamp"]
            role = entry.get("message", {}).get("role")
            content = entry.get("message", {}).get("content", "")
            if role == "user" and not entry.get("isMeta"):
                text = _extract_user_text(content)
                if text and not text.startswith("<local-command"):
                    if not preview:
                        preview = re.sub(r"<[^>]+>", "", text[:200]).strip()

    last_preview = _tail_user_preview(filepath)
    # Fall back to first preview when the tail window can't surface a user
    # message (very long files where the last user message lives >4MB back
    # from EOF, or single-turn convos where first == last). Only files with
    # genuinely no user content stay "(empty)" on both.
    if not last_preview and preview:
        last_preview = preview
    return {
        "cwd": cwd,
        "preview": preview or "(empty)",
        "last_preview": last_preview or "(empty)",
        "first_ts": first_ts,
        "agent": agent,
    }


def _peek(filepath: Path, stat: os.stat_result) -> dict:
    cached = peek_cache.get(filepath, stat)
    if cached and "preview" in cached and "agent" in cached and "last_preview" in cached:
        return {
            "cwd": cached.get("cwd"),
            "preview": cached["preview"],
            "last_preview": cached["last_preview"],
            "first_ts": cached.get("first_ts"),
            "agent": cached.get("agent"),
        }
    result = _peek_jsonl_cached(str(filepath), stat.st_mtime, stat.st_size)
    peek_cache.set(
        filepath, stat,
        preview=result["preview"],
        last_preview=result["last_preview"],
        cwd=result["cwd"],
        first_ts=result["first_ts"],
        agent=result.get("agent"),
    )
    return result


@lru_cache(maxsize=512)
def _custom_title_cached(filepath_str: str, mtime: float, size: int):
    """Scan a JSONL for a `{type: "custom-title", customTitle: "..."}` line.

    `/rename` writes this line at whatever position it was issued, so the
    head-only peek can't see it — we scan the whole file but short-circuit
    JSON parsing with a substring pre-filter, and cache by (path,mtime,size).
    """
    try:
        with open(filepath_str, "r") as fh:
            for line in fh:
                if '"custom-title"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "custom-title" and entry.get("customTitle"):
                    return entry["customTitle"]
    except OSError:
        pass
    return None


def _custom_title(filepath: Path, stat: os.stat_result):
    cached = peek_cache.get(filepath, stat)
    if cached and "custom_title" in cached:
        return cached["custom_title"]
    title = _custom_title_cached(str(filepath), stat.st_mtime, stat.st_size)
    peek_cache.set(filepath, stat, custom_title=title)
    return title


# Keys extracted in one whole-file pass. Collected together so we pay the
# I/O cost once per file rev.
_STATS_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens",
               "cache_creation_tokens", "models", "tool_uses",
               "thinking_count", "git_branch", "per_model", "commands",
               "tool_turn_tokens", "command_turn_tokens",
               "last_context_input_tokens", "last_context_cache_creation_tokens",
               "last_context_cache_read_tokens", "last_model_for_context",
               # Session-artifact counters. Tracked alongside tool / thinking
               # stats but derived from non-assistant entries, so they never
               # contribute to token totals — useful for "workflow" breakdowns
               # (how often you /clear, how many queued drafts, etc.) without
               # affecting cost attribution.
               "slash_commands", "queued_count", "compact_count",
               "away_count", "info_count", "scheduled_count",
               # Cost-estimation inputs. Thinking tokens are bundled into
               # Anthropic's `output_tokens` — no separate field, so we
               # prorate per turn by text character ratio. Compaction
               # summaries aren't logged as their own API call in the
               # JSONL; we estimate output cost from the summary message's
               # character length.
               "thinking_output_tokens_estimate", "compact_summary_chars")


# Tokens that wrap another command and shouldn't be counted themselves
# (`sudo grep foo` → "grep", not "sudo"). Env assignments (`FOO=1 grep x`)
# are also skipped by detecting `=` in a leading token.
_CMD_WRAPPERS = {"sudo", "bash", "sh", "nohup", "time", "exec", "env",
                 "xargs", "doas", "command"}

import shlex as _shlex

def _extract_command_name(cmd: str) -> str:
    """Best-effort extract the invoked command name from a shell string.

    - `sudo apt install foo` → `apt`
    - `env FOO=1 grep x` → `grep`
    - `bash -c "ls -la | wc"` → `ls` (recurses into the script)
    - `/usr/bin/python3 ...` → `python3`
    - pipes/chains: returns the first command (`ls -la | grep foo` → `ls`).

    Returns "" when we can't figure it out.
    """
    if not cmd:
        return ""
    cmd = cmd.strip()
    try:
        tokens = _shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return ""
    # Recurse into the inner script for `bash -c 'script'` / `sh -c '...'`.
    if len(tokens) >= 3 and tokens[0] in ("bash", "sh") and tokens[1] == "-c":
        inner = _extract_command_name(tokens[2])
        if inner:
            return inner
    # Walk past env assignments, wrappers, and flags until we hit the real command.
    for tok in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue  # VAR=value prefix
        if tok in _CMD_WRAPPERS:
            continue
        if tok.startswith("-"):
            continue
        return tok.rsplit("/", 1)[-1]
    return tokens[0].rsplit("/", 1)[-1]


def _empty_stats() -> dict:
    """Canonical empty shape for the stats delta schema.

    The tombstone `deleted_delta` shape is kept equal to this so that adding
    a new field here auto-flows through every destructive op (edit, scrub,
    per-msg delete, whole-file delete) and through `api_overview` aggregation.

    Identity fields (`models` list, `git_branch`, `last_context_*`,
    `last_model_for_context`) are intentionally NOT in this shape — they're
    per-file snapshots, not delta-able quantities.
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "tool_uses": {},
        "commands": {},
        "thinking_count": 0,
        "tool_turn_tokens": {},
        "command_turn_tokens": {},
        "thinking_output_tokens_estimate": {},
        "slash_commands": {},
        "compact_summary_chars": 0,
        "queued_count": 0,
        "compact_count": 0,
        "away_count": 0,
        "info_count": 0,
        "scheduled_count": 0,
        "per_model": {},
    }


def _empty_per_model_entry() -> dict:
    """Shape of a `per_model[model]` entry — same delta-able content-derived
    fields as `_empty_stats()` minus file-level keys. No recursive per_model."""
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "commands": {},
        "thinking_count": 0,
        "tool_turn_tokens": {}, "command_turn_tokens": {},
    }


def _ttt_bucket(d: dict, name: str) -> dict:
    return d.setdefault(name, {k: 0 for k in _TTT_FIELDS})


def _text_len_of(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                n += len(b.get("text") or "")
        return n
    return 0


def _accumulate_entry_into(acc: dict, entry: dict, ctx: dict):
    """Fold one JSONL entry's stats into `acc` in place.

    `ctx` is a mutable dict carrying cross-entry state:
      - "last_model": str — last assistant model seen (for sidechain entries
        missing a model field on the message)
      - "pending_compact": bool — true when the previous system entry was a
        compact_boundary; the next non-meta user message's text length is
        credited to compact_summary_chars

    Populates the full `_empty_stats()` shape including `per_model[mkey]`.
    File-level identity fields (`models`, `git_branch`, `last_context_*`)
    are NOT populated here — they stay in `_stats_cached`'s loop.
    """
    t = entry.get("type")
    st = entry.get("subtype")
    if t == "queue-operation":
        acc["queued_count"] = acc.get("queued_count", 0) + 1
    elif t == "system":
        if st == "local_command":
            m = _CMD_NAME_RE.search(entry.get("content") or "")
            if m:
                n = m.group(1).strip()
                acc["slash_commands"][n] = acc["slash_commands"].get(n, 0) + 1
        elif st == "compact_boundary":
            acc["compact_count"] = acc.get("compact_count", 0) + 1
            ctx["pending_compact"] = True
        elif st == "away_summary":
            acc["away_count"] = acc.get("away_count", 0) + 1
        elif st == "informational":
            acc["info_count"] = acc.get("info_count", 0) + 1
        elif st == "scheduled_task_fire":
            acc["scheduled_count"] = acc.get("scheduled_count", 0) + 1

    msg = entry.get("message") or {}
    if isinstance(msg.get("content"), str):
        for m in _CMD_NAME_RE.finditer(msg["content"]):
            n = m.group(1).strip()
            acc["slash_commands"][n] = acc["slash_commands"].get(n, 0) + 1

    role = msg.get("role")

    if ctx.get("pending_compact") and role == "user" and not entry.get("isMeta"):
        acc["compact_summary_chars"] = acc.get("compact_summary_chars", 0) + _text_len_of(msg.get("content"))
        ctx["pending_compact"] = False

    if role != "assistant":
        return

    model = msg.get("model")
    if model:
        ctx["last_model"] = model
    mkey = model or ctx.get("last_model") or "?"
    pm = acc["per_model"].setdefault(mkey, _empty_per_model_entry())

    usage = msg.get("usage") or {}
    in_t = int(usage.get("input_tokens") or 0)
    out_t = int(usage.get("output_tokens") or 0)
    cr_t = int(usage.get("cache_read_input_tokens") or 0)
    cc_t = int(usage.get("cache_creation_input_tokens") or 0)
    acc["input_tokens"] += in_t
    acc["output_tokens"] += out_t
    acc["cache_read_tokens"] += cr_t
    acc["cache_creation_tokens"] += cc_t
    pm["input_tokens"] += in_t
    pm["output_tokens"] += out_t
    pm["cache_read_tokens"] += cr_t
    pm["cache_creation_tokens"] += cc_t

    tool_block_entries = []
    text_chars_turn = 0
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                name = block.get("name") or "?"
                acc["tool_uses"][name] = acc["tool_uses"].get(name, 0) + 1
                pm["tool_uses"][name] = pm["tool_uses"].get(name, 0) + 1
                cname = None
                if name == "Bash":
                    cmd = (block.get("input") or {}).get("command", "")
                    cname = _extract_command_name(cmd) or None
                    if cname:
                        acc["commands"][cname] = acc["commands"].get(cname, 0) + 1
                        pm["commands"][cname] = pm["commands"].get(cname, 0) + 1
                tool_block_entries.append((name, cname))
            elif bt == "thinking":
                acc["thinking_count"] += 1
                pm["thinking_count"] += 1
            elif bt == "text":
                text_chars_turn += len(block.get("text") or "")

    has_thinking_block = any(
        isinstance(b, dict) and b.get("type") == "thinking"
        for b in (content if isinstance(content, list) else [])
    )
    if has_thinking_block and out_t:
        response_tokens_est = text_chars_turn // 4
        est = max(0, min(out_t, out_t - response_tokens_est))
        if est > 0:
            acc["thinking_output_tokens_estimate"][mkey] = (
                acc["thinking_output_tokens_estimate"].get(mkey, 0) + est
            )

    n_blocks = len(tool_block_entries)
    if n_blocks:
        share_in = in_t / n_blocks
        share_out = out_t / n_blocks
        share_cr = cr_t / n_blocks
        share_cc = cc_t / n_blocks
        for tname, cname in tool_block_entries:
            for tgt in (
                _ttt_bucket(acc["tool_turn_tokens"], tname),
                _ttt_bucket(pm["tool_turn_tokens"], tname),
            ):
                tgt["input_tokens"] += share_in
                tgt["output_tokens"] += share_out
                tgt["cache_read_tokens"] += share_cr
                tgt["cache_creation_tokens"] += share_cc
            if cname:
                for tgt in (
                    _ttt_bucket(acc["command_turn_tokens"], cname),
                    _ttt_bucket(pm["command_turn_tokens"], cname),
                ):
                    tgt["input_tokens"] += share_in
                    tgt["output_tokens"] += share_out
                    tgt["cache_read_tokens"] += share_cr
                    tgt["cache_creation_tokens"] += share_cc


def _diff_stats(before: dict, after: dict) -> dict:
    """Positive per-key difference between two stats dicts. Recursive over
    nested dicts. Scalars: max(0, b - a). Type-mismatched keys are skipped.
    Sparse output — keys with zero diff are omitted."""
    out: dict = {}
    for k, bv in (before or {}).items():
        av = after.get(k) if isinstance(after, dict) else None
        if isinstance(bv, dict):
            sub = _diff_stats(bv, av if isinstance(av, dict) else {})
            if sub:
                out[k] = sub
        elif isinstance(bv, (int, float)):
            d = bv - (av or 0)
            if d > 0:
                out[k] = d
    return out


def _ctx_at(entries: list, target_uuid: str) -> dict:
    """Replay cross-entry ctx state up to (but not including) `target_uuid`.

    Used by destructive endpoints so `_message_stats` sees the correct
    `pending_compact` / `last_model` state when computing before/after
    tombstone diffs. Without this, edits to the first user message after a
    compact_boundary would miss the `compact_summary_chars` credit.

    O(target_index). Fine for typical files.
    """
    ctx = {"last_model": "?", "pending_compact": False}
    scratch = _empty_stats()
    for e in entries:
        if e.get("uuid") == target_uuid:
            break
        _accumulate_entry_into(scratch, e, ctx)
    return ctx


@lru_cache(maxsize=256)
def _stats_cached(filepath_str: str, mtime: float, size: int):
    """One-pass aggregation of model(s), token totals, tool-use and thinking
    block counts, shell-command frequencies (from Bash tool_use blocks),
    and the session's starting git branch. Token counts come from each
    assistant message's real `message.usage` — no estimation.

    The per-entry extraction is delegated to `_accumulate_entry_into` so the
    exact same code path feeds both whole-file scan and single-message
    tombstone deltas (`_message_stats`). The delta schema therefore tracks
    the full live shape — no fields are dropped on destructive ops.

    Per-model breakdowns (`per_model`) include `tool_uses`, `commands`,
    `thinking_count`, `tool_turn_tokens`, and `command_turn_tokens`.

    Cost attribution (both `tool_turn_tokens` and `command_turn_tokens`):
    each `tool_use` block in a turn gets an equal slice of the turn's
    `usage` (turn_cost / num_blocks_in_turn). For tool-level the slice is
    keyed by tool name; for command-level (Bash only) the slice is keyed
    by the extracted command name.

    `last_context_*` fields snapshot the final main-thread assistant
    turn's usage — the model's view of the conversation at that point,
    which is what determines how close we are to compaction. Sidechains
    (Task-subagent turns) are skipped because their usage reflects the
    subagent's context, not the main conversation's.

    Session-artifact counters (`slash_commands`, `queued_count`,
    `compact_count`, `away_count`, `info_count`, `scheduled_count`) come
    from non-assistant entries and don't affect token/cost totals. They
    power the "session events" breakdown in the stats modal.

    Thinking / compaction cost estimates (`thinking_output_tokens_estimate`
    is per model, `compact_summary_chars` is scalar) support the estimate
    rows in the cost tab. See `_accumulate_entry_into` for methodology.
    """
    acc = _empty_stats()
    models: list = []
    git_branch = None
    last_ctx_in = last_ctx_cc = last_ctx_cr = 0
    last_ctx_model = ""
    ctx = {"last_model": "?", "pending_compact": False}

    try:
        with open(filepath_str, "r") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if git_branch is None and entry.get("gitBranch"):
                    git_branch = entry["gitBranch"]

                _accumulate_entry_into(acc, entry, ctx)

                # File-level identity fields: tracked here, not in the extractor.
                msg = entry.get("message") or {}
                if msg.get("role") == "assistant":
                    model = msg.get("model")
                    if model and model not in models:
                        models.append(model)
                    if not entry.get("isSidechain"):
                        usage = msg.get("usage") or {}
                        last_ctx_in = int(usage.get("input_tokens") or 0)
                        last_ctx_cc = int(usage.get("cache_creation_input_tokens") or 0)
                        last_ctx_cr = int(usage.get("cache_read_input_tokens") or 0)
                        last_ctx_model = model or ctx.get("last_model") or ""
    except OSError:
        pass

    out = dict(acc)
    out["models"] = models
    out["git_branch"] = git_branch
    out["last_context_input_tokens"] = last_ctx_in
    out["last_context_cache_creation_tokens"] = last_ctx_cc
    out["last_context_cache_read_tokens"] = last_ctx_cr
    out["last_model_for_context"] = last_ctx_model
    return out


def _message_stats(entry: dict, ctx: dict | None = None) -> dict:
    """Per-entry stats in the canonical `_empty_stats()` shape.

    Used by destructive endpoints (edit, scrub, per-msg delete) to compute
    before/after tombstone deltas. The extractor is shared with
    `_stats_cached`, so the delta schema covers every field the live scan
    produces — nothing drops silently on mutation.

    `ctx` carries cross-entry state if the caller has it (e.g.
    `pending_compact` after a compact_boundary). Defaults to fresh state.
    """
    acc = _empty_stats()
    if ctx is None:
        ctx = {"last_model": "?", "pending_compact": False}
    _accumulate_entry_into(acc, entry, ctx)
    return acc


def _fold_delta_into(target: dict, delta: dict):
    """Recursively fold `delta` into `target` in place. Scalars sum; dicts
    recurse. Type-mismatched keys are skipped (defensive)."""
    if not delta:
        return
    for k, v in delta.items():
        if isinstance(v, dict):
            existing = target.get(k)
            if existing is None:
                target[k] = {}
            elif not isinstance(existing, dict):
                continue
            _fold_delta_into(target[k], v)
        elif isinstance(v, (int, float)):
            existing = target.get(k, 0)
            if isinstance(existing, dict):
                continue
            target[k] = (existing or 0) + v


_TTT_FIELDS = ("input_tokens", "output_tokens",
               "cache_read_tokens", "cache_creation_tokens")


def _merge_ttt(target: dict, src: dict):
    """In-place merge of two `tool_turn_tokens` dicts (`{name: {tokens, turns}}`)."""
    if not src:
        return
    for name, vals in src.items():
        bucket = target.setdefault(name, {k: 0 for k in _TTT_FIELDS})
        for k in _TTT_FIELDS:
            bucket[k] = bucket.get(k, 0) + (vals.get(k) or 0)


def _merge_per_model(target: dict, src: dict):
    """In-place merge of a per_model dict produced by _stats_cached."""
    if not src:
        return
    for mkey, mstats in src.items():
        pm = target.setdefault(mkey, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "commands": {},
            "thinking_count": 0,
            "tool_turn_tokens": {}, "command_turn_tokens": {},
        })
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens",
                      "thinking_count"):
            pm[field] = pm.get(field, 0) + (mstats.get(field) or 0)
        for name, count in (mstats.get("tool_uses") or {}).items():
            pm["tool_uses"][name] = pm["tool_uses"].get(name, 0) + count
        for name, count in (mstats.get("commands") or {}).items():
            pm.setdefault("commands", {})
            pm["commands"][name] = pm["commands"].get(name, 0) + count
        _merge_ttt(pm.setdefault("tool_turn_tokens", {}),
                   mstats.get("tool_turn_tokens") or {})
        _merge_ttt(pm.setdefault("command_turn_tokens", {}),
                   mstats.get("command_turn_tokens") or {})


def _duplicate_sidecar_path(jsonl_path: Path) -> Path:
    return jsonl_path.with_suffix(".dup.json")


def _read_duplicate_meta(jsonl_path: Path):
    sidecar = _duplicate_sidecar_path(jsonl_path)
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _duplicate_parent_counted(dup_path: Path, parent_id) -> bool:
    if not parent_id:
        return False
    folder_name = dup_path.parent.name
    if (CLAUDE_PROJECTS_DIR / folder_name / f"{parent_id}.jsonl").exists():
        return True
    if (_archive_folder(folder_name) / f"{parent_id}.jsonl").exists():
        return True
    return False


def _subtract_shared_prefix(stats: dict, sp: dict) -> dict:
    out = {
        **stats,
        "tool_uses": {**(stats.get("tool_uses") or {})},
        "per_model": {k: {**v, "tool_uses": {**(v.get("tool_uses") or {})}}
                      for k, v in (stats.get("per_model") or {}).items()},
    }
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_creation_tokens", "thinking_count"):
        out[k] = max(0, (out.get(k) or 0) - (sp.get(k) or 0))
    for name, cnt in (sp.get("tool_uses") or {}).items():
        nv = out["tool_uses"].get(name, 0) - cnt
        if nv > 0:
            out["tool_uses"][name] = nv
        else:
            out["tool_uses"].pop(name, None)
    for mkey, mstats in (sp.get("per_model") or {}).items():
        pm = out["per_model"].get(mkey)
        if not pm:
            continue
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens"):
            pm[field] = max(0, (pm.get(field) or 0) - (mstats.get(field) or 0))
        for name, cnt in (mstats.get("tool_uses") or {}).items():
            nv = pm["tool_uses"].get(name, 0) - cnt
            if nv > 0:
                pm["tool_uses"][name] = nv
            else:
                pm["tool_uses"].pop(name, None)
    return out


def _stats(filepath, stat) -> dict:
    cached = peek_cache.get(filepath, stat)
    if cached and all(k in cached for k in _STATS_KEYS):
        out = {k: cached[k] for k in _STATS_KEYS}
        if cached.get("deleted_delta"):
            out["deleted_delta"] = cached["deleted_delta"]
    else:
        stats = _stats_cached(str(filepath), stat.st_mtime, stat.st_size)
        peek_cache.set(filepath, stat, **stats)
        # Re-read so any preserved deleted_delta from a prior tombstone is picked
        # up (set() carries deleted_delta across mtime/size bumps).
        cached = peek_cache.get(filepath, stat) or {}
        out = {**stats}
        if cached.get("deleted_delta"):
            out["deleted_delta"] = cached["deleted_delta"]
    # Duplicate subtraction lives outside the LRU/peek caches so that deleting
    # or archiving the parent flips the subtraction on/off without needing to
    # invalidate the dup's cached stats.
    meta = _read_duplicate_meta(filepath)
    if meta:
        out["duplicate_of"] = meta.get("duplicate_of")
        if _duplicate_parent_counted(filepath, meta.get("duplicate_of")):
            out = _subtract_shared_prefix(out, meta.get("shared_prefix_stats") or {})
            out["duplicate_of"] = meta.get("duplicate_of")
    return out


_AGENT_FILENAME_RE = re.compile(r"^agent-(?:(.+)-)?([0-9a-f]{8,})$")


_CMD_NAME_RE = re.compile(r"<command-name>\s*([^<\s]+)\s*</command-name>", re.IGNORECASE)
_CMD_WRAP_RE = re.compile(
    r"<command-(?:message|args|contents)\b[^>]*>[\s\S]*?</command-(?:message|args|contents)>\s*",
    re.IGNORECASE,
)
_STDOUT_RE = re.compile(r"<local-command-stdout>([\s\S]*?)</local-command-stdout>", re.IGNORECASE)
_STDERR_RE = re.compile(r"<local-command-stderr>([\s\S]*?)</local-command-stderr>", re.IGNORECASE)


def _collapse_command_wrappers(s: str) -> str:
    """Rewrite Claude Code's XML-ish slash-command wrappers into inline
    badge markers the frontend renders as pills.

      <command-name>/btw</command-name>…<command-args></command-args>
        → [Slash: /btw]
      <local-command-stdout>Set model…</local-command-stdout>
        → [SlashOut] Set model…
      <local-command-stderr>oops</local-command-stderr>
        → [SlashErr] oops

    Any unmatched trailing <command-message>/<command-args>/<command-contents>
    wrappers are stripped so they don't leak through as raw XML.
    """
    out = _CMD_NAME_RE.sub(lambda m: f"[Slash: {m.group(1).strip()}]", s)
    out = _CMD_WRAP_RE.sub("", out)
    out = _STDOUT_RE.sub(
        lambda m: "[SlashOut]" + ((" " + m.group(1).strip()) if m.group(1).strip() else ""),
        out,
    )
    out = _STDERR_RE.sub(
        lambda m: "[SlashErr]" + ((" " + m.group(1).strip()) if m.group(1).strip() else ""),
        out,
    )
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _format_entry_message(entry):
    """Convert a raw JSONL entry to a display message dict, or None to skip.

    Shared between parent-convo parsing and subagent-run parsing — the entry
    shape is identical, only the filter (parentToolUseID vs. not) differs at
    the call site.

    Handles three envelope shapes:
      1. Normal user/assistant turns: `entry.message.{role,content}`.
      2. Subagent progress events: `entry.type == "progress"` wraps a real
         message at `entry.data.message` (uuid/timestamp stay on the outer).
      3. Slash-command / queue / system events: top-level `entry.content`
         with no `message` object. Normalized into a pseudo-message with a
         prefix marker that the frontend renders as a badge.

    Sets `has_tool_use` / `has_thinking` / `has_tool_result` booleans on the
    output so the frontend can gate non-prose warnings (bulk edit/delete/scrub
    UI) without re-parsing the flattened display content.
    """
    if entry.get("type") == "file-history-snapshot":
        return None
    if entry.get("type") == "progress":
        inner = (entry.get("data") or {}).get("message") or {}
        if inner:
            entry = {
                **entry,
                "type": inner.get("type", "progress"),
                "message": inner.get("message") or {},
            }

    if "message" not in entry and isinstance(entry.get("content"), str):
        t = entry.get("type")
        st = entry.get("subtype")
        prefix = ""
        role = "system"
        if t == "queue-operation":
            role, prefix = "user", "[Queued] "
        elif t == "system":
            if st == "local_command":
                role, prefix = "system", ""
            elif st == "away_summary":
                role, prefix = "system", "[Away] "
            elif st == "compact_boundary":
                role, prefix = "system", "[Compacted] "
            elif st == "informational":
                role, prefix = "system", "[Info] "
            elif st == "scheduled_task_fire":
                role, prefix = "system", "[Scheduled] "
            else:
                role, prefix = "system", "[System] "
        else:
            return None
        entry = {**entry, "message": {"role": role, "content": prefix + entry["content"]}}

    message_obj = entry.get("message", {}) or {}
    role = message_obj.get("role", entry.get("type", "unknown"))
    content = message_obj.get("content", "")
    ts = entry.get("timestamp")
    uid = entry.get("uuid")
    model = message_obj.get("model")
    usage = message_obj.get("usage")
    is_meta = entry.get("isMeta", False)

    has_tool_use = False
    has_thinking = False
    has_tool_result = False

    tool_commands = []
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                has_tool_use = True
                name = block.get("name", "?")
                tid = block.get("id", "")
                parts.append(f"[Tool: {name}:{tid}]")
                if name == "Bash":
                    cmd = (block.get("input") or {}).get("command", "")
                    if cmd:
                        tool_commands.append({"id": tid, "command": cmd})
            elif t == "tool_result":
                has_tool_result = True
                parts.append("[Tool Result]")
            elif t == "thinking":
                has_thinking = True
                th = block.get("thinking", "")
                if th:
                    parts.append(f"<thinking>{th}</thinking>")
        content = "\n".join(parts)

    if isinstance(content, str):
        content = _collapse_command_wrappers(content)

    if is_meta:
        return None
    if role == "assistant" and not content:
        return None

    msg = {"uuid": uid, "role": role, "content": content if isinstance(content, str) else str(content), "timestamp": ts}
    if tool_commands:
        msg["commands"] = tool_commands
    if model:
        msg["model"] = model
    if isinstance(usage, dict) and usage:
        msg["usage"] = usage
    if has_tool_use:
        msg["has_tool_use"] = True
    if has_thinking:
        msg["has_thinking"] = True
    if has_tool_result:
        msg["has_tool_result"] = True
    return msg


def _dedup_by_uuid(msgs):
    # Drop duplicate uuids (happens when /resume appends replay entries).
    # Keep first occurrence so chronological order and parent links stay
    # stable. Content-based dedup is wrong: tool messages like "[Tool Result]"
    # repeat legitimately.
    seen = set()
    out = []
    for m in msgs:
        uid = m.get("uuid")
        if uid is not None:
            if uid in seen:
                continue
            seen.add(uid)
        out.append(m)
    return out


@lru_cache(maxsize=128)
def _parse_messages_cached(filepath_str: str, mtime: float, size: int):
    """Cached full message parse of a parent conversation .jsonl.

    Agent (sidechain) runs no longer live inline here — Claude Code writes
    them to sibling `<convo_id>/subagents/agent-*.jsonl` files, loaded via
    `_agent_runs_for_convo`. Any stray `isSidechain: true` entries (old
    inline format, not observed in current data) are skipped silently.
    """
    filepath = Path(filepath_str)
    main = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("isSidechain"):
                continue
            msg = _format_entry_message(entry)
            if msg is None:
                continue
            main.append(msg)
    return _dedup_by_uuid(main)


def _subagents_dir(folder: str, convo_id: str) -> Path:
    return CLAUDE_PROJECTS_DIR / folder / convo_id / "subagents"


@lru_cache(maxsize=128)
def _inline_agent_runs_from_parent(filepath_str: str, mtime: float, size: int):
    """Extract old-format inline agent runs from a parent .jsonl.

    A run = a cluster of entries with `isSidechain: true`, linked by
    `parentUuid`. Anchor = first non-sidechain ancestor (the main message
    the cluster branched from). Cached on (path, mtime, size).
    """
    filepath = Path(filepath_str)
    entries = []
    by_uuid = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(entry)
            if entry.get("uuid"):
                by_uuid[entry["uuid"]] = entry

    def anchor_for(entry):
        cur = entry.get("parentUuid")
        seen = set()
        while cur and cur in by_uuid and by_uuid[cur].get("isSidechain"):
            if cur in seen:
                break
            seen.add(cur)
            cur = by_uuid[cur].get("parentUuid")
        return cur

    runs = {}
    for e in entries:
        if not e.get("isSidechain"):
            continue
        anchor = anchor_for(e) or "__root__"
        ts = e.get("timestamp")
        r = runs.get(anchor)
        if r is None:
            r = {
                "run_id": f"inline:{anchor}",
                "name": "agent",
                "source": "inline",
                "source_file": str(filepath),
                "anchor_uuid": anchor,
                "message_count": 0,
                "first_ts": ts,
            }
            runs[anchor] = r
        r["message_count"] += 1
        if ts and (r["first_ts"] is None or ts < r["first_ts"]):
            r["first_ts"] = ts
    return list(runs.values())


def _agent_runs_for_convo(folder: str, convo_id: str):
    """Discover subagent runs spawned by a parent conversation.

    Two source formats are unified under one "agent run" concept:

    * **New (subagents dir)** — Claude Code writes each run as its own file
      at `<project>/<convo_id>/subagents/agent-[<name>-]<hash>.jsonl`. One
      file = one run (the whole file's formatted entries are its transcript).
      The `<hash>` in the filename is the run's stable id. The parent's
      anchor is the `Agent`/`Task`-named `tool_use` block whose id appears
      as a `parentToolUseID` on one of the run's entries — used by the UI
      to attach a `→ <name>` marker to the right parent message. Files
      without any Agent-anchored ptu are still runs (standalone / passive
      agents); they surface in the subagents list but have no inline marker.
      Non-Agent `parentToolUseID` values (Read/Write/Edit/etc.) are audit-
      trail noise and are intentionally ignored for anchoring.
      `source = "subagent"`.

    * **Old (inline)** — historically Claude Code wrote sidechain entries
      directly into the parent .jsonl with `isSidechain: true`. Each
      contiguous cluster (walked via `parentUuid`) is one run, anchored on
      the closest non-sidechain ancestor. `source = "inline"`. Run id =
      `inline:<anchor_uuid>`.

    Returns: {run_id: run_dict}. All runs share: run_id, name, source,
    message_count, first_ts. Subagent runs additionally carry
    `anchor_tool_use_id` (or None). Inline runs carry `anchor_uuid`. The
    loader also stores `source_file` for internal use.
    """
    runs = {}

    # Parent tool_use_id -> tool name, so we can filter ptus to only the
    # ones that represent an actual subagent invocation (Agent / Task).
    parent_path = _convo_path(folder, convo_id)
    tool_names = {}
    if parent_path and parent_path.exists():
        with open(parent_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_names[b.get("id")] = b.get("name")

        # Old-format: inline clusters in the parent file.
        stat = parent_path.stat()
        for r in _inline_agent_runs_from_parent(str(parent_path), stat.st_mtime, stat.st_size):
            runs[r["run_id"]] = r

    # New-format: one run per file in <convo_id>/subagents/.
    subdir = _subagents_dir(folder, convo_id)
    if subdir.is_dir():
        for fp in sorted(subdir.glob("agent-*.jsonl")):
            m = _AGENT_FILENAME_RE.match(fp.stem)
            name = m.group(1) if (m and m.group(1)) else "agent"
            run_hash = m.group(2) if m else fp.stem
            try:
                message_count = 0
                first_ts = None
                anchor_tool_use_id = None
                with open(fp, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = entry.get("timestamp")
                        # Walk the file once: count formatted messages, track
                        # earliest timestamp, and pick up the first Agent/Task
                        # ptu we see as the parent-side anchor.
                        if _format_entry_message(entry) is not None:
                            message_count += 1
                            if ts and (first_ts is None or ts < first_ts):
                                first_ts = ts
                        ptu = entry.get("parentToolUseID")
                        if (
                            anchor_tool_use_id is None
                            and ptu
                            and tool_names.get(ptu) in ("Agent", "Task")
                        ):
                            anchor_tool_use_id = ptu
                runs[run_hash] = {
                    "run_id": run_hash,
                    "name": name,
                    "source": "subagent",
                    "source_file": str(fp),
                    "anchor_tool_use_id": anchor_tool_use_id,
                    "message_count": message_count,
                    "first_ts": first_ts,
                }
            except OSError:
                continue

    return runs


def _load_agent_run_messages(run):
    """Load and format messages for a single agent run, dispatched by source."""
    if run.get("source") == "inline":
        return _load_inline_run_messages(run)
    return _load_subagent_run_messages(run)


def _load_subagent_run_messages(run):
    """New-format loader: return every formatted message in the subagent
    file. `parentToolUseID` isn't used for filtering (one file = one run);
    it only anchors the parent-side UI marker, computed elsewhere."""
    messages = []
    fp = Path(run["source_file"])
    with open(fp, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = _format_entry_message(entry)
            if msg is None:
                continue
            messages.append(msg)
    return _dedup_by_uuid(messages)


def _load_inline_run_messages(run):
    """Old-format loader: pull isSidechain:true entries from the parent file
    whose root (closest non-sidechain ancestor via parentUuid chain) matches
    this run's anchor_uuid."""
    fp = Path(run["source_file"])
    anchor = run["anchor_uuid"]
    entries = []
    by_uuid = {}
    with open(fp, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(entry)
            if entry.get("uuid"):
                by_uuid[entry["uuid"]] = entry

    def anchor_for(entry):
        cur = entry.get("parentUuid")
        seen = set()
        while cur and cur in by_uuid and by_uuid[cur].get("isSidechain"):
            if cur in seen:
                break
            seen.add(cur)
            cur = by_uuid[cur].get("parentUuid")
        return cur or "__root__"

    messages = []
    for e in entries:
        if not e.get("isSidechain"):
            continue
        if anchor_for(e) != anchor:
            continue
        msg = _format_entry_message(e)
        if msg is None:
            continue
        messages.append(msg)
    return _dedup_by_uuid(messages)


def _convo_files(project_dir: Path):
    """Return [(path, stat)] for top-level .jsonl, newest-first."""
    items = []
    for f in project_dir.glob("*.jsonl"):
        try:
            items.append((f, f.stat()))
        except OSError:
            continue
    items.sort(key=lambda t: t[1].st_mtime, reverse=True)
    return items


def _invalidate_cache_for(filepath: Path):
    """After a mutation, clear caches that might hold stale data."""
    _peek_jsonl_cached.cache_clear()
    _parse_messages_cached.cache_clear()
    _inline_agent_runs_from_parent.cache_clear()


# ---------------------------------------------------------------------------
# API: Projects
# ---------------------------------------------------------------------------

@app.route("/api/projects")
def api_projects():
    if not CLAUDE_PROJECTS_DIR.exists() and not ARCHIVE_ROOT.exists():
        return jsonify([])

    # Union of folder names from both live and archive roots, so projects
    # whose only surviving convos are archived still appear in the list.
    folder_names: set = set()
    if CLAUDE_PROJECTS_DIR.exists():
        for d in CLAUDE_PROJECTS_DIR.iterdir():
            if d.is_dir():
                folder_names.add(d.name)
    if ARCHIVE_ROOT.exists():
        for d in ARCHIVE_ROOT.iterdir():
            if d.is_dir():
                folder_names.add(d.name)

    projects = []
    for name in folder_names:
        live_dir = CLAUDE_PROJECTS_DIR / name
        arch_dir = _archive_folder(name)
        live_files = _convo_files(live_dir) if live_dir.exists() else []
        arch_files = _convo_files(arch_dir) if arch_dir.exists() else []
        if not live_files and not arch_files:
            continue

        all_files = live_files + arch_files
        total_kb = sum(s.st_size for _, s in all_files) / 1024
        newest_file, newest_stat = max(all_files, key=lambda t: t[1].st_mtime)
        peek = _peek(newest_file, newest_stat)

        projects.append({
            "folder": name,
            "path": peek["cwd"] or name,
            "conversation_count": len(live_files),
            "archived_count": len(arch_files),
            "total_size_kb": round(total_kb, 1),
            "last_activity": _mtime_iso(newest_stat.st_mtime),
            "latest_preview": peek["preview"][:150],
        })

    projects.sort(key=lambda p: p["last_activity"], reverse=True)
    return jsonify(projects)


# ---------------------------------------------------------------------------
# API: Conversations (paginated)
# ---------------------------------------------------------------------------

# Bucket granularity per range — each range's bar chart segments the span
# into the next-smaller unit: all->year, year->month, month->week, week->day,
# day->hour. Matches how people actually think about time filtering.
_RANGE_BUCKET = {
    "all":   "year",
    "year":  "month",
    "month": "week",
    "week":  "day",
    "day":   "hour",
}


def _bucket_key(mtime: float, bucket: str) -> str:
    dt = datetime.fromtimestamp(mtime)
    if bucket == "hour":  return dt.strftime("%Y-%m-%d %H")
    if bucket == "day":   return dt.strftime("%Y-%m-%d")
    if bucket == "week":
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    if bucket == "month": return dt.strftime("%Y-%m")
    if bucket == "year":  return dt.strftime("%Y")
    return dt.strftime("%Y-%m-%d")


def _shift_months(dt: datetime, months: int) -> datetime:
    """Shift `dt` by N calendar months, keeping day-of-month when possible
    (clamps to end-of-month when the target month is shorter)."""
    y, m = dt.year, dt.month + months
    while m <= 0:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    # Handle day clamp (e.g. Jan 31 + 1mo -> Feb 28/29).
    from calendar import monthrange
    day = min(dt.day, monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def _aligned_bounds(rng: str, offset: int) -> tuple:
    """Calendar-aligned `[cutoff, cutoff_upper)` window matching how the
    frontend draws its buckets. Each range spans exactly N aligned buckets
    ending at the *end* of the current one, so the rightmost bar is always
    the in-progress bucket. `offset=-1` shifts back one whole window.

      day   -> 24 calendar hours   (end = end of current hour)
      week  -> 7  calendar days    (end = end of today)
      month -> 5  ISO weeks        (end = end of this ISO week)
      year  -> 12 calendar months  (end = end of this month)
      all   -> unbounded

    Returns (cutoff_epoch, cutoff_upper_epoch). Either may be None (all range).
    """
    if rng == "all":
        return None, None
    now = datetime.now()
    if rng == "day":
        end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        end += timedelta(hours=offset * 24)
        start = end - timedelta(hours=24)
    elif rng == "week":
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end += timedelta(days=offset * 7)
        start = end - timedelta(days=7)
    elif rng == "month":
        # End of current ISO week = next Monday 00:00. weekday(): Mon=0..Sun=6.
        days_to_next_mon = 7 - now.weekday()
        eow = (now + timedelta(days=days_to_next_mon)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = eow + timedelta(days=offset * 5 * 7)
        start = end - timedelta(days=5 * 7)
    elif rng == "year":
        # End of this month = first day of next month 00:00.
        eom = _shift_months(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1)
        end = _shift_months(eom, offset * 12)
        start = _shift_months(end, -12)
    else:
        return None, None
    return start.timestamp(), end.timestamp()


@app.route("/api/overview")
def api_overview():
    """Account-wide totals + per-period buckets, filtered by convo mtime.

    Three buckets: active (live jsonls), `archived_delta` (archived jsonls,
    still contribute time-aligned since archive preserves mtime), and
    `deleted_delta` (tombstones bucketed by `deleted_at`).

    Optional `tags=0,2` query param (folder-scoped only): further restrict to
    conversations carrying any of those tag indices. Ignored when no folder
    is set, since tag assignments are keyed per-folder.
    """
    rng = request.args.get("range", "all")
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    bucket_unit = _RANGE_BUCKET.get(rng, "day")

    cutoff, cutoff_upper = _aligned_bounds(rng, offset)
    since_iso = (datetime.utcfromtimestamp(cutoff).isoformat() + "Z") if cutoff else None
    until_iso = (datetime.utcfromtimestamp(cutoff_upper).isoformat() + "Z") if cutoff_upper else None

    # All aggregation shapes derive from _empty_stats() so adding a new
    # stat field auto-flows through totals, archived, deleted, and
    # per-bucket folds without touching this function.
    totals = _empty_stats()
    archived_total = {**_empty_stats(), "messages_deleted": 0}
    deleted_total = {**_empty_stats(), "messages_deleted": 0}
    models_seen: list = []
    branches_seen: list = []
    by_period: dict = {}
    convo_count = 0
    archived_count = 0
    seen_paths: set = set()

    def _mk_bucket():
        # archived_delta and deleted_delta use the full _empty_stats() shape
        # (plus messages_deleted counter) so every tombstoned field surfaces.
        return {
            "input_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "output_tokens": 0,
            "tool_calls": 0, "tool_uses": {}, "convos": 0,
            "per_model": {},
            "archived_delta": {**_empty_stats(), "messages_deleted": 0},
            "deleted_delta": {**_empty_stats(), "messages_deleted": 0},
        }

    def _fold_into_bucket_delta(bucket_key: str, field: str, delta: dict):
        b = by_period.setdefault(bucket_key, _mk_bucket())
        _fold_delta_into(
            b.setdefault(field, {**_empty_stats(), "messages_deleted": 0}),
            delta,
        )

    folder = request.args.get("folder")

    # Tag filter: only honored when a folder is scoped (tag assignments are
    # per-folder). Resolve the allowed convo-id set up front so the inner
    # loops can skip cheaply.
    tags_param = request.args.get("tags", "").strip()
    allowed_ids: set | None = None
    if tags_param and folder:
        try:
            tag_indices = {int(t) for t in tags_param.split(",") if t.strip()}
        except ValueError:
            tag_indices = set()
        if tag_indices:
            assignments = tag_store.get_project(folder).get("assignments", {})
            allowed_ids = {cid for cid, tags in assignments.items()
                           if any(t in tag_indices for t in tags)}

    def _convo_allowed(fp: Path) -> bool:
        if allowed_ids is None:
            return True
        return fp.stem in allowed_ids

    def _live_dirs():
        if not CLAUDE_PROJECTS_DIR.exists():
            return []
        if folder:
            d = CLAUDE_PROJECTS_DIR / folder
            return [d] if d.is_dir() else []
        return [d for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()]

    def _archive_dirs():
        if not ARCHIVE_ROOT.exists():
            return []
        if folder:
            d = _archive_folder(folder)
            return [d] if d.is_dir() else []
        return [d for d in ARCHIVE_ROOT.iterdir() if d.is_dir()]

    def _in_window(mt):
        if cutoff is not None and mt < cutoff:
            return False
        if cutoff_upper is not None and mt >= cutoff_upper:
            return False
        return True

    for project_dir in _live_dirs():
        for fp in project_dir.glob("*.jsonl"):
            if not _convo_allowed(fp):
                continue
            try:
                stat = fp.stat()
            except OSError:
                continue
            seen_paths.add(str(fp))
            if not _in_window(stat.st_mtime):
                continue
            s = _stats(fp, stat)
            convo_count += 1
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "thinking_count"):
                totals[k] += s.get(k) or 0
            for name, count in (s.get("tool_uses") or {}).items():
                totals["tool_uses"][name] = totals["tool_uses"].get(name, 0) + count
            for name, count in (s.get("commands") or {}).items():
                totals["commands"][name] = totals["commands"].get(name, 0) + count
            _merge_ttt(totals["tool_turn_tokens"], s.get("tool_turn_tokens") or {})
            _merge_ttt(totals["command_turn_tokens"], s.get("command_turn_tokens") or {})
            for m in s.get("models") or []:
                if m and m not in models_seen:
                    models_seen.append(m)
            b = s.get("git_branch")
            if b and b not in branches_seen:
                branches_seen.append(b)

            key = _bucket_key(stat.st_mtime, bucket_unit)
            bucket = by_period.setdefault(key, _mk_bucket())
            bucket["input_tokens"] += s.get("input_tokens") or 0
            bucket["cache_read_tokens"] += s.get("cache_read_tokens") or 0
            bucket["cache_creation_tokens"] += s.get("cache_creation_tokens") or 0
            bucket["output_tokens"] += s.get("output_tokens") or 0
            for name, count in (s.get("tool_uses") or {}).items():
                bucket["tool_uses"][name] = bucket["tool_uses"].get(name, 0) + count
            bucket["tool_calls"] += sum((s.get("tool_uses") or {}).values())
            bucket["convos"] += 1
            _merge_per_model(bucket["per_model"], s.get("per_model") or {})
            _merge_per_model(totals["per_model"], s.get("per_model") or {})

            dd = s.get("deleted_delta") or {}
            if dd:
                _fold_delta_into(deleted_total, dd)
                _fold_into_bucket_delta(key, "deleted_delta", dd)

    for arch_dir in _archive_dirs():
        for fp in arch_dir.glob("*.jsonl"):
            if not _convo_allowed(fp):
                continue
            try:
                stat = fp.stat()
            except OSError:
                continue
            seen_paths.add(str(fp))
            if not _in_window(stat.st_mtime):
                continue
            s = _stats(fp, stat)
            archived_count += 1
            archived_entry = {
                "input_tokens": s.get("input_tokens") or 0,
                "output_tokens": s.get("output_tokens") or 0,
                "cache_read_tokens": s.get("cache_read_tokens") or 0,
                "cache_creation_tokens": s.get("cache_creation_tokens") or 0,
                "thinking_count": s.get("thinking_count") or 0,
                "tool_uses": s.get("tool_uses") or {},
            }
            _fold_delta_into(archived_total, archived_entry)
            for name, count in (s.get("commands") or {}).items():
                archived_total["commands"][name] = archived_total["commands"].get(name, 0) + count
            _merge_ttt(archived_total["tool_turn_tokens"],
                       s.get("tool_turn_tokens") or {})
            _merge_ttt(archived_total["command_turn_tokens"],
                       s.get("command_turn_tokens") or {})
            key = _bucket_key(stat.st_mtime, bucket_unit)
            _fold_into_bucket_delta(key, "archived_delta", archived_entry)
            dd = s.get("deleted_delta") or {}
            if dd:
                _fold_delta_into(deleted_total, dd)
                _fold_into_bucket_delta(key, "deleted_delta", dd)

    # Tombstones: only surface when NOT tag-filtering. A deleted convo no
    # longer has tag assignments (cleaned up on delete), so it can't match
    # a tag filter anyway — including them would silently skew totals.
    if allowed_ids is None:
        tomb_roots = [*_live_dirs(), *_archive_dirs()]
        for root in tomb_roots:
            for path, entry in peek_cache.iter_folder(root):
                if path in seen_paths:
                    continue
                dd = entry.get("deleted_delta") or {}
                if not dd:
                    continue
                da = entry.get("deleted_at")
                if cutoff is not None and (da is None or da < cutoff):
                    continue
                if cutoff_upper is not None and (da is None or da >= cutoff_upper):
                    continue
                _fold_delta_into(deleted_total, dd)
                if da is not None:
                    _fold_into_bucket_delta(_bucket_key(da, bucket_unit), "deleted_delta", dd)

    totals["models"] = models_seen
    totals["branches"] = branches_seen
    totals["archived_delta"] = archived_total
    totals["deleted_delta"] = deleted_total
    return jsonify({
        "range": rng,
        "bucket": bucket_unit,
        "offset": offset,
        "since": since_iso,
        "until": until_iso,
        "totals": totals,
        "convo_count": convo_count,
        "archived_count": archived_count,
        "by_period": by_period,
    })


@app.route("/api/projects/stats", methods=["POST"])
def api_projects_stats():
    """Aggregate per-convo stats up to the project level. Intended for the
    landing page to hydrate its cards after the initial list renders.

    Three stat buckets returned per project:
      - active fields (`input_tokens` etc.) — from live convos
      - `archived_delta` — sum of archived convo stats
      - `deleted_delta` — sum of per-convo delete deltas + tombstoned files

    Optional per-project `tags` in the POST body restricts aggregation to
    conversations assigned any of the listed tag indices. Shape:
        {"folders": ["foo"], "tags": {"foo": [0, 2]}}

    Cold cache: scans every jsonl (live + archive) once per file rev.
    """
    payload = request.get_json(silent=True) or {}
    folders = payload.get("folders") or []
    tag_map = payload.get("tags") or {}  # {folder: [tag_idx, ...]}
    out = {}
    for folder in folders:
        project_dir = CLAUDE_PROJECTS_DIR / folder
        archive_dir = _archive_folder(folder)

        # Resolve per-folder tag filter (same per-folder semantics as overview).
        tag_indices = set()
        raw = tag_map.get(folder) or []
        for t in raw:
            try:
                tag_indices.add(int(t))
            except (TypeError, ValueError):
                pass
        allowed_ids: set | None = None
        if tag_indices:
            assignments = tag_store.get_project(folder).get("assignments", {})
            allowed_ids = {cid for cid, tags in assignments.items()
                           if any(t in tag_indices for t in tags)}

        def _convo_allowed(fp: Path, _allowed=allowed_ids) -> bool:
            if _allowed is None:
                return True
            return fp.stem in _allowed

        totals = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0,
            "per_model": {}, "commands": {},
            "tool_turn_tokens": {}, "command_turn_tokens": {},
        }
        archived_total = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
            "commands": {}, "tool_turn_tokens": {}, "command_turn_tokens": {},
        }
        deleted_total = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
        }
        models_seen: list = []
        branches_seen: list = []
        seen_paths: set = set()

        def _sum_into_active(s):
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "thinking_count"):
                totals[k] += s.get(k) or 0
            for name, count in (s.get("tool_uses") or {}).items():
                totals["tool_uses"][name] = totals["tool_uses"].get(name, 0) + count
            for name, count in (s.get("commands") or {}).items():
                totals["commands"][name] = totals["commands"].get(name, 0) + count
            _merge_ttt(totals["tool_turn_tokens"], s.get("tool_turn_tokens") or {})
            _merge_ttt(totals["command_turn_tokens"], s.get("command_turn_tokens") or {})
            for m in s.get("models") or []:
                if m and m not in models_seen:
                    models_seen.append(m)
            b = s.get("git_branch")
            if b and b not in branches_seen:
                branches_seen.append(b)
            _merge_per_model(totals["per_model"], s.get("per_model") or {})

        if project_dir.exists():
            for fp in project_dir.glob("*.jsonl"):
                if not _convo_allowed(fp):
                    continue
                seen_paths.add(str(fp))
                try:
                    s = _stats(fp, fp.stat())
                except OSError:
                    continue
                _sum_into_active(s)
                dd = s.get("deleted_delta") or {}
                _fold_delta_into(deleted_total, dd)

        if archive_dir.exists():
            for fp in archive_dir.glob("*.jsonl"):
                if not _convo_allowed(fp):
                    continue
                seen_paths.add(str(fp))
                try:
                    s = _stats(fp, fp.stat())
                except OSError:
                    continue
                # Archived convo totals -> archived_delta (fold the same shape
                # that _stats returns). Don't touch the active totals.
                _fold_delta_into(archived_total, {
                    "input_tokens": s.get("input_tokens") or 0,
                    "output_tokens": s.get("output_tokens") or 0,
                    "cache_read_tokens": s.get("cache_read_tokens") or 0,
                    "cache_creation_tokens": s.get("cache_creation_tokens") or 0,
                    "thinking_count": s.get("thinking_count") or 0,
                    "tool_uses": s.get("tool_uses") or {},
                })
                for name, count in (s.get("commands") or {}).items():
                    archived_total["commands"][name] = archived_total["commands"].get(name, 0) + count
                _merge_ttt(archived_total["tool_turn_tokens"],
                           s.get("tool_turn_tokens") or {})
                _merge_ttt(archived_total["command_turn_tokens"],
                           s.get("command_turn_tokens") or {})
                # Archived convos can still have their own deleted_delta if
                # messages were deleted before archiving.
                dd = s.get("deleted_delta") or {}
                _fold_delta_into(deleted_total, dd)

        # Tombstones: skip when tag-filtering (see comment in api_overview).
        if allowed_ids is None:
            for root in (project_dir, archive_dir):
                for path, entry in peek_cache.iter_folder(root):
                    if path in seen_paths:
                        continue
                    dd = entry.get("deleted_delta") or {}
                    _fold_delta_into(deleted_total, dd)

        totals["models"] = models_seen
        totals["branches"] = branches_seen
        totals["archived_delta"] = archived_total
        totals["deleted_delta"] = deleted_total
        out[folder] = totals
    return jsonify(out)


@app.route("/api/projects/<folder>/conversations")
def api_conversations(folder):
    project_dir = CLAUDE_PROJECTS_DIR / folder
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 30))
    sort = request.args.get("sort", "recent")
    desc = request.args.get("desc", "1") != "0"

    files = _convo_files(project_dir)
    total = len(files)

    if sort == "size":
        files.sort(key=lambda t: t[1].st_size, reverse=desc)
    elif sort == "recent":
        files.sort(key=lambda t: t[1].st_mtime, reverse=desc)
    # for "msgs" and "context" we need per-file data — sort after peeking all files

    # Build full metadata for the page (or all for client-side sort on msgs/context)
    needs_full_scan = sort in ("msgs", "context")
    target = files if needs_full_scan else files[offset:offset + limit]

    convos = []
    for f, stat in target:
        size_kb = stat.st_size / 1024
        peek = _peek(f, stat)
        c = {
            "id": f.stem,
            "size_kb": round(size_kb, 1),
            "last_modified": _mtime_iso(stat.st_mtime),
            "preview": peek["preview"][:150],
            "last_preview": peek.get("last_preview", "(empty)")[:150],
            "cwd": peek["cwd"],
        }
        if peek.get("agent"):
            c["agent"] = peek["agent"]
        if sort == "msgs":
            cached = peek_cache.get(f, stat)
            if cached and "message_count" in cached:
                c["message_count"] = cached["message_count"]
            else:
                with open(f, "rb") as fh:
                    c["message_count"] = sum(1 for _ in fh)
                peek_cache.set(f, stat, message_count=c["message_count"])
        elif sort == "context":
            s = _stats(f, stat)
            c["_context_tokens"] = (
                (s.get("last_context_input_tokens") or 0)
                + (s.get("last_context_cache_creation_tokens") or 0)
                + (s.get("last_context_cache_read_tokens") or 0)
            )
        convos.append(c)

    if sort == "msgs":
        convos.sort(key=lambda c: c.get("message_count", 0), reverse=desc)
        convos = convos[offset:offset + limit]
    elif sort == "context":
        convos.sort(key=lambda c: c.get("_context_tokens", 0), reverse=desc)
        convos = convos[offset:offset + limit]
        for c in convos:
            c.pop("_context_tokens", None)

    return jsonify({"items": convos, "total": total, "offset": offset, "limit": limit})


@app.route("/api/projects/<folder>/refresh-cache", methods=["POST"])
def api_refresh_cache(folder):
    """Drop all sidecar and in-process cache entries for this project.

    Escape hatch for suspected cache staleness. Correctness shouldn't require
    this (cache keys on mtime+size), but network-mounted dirs with clock skew
    or manually rewritten jsonls can still surprise us.
    """
    project_dir = CLAUDE_PROJECTS_DIR / folder
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404
    peek_cache.invalidate_folder(project_dir)
    # Clear in-process lru_caches too. They're global (not per-folder), so
    # this drops entries for every project — fine, they refill lazily.
    _peek_jsonl_cached.cache_clear()
    _custom_title_cached.cache_clear()
    _parse_messages_cached.cache_clear()
    _inline_agent_runs_from_parent.cache_clear()
    return jsonify({"ok": True})


@app.route("/api/meta/context-window")
def api_context_window():
    """Return the effective context-window size to render percentages against.

    The JSONL doesn't record whether a session is on the 200k- or 1M-token
    plan (the `message.model` field is identical). We use two signals:

    1. `LLM_LENS_CONTEXT_WINDOW` env var — explicit override, wins if set.
    2. Max observed `last_context_*` tokens across every cached session —
       if any one session has exceeded 200k, the account must be on the 1M
       plan, so we apply 1M to every session to keep the denominator honest.
       Still defaults to 200k when nothing is cached yet.
    """
    override = os.environ.get("LLM_LENS_CONTEXT_WINDOW", "").strip()
    if override:
        try:
            val = int(override)
            return jsonify({"plan_window": val, "max_observed": None, "source": "env"})
        except ValueError:
            pass

    max_observed = 0
    for _, entry in peek_cache.iter_all():
        ctx = (int(entry.get("last_context_input_tokens") or 0)
               + int(entry.get("last_context_cache_creation_tokens") or 0)
               + int(entry.get("last_context_cache_read_tokens") or 0))
        if ctx > max_observed:
            max_observed = ctx

    plan_window = 1_000_000 if max_observed > 200_000 else 200_000
    return jsonify({
        "plan_window": plan_window,
        "max_observed": max_observed,
        "source": "inferred",
    })


def _convo_path(folder: str, convo_id: str) -> Path | None:
    """Return the live path if it exists, else archive path if it exists,
    else None. Lets read-only endpoints serve both states transparently.
    """
    live = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if live.exists():
        return live
    archived = _archive_path(folder, convo_id)
    if archived.exists():
        return archived
    return None


@app.route("/api/projects/<folder>/stats", methods=["POST"])
def api_conversation_stats(folder):
    """Batch lookup of per-convo stats (tokens, models, tool/thinking counts,
    git branch). Checks live then archive path so the hydration call works
    uniformly for either state. First scan is cached persistently.
    """
    project_dir = CLAUDE_PROJECTS_DIR / folder
    archive_dir = _archive_folder(folder)

    ids = (request.get_json(silent=True) or {}).get("ids") or []
    out = {}
    for convo_id in ids:
        fp = project_dir / f"{convo_id}.jsonl"
        if not fp.exists():
            fp = archive_dir / f"{convo_id}.jsonl"
        if not fp.exists():
            continue
        out[convo_id] = _stats(fp, fp.stat())
    return jsonify(out)


@app.route("/api/projects/<folder>/conversations/<convo_id>/stats")
def api_single_conversation_stats(folder, convo_id):
    fp = _convo_path(folder, convo_id)
    if not fp:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(_stats(fp, fp.stat()))


@app.route("/api/projects/<folder>/names", methods=["POST"])
def api_conversation_names(folder):
    """Batch lookup of `/rename`-assigned titles. Checks live then archive."""
    project_dir = CLAUDE_PROJECTS_DIR / folder
    archive_dir = _archive_folder(folder)

    ids = (request.get_json(silent=True) or {}).get("ids") or []
    out = {}
    for convo_id in ids:
        fp = project_dir / f"{convo_id}.jsonl"
        if not fp.exists():
            fp = archive_dir / f"{convo_id}.jsonl"
        if not fp.exists():
            continue
        title = _custom_title(fp, fp.stat())
        if title:
            out[convo_id] = title
    return jsonify(out)


# ---------------------------------------------------------------------------
# API: Messages (paginated)
# ---------------------------------------------------------------------------

@app.route("/api/projects/<folder>/conversations/<convo_id>")
def api_conversation(folder, convo_id):
    filepath = _convo_path(folder, convo_id)
    if not filepath:
        return jsonify({"error": "Conversation not found"}), 404

    limit = int(request.args.get("limit", 50))
    offset_arg = request.args.get("offset")

    stat = filepath.stat()
    main = _parse_messages_cached(str(filepath), stat.st_mtime, stat.st_size)

    total_main = len(main)
    # Default to the most recent page (chat-style), so long conversations
    # open at the latest messages and the "Earlier" button can page backwards.
    if offset_arg is None:
        page_start = max(0, total_main - limit)
    else:
        page_start = max(0, min(int(offset_arg), total_main))
    page_main = main[page_start:page_start + limit]

    runs = _agent_runs_for_convo(folder, convo_id)
    agent_runs = []
    for r in runs.values():
        out = {
            "run_id": r["run_id"],
            "name": r["name"],
            "source": r["source"],
            "message_count": r["message_count"],
            "first_ts": r["first_ts"],
        }
        # Anchor fields are format-specific: subagent runs point to a parent
        # Task/Agent tool_use_id (may be None for standalone subagents);
        # inline runs point to the main-message uuid their cluster branched
        # from. The frontend uses whichever is present to place the marker.
        if "anchor_tool_use_id" in r:
            out["anchor_tool_use_id"] = r["anchor_tool_use_id"]
        if "anchor_uuid" in r:
            out["anchor_uuid"] = r["anchor_uuid"]
        agent_runs.append(out)
    agent_runs.sort(key=lambda r: r["first_ts"] or "")

    peek = _peek(filepath, stat)
    stats = _stats(filepath, stat)
    header = {
        "agent": peek.get("agent"),
        "last_context_input_tokens": stats.get("last_context_input_tokens"),
        "last_context_cache_creation_tokens": stats.get("last_context_cache_creation_tokens"),
        "last_context_cache_read_tokens": stats.get("last_context_cache_read_tokens"),
        "last_model_for_context": stats.get("last_model_for_context"),
    }

    return jsonify({
        "main": page_main,
        "total": total_main,
        "offset": page_start,
        "limit": limit,
        "agent_runs": agent_runs,
        "header": header,
    })



@app.route("/api/projects/<folder>/conversations/<convo_id>/agent/<run_id>")
def api_agent_run(folder, convo_id, run_id):
    """Messages for one agent run.

    `run_id` is the filename `<hash>` for subagent runs, or `inline:<uuid>`
    for legacy inline clusters. Returns the same shape as `api_conversation`
    so the frontend reuses its renderer; offset/limit aren't wired (runs
    are small enough to ship whole).
    """
    runs = _agent_runs_for_convo(folder, convo_id)
    run = runs.get(run_id)
    if not run:
        return jsonify({"error": "Agent run not found"}), 404
    messages = _load_agent_run_messages(run)
    return jsonify({
        "main": messages,
        "total": len(messages),
        "offset": 0,
        "limit": len(messages),
        "agent_name": run["name"],
        "run_id": run_id,
        "parent_convo_id": convo_id,
    })


@app.route("/api/projects/<folder>/conversations/<convo_id>/raw")
def api_conversation_raw(folder, convo_id):
    """Return the unmodified source .jsonl as an attachment.

    Preserves all fields the parser strips (parentUuid, sessionId, cwd,
    message.id, usage, toolUseResult, etc.) so the download round-trips.
    """
    filepath = _convo_path(folder, convo_id)
    if not filepath:
        return jsonify({"error": "Conversation not found"}), 404
    return send_file(
        filepath,
        mimetype="application/x-ndjson",
        as_attachment=True,
        download_name=f"{convo_id}.jsonl",
    )


# ---------------------------------------------------------------------------
# API: Mutations (all invalidate cache)
# ---------------------------------------------------------------------------

@app.route("/api/projects/<folder>/conversations/<convo_id>", methods=["DELETE"])
def api_delete_conversation(folder, convo_id):
    """Delete a convo regardless of whether it's live or archived.

    Tombstones stats into peek_cache so deleted_delta aggregations see the
    prior totals even after the file is gone.
    """
    live_fp = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    live_sd = CLAUDE_PROJECTS_DIR / folder / convo_id
    arch_fp = _archive_path(folder, convo_id)
    arch_sd = _archive_folder(folder) / convo_id

    if live_fp.exists():
        filepath, subdir = live_fp, live_sd
    elif arch_fp.exists():
        filepath, subdir = arch_fp, arch_sd
    else:
        return jsonify({"error": "Not found"}), 404

    try:
        final_stats = _stats(filepath, filepath.stat())
    except OSError:
        final_stats = {}
    filepath.unlink()
    if subdir.exists():
        shutil.rmtree(subdir)
    sidecar = _duplicate_sidecar_path(filepath)
    if sidecar.exists():
        sidecar.unlink()
    peek_cache.mark_deleted(filepath, final_stats)
    tag_store.remove_conversation(folder, convo_id)
    _invalidate_cache_for(filepath)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/conversations/<convo_id>/duplicate", methods=["POST"])
def api_duplicate_conversation(folder, convo_id):
    src = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404
    new_id = str(uuid_mod.uuid4())
    dst = CLAUDE_PROJECTS_DIR / folder / f"{new_id}.jsonl"

    # Capture parent stats before the copy — these are the shared-prefix stats
    # at fork time. Stored in a sidecar and subtracted by `_stats` so totals
    # aren't double-counted while parent still exists.
    try:
        parent_stats = _stats(src, src.stat())
    except OSError:
        parent_stats = {}

    # Stream-rewrite sessionId / uuid / parentUuid so Claude Code's `/resume`
    # keys don't collide with the parent. The old→new uuid map keeps the
    # parent-chain intact within the duplicate; parentUuids pointing outside
    # the file collapse to None (mirrors `remap_parent` in api_extract_messages).
    uuid_map: dict = {}
    def _remap(old):
        if old is None:
            return None
        nu = uuid_map.get(old)
        if nu is None:
            nu = str(uuid_mod.uuid4())
            uuid_map[old] = nu
        return nu

    with open(src, "r") as fh_in, open(dst, "w") as fh_out:
        for line in fh_in:
            if not line.strip():
                fh_out.write(line)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                fh_out.write(line)
                continue
            if "sessionId" in entry:
                entry["sessionId"] = new_id
            if "uuid" in entry:
                entry["uuid"] = _remap(entry["uuid"])
            if entry.get("parentUuid") is not None:
                entry["parentUuid"] = uuid_map.get(entry["parentUuid"])
            fh_out.write(json.dumps(entry) + "\n")

    # Subagent subdir copied as-is; those nested sessions still carry the
    # parent's sessionId. Fixing that is out of scope for this change.
    src_sub = CLAUDE_PROJECTS_DIR / folder / convo_id
    if src_sub.exists():
        shutil.copytree(src_sub, CLAUDE_PROJECTS_DIR / folder / new_id)

    sidecar = _duplicate_sidecar_path(dst)
    sidecar.write_text(json.dumps({
        "duplicate_of": convo_id,
        "shared_prefix_stats": {
            k: parent_stats.get(k) for k in (
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "thinking_count", "tool_uses",
                "per_model",
            )
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))
    _invalidate_cache_for(dst)
    return jsonify({"ok": True, "new_id": new_id})


@app.route("/api/projects/<folder>/conversations/<convo_id>/archive", methods=["POST"])
def api_archive_conversation(folder, convo_id):
    """Move a convo's jsonl (and optional subagent dir) to ARCHIVE_ROOT.

    Non-destructive: content is preserved byte-for-byte and mtime is kept so
    time-bucketed stats don't shift. Peek_cache entries are re-keyed on the
    new path via natural cache miss — no explicit cache surgery needed.
    """
    live_path = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    live_sub = CLAUDE_PROJECTS_DIR / folder / convo_id
    if not live_path.exists():
        return jsonify({"error": "Not found"}), 404
    dest = _archive_path(folder, convo_id)
    dest_sub = _archive_folder(folder) / convo_id
    if dest.exists():
        return jsonify({"error": "Archive already exists at destination"}), 409
    _move_preserving_mtime(live_path, dest)
    if live_sub.exists():
        _move_preserving_mtime(live_sub, dest_sub)
    live_sidecar = _duplicate_sidecar_path(live_path)
    if live_sidecar.exists():
        _move_preserving_mtime(live_sidecar, _duplicate_sidecar_path(dest))
    # Drop cached active-state entry for the live path; archived path will
    # populate its own entry on first stats read.
    peek_cache.invalidate(live_path)
    _invalidate_cache_for(live_path)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/conversations/<convo_id>/unarchive", methods=["POST"])
def api_unarchive_conversation(folder, convo_id):
    """Restore an archived convo back to ~/.claude/projects/<folder>/.

    Fails with 409 if a live convo already exists at the target path — safer
    than silently overwriting if Claude Code happened to start a new session
    with the same UUID while this one was archived.
    """
    src = _archive_path(folder, convo_id)
    src_sub = _archive_folder(folder) / convo_id
    if not src.exists():
        return jsonify({"error": "Not archived"}), 404
    dest = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    dest_sub = CLAUDE_PROJECTS_DIR / folder / convo_id
    if dest.exists():
        return jsonify({"error": "A live conversation already exists at the target path"}), 409
    dest.parent.mkdir(parents=True, exist_ok=True)
    _move_preserving_mtime(src, dest)
    if src_sub.exists():
        _move_preserving_mtime(src_sub, dest_sub)
    src_sidecar = _duplicate_sidecar_path(src)
    if src_sidecar.exists():
        _move_preserving_mtime(src_sidecar, _duplicate_sidecar_path(dest))
    peek_cache.invalidate(src)
    _invalidate_cache_for(dest)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/archived")
def api_archived_conversations(folder):
    """List archived convos in the same shape as `api_conversations`.

    Simpler than the live endpoint — no paginated sort modes; archived sets
    are expected to be small. If that assumption breaks, mirror the live
    endpoint's pagination.
    """
    archive_dir = _archive_folder(folder)
    if not archive_dir.exists():
        return jsonify({"items": [], "total": 0})
    files = _convo_files(archive_dir)
    convos = []
    for f, stat in files:
        peek = _peek(f, stat)
        convos.append({
            "id": f.stem,
            "size_kb": round(stat.st_size / 1024, 1),
            "last_modified": _mtime_iso(stat.st_mtime),
            "preview": peek["preview"][:150],
            "last_preview": peek.get("last_preview", "(empty)")[:150],
            "cwd": peek["cwd"],
            "archived": True,
        })
    return jsonify({"items": convos, "total": len(convos)})


@app.route("/api/projects/<folder>/conversations/bulk-archive", methods=["POST"])
def api_bulk_archive(folder):
    """Move many live convos to the archive dir in one call.

    Missing ids are silently skipped (idempotent-ish). Destination collisions
    are also skipped so a re-run after a partial failure is safe.
    """
    ids = request.get_json().get("ids", [])
    archived = 0
    skipped = []
    for cid in ids:
        live_fp = CLAUDE_PROJECTS_DIR / folder / f"{cid}.jsonl"
        live_sd = CLAUDE_PROJECTS_DIR / folder / cid
        if not live_fp.exists():
            skipped.append(cid)
            continue
        dest = _archive_path(folder, cid)
        dest_sd = _archive_folder(folder) / cid
        if dest.exists():
            skipped.append(cid)
            continue
        _move_preserving_mtime(live_fp, dest)
        if live_sd.exists():
            _move_preserving_mtime(live_sd, dest_sd)
        peek_cache.invalidate(live_fp)
        archived += 1
    _invalidate_cache_for(CLAUDE_PROJECTS_DIR / folder)
    return jsonify({"ok": True, "archived": archived, "skipped": skipped})


@app.route("/api/projects/<folder>/conversations/bulk-unarchive", methods=["POST"])
def api_bulk_unarchive(folder):
    """Restore many archived convos back to the live dir.

    Any id whose live path already exists is reported as skipped rather than
    overwritten — protects against collisions with fresh Claude Code sessions
    that happened to reuse the same UUID.
    """
    ids = request.get_json().get("ids", [])
    unarchived = 0
    skipped = []
    for cid in ids:
        src = _archive_path(folder, cid)
        src_sd = _archive_folder(folder) / cid
        if not src.exists():
            skipped.append(cid)
            continue
        dest = CLAUDE_PROJECTS_DIR / folder / f"{cid}.jsonl"
        dest_sd = CLAUDE_PROJECTS_DIR / folder / cid
        if dest.exists():
            skipped.append(cid)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _move_preserving_mtime(src, dest)
        if src_sd.exists():
            _move_preserving_mtime(src_sd, dest_sd)
        peek_cache.invalidate(src)
        unarchived += 1
    _invalidate_cache_for(CLAUDE_PROJECTS_DIR / folder)
    return jsonify({"ok": True, "unarchived": unarchived, "skipped": skipped})


# ── Tag management ────────────────────────────────────────────────────

@app.route("/api/projects/<folder>/tags")
def api_get_tags(folder):
    """Return label definitions + per-conversation assignments for a project."""
    return jsonify(tag_store.get_project(folder))


@app.route("/api/projects/<folder>/tags/labels", methods=["PUT"])
def api_set_tag_labels(folder):
    """Replace label definitions for a project (max 5)."""
    labels = (request.get_json(silent=True) or {}).get("labels", [])
    tag_store.set_labels(folder, labels)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/tags/assign", methods=["POST"])
def api_assign_tags(folder):
    """Set the tags for one conversation."""
    payload = request.get_json(silent=True) or {}
    convo_id = payload.get("convo_id", "")
    tags = payload.get("tags", [])
    tag_store.assign(folder, convo_id, tags)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/tags/bulk-assign", methods=["POST"])
def api_bulk_assign_tag(folder):
    """Add or remove a single tag from multiple conversations."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", [])
    tag_id = payload.get("tag", 0)
    add = payload.get("add", True)
    count = tag_store.bulk_assign(folder, ids, tag_id, add)
    return jsonify({"ok": True, "count": count})


# ── Project-tag namespace ────────────────────────────────────────────
# Parallel to the per-folder convo-tag endpoints above, but operating on
# the single project-level tag set. Assignments are keyed by folder name
# (not convo id), so "tag this project" vs. "tag this conversation" are
# cleanly separated namespaces — they can share label names, they can
# share colors, but their id spaces and assignment tables never mix.


@app.route("/api/tags/projects")
def api_get_project_tags():
    """Return the project-level label definitions + folder→tag assignments."""
    return jsonify(tag_store.get(("projects",)))


@app.route("/api/tags/projects/labels", methods=["PUT"])
def api_set_project_tag_labels():
    labels = (request.get_json(silent=True) or {}).get("labels", [])
    tag_store.set_labels_ns(("projects",), labels)
    return jsonify({"ok": True})


@app.route("/api/tags/projects/assign", methods=["POST"])
def api_assign_project_tags():
    payload = request.get_json(silent=True) or {}
    folder = payload.get("folder", "")
    tags = payload.get("tags", [])
    tag_store.assign_ns(("projects",), folder, tags)
    return jsonify({"ok": True})


@app.route("/api/tags/projects/bulk-assign", methods=["POST"])
def api_bulk_assign_project_tag():
    payload = request.get_json(silent=True) or {}
    folders = payload.get("folders", [])
    tag_id = payload.get("tag", 0)
    add = payload.get("add", True)
    count = tag_store.bulk_assign_ns(("projects",), folders, tag_id, add)
    return jsonify({"ok": True, "count": count})


def _tool_use_ids(entry):
    msg = entry.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b.get("id") for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]


def _strip_blocks(entry, drop_tool_use_ids=None, drop_tool_result_ids=None):
    """Remove tool_use/tool_result blocks whose ids are in the given sets.
    Returns True if the message still has content, False if it's now empty."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return True
    drop_tu = drop_tool_use_ids or set()
    drop_tr = drop_tool_result_ids or set()
    kept = []
    for b in content:
        if not isinstance(b, dict):
            kept.append(b)
            continue
        if b.get("type") == "tool_use" and b.get("id") in drop_tu:
            continue
        if b.get("type") == "tool_result" and b.get("tool_use_id") in drop_tr:
            continue
        kept.append(b)
    msg["content"] = kept
    return bool(kept)


def _find_message_file(folder: str, convo_id: str, msg_uuid: str) -> Path | None:
    """Locate the JSONL file containing `msg_uuid`.

    Searches the main convo file first, then subagent run files under
    `<convo_id>/subagents/*.jsonl`. Returns the file Path or None.

    Enables edit/delete on subagent-run messages (which live in separate
    files from the main convo) without requiring the frontend to pass a
    file hint — the backend resolves it.
    """
    main = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    candidates = []
    if main.exists():
        candidates.append(main)
    sub = _subagents_dir(folder, convo_id)
    if sub.is_dir():
        candidates.extend(sorted(sub.glob("agent-*.jsonl")))
    for fp in candidates:
        try:
            with open(fp, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("uuid") == msg_uuid:
                        return fp
        except OSError:
            continue
    return None


@app.route("/api/projects/<folder>/conversations/<convo_id>/messages/<msg_uuid>", methods=["DELETE"])
def api_delete_message(folder, convo_id, msg_uuid):
    # Resolve the actual file — message may live in the main convo OR in a
    # subagent run file (<convo_id>/subagents/agent-*.jsonl). Without this
    # lookup, deletes on subagent-view messages returned 404.
    filepath = _find_message_file(folder, convo_id, msg_uuid)
    if not filepath:
        return jsonify({"error": "Not found"}), 404

    pre_stat = filepath.stat()

    with open(filepath, "r") as f:
        raw_lines = f.readlines()

    entries = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue

    deleted = next((e for e in entries if e.get("uuid") == msg_uuid), None)
    if not deleted:
        return jsonify({"error": "Message not found"}), 404

    # Tombstone the full per-entry stats so totals survive the re-scan.
    # Uses the uniform before/after pattern with after = empty (message is
    # fully removed). ctx is replayed to the target's position so fields
    # that depend on cross-entry state (compact_summary_chars) are captured.
    # Children whose tool_result blocks get stripped below are already
    # accounted for in the parent assistant turn — we don't double-count.
    ctx_base = _ctx_at(entries, msg_uuid)
    before = _message_stats(deleted, ctx=dict(ctx_base))
    delta = _diff_stats(before, _empty_stats())
    delta["messages_deleted"] = 1
    peek_cache.accumulate_deleted(filepath, pre_stat, delta)

    deleted_parent = deleted.get("parentUuid")
    orphaned_tool_use_ids = set(_tool_use_ids(deleted))

    out = []
    for e in entries:
        if e.get("uuid") == msg_uuid:
            continue
        if e.get("parentUuid") == msg_uuid:
            e["parentUuid"] = deleted_parent
        if orphaned_tool_use_ids:
            has_content = _strip_blocks(e, drop_tool_result_ids=orphaned_tool_use_ids)
            if not has_content:
                # message became empty after stripping refs — skip it and
                # re-point its children to its parent
                for child in entries:
                    if child.get("parentUuid") == e.get("uuid"):
                        child["parentUuid"] = e.get("parentUuid")
                continue
        out.append(e)

    with open(filepath, "w") as f:
        for e in out:
            f.write(json.dumps(e) + "\n")
    _invalidate_cache_for(filepath)
    return jsonify({"ok": True})


def _is_prose_only(message: dict) -> bool:
    """Prose-only = `message.content` is a string or a list containing only
    text blocks. Any tool_use, tool_result, thinking, image, or other
    structured block disqualifies — their shape carries meaning that scrub
    would break.
    """
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        if not content:
            return False
        return all(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        )
    return False


def _replace_content(message: dict, new_text: str):
    """Replace the text content of a prose-only message in place.

    For string content, swaps the string. For list-of-text-blocks content,
    collapses to a single text block — the UI shows the blocks joined as one
    buffer when editing, so saving should produce a single coherent block.
    Leaves `usage`, `uuid`, `parentUuid`, etc. untouched.
    """
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = new_text
        return
    if isinstance(content, list):
        message["content"] = [{"type": "text", "text": new_text}]


# Default word/phrase lists. Conservative starting points — users curate
# their own via the /api/word-lists endpoint. Stored as plain strings;
# matched case-insensitively with word boundaries (swears) or as exact
# phrases (filler).
_DEFAULT_SWEARS = [
    # `*` = stem match (catches plurals/conjugations: fuck/fucks/fucker/...).
    # Bare words match exactly to protect short stems from false positives
    # (e.g. plain "ass" instead of "ass*" so we don't catch "assess").
    "fuck*", "shit*", "damn*", "bitch*", "crap*", "piss*", "bullshit*",
    "asshole", "ass", "dick", "cunt",
]

# Sycophancy + AI-tic phrases that add tokens without information. These
# are the "drift" patterns — agent over-apologizing, hyping the user,
# meta-narrating its own thinking. Removing them shortens context without
# losing meaning.
_DEFAULT_FILLER = [
    # Substring match (case-insensitive) — one entry per concept. Trailing
    # punctuation (!/./?) is implicitly handled: matching "You're absolutely
    # right" as a substring still catches "You're absolutely right!" in
    # text (the "!" is left in place; the whitespace-cleanup pass
    # normalizes it). Keep contraction variants when they're different
    # substrings ("You're" vs "You are").
    "You're absolutely right",
    "You are absolutely right",
    "That's a great question",
    "Great question",
    "Excellent question",
    "Certainly",
    "Of course",
    "Absolutely",
    "I'd be happy to help",
    "I'm happy to help",
    "I hope this helps",
    "I hope that helps",
    "Feel free to ask if you have more questions",
    "Let me know if you have any questions",
    "I apologize for the confusion",
    "I apologize for any confusion",
    "Sorry for the confusion",
    "My apologies for the confusion",
    "Let me think about this step by step",
    "Let me think step by step",
    "Let's think step by step",
    "Let me break this down",
    "Let me explain",
    "To summarize",
    "In summary",
    "In conclusion",
]


# Verbosity signalers — separate motivation from swears/filler. These
# don't change agent behavior; they just cost tokens and obscure meaning.
# Default list is conservative: obviousness signalers and meta-commentary
# phrases that are near-always filler. Users can opt into sincerity
# markers, intensifiers, and hedges via the curation modal.
# Matched word-bounded, case-insensitive (same mechanism as swears, minus
# the `*` stem syntax — verbosity entries are literal words/phrases).
_DEFAULT_VERBOSITY = [
    # Obviousness signalers — assert agreement rather than earn it
    "needless to say",
    "of course",
    "obviously",
    "clearly",
    "evidently",
    # Meta-commentary phrases (near-always filler)
    "that's a great question",
    "that is a great question",
    "what I'd say is",
    "what I'm saying is",
    "what I mean is",
    "the thing is",
    "at the end of the day",
    "when all is said and done",
    # Throat-clearing transitions — only the near-always-filler ones.
    # Risky single words like "so", "well", "look", "right" stay opt-in
    # (users add via Curate); those words carry real meaning too often.
    "you know",
    "I mean",
    "anyway",
    # Diplomatic throat-clearing
    "to be honest",
    "to be fair",
]


# Global whitelist — honored by every remove_* transform (the transform
# filters its trigger list against this before building its regex) and
# by the custom_filter scan (candidates containing any whitelist entry
# as a case-insensitive substring are excluded). Ships with a curated
# seed of SWE / tool / code-concept terms that would otherwise surface
# as scan repeats or partial-match verbosity. Users curate via the
# Curate word lists modal.
_DEFAULT_WHITELIST = [
    # SWE / perf terms
    "benchmark",
    "latency",
    "throughput",
    "compile",
    "refactor",
    "deprecate",
    "idempotent",
    "invariant",
    # Tool / product names
    "claude",
    "anthropic",
    "openai",
    "github",
    "docker",
    "kubernetes",
    "postgres",
    # Code concepts
    "interface",
    "async",
    "await",
    "generic",
    "abstract",
]


# Abbreviation substitution pairs — opt-in modifier to `remove_verbosity`
# when the user toggles `apply_abbreviations` in the Curate word lists
# modal. Each pair is `{from, to}`; matched word-bounded and case-
# insensitive, longest-`from`-first so multi-word sources don't get
# chopped up by shorter rules. Split into two groups by motivation:
#
#   1. Token-savers (de-abbreviation): shorthand forms that tokenize
#      worse than the spelled-out word. Empirically verified via
#      tiktoken o200k_base — e.g. `i.e.` is 3 tokens, `ie` is 1;
#      `w/` is 2, `with` is 1. Net saves tokens.
#
#   2. Token-neutral abbreviation (disk savers): common words whose
#      short form tokenizes to the same count but saves characters on
#      disk / in-flight bytes. Cosmetic churn but real byte savings.
#
# Pairs that cost tokens either direction (`people`→`ppl`,
# `probably`→`probs`) are omitted. Users can add/remove in the modal.
_DEFAULT_ABBREVIATIONS = [
    # Token-savers — normalize costly shorthand to the full word
    {"from": "w/o", "to": "without"},
    {"from": "w/", "to": "with"},
    {"from": "i.e.", "to": "ie"},
    {"from": "e.g.", "to": "eg"},
    {"from": "smth", "to": "something"},
    {"from": "sm1", "to": "someone"},
    {"from": "probs", "to": "probably"},
    {"from": "ppl", "to": "people"},
    # Token-savers — abbreviate long forms that cost tokens
    {"from": "I don't think", "to": "idt"},
    {"from": "you are", "to": "ur"},
    {"from": "youre", "to": "ur"},
    {"from": "you're", "to": "ur"},
    {"from": "right now", "to": "rn"},
    {"from": "thank you", "to": "ty"},
    {"from": "thankyou", "to": "ty"},
    {"from": "thx", "to": "ty"},
    {"from": "tysm", "to": "ty"},
    {"from": "do not", "to": "don't"},
    # Disk-savers (token-neutral)
    {"from": "your", "to": "ur"},
    {"from": "you", "to": "u"},
    {"from": "are", "to": "r"},
    {"from": "please", "to": "pls"},
    {"from": "because", "to": "bc"},
    {"from": "thanks", "to": "ty"},
    {"from": "what", "to": "wat"},
    {"from": "why", "to": "y"},
]


def _word_lists_path() -> Path:
    return Path.home() / ".cache" / "llm-lens" / "word_lists.json"


def _load_word_lists() -> dict:
    """Return the effective lists (user-saved, falling back to defaults per
    key). The user file fully replaces the defaults for whichever keys it
    contains — that way a user can prune the default list, not just
    augment it."""
    path = _word_lists_path()
    user = {}
    if path.exists():
        try:
            user = json.loads(path.read_text()) or {}
        except (OSError, json.JSONDecodeError):
            user = {}
    return {
        "swears": user.get("swears") if isinstance(user.get("swears"), list) else list(_DEFAULT_SWEARS),
        "filler": user.get("filler") if isinstance(user.get("filler"), list) else list(_DEFAULT_FILLER),
        "verbosity": user.get("verbosity") if isinstance(user.get("verbosity"), list) else list(_DEFAULT_VERBOSITY),
        "custom_filter": user.get("custom_filter") if isinstance(user.get("custom_filter"), list) else [],
        "whitelist": user.get("whitelist") if isinstance(user.get("whitelist"), list) else list(_DEFAULT_WHITELIST),
        "lowercase_user_text": bool(user["lowercase_user_text"]) if isinstance(user.get("lowercase_user_text"), bool) else False,
        "abbreviations": _coerce_abbreviations(user.get("abbreviations")) if isinstance(user.get("abbreviations"), list) else [dict(p) for p in _DEFAULT_ABBREVIATIONS],
        "apply_abbreviations": bool(user["apply_abbreviations"]) if isinstance(user.get("apply_abbreviations"), bool) else False,
        "custom_filter_enabled": bool(user["custom_filter_enabled"]) if isinstance(user.get("custom_filter_enabled"), bool) else False,
        "collapse_punct_repeats": bool(user["collapse_punct_repeats"]) if isinstance(user.get("collapse_punct_repeats"), bool) else False,
    }


def _save_word_lists(data: dict) -> dict:
    path = _word_lists_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {
        "swears": [s for s in (data.get("swears") or []) if isinstance(s, str) and s.strip()],
        "filler": [s for s in (data.get("filler") or []) if isinstance(s, str) and s.strip()],
        "verbosity": [s for s in (data.get("verbosity") or []) if isinstance(s, str) and s.strip()],
        "custom_filter": [s for s in (data.get("custom_filter") or []) if isinstance(s, str) and s.strip()],
        "whitelist": [s for s in (data.get("whitelist") or []) if isinstance(s, str) and s.strip()],
        "lowercase_user_text": bool(data["lowercase_user_text"]) if isinstance(data.get("lowercase_user_text"), bool) else False,
        "abbreviations": _coerce_abbreviations(data.get("abbreviations")),
        "apply_abbreviations": bool(data["apply_abbreviations"]) if isinstance(data.get("apply_abbreviations"), bool) else False,
        "custom_filter_enabled": bool(data["custom_filter_enabled"]) if isinstance(data.get("custom_filter_enabled"), bool) else False,
        "collapse_punct_repeats": bool(data["collapse_punct_repeats"]) if isinstance(data.get("collapse_punct_repeats"), bool) else False,
    }
    path.write_text(json.dumps(cleaned, indent=2))
    return cleaned


def _coerce_abbreviations(raw) -> list:
    """Accept only {from: str, to: str} dicts with a non-blank `from`.
    Everything else is silently dropped — same posture as the other list
    cleaners."""
    out = []
    if not isinstance(raw, list):
        return out
    for p in raw:
        if not isinstance(p, dict):
            continue
        frm = p.get("from")
        to = p.get("to")
        if not isinstance(frm, str) or not isinstance(to, str):
            continue
        if not frm.strip():
            continue
        out.append({"from": frm, "to": to})
    return out


_DEFAULT_DOWNLOAD_FIELDS = {
    "uuid": True,
    "role": True,
    "content": True,
    "timestamp": True,
    "commands": False,
    "model": False,
    "usage": False,
}
# role + content are structurally required for a message to be meaningful;
# the frontend disables these checkboxes so they're always included.
_REQUIRED_DOWNLOAD_FIELDS = ("role", "content")
_ALL_DOWNLOAD_FIELDS = tuple(_DEFAULT_DOWNLOAD_FIELDS.keys())


def _download_fields_path() -> Path:
    return Path.home() / ".cache" / "llm-lens" / "download_fields.json"


def _load_download_fields() -> dict:
    """Return the effective on/off state for each exportable field."""
    path = _download_fields_path()
    user = {}
    if path.exists():
        try:
            user = json.loads(path.read_text()) or {}
        except (OSError, json.JSONDecodeError):
            user = {}
    out = {}
    for k in _ALL_DOWNLOAD_FIELDS:
        if k in _REQUIRED_DOWNLOAD_FIELDS:
            out[k] = True
        else:
            out[k] = bool(user[k]) if isinstance(user.get(k), bool) else _DEFAULT_DOWNLOAD_FIELDS[k]
    return out


def _save_download_fields(data: dict) -> dict:
    path = _download_fields_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {}
    for k in _ALL_DOWNLOAD_FIELDS:
        if k in _REQUIRED_DOWNLOAD_FIELDS:
            cleaned[k] = True
        else:
            cleaned[k] = bool(data.get(k)) if isinstance(data.get(k), bool) else _DEFAULT_DOWNLOAD_FIELDS[k]
    path.write_text(json.dumps(cleaned, indent=2))
    return cleaned


@app.route(
    "/api/projects/<folder>/conversations/<convo_id>/messages/<msg_uuid>/edit",
    methods=["POST"],
)
def api_edit_message(folder, convo_id, msg_uuid):
    """Replace the text content of a message in place.

    Body: {"text": "<new content>"}. Preserves `usage`, `uuid`, `parentUuid`,
    `sessionId`, and message shape. Same resume-chain caveats as scrub.

    Stats preservation: computes before/after `_message_stats` with ctx
    replayed to the target's position, and tombstones the positive diff.
    When the edit collapses tool_use / thinking / Bash content, the lost
    counts (tool_uses, commands, thinking_count, tool_turn_tokens,
    command_turn_tokens, thinking_output_tokens_estimate, plus per-model
    breakdowns) move into `deleted_delta`. Tokens stay at 0 in the delta
    because `usage` is preserved on the message. Always bumps
    `messages_edited` so the "edited" smart filter matches even for
    prose-only edits that produce an empty stats diff.
    """
    payload = request.get_json(silent=True) or {}
    new_text = payload.get("text")
    if not isinstance(new_text, str):
        return jsonify({"error": "Missing 'text' (string) in body."}), 400

    # Resolve the actual file — may be the main convo or a subagent run file.
    filepath = _find_message_file(folder, convo_id, msg_uuid)
    if not filepath:
        return jsonify({"error": "Not found"}), 404

    pre_stat = filepath.stat()

    with open(filepath, "r") as f:
        raw_lines = f.readlines()

    entries = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue

    target = next((e for e in entries if e.get("uuid") == msg_uuid), None)
    if not target:
        return jsonify({"error": "Message not found"}), 404

    ctx_base = _ctx_at(entries, msg_uuid)
    before = _message_stats(target, ctx=dict(ctx_base))

    message = target.get("message") or {}
    _replace_content(message, new_text)

    after = _message_stats(target, ctx=dict(ctx_base))
    delta = _diff_stats(before, after)
    delta["messages_edited"] = 1
    peek_cache.accumulate_deleted(filepath, pre_stat, delta)

    with open(filepath, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    _invalidate_cache_for(filepath)
    return jsonify({"ok": True})


@app.route("/api/word-lists", methods=["GET"])
def api_get_word_lists():
    """Return the effective swears + filler + verbosity lists. Defaults
    shipped in code are used for any key the user hasn't customized."""
    return jsonify(_load_word_lists())


@app.route("/api/word-lists", methods=["POST"])
def api_save_word_lists():
    """Persist user-curated lists. Body shape:
        {"swears": [...], "filler": [...], "verbosity": [...]}
    Each list fully replaces the default for that key — pass an empty
    list to disable a category entirely. Missing keys fall back to the
    shipped defaults on subsequent loads.
    """
    payload = request.get_json(silent=True) or {}
    saved = _save_word_lists(payload)
    return jsonify(saved)


@app.route("/api/word-lists/defaults", methods=["GET"])
def api_get_word_list_defaults():
    """Surface the shipped defaults so the curation UI can offer a 'reset'
    or show them as faded suggestions."""
    return jsonify({
        "swears": list(_DEFAULT_SWEARS),
        "filler": list(_DEFAULT_FILLER),
        "verbosity": list(_DEFAULT_VERBOSITY),
        "custom_filter": [],
        "whitelist": list(_DEFAULT_WHITELIST),
        "lowercase_user_text": False,
        "abbreviations": [dict(p) for p in _DEFAULT_ABBREVIATIONS],
        "apply_abbreviations": False,
        "custom_filter_enabled": False,
        "collapse_punct_repeats": False,
    })


def _extract_message_text(msg: dict) -> list:
    """Return a list of text strings from a message's content. Pulls from
    string content and text blocks in list content; skips tool_use,
    tool_result, thinking, image, and other structured blocks."""
    content = msg.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str) and t:
                    out.append(t)
        return out
    return []


def _ngram_scan_single_convo(texts, min_length_chars: int, min_count: int, exclusions: list, n_min: int = 1, n_max: int = 3) -> list:
    """Within-convo n-gram frequency scan. Counts total occurrences of
    each phrase inside the convo (not distinct-convo count — this runs
    on a single convo so every hit is a real occurrence). Default range
    is 1-3 words: user-aligned, biased toward concrete boilerplate
    ("error occurred", "let me check") rather than random statistical
    coincidences that longer n-grams produce.

    Returns a list of candidate phrases (lowercased), filtered by length
    and occurrence thresholds, with phrases containing any exclusion
    entry (case-insensitive substring) removed. Sorted by
    count*length descending, capped at 200.
    """
    from collections import Counter
    counter: Counter = Counter()
    for text in texts:
        words = text.lower().split()
        n_words = len(words)
        if n_words < n_min or n_words > 500:
            continue
        upper = min(n_max, n_words) + 1
        for n in range(n_min, upper):
            for i in range(n_words - n + 1):
                phrase = " ".join(words[i:i + n])
                if len(phrase) >= min_length_chars:
                    counter[phrase] += 1
    excl_lower = [
        e.lower() for e in (exclusions or [])
        if isinstance(e, str) and e.strip()
    ]
    out = []
    for phrase, count in counter.items():
        if count < min_count:
            continue
        if any(e in phrase for e in excl_lower):
            continue
        out.append((phrase, count))
    out.sort(key=lambda pc: -(pc[1] * len(pc[0])))
    return [p for p, _ in out[:200]]





@app.route(
    "/api/projects/<folder>/conversations/<convo_id>/custom-filter/scan",
    methods=["POST"],
)
def api_custom_filter_scan(folder, convo_id):
    """Scan a single conversation for repeated phrases. Body:
    {min_length_chars: int, min_count: int, n_min: int, n_max: int}.
    min_count is the number of occurrences within this conversation.
    Excludes phrases that contain any current custom_filter or whitelist
    entry (case-insensitive substring).

    Emits progress to stdout so a slow scan isn't opaque; bails with 413
    if the intermediate counter exceeds 2M entries."""
    import time

    payload = request.get_json(silent=True) or {}
    try:
        min_length_chars = int(payload.get("min_length_chars", 6))
        min_count = int(payload.get("min_count", 3))
        n_min = int(payload.get("n_min", 1))
        n_max = int(payload.get("n_max", 3))
    except (TypeError, ValueError):
        return jsonify({"error": "min_length_chars, min_count, n_min, n_max must be integers"}), 400
    if min_length_chars < 1 or min_count < 2:
        return jsonify({"error": "min_length_chars >= 1 and min_count >= 2 required"}), 400
    if n_min < 1 or n_max < n_min or n_max > 10:
        return jsonify({"error": "n_min >= 1, n_max >= n_min, n_max <= 10 required"}), 400

    filepath = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not filepath.exists():
        return jsonify({"error": "Conversation not found"}), 404

    lists = _load_word_lists()
    exclusions = list(lists.get("custom_filter") or []) + list(lists.get("whitelist") or [])

    t_start = time.time()
    print(
        f"[custom-filter-scan] {folder}/{convo_id} n={n_min}-{n_max} "
        f"min_length={min_length_chars} min_count={min_count}",
        flush=True,
    )

    texts: list = []
    msg_count = 0
    try:
        with open(filepath, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                blocks = _extract_message_text(msg)
                if blocks:
                    msg_count += 1
                    texts.extend(blocks)
    except OSError as e:
        return jsonify({"error": f"Could not read conversation: {e}"}), 500

    candidates = _ngram_scan_single_convo(
        texts, min_length_chars, min_count, exclusions, n_min=n_min, n_max=n_max
    )

    elapsed = time.time() - t_start
    print(
        f"[custom-filter-scan] done in {elapsed:.2f}s — "
        f"{len(candidates)} candidates from {msg_count} messages",
        flush=True,
    )

    return jsonify({"msg_count": msg_count, "candidates": candidates})





@app.route("/api/download-fields", methods=["GET"])
def api_get_download_fields():
    """Return the effective JSONL download field map.

    Keys cover every field the exporter can emit. `role` and `content` are
    always true (frontend disables those checkboxes — a message without them
    isn't useful to export)."""
    return jsonify(_load_download_fields())


@app.route("/api/download-fields", methods=["POST"])
def api_save_download_fields():
    """Persist the user's JSONL download field selection. Body shape:
        {"uuid": bool, "role": bool, ...}
    Unknown keys are ignored; required keys are forced true."""
    payload = request.get_json(silent=True) or {}
    return jsonify(_save_download_fields(payload))


@app.route("/api/projects/<folder>/conversations/<convo_id>/extract", methods=["POST"])
def api_extract_messages(folder, convo_id):
    """Create a new conversation from selected message UUIDs.

    Non-destructive: the source file is not modified. No deleted_delta is
    accumulated — the selected messages still exist in the source.
    """
    src = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404
    uuids = set(request.get_json().get("uuids", []))
    if not uuids:
        return jsonify({"error": "No messages selected"}), 400

    new_id = str(uuid_mod.uuid4())
    dst = CLAUDE_PROJECTS_DIR / folder / f"{new_id}.jsonl"

    with open(src, "r") as fin:
        all_entries = []
        for line in fin:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                all_entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue

    by_uuid = {e.get("uuid"): e for e in all_entries if e.get("uuid")}

    # Walk up to nearest extracted ancestor; None if none exist above.
    def remap_parent(entry):
        p = entry.get("parentUuid")
        while p and p not in uuids:
            parent = by_uuid.get(p)
            if not parent:
                return None
            p = parent.get("parentUuid")
        return p

    # Any tool_use/tool_result whose counterpart isn't in the extracted set
    # gets its block stripped (not the whole message dropped).
    extracted_tool_use_ids = set()
    extracted_tool_result_ids = set()
    for e in all_entries:
        if e.get("uuid") in uuids:
            extracted_tool_use_ids.update(_tool_use_ids(e))
            extracted_tool_result_ids.update(
                b.get("tool_use_id") for b in (e.get("message") or {}).get("content", [])
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
            )

    orphan_tool_uses = extracted_tool_use_ids - extracted_tool_result_ids
    orphan_tool_results = extracted_tool_result_ids - extracted_tool_use_ids

    with open(dst, "w") as fout:
        for e in all_entries:
            if e.get("type") == "file-history-snapshot":
                fout.write(json.dumps(e) + "\n")
                continue
            if e.get("uuid") not in uuids:
                continue
            has_content = _strip_blocks(
                e,
                drop_tool_use_ids=orphan_tool_uses,
                drop_tool_result_ids=orphan_tool_results,
            )
            if not has_content:
                continue
            e["parentUuid"] = remap_parent(e)
            fout.write(json.dumps(e) + "\n")

    _invalidate_cache_for(dst)
    return jsonify({"ok": True, "new_id": new_id})


@app.route("/api/projects/<folder>/conversations/bulk-delete", methods=["POST"])
def api_bulk_delete(folder):
    """Delete convos given by id. Handles both live and archived locations —
    if a convo is archived, we delete from the archive dir and still tombstone
    its stats so deleted_delta aggregations pick them up.
    """
    ids = request.get_json().get("ids", [])
    deleted = 0
    for cid in ids:
        live_fp = CLAUDE_PROJECTS_DIR / folder / f"{cid}.jsonl"
        live_sd = CLAUDE_PROJECTS_DIR / folder / cid
        arch_fp = _archive_path(folder, cid)
        arch_sd = _archive_folder(folder) / cid

        fp = live_fp if live_fp.exists() else (arch_fp if arch_fp.exists() else None)
        sd = live_sd if live_fp.exists() else (arch_sd if arch_fp.exists() else None)
        if not fp:
            continue
        try:
            final_stats = _stats(fp, fp.stat())
        except OSError:
            final_stats = {}
        fp.unlink()
        if sd and sd.exists():
            shutil.rmtree(sd)
        peek_cache.mark_deleted(fp, final_stats)
        tag_store.remove_conversation(folder, cid)
        deleted += 1
    _invalidate_cache_for(CLAUDE_PROJECTS_DIR / folder)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/projects/<folder>", methods=["DELETE"])
def api_delete_project(folder):
    d = CLAUDE_PROJECTS_DIR / folder
    if not d.exists():
        return jsonify({"error": "Not found"}), 404
    # Tombstone every jsonl in the folder so the project's totals stay visible
    # under "Include deleted".
    for fp in d.glob("*.jsonl"):
        try:
            final_stats = _stats(fp, fp.stat())
        except OSError:
            final_stats = {}
        peek_cache.mark_deleted(fp, final_stats)
    shutil.rmtree(d)
    tag_store.remove_folder(folder)
    _invalidate_cache_for(d)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print("Usage: llm-lens-web [PORT]\n\n"
              "  PORT    port to bind (default: 5111)\n\n"
              "Environment:\n"
              "  LLM_LENS_DEBUG=1   enable Flask auto-reload (dev only)\n")
        return
    try:
        port = int(args[0]) if args else 5111
    except ValueError:
        print(f"Error: invalid port '{args[0]}'. Expected an integer.", file=sys.stderr)
        sys.exit(2)
    debug = os.environ.get("LLM_LENS_DEBUG") == "1"
    print(f"llm-lens-web: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
