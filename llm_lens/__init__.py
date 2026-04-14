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
from datetime import datetime, timezone


def _mtime_iso(mtime: float) -> str:
    """Convert a POSIX mtime (UTC epoch) to an ISO-8601 UTC string.

    Using `datetime.fromtimestamp(mtime)` (no tz) gives a naive *local* time;
    suffixing `Z` would lie about the zone and cause the JS side to shift
    by the server's UTC offset a second time. Always anchor to UTC.
    """
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")

from flask import Flask, jsonify, request, send_from_directory

from . import peek_cache

app = Flask(__name__, static_folder="static")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# ---------------------------------------------------------------------------
# LRU cache – keyed on (path, mtime, size) so edits/new files invalidate
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _peek_jsonl_cached(filepath_str: str, mtime: float, size: int) -> dict:
    """Cached peek: only re-reads when file changes."""
    filepath = Path(filepath_str)
    cwd = None
    preview = None
    first_ts = None
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
            if not cwd and entry.get("cwd"):
                cwd = entry["cwd"]
            if not first_ts and entry.get("timestamp"):
                first_ts = entry["timestamp"]
            role = entry.get("message", {}).get("role")
            content = entry.get("message", {}).get("content", "")
            if role == "user" and isinstance(content, str) and not entry.get("isMeta") and not content.startswith("<local-command"):
                if not preview:
                    preview = re.sub(r"<[^>]+>", "", content[:200]).strip()
                    if cwd:
                        break
    return {"cwd": cwd, "preview": preview or "(empty)", "first_ts": first_ts}


def _peek(filepath: Path, stat: os.stat_result) -> dict:
    cached = peek_cache.get(filepath, stat)
    if cached and "preview" in cached:
        return {"cwd": cached.get("cwd"), "preview": cached["preview"], "first_ts": cached.get("first_ts")}
    result = _peek_jsonl_cached(str(filepath), stat.st_mtime, stat.st_size)
    peek_cache.set(filepath, stat, preview=result["preview"], cwd=result["cwd"], first_ts=result["first_ts"])
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
               "thinking_count", "git_branch")


@lru_cache(maxsize=256)
def _stats_cached(filepath_str: str, mtime: float, size: int):
    """One-pass aggregation of model(s), token totals, tool-use and thinking
    block counts, and the session's starting git branch. Token counts come
    from each assistant message's real `message.usage` — no estimation.
    """
    input_tokens = output_tokens = cache_read = cache_creation = 0
    models = []
    tool_uses = {}
    thinking_count = 0
    git_branch = None

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

                msg = entry.get("message") or {}
                if msg.get("role") != "assistant":
                    continue

                model = msg.get("model")
                if model and model not in models:
                    models.append(model)

                usage = msg.get("usage") or {}
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
                cache_creation += int(usage.get("cache_creation_input_tokens") or 0)

                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        t = block.get("type")
                        if t == "tool_use":
                            name = block.get("name") or "?"
                            tool_uses[name] = tool_uses.get(name, 0) + 1
                        elif t == "thinking":
                            thinking_count += 1
    except OSError:
        pass

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "models": models,
        "tool_uses": tool_uses,
        "thinking_count": thinking_count,
        "git_branch": git_branch,
    }


def _stats(filepath, stat) -> dict:
    cached = peek_cache.get(filepath, stat)
    if cached and all(k in cached for k in _STATS_KEYS):
        return {k: cached[k] for k in _STATS_KEYS}
    stats = _stats_cached(str(filepath), stat.st_mtime, stat.st_size)
    peek_cache.set(filepath, stat, **stats)
    return stats


