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

from flask import Flask, jsonify, request, send_from_directory

from . import peek_cache

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
               "thinking_count", "git_branch", "per_model")


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
    per_model: dict = {}
    last_model = "?"

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
                if model:
                    last_model = model
                mkey = model or last_model
                pm = per_model.setdefault(mkey, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "tool_uses": {},
                })

                usage = msg.get("usage") or {}
                in_t = int(usage.get("input_tokens") or 0)
                out_t = int(usage.get("output_tokens") or 0)
                cr_t = int(usage.get("cache_read_input_tokens") or 0)
                cc_t = int(usage.get("cache_creation_input_tokens") or 0)
                input_tokens += in_t
                output_tokens += out_t
                cache_read += cr_t
                cache_creation += cc_t
                pm["input_tokens"] += in_t
                pm["output_tokens"] += out_t
                pm["cache_read_tokens"] += cr_t
                pm["cache_creation_tokens"] += cc_t

                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        t = block.get("type")
                        if t == "tool_use":
                            name = block.get("name") or "?"
                            tool_uses[name] = tool_uses.get(name, 0) + 1
                            pm["tool_uses"][name] = pm["tool_uses"].get(name, 0) + 1
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
        "per_model": per_model,
    }


def _message_stats(entry: dict) -> dict:
    """Extract the same shape as `_stats_cached` but for one parsed JSONL entry.

    Used by delete paths to compute the stats delta to tombstone before the
    file is mutated. Mirrors the per-entry branch in `_stats_cached`.
    """
    result = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "thinking_count": 0,
    }
    msg = entry.get("message") or {}
    if msg.get("role") != "assistant":
        return result
    usage = msg.get("usage") or {}
    result["input_tokens"] = int(usage.get("input_tokens") or 0)
    result["output_tokens"] = int(usage.get("output_tokens") or 0)
    result["cache_read_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
    result["cache_creation_tokens"] = int(usage.get("cache_creation_input_tokens") or 0)
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "tool_use":
                name = block.get("name") or "?"
                result["tool_uses"][name] = result["tool_uses"].get(name, 0) + 1
            elif t == "thinking":
                result["thinking_count"] += 1
    return result


def _fold_delta_into(target: dict, delta: dict):
    """Merge a `deleted_delta` dict into an accumulator in place."""
    if not delta:
        return
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_creation_tokens", "thinking_count", "messages_deleted"):
        target[k] = target.get(k, 0) + (delta.get(k) or 0)
    for name, count in (delta.get("tool_uses") or {}).items():
        target["tool_uses"][name] = target["tool_uses"].get(name, 0) + count


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

    totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "thinking_count": 0,
        "per_model": {},
    }
    archived_total = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
    }
    deleted_total = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
    }
    models_seen: list = []
    branches_seen: list = []
    by_period: dict = {}
    convo_count = 0
    archived_count = 0
    seen_paths: set = set()

    def _mk_bucket():
        return {
            "input_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "output_tokens": 0,
            "tool_calls": 0, "tool_uses": {}, "convos": 0,
            "per_model": {},
            "archived_delta": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
            },
            "deleted_delta": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
            },
        }

    def _fold_into_bucket_delta(bucket_key: str, field: str, delta: dict):
        b = by_period.setdefault(bucket_key, _mk_bucket())
        _fold_delta_into(b.setdefault(field, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
        }), delta)

    # Optional folder scope — when set, only that project's jsonls (live +
    # archive) contribute.
    folder = request.args.get("folder")

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
            for mkey, mstats in (s.get("per_model") or {}).items():
                pm = bucket["per_model"].setdefault(mkey, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "tool_uses": {},
                })
                tpm = totals["per_model"].setdefault(mkey, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "tool_uses": {},
                })
                for field in ("input_tokens", "output_tokens",
                              "cache_read_tokens", "cache_creation_tokens"):
                    v = mstats.get(field) or 0
                    pm[field] += v
                    tpm[field] += v
                for name, count in (mstats.get("tool_uses") or {}).items():
                    pm["tool_uses"][name] = pm["tool_uses"].get(name, 0) + count
                    tpm["tool_uses"][name] = tpm["tool_uses"].get(name, 0) + count

            dd = s.get("deleted_delta") or {}
            if dd:
                _fold_delta_into(deleted_total, dd)
                _fold_into_bucket_delta(key, "deleted_delta", dd)

    # Archived convos — same time-bucketing logic, but contribute to
    # `archived_delta` instead of the active totals.
    for arch_dir in _archive_dirs():
        for fp in arch_dir.glob("*.jsonl"):
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
            key = _bucket_key(stat.st_mtime, bucket_unit)
            _fold_into_bucket_delta(key, "archived_delta", archived_entry)
            dd = s.get("deleted_delta") or {}
            if dd:
                _fold_delta_into(deleted_total, dd)
                _fold_into_bucket_delta(key, "deleted_delta", dd)

    # Tombstones — entries under any scoped folder whose file is gone. Bucket
    # them by `deleted_at` since they have no mtime.
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

    Cold cache: scans every jsonl (live + archive) once per file rev.
    """
    folders = (request.get_json(silent=True) or {}).get("folders") or []
    out = {}
    for folder in folders:
        project_dir = CLAUDE_PROJECTS_DIR / folder
        archive_dir = _archive_folder(folder)
        totals = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0,
            "per_model": {},
        }
        archived_total = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_uses": {}, "thinking_count": 0, "messages_deleted": 0,
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
            for m in s.get("models") or []:
                if m and m not in models_seen:
                    models_seen.append(m)
            b = s.get("git_branch")
            if b and b not in branches_seen:
                branches_seen.append(b)
            for mkey, mstats in (s.get("per_model") or {}).items():
                pm = totals["per_model"].setdefault(mkey, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "tool_uses": {},
                })
                pm["input_tokens"] += mstats.get("input_tokens") or 0
                pm["output_tokens"] += mstats.get("output_tokens") or 0
                pm["cache_read_tokens"] += mstats.get("cache_read_tokens") or 0
                pm["cache_creation_tokens"] += mstats.get("cache_creation_tokens") or 0
                for name, count in (mstats.get("tool_uses") or {}).items():
                    pm["tool_uses"][name] = pm["tool_uses"].get(name, 0) + count

        if project_dir.exists():
            for fp in project_dir.glob("*.jsonl"):
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
                # Archived convos can still have their own deleted_delta if
                # messages were deleted before archiving.
                dd = s.get("deleted_delta") or {}
                _fold_delta_into(deleted_total, dd)

        # Pick up tombstones under live + archive folders.
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

    # Accumulate stats about the message being removed BEFORE we mutate, so
    # the totals survive the re-scan. Children whose blocks get stripped are
    # usually tool_result-only; we don't count those separately (their tokens
    # are already attributed to the parent assistant turn).
    delta = _message_stats(deleted)
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


_SCRUB_PLACEHOLDER = "."


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


def _scrub_content(message: dict):
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _SCRUB_PLACEHOLDER
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = _SCRUB_PLACEHOLDER


_WS_RUN_RE = re.compile(r"[ \t]+")
_NL_RUN_RE = re.compile(r"\n{3,}")


def _normalize_ws(text: str) -> str:
    """Collapse cosmetic whitespace without touching code-shaped content.

    Rules, applied per line:
      - Inside a triple-backtick fenced code block: leave the line alone.
      - Lines that start with a space or tab: leave leading whitespace
        intact (preserves Python/YAML/Makefile indentation, list/quote
        continuations, indent-style code blocks). Only rstrip trailing
        whitespace.
      - All other (prose) lines: collapse runs of inline spaces/tabs to a
        single space, then rstrip.
    Then: collapse 3+ consecutive newlines to 2 to tighten paragraph gaps.

    Inline code spans (`` `like this` ``) inside prose lines are not
    detected — internal multi-space inside a backtick span will still be
    collapsed. Acceptable v1 trade-off; documented limitation.
    """
    out = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if line and line[0] in " \t":
            out.append(line.rstrip())
            continue
        out.append(_WS_RUN_RE.sub(" ", line).rstrip())
    return _NL_RUN_RE.sub("\n\n", "\n".join(out))


def _normalize_whitespace_content(message: dict):
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _normalize_ws(content)
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = _normalize_ws(block.get("text") or "")


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
    "You're absolutely right!",
    "You're absolutely right.",
    "You are absolutely right.",
    "Great question!",
    "Great question.",
    "That's a great question!",
    "Excellent question!",
    "Certainly!",
    "Of course!",
    "Absolutely!",
    "I'd be happy to help!",
    "I'm happy to help.",
    "I hope this helps!",
    "I hope that helps.",
    "Feel free to ask if you have more questions.",
    "Let me know if you have any questions.",
    "I apologize for the confusion.",
    "I apologize for any confusion.",
    "Sorry for the confusion.",
    "My apologies for the confusion.",
    "Let me think about this step by step.",
    "Let me think step by step.",
    "Let's think step by step.",
    "Let me break this down.",
    "Let me explain.",
    "To summarize:",
    "In summary,",
    "In conclusion,",
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
    }


def _save_word_lists(data: dict) -> dict:
    path = _word_lists_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {
        "swears": [s for s in (data.get("swears") or []) if isinstance(s, str) and s.strip()],
        "filler": [s for s in (data.get("filler") or []) if isinstance(s, str) and s.strip()],
    }
    path.write_text(json.dumps(cleaned, indent=2))
    return cleaned


_SAFE_STEM_SUFFIXES = ["", "s", "es", "ed", "er", "ers", "ing", "ings", "y", "ery", "ies"]


def _swear_regex_parts(words: list) -> list:
    """Translate user entries into regex fragments.

    A trailing `*` marks a stem — the regex matches the stem followed by
    one of a closed list of safe suffixes (`s`, `es`, `ed`, `er`, `ers`,
    `ing`, `y`, etc.), bounded by word boundaries. So `fuck*` catches
    fuck/fucks/fucker/fucking/etc., and `ass*` catches ass/asses/assing
    *without* matching `assist`/`assess`/`assistant` — the suffix `ist` /
    `ess` / `istant` aren't in the safe list.

    Plain (no `*`) words match exactly with word boundaries.
    """
    parts = []
    for w in words or []:
        if not isinstance(w, str) or not w.strip():
            continue
        if w.endswith("*") and len(w) > 1:
            stem = re.escape(w[:-1])
            suffix_alt = "|".join(re.escape(s) for s in _SAFE_STEM_SUFFIXES)
            parts.append(f"{stem}(?:{suffix_alt})")
        else:
            parts.append(re.escape(w))
    return parts


def _strip_swears(text: str, words: list) -> str:
    parts = _swear_regex_parts(words)
    if not text or not parts:
        return text
    pattern = r"\b(?:" + "|".join(parts) + r")\b"
    out = re.sub(pattern, "", text, flags=re.IGNORECASE)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out


def _strip_filler(text: str, phrases: list) -> str:
    if not text or not phrases:
        return text
    # Sort longest-first so "I apologize for any confusion." matches before
    # a shorter substring would.
    pattern = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    out = re.sub(pattern, "", text, flags=re.IGNORECASE)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()


def _apply_text_transform(message: dict, fn):
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = fn(content)
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = fn(block.get("text") or "")


def _remove_swears_content(message: dict):
    words = _load_word_lists()["swears"]
    _apply_text_transform(message, lambda t: _strip_swears(t, words))


def _remove_filler_content(message: dict):
    phrases = _load_word_lists()["filler"]
    _apply_text_transform(message, lambda t: _strip_filler(t, phrases))


_TRANSFORMS = {
    "scrub": _scrub_content,
    "normalize_whitespace": _normalize_whitespace_content,
    "remove_swears": _remove_swears_content,
    "remove_filler": _remove_filler_content,
}


@app.route(
    "/api/projects/<folder>/conversations/<convo_id>/messages/<msg_uuid>/scrub",
    methods=["POST"],
)
def api_scrub_message(folder, convo_id, msg_uuid):
    """Apply a text transform to a prose-only message in place.

    `kind` (body JSON) selects the transform:
      - "scrub" (default): replace text with "."
      - "normalize_whitespace": collapse runs of spaces/tabs; 3+ newlines → 2

    Both preserve `usage`, `uuid`, `parentUuid`, `sessionId`, and any
    non-text structural blocks. Stats unchanged because `usage` is left
    alone — it's a historical billing record, not a recount of bytes.
    """
    payload = request.get_json(silent=True) or {}
    kind = payload.get("kind") or "scrub"
    transform = _TRANSFORMS.get(kind)
    if transform is None:
        return jsonify({
            "error": f"Unknown transform kind '{kind}'. "
                     f"Supported: {sorted(_TRANSFORMS)}"
        }), 400

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

    target = next((e for e in entries if e.get("uuid") == msg_uuid), None)
    if not target:
        return jsonify({"error": "Message not found"}), 404

    message = target.get("message") or {}
    if not _is_prose_only(message):
        return jsonify({
            "error": "Transform only applies to prose-only messages (text "
                     "blocks only — no tool_use, tool_result, thinking, or "
                     "images)."
        }), 400

    transform(message)

    with open(filepath, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    _invalidate_cache_for(filepath)
    return jsonify({"ok": True, "kind": kind})


@app.route("/api/word-lists", methods=["GET"])
def api_get_word_lists():
    """Return the effective swears + filler lists. Defaults shipped in
    code are used for any key the user hasn't customized."""
    return jsonify(_load_word_lists())


@app.route("/api/word-lists", methods=["POST"])
def api_save_word_lists():
    """Persist user-curated lists. Body shape:
        {"swears": [...], "filler": [...]}
    Each list fully replaces the default for that key — pass an empty
    list to disable a category entirely.
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
    })


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
