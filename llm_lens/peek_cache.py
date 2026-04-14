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
from pathlib import Path

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


def set(filepath, stat, **fields):
    """Merge fields into the entry for this file, keyed by (mtime, size)."""
    global _dirty
    key = str(filepath)
    with _lock:
        existing = _store.get(key)
        # Drop old data if file rev changed.
        if (not existing
                or existing.get("mtime") != stat.st_mtime
                or existing.get("size") != stat.st_size):
            existing = {"mtime": stat.st_mtime, "size": stat.st_size}
        existing.update(fields)
        _store[key] = existing
        _dirty = True
    _schedule_flush()


def invalidate(filepath):
    global _dirty
    key = str(filepath)
    with _lock:
        if key in _store:
            del _store[key]
            _dirty = True
    _schedule_flush()


def invalidate_folder(folder_path):
    """Drop every entry whose path is under this folder."""
    global _dirty
    prefix = str(folder_path).rstrip("/") + "/"
    with _lock:
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            del _store[k]
        if keys:
            _dirty = True
    _schedule_flush()