@lru_cache(maxsize=128)
def _parse_messages_cached(filepath_str: str, mtime: float, size: int):
    """Cached full message parse."""
    filepath = Path(filepath_str)
    main = []
    side = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "file-history-snapshot":
                continue

            role = entry.get("message", {}).get("role", entry.get("type", "unknown"))
            content = entry.get("message", {}).get("content", "")
            ts = entry.get("timestamp")
            uid = entry.get("uuid")
            is_meta = entry.get("isMeta", False)
            is_sidechain = entry.get("isSidechain", False)

            if isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    t = block.get("type")
                    if t == "text":
                        parts.append(block.get("text", ""))
                    elif t == "tool_use":
                        parts.append(f"[Tool: {block.get('name', '?')}]")
                    elif t == "tool_result":
                        parts.append("[Tool Result]")
                    elif t == "thinking":
                        th = block.get("thinking", "")
                        if th:
                            parts.append(f"<thinking>{th}</thinking>")
                content = "\n".join(parts)

            if is_meta:
                continue
            if role == "assistant" and not content:
                continue

            msg = {"uuid": uid, "role": role, "content": content if isinstance(content, str) else str(content), "timestamp": ts}
            (side if is_sidechain else main).append(msg)

    def dedup(msgs):
        # Drop duplicate uuids (can happen when Claude Code /resume appends
        # replay entries). Keep the first occurrence so chronological order
        # and parent links stay stable. Content-based dedup was wrong: tool
        # messages like "[Tool Result]" repeat legitimately and must not
        # collapse.
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

    return dedup(main), dedup(side)


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


# ---------------------------------------------------------------------------
# API: Projects
# ---------------------------------------------------------------------------

@app.route("/api/projects")
def api_projects():
    if not CLAUDE_PROJECTS_DIR.exists():
        return jsonify([])

    projects = []
    for d in CLAUDE_PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        files = _convo_files(d)
        if not files:
            continue

        total_kb = sum(s.st_size for _, s in files) / 1024
        newest_file, newest_stat = files[0]
        peek = _peek(newest_file, newest_stat)

        projects.append({
            "folder": d.name,
            "path": peek["cwd"] or d.name,
            "conversation_count": len(files),
            "total_size_kb": round(total_kb, 1),
            "last_activity": _mtime_iso(newest_stat.st_mtime),
            "latest_preview": peek["preview"][:150],
        })

    projects.sort(key=lambda p: p["last_activity"], reverse=True)
    return jsonify(projects)


# ---------------------------------------------------------------------------
# API: Conversations (paginated)
# ---------------------------------------------------------------------------

_RANGE_SECONDS = {
    "day":   86_400,
    "week":  7 * 86_400,
    "month": 30 * 86_400,
    "year":  365 * 86_400,
}

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


@app.route("/api/overview")
def api_overview():
    """Account-wide totals + per-day buckets, filtered by convo mtime.

    Cheap bucketing: each jsonl is assigned entirely to its last_modified date.
    A session that spans multiple days lands on whatever day it was last
    touched. Good enough for an activity heatmap; per-turn bucketing is a
    future upgrade if needed.
    """
    import time
    rng = request.args.get("range", "all")
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    now = time.time()
    bucket_unit = _RANGE_BUCKET.get(rng, "day")

    # Windowed filter: `offset=0` is the current period ending now; negative
    # offsets page backward, positive would page forward (usually empty).
    range_sec = _RANGE_SECONDS.get(rng)
    if range_sec is None:
        cutoff = None
        cutoff_upper = None
    else:
        window_end = now + offset * range_sec
        cutoff = window_end - range_sec
        cutoff_upper = window_end
    since_iso = (datetime.utcfromtimestamp(cutoff).isoformat() + "Z") if cutoff else None
    until_iso = (datetime.utcfromtimestamp(cutoff_upper).isoformat() + "Z") if cutoff_upper else None

    totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "thinking_count": 0,
    }
    models_seen: list = []
    branches_seen: list = []
    by_period: dict = {}
    convo_count = 0

    if not CLAUDE_PROJECTS_DIR.exists():
        return jsonify({"range": rng, "bucket": bucket_unit, "offset": offset,
                        "since": since_iso, "until": until_iso,
                        "totals": totals, "convo_count": 0, "by_period": {}})

    # Optional folder scope — when set, only that project's jsonls contribute.
    folder = request.args.get("folder")
    if folder:
        scoped = CLAUDE_PROJECTS_DIR / folder
        dirs = [scoped] if scoped.is_dir() else []
    else:
        dirs = [d for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()]

    for project_dir in dirs:
        if not project_dir.is_dir():
            continue
        for fp in project_dir.glob("*.jsonl"):
            try:
                stat = fp.stat()
            except OSError:
                continue
            if cutoff is not None and stat.st_mtime < cutoff:
                continue
            if cutoff_upper is not None and stat.st_mtime >= cutoff_upper:
                continue
            s = _stats(fp, stat)
            convo_count += 1
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "thinking_count"):
                totals[k] += s.get(k) or 0
            for name, count in (s.get("tool_uses") or {}).items():
                totals["tool_uses"][name] = totals["tool_uses"].get(name, 0) + count
            for m in s.get("models") or []:
                if m and m not in models_seen:
                    models_seen.append(m)
            b = s.get("git_branch")
            if b and b not in branches_seen:
                branches_seen.append(b)

            key = _bucket_key(stat.st_mtime, bucket_unit)
            bucket = by_period.setdefault(key, {
                "input_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 0,
                "tool_calls": 0, "tool_uses": {}, "convos": 0,
            })
            bucket["input_tokens"] += s.get("input_tokens") or 0
            bucket["cache_read_tokens"] += s.get("cache_read_tokens") or 0
            bucket["cache_creation_tokens"] += s.get("cache_creation_tokens") or 0
            bucket["output_tokens"] += s.get("output_tokens") or 0
            for name, count in (s.get("tool_uses") or {}).items():
                bucket["tool_uses"][name] = bucket["tool_uses"].get(name, 0) + count
            bucket["tool_calls"] += sum((s.get("tool_uses") or {}).values())
            bucket["convos"] += 1

    totals["models"] = models_seen
    totals["branches"] = branches_seen
    return jsonify({
        "range": rng,
        "bucket": bucket_unit,
        "offset": offset,
        "since": since_iso,
        "until": until_iso,
        "totals": totals,
        "convo_count": convo_count,
        "by_period": by_period,
    })


