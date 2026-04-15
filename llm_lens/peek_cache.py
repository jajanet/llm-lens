"""Persistent sidecar cache for expensive JSONL scans.

Keyed on absolute file path; each entry stores mtime+size so stale entries
auto-invalidate the moment the underlying file changes. Read on startup,
written atomically with a short debounce so bursts of updates collapse.

Intentionally schemaless — callers stash whatever they like in the entry
(`custom_title`, `message_count`, `preview`, ...). Adding a new field
requires no migration: readers check for presence.
"""

import json
import os
import threading
import time
from pathlib import Path

# Active-stats fields that should be zeroed on full-file deletion. Kept here
# so `mark_deleted` doesn't need to know about the call site's shape.
_ACTIVE_SCALAR_FIELDS = ("input_tokens", "output_tokens",
                         "cache_read_tokens", "cache_creation_tokens",
                         "thinking_count")
_ACTIVE_DICT_FIELDS = ("tool_uses", "per_model")
_ACTIVE_LIST_FIELDS = ("models",)

CACHE_DIR = Path.home() / ".cache" / "llm-lens"
CACHE_PATH = CACHE_DIR / "sessions.json"
FLUSH_DELAY = 2.0  # seconds

_lock = threading.Lock()
_store: dict = {}
_dirty = False
_flush_timer: threading.Timer | None = None


