"""Persistent multi-namespace tag store.

On-disk schema v2:

    {
      "schema": 2,
      "convos":  {folder: {labels, assignments, next_id}},
      "projects": {labels, assignments, next_id},
      "_seeded": ["projects"]   # namespaces we already seeded defaults for
    }

Old schema v1 was a flat `{folder: {labels, assignments}}`. We migrate
it on load; see `_migrate_schema`. The upgrade preserves all existing
convo-tag data byte-for-byte (just nests it under `"convos"`), so
the wire format the frontend sees is unchanged.

Concurrency: a module-level `_lock` serializes mutations. Each public
entry point briefly holds the lock, constructs a fresh `TagSet` over
the relevant dict slice, runs the operation, and releases the lock.
The `TagSet` itself is not thread-safe — that responsibility lives
here, on purpose, so the primitive stays testable in isolation.
"""

import json
import os
import shutil
import threading
from pathlib import Path

from .tag_set import TagSet, NUM_COLORS  # noqa: F401 (NUM_COLORS re-exported)

SCHEMA_VERSION = 2
CACHE_DIR = Path.home() / ".cache" / "llm-lens"
TAGS_PATH = CACHE_DIR / "tags.json"

# Seeded once on fresh installs. Deliberately small so users aren't
# overwhelmed; they can delete / rename / recolor freely. Existing
# installs being migrated from v1 are NOT reseeded (see _migrate_schema).
DEFAULT_PROJECT_TAGS = [
    {"name": "work",     "color": 1},
    {"name": "creative", "color": 4},
    {"name": "tools",    "color": 2},
]

_lock = threading.Lock()
_store: dict = {}
_dirty = False


# ── Schema + load ─────────────────────────────────────────────────────

def _fresh_store() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "convos": {},
        "projects": {"labels": [], "assignments": {}, "next_id": 0},
        "_seeded": [],
    }


def _migrate_schema(data: dict) -> dict:
    """Upgrade a raw on-disk dict to schema v2. Idempotent.

    v1 is recognized by the absence of a `schema` key and a flat
    `{folder: {labels, assignments}}` shape. We nest every such entry
    under `"convos"` and mark `"projects"` as already-seeded so that
    existing users don't suddenly see three unexpected project tags
    appear on next load.
    """
    if data.get("schema") == SCHEMA_VERSION:
        data.setdefault("convos", {})
        data.setdefault(
            "projects",
            {"labels": [], "assignments": {}, "next_id": 0},
        )
        data.setdefault("_seeded", [])
        return data
    migrated = _fresh_store()
    # v1 → v2: existing users shouldn't be surprised by new defaults.
    migrated["_seeded"] = ["projects"]
    for key, val in data.items():
        if key in ("schema", "convos", "projects", "_seeded"):
            continue
        if isinstance(val, dict) and ("labels" in val or "assignments" in val):
            migrated["convos"][key] = val
    return migrated


def _seed_fresh_defaults():
    """Populate `projects` defaults exactly once per fresh install.

    Gated by `_seeded` so a user who intentionally deletes every
    project tag doesn't get the defaults resurrected. The v1→v2
    migration path adds "projects" to `_seeded` upfront, so existing
    installs are opted out.
    """
    global _dirty
    seeded = _store.setdefault("_seeded", [])
    if "projects" in seeded:
        return
    projects_slice = _store.setdefault(
        "projects", {"labels": [], "assignments": {}, "next_id": 0}
    )
    if not projects_slice.get("labels"):
        ts = TagSet(projects_slice)
        ts.set_labels(DEFAULT_PROJECT_TAGS)
    seeded.append("projects")
    _dirty = True


def _load():
    global _store
    try:
        with open(TAGS_PATH, "r") as fh:
            raw = json.load(fh)
        _store = _migrate_schema(raw) if isinstance(raw, dict) else _fresh_store()
    except (OSError, json.JSONDecodeError):
        _store = _fresh_store()
    _seed_fresh_defaults()
    # Persist migration/seeding immediately. Previously deferred to atexit,
    # but atexit runs after pytest's monkeypatch teardown, which could flush
    # test state to the real ~/.cache/llm-lens/tags.json.
    flush()