@app.route("/api/projects/stats", methods=["POST"])
def api_projects_stats():
    """Aggregate per-convo stats up to the project level. Intended for the
    landing page to hydrate its cards after the initial list renders.

    Cold cache: scans every jsonl in every requested folder (cached per file
    via peek_cache, so subsequent hits are ~free).
    """
    folders = (request.get_json(silent=True) or {}).get("folders") or []
    out = {}
    for folder in folders:
        project_dir = CLAUDE_PROJECTS_DIR / folder
        if not project_dir.exists():
            continue
        totals = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0,
        }
        models_seen: list = []
        branches_seen: list = []
        for fp in project_dir.glob("*.jsonl"):
            try:
                s = _stats(fp, fp.stat())
            except OSError:
                continue
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "thinking_count"):
                totals[k] += s.get(k) or 0
            for name, count in (s.get("tool_uses") or {}).items():
                totals["tool_uses"][name] = totals["tool_uses"].get(name, 0) + count
            for m in s.get("models") or []:
                if m and m not in models_seen:
                    models_seen.append(m)
            b = s.get("git_branch")
            if b and b not in branches_seen:
                branches_seen.append(b)
        totals["models"] = models_seen
        totals["branches"] = branches_seen
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
    # for "msgs" we need line counts — do it after peeking all files

    # Build full metadata for the page (or all for client-side sort on msgs)
    target = files if sort == "msgs" else files[offset:offset + limit]

    convos = []
    for f, stat in target:
        size_kb = stat.st_size / 1024
        peek = _peek(f, stat)
        c = {
            "id": f.stem,
            "size_kb": round(size_kb, 1),
            "last_modified": _mtime_iso(stat.st_mtime),
            "preview": peek["preview"][:150],
            "cwd": peek["cwd"],
        }
        if sort == "msgs":
            cached = peek_cache.get(f, stat)
            if cached and "message_count" in cached:
                c["message_count"] = cached["message_count"]
            else:
                with open(f, "rb") as fh:
                    c["message_count"] = sum(1 for _ in fh)
                peek_cache.set(f, stat, message_count=c["message_count"])
        convos.append(c)

    if sort == "msgs":
        convos.sort(key=lambda c: c.get("message_count", 0), reverse=desc)
        convos = convos[offset:offset + limit]

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
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/stats", methods=["POST"])
def api_conversation_stats(folder):
    """Batch lookup of per-convo stats (tokens, models, tool/thinking counts,
    git branch). Separate endpoint so the list renders fast and cards
    hydrate in the background; first scan is cached persistently.
    """
    project_dir = CLAUDE_PROJECTS_DIR / folder
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    ids = (request.get_json(silent=True) or {}).get("ids") or []
    out = {}
    for convo_id in ids:
        fp = project_dir / f"{convo_id}.jsonl"
        if not fp.exists():
            continue
        out[convo_id] = _stats(fp, fp.stat())
    return jsonify(out)