def _load():
    global _store
    try:
        with open(CACHE_PATH, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _store = data
    except (OSError, json.JSONDecodeError):
        _store = {}


_load()


def _schedule_flush():
    global _flush_timer
    if _flush_timer is not None:
        return
    _flush_timer = threading.Timer(FLUSH_DELAY, flush)
    _flush_timer.daemon = True
    _flush_timer.start()


def flush():
    """Atomically write the current store to disk. Safe to call anytime."""
    global _dirty, _flush_timer
    with _lock:
        if not _dirty:
            _flush_timer = None
            return
        snapshot = dict(_store)
        _dirty = False
        _flush_timer = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def get(filepath, stat) -> dict | None:
    """Return cached entry dict if mtime+size still match, else None."""
    key = str(filepath)
    with _lock:
        entry = _store.get(key)
    if not entry:
        return None
    if entry.get("mtime") != stat.st_mtime or entry.get("size") != stat.st_size:
        return None
    return entry


def get_raw(filepath) -> dict | None:
    """Return the raw entry regardless of mtime/size match. Useful for reading
    deleted_delta / deleted_at on tombstones that no longer have a live file."""
    with _lock:
        entry = _store.get(str(filepath))
    return dict(entry) if entry else None


def set(filepath, stat, **fields):
    """Merge fields into the entry for this file, keyed by (mtime, size)."""
    global _dirty
    key = str(filepath)
    with _lock:
        existing = _store.get(key)
        # Drop old data if file rev changed — but carry deletion history across
        # the rev bump so pruned tokens survive re-scans.
        if (not existing
                or existing.get("mtime") != stat.st_mtime
                or existing.get("size") != stat.st_size):
            preserved = {p: existing[p] for p in _PRESERVE_ON_INVALIDATE
                         if existing and p in existing}
            existing = {"mtime": stat.st_mtime, "size": stat.st_size, **preserved}
        existing.update(fields)
        _store[key] = existing
        _dirty = True
    _schedule_flush()


_PRESERVE_ON_INVALIDATE = ("deleted_delta", "deleted_at")


def invalidate(filepath):
    """Drop active fields but preserve any deletion history on the entry.

    After this call the entry has no mtime/size, so `get()` will miss and the
    caller re-scans from disk. `deleted_delta` and `deleted_at` survive so
    aggregation can still count pruned tokens/tool-uses.
    """
    global _dirty
    key = str(filepath)
    with _lock:
        entry = _store.get(key)
        if not entry:
            return
        preserved = {k: entry[k] for k in _PRESERVE_ON_INVALIDATE if k in entry}
        if preserved:
            _store[key] = preserved
        else:
            del _store[key]
        _dirty = True
    _schedule_flush()


def invalidate_folder(folder_path):
    """Like `invalidate` but for every entry under this folder."""
    global _dirty
    prefix = str(folder_path).rstrip("/") + "/"
    with _lock:
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            entry = _store[k]
            preserved = {p: entry[p] for p in _PRESERVE_ON_INVALIDATE if p in entry}
            if preserved:
                _store[k] = preserved
            else:
                del _store[k]
        if keys:
            _dirty = True
    _schedule_flush()


def hard_clear(filepath):
    """Genuinely remove the entry, including any deletion history."""
    global _dirty
    key = str(filepath)
    with _lock:
        if key in _store:
            del _store[key]
            _dirty = True
    _schedule_flush()


def hard_clear_folder(folder_path):
    global _dirty
    prefix = str(folder_path).rstrip("/") + "/"
    with _lock:
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            del _store[k]
        if keys:
            _dirty = True
    _schedule_flush()


def iter_folder(folder_path):
    """Yield (path_str, entry_copy) for every entry under this folder.

    Used by aggregation code to find tombstones (files no longer on disk but
    whose deleted_delta should still count toward project/account totals).
    """
    prefix = str(folder_path).rstrip("/") + "/"
    with _lock:
        items = [(k, dict(v)) for k, v in _store.items() if k.startswith(prefix)]
    return items


def iter_all():
    """Yield (path_str, entry_copy) for every entry. Used by account-level
    aggregation to pick up tombstones across all projects."""
    with _lock:
        return [(k, dict(v)) for k, v in _store.items()]


def _merge_delta(target: dict, delta: dict):
    """In-place add numeric fields and merge dict-of-counts fields."""
    for k, v in delta.items():
        if isinstance(v, dict):
            sub = target.setdefault(k, {})
            for kk, vv in v.items():
                sub[kk] = sub.get(kk, 0) + vv
        elif isinstance(v, (int, float)):
            target[k] = target.get(k, 0) + v


def accumulate_deleted(filepath, stat, delta: dict):
    """Merge `delta` into the entry's `deleted_delta` dict.

    Call this BEFORE mutating the file — we key on the pre-mutation stat so
    future reads (which see a new mtime/size) still find the tombstoned totals.
    """
    global _dirty
    key = str(filepath)
    with _lock:
        entry = _store.get(key)
        if not entry or entry.get("mtime") != (stat.st_mtime if stat else None):
            # Preserve any prior delta if the entry has one.
            prior = entry.get("deleted_delta") if entry else None
            entry = {
                "mtime": stat.st_mtime if stat else None,
                "size": stat.st_size if stat else 0,
            }
            if prior:
                entry["deleted_delta"] = dict(prior)
        dd = entry.setdefault("deleted_delta", {})
        _merge_delta(dd, delta)
        _store[key] = entry
        _dirty = True
    _schedule_flush()


def mark_deleted(filepath, final_stats: dict):
    """Tombstone an entry: move its active stats into `deleted_delta`, zero
    actives, drop mtime/size so it won't accidentally rematch if the path is
    reused by a new file. Safe to call multiple times (deltas add)."""
    global _dirty
    key = str(filepath)
    with _lock:
        entry = _store.get(key, {})
        dd = entry.get("deleted_delta") or {}

        delta = {}
        for k in _ACTIVE_SCALAR_FIELDS:
            v = final_stats.get(k) or 0
            if v:
                delta[k] = v
        for k in _ACTIVE_DICT_FIELDS:
            v = final_stats.get(k)
            if isinstance(v, dict) and v:
                # per_model values are themselves dicts; flatten into tool_uses
                # for the top-level delta, skip per_model (we don't surface it
                # in the deleted view — keep the delta shape flat).
                if k == "tool_uses":
                    delta[k] = dict(v)
        _merge_delta(dd, delta)

        new_entry = {
            "mtime": None,
            "size": 0,
            "deleted_delta": dd,
            "deleted_at": entry.get("deleted_at") or time.time(),
        }
        _store[key] = new_entry
        _dirty = True
    _schedule_flush()
