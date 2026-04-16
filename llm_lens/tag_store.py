"""Persistent per-project tag storage.

Manages ~/.cache/llm-lens/tags.json — separate from sessions.json so tags
survive peek_cache.hard_clear(). Uses the same lock + debounce-flush pattern.

Structure:
    {
      "folder-name": {
        "labels": [{"name": "bug fix", "color": 0}, ...],   # up to 5
        "assignments": {"convo-id": [0, 2], ...}
      }
    }
"""

import atexit
import json
import os
import threading
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "llm-lens"
TAGS_PATH = CACHE_DIR / "tags.json"
FLUSH_DELAY = 2.0
MAX_LABELS = 5

_DEFAULT_LABELS = [{"name": "", "color": i} for i in range(MAX_LABELS)]

_lock = threading.Lock()
_store: dict = {}
_dirty = False
_flush_timer: threading.Timer | None = None


def _load():
    global _store
    try:
        with open(TAGS_PATH, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _store = data
    except (OSError, json.JSONDecodeError):
        _store = {}

_load()


# On abrupt shutdown (kill, SIGTERM), the debounce timer may not fire. Flush
# synchronously at exit so the in-memory store always makes it to disk.
atexit.register(lambda: flush())


def _schedule_flush():
    global _flush_timer
    if _flush_timer is not None:
        return
    _flush_timer = threading.Timer(FLUSH_DELAY, flush)
    _flush_timer.daemon = True
    _flush_timer.start()


def flush():
    """Atomically write the current store to disk."""
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
        tmp = TAGS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh)
        os.replace(tmp, TAGS_PATH)
    except OSError:
        pass


def get_project(folder: str) -> dict:
    """Return {"labels": [...], "assignments": {...}} for a folder."""
    with _lock:
        project = _store.get(folder)
    if not project:
        return {"labels": list(_DEFAULT_LABELS), "assignments": {}}
    return {
        "labels": project.get("labels", list(_DEFAULT_LABELS)),
        "assignments": project.get("assignments", {}),
    }


def set_labels(folder: str, labels: list[dict]):
    """Replace label definitions for a folder (max 5)."""
    global _dirty
    labels = labels[:MAX_LABELS]
    with _lock:
        project = _store.setdefault(folder, {})
        project["labels"] = labels
        _dirty = True
    _schedule_flush()


def assign(folder: str, convo_id: str, tag_indices: list[int]):
    """Set the tags for one conversation (replaces prior assignment)."""
    global _dirty
    tag_indices = [i for i in tag_indices if 0 <= i < MAX_LABELS]
    with _lock:
        project = _store.setdefault(folder, {})
        assignments = project.setdefault("assignments", {})
        if tag_indices:
            assignments[convo_id] = sorted(set(tag_indices))
        else:
            assignments.pop(convo_id, None)
        _dirty = True
    _schedule_flush()


def bulk_assign(folder: str, convo_ids: list[str], tag_index: int, add: bool) -> int:
    """Add or remove a single tag from multiple conversations. Returns count."""
    global _dirty
    if not (0 <= tag_index < MAX_LABELS):
        return 0
    count = 0
    with _lock:
        project = _store.setdefault(folder, {})
        assignments = project.setdefault("assignments", {})
        for cid in convo_ids:
            current = set(assignments.get(cid, []))
            if add:
                if tag_index not in current:
                    current.add(tag_index)
                    count += 1
            else:
                if tag_index in current:
                    current.discard(tag_index)
                    count += 1
            if current:
                assignments[cid] = sorted(current)
            else:
                assignments.pop(cid, None)
        if count:
            _dirty = True
    _schedule_flush()
    return count


def remove_conversation(folder: str, convo_id: str):
    """Clean up tags when a conversation is deleted."""
    global _dirty
    with _lock:
        project = _store.get(folder)
        if not project:
            return
        assignments = project.get("assignments", {})
        if convo_id in assignments:
            del assignments[convo_id]
            _dirty = True
    _schedule_flush()


def remove_folder(folder: str):
    """Clean up when a project is deleted."""
    global _dirty
    with _lock:
        if folder in _store:
            del _store[folder]
            _dirty = True
    _schedule_flush()