# ── Persistence ──────────────────────────────────────────────────────

def _schedule_flush():
    """Write pending changes to disk. Synchronous — previously a 2-second
    Timer debounce, which raced with server restarts and test teardown."""
    flush()


def flush():
    """Atomically write the current store to disk. Keeps a single .bak
    copy of the prior file as a safety net against accidental wipes."""
    global _dirty
    with _lock:
        if not _dirty:
            return
        snapshot = dict(_store)
        _dirty = False
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if TAGS_PATH.exists():
            shutil.copy2(TAGS_PATH, TAGS_PATH.with_suffix(".json.bak"))
        tmp = TAGS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh)
        os.replace(tmp, TAGS_PATH)
    except OSError:
        pass


_load()


def _mark_dirty():
    global _dirty
    _dirty = True


# ── Namespace resolution ─────────────────────────────────────────────

def _slot_for(namespace: tuple) -> dict:
    """Return (creating if needed) the dict slice that backs a namespace.

    Caller must hold `_lock`. Recognized namespaces:
    * `("convos", folder)` — one set per project folder.
    * `("projects",)`       — single global set of project-level tags.
    """
    if namespace == ("projects",):
        return _store.setdefault(
            "projects", {"labels": [], "assignments": {}, "next_id": 0}
        )
    if len(namespace) == 2 and namespace[0] == "convos":
        convos = _store.setdefault("convos", {})
        return convos.setdefault(
            namespace[1], {"labels": [], "assignments": {}, "next_id": 0}
        )
    raise ValueError(f"unknown tag namespace: {namespace!r}")


# ── Generic namespace API ────────────────────────────────────────────

def get(namespace: tuple) -> dict:
    """Return a `{labels, assignments}` snapshot for a namespace.

    Construction of the TagSet may trigger a lazy migration of legacy
    5-slot data, so we always schedule a flush after this call.
    """
    with _lock:
        ts = TagSet(_slot_for(namespace), on_change=_mark_dirty)
        out = ts.snapshot()
    _schedule_flush()  # no-op if nothing got dirty
    return out


def set_labels_ns(namespace: tuple, labels: list):
    with _lock:
        ts = TagSet(_slot_for(namespace), on_change=_mark_dirty)
        ts.set_labels(labels)
    _schedule_flush()


def assign_ns(namespace: tuple, key: str, tag_ids: list):
    with _lock:
        ts = TagSet(_slot_for(namespace), on_change=_mark_dirty)
        ts.assign(key, tag_ids)
    _schedule_flush()


def bulk_assign_ns(namespace: tuple, keys: list, tag_id: int, add: bool) -> int:
    with _lock:
        ts = TagSet(_slot_for(namespace), on_change=_mark_dirty)
        count = ts.bulk_assign(keys, tag_id, add)
    _schedule_flush()
    return count


def remove_key_ns(namespace: tuple, key: str):
    with _lock:
        ts = TagSet(_slot_for(namespace), on_change=_mark_dirty)
        ts.remove_key(key)
    _schedule_flush()


# ── Backward-compat convo-tag shortcuts ──────────────────────────────

def get_project(folder: str) -> dict:
    return get(("convos", folder))


def set_labels(folder: str, labels: list):
    set_labels_ns(("convos", folder), labels)


def assign(folder: str, convo_id: str, tag_ids: list):
    assign_ns(("convos", folder), convo_id, tag_ids)


def bulk_assign(folder: str, convo_ids: list, tag_id: int, add: bool) -> int:
    return bulk_assign_ns(("convos", folder), convo_ids, tag_id, add)


def remove_conversation(folder: str, convo_id: str):
    remove_key_ns(("convos", folder), convo_id)


def remove_folder(folder: str):
    """Clean up when a project is deleted: drop the folder's convo-tag
    set entirely, and scrub its id from project-tag assignments too
    (the folder may itself have been tagged at the project level)."""
    global _dirty
    with _lock:
        convos = _store.setdefault("convos", {})
        if folder in convos:
            del convos[folder]
            _dirty = True
        projects = _store.get("projects", {})
        assignments = projects.get("assignments", {})
        if folder in assignments:
            del assignments[folder]
            _dirty = True
    _schedule_flush()