@app.route("/api/projects/<folder>/conversations/<convo_id>/stats")
def api_single_conversation_stats(folder, convo_id):
    fp = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not fp.exists():
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(_stats(fp, fp.stat()))


@app.route("/api/projects/<folder>/names", methods=["POST"])
def api_conversation_names(folder):
    """Batch lookup of `/rename`-assigned titles. Separate endpoint so the
    conversations list can render immediately and hydrate names in the background.
    """
    project_dir = CLAUDE_PROJECTS_DIR / folder
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    ids = (request.get_json(silent=True) or {}).get("ids") or []
    out = {}
    for convo_id in ids:
        fp = project_dir / f"{convo_id}.jsonl"
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
    filepath = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not filepath.exists():
        return jsonify({"error": "Conversation not found"}), 404

    limit = int(request.args.get("limit", 50))
    offset_arg = request.args.get("offset")

    stat = filepath.stat()
    main, side = _parse_messages_cached(str(filepath), stat.st_mtime, stat.st_size)

    total_main = len(main)
    # Default to the most recent page (chat-style), so long conversations
    # open at the latest messages and the "Earlier" button can page backwards.
    if offset_arg is None:
        page_start = max(0, total_main - limit)
    else:
        page_start = max(0, min(int(offset_arg), total_main))
    page_main = main[page_start:page_start + limit]

    return jsonify({
        "main": page_main,
        "sidechain": side,
        "total": total_main,
        "offset": page_start,
        "limit": limit,
    })


# ---------------------------------------------------------------------------
# API: Mutations (all invalidate cache)
# ---------------------------------------------------------------------------

@app.route("/api/projects/<folder>/conversations/<convo_id>", methods=["DELETE"])
def api_delete_conversation(folder, convo_id):
    filepath = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    subagent_dir = CLAUDE_PROJECTS_DIR / folder / convo_id
    if not filepath.exists():
        return jsonify({"error": "Not found"}), 404
    filepath.unlink()
    if subagent_dir.exists():
        shutil.rmtree(subagent_dir)
    _invalidate_cache_for(filepath)
    return jsonify({"ok": True})


@app.route("/api/projects/<folder>/conversations/<convo_id>/duplicate", methods=["POST"])
def api_duplicate_conversation(folder, convo_id):
    src = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404
    new_id = str(uuid_mod.uuid4())
    dst = CLAUDE_PROJECTS_DIR / folder / f"{new_id}.jsonl"
    shutil.copy2(src, dst)
    src_sub = CLAUDE_PROJECTS_DIR / folder / convo_id
    if src_sub.exists():
        shutil.copytree(src_sub, CLAUDE_PROJECTS_DIR / folder / new_id)
    _invalidate_cache_for(dst)
    return jsonify({"ok": True, "new_id": new_id})


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


@app.route("/api/projects/<folder>/conversations/<convo_id>/messages/<msg_uuid>", methods=["DELETE"])
def api_delete_message(folder, convo_id, msg_uuid):
    filepath = CLAUDE_PROJECTS_DIR / folder / f"{convo_id}.jsonl"
    if not filepath.exists():
        return jsonify({"error": "Not found"}), 404

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


@app.route("/api/projects/<folder>/conversations/<convo_id>/extract", methods=["POST"])
def api_extract_messages(folder, convo_id):
    """Create a new conversation from selected message UUIDs."""
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
    ids = request.get_json().get("ids", [])
    deleted = 0
    for cid in ids:
        fp = CLAUDE_PROJECTS_DIR / folder / f"{cid}.jsonl"
        sd = CLAUDE_PROJECTS_DIR / folder / cid
        if fp.exists():
            fp.unlink()
            if sd.exists():
                shutil.rmtree(sd)
            deleted += 1
    _invalidate_cache_for(CLAUDE_PROJECTS_DIR / folder)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/projects/<folder>", methods=["DELETE"])
def api_delete_project(folder):
    d = CLAUDE_PROJECTS_DIR / folder
    if not d.exists():
        return jsonify({"error": "Not found"}), 404
    shutil.rmtree(d)
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
