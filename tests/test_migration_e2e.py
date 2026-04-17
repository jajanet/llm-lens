"""End-to-end schema v1 → v2 migration test.

Covers the full stack:

* Write a realistic v1 `tags.json` to disk (flat `{folder: {labels,
  assignments}}`), with labels still in the old 5-slot `{name, color}`
  shape and assignments referencing slot indices.
* Boot the Flask app against a tmp project dir; hit the HTTP routes
  an older client would hit (per-folder convo-tag endpoints) and
  assert the upgraded shape is served back with preserved data.
* Hit the *new* project-tag routes and verify they work without
  interfering with the migrated convo data.
* Flush to disk and re-parse the JSON: schema:2, convos/projects
  namespaces, `_seeded` includes "projects" (so an upgraded install
  never sees the work/creative/tools defaults it didn't ask for).
* Reload from disk and re-verify — migration is idempotent.

This test is the one that catches "I touched something and the whole
stack no longer carries old data forward". It's intentionally broad.
"""

import json
import pytest

import llm_lens
from llm_lens import tag_store


# ---------------------------------------------------------------------------
# Fixture: a tmp_path with a realistic v1 tags.json already on disk.
# ---------------------------------------------------------------------------

V1_SNAPSHOT = {
    "proj-a": {
        "labels": [
            {"name": "bug",     "color": 0},
            {"name": "",        "color": 1},   # blank filler, should drop
            {"name": "urgent",  "color": 3},
            {"name": "",        "color": 4},
        ],
        "assignments": {
            "convo-1": [0, 2],   # "bug" + "urgent"
            "convo-2": [0],      # just "bug"
        },
    },
    "proj-b": {
        "labels": [
            {"name": "feature", "color": 2},
        ],
        "assignments": {
            "convo-3": [0],
        },
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Seed a v1-shape tags.json on disk BEFORE we load.
    tags_path = tmp_path / "tags.json"
    tags_path.write_text(json.dumps(V1_SNAPSHOT))

    # Isolate every global the store/app touches.
    monkeypatch.setattr(tag_store, "TAGS_PATH", tags_path)
    monkeypatch.setattr(tag_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "ARCHIVE_ROOT", tmp_path / "_archive")
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens._stats_cached.cache_clear()
    tag_store._store.clear()
    tag_store._dirty = False
    tag_store._load()   # triggers _migrate_schema + default-seed path

    # Give the Flask client real folders so per-folder routes don't 404.
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-a" / "convo-1.jsonl").write_text("")
    (tmp_path / "proj-a" / "convo-2.jsonl").write_text("")
    (tmp_path / "proj-b").mkdir()
    (tmp_path / "proj-b" / "convo-3.jsonl").write_text("")

    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client(), tmp_path


# ---------------------------------------------------------------------------
# 1. Convo-tag HTTP routes serve upgraded data from the migrated store.
# ---------------------------------------------------------------------------

def test_convo_tag_routes_serve_migrated_labels(client):
    c, _ = client
    r = c.get("/api/projects/proj-a/tags").get_json()
    # Empty-name slots were dropped; surviving slot indices became ids.
    assert r["labels"] == [
        {"id": 0, "name": "bug", "color": 0},
        {"id": 2, "name": "urgent", "color": 3},
    ]
    # Assignment JSON was never rewritten — old indices already are the
    # new ids, so these come through byte-for-byte.
    assert r["assignments"] == {"convo-1": [0, 2], "convo-2": [0]}


def test_old_client_can_still_mutate_via_per_folder_routes(client):
    """An older frontend hitting the pre-existing per-folder routes
    should keep working even after migration — no endpoint moved."""
    c, _ = client
    # Append a new label with no id; server mints one (past the old max).
    c.put(
        "/api/projects/proj-a/tags/labels",
        data=json.dumps({"labels": [
            {"id": 0, "name": "bug",    "color": 0},
            {"id": 2, "name": "urgent", "color": 3},
            {"name": "wip", "color": 5},
        ]}),
        content_type="application/json",
    )
    r = c.get("/api/projects/proj-a/tags").get_json()
    wip = [l for l in r["labels"] if l["name"] == "wip"][0]
    # Fresh id is strictly greater than any old slot index we had.
    assert wip["id"] > 2
    # Prior assignments still resolve — nothing got clobbered.
    assert r["assignments"]["convo-1"] == [0, 2]


# ---------------------------------------------------------------------------
# 2. Upgraded installs don't get the default project-tag seed.
# ---------------------------------------------------------------------------

def test_upgraded_install_has_empty_project_tags(client):
    """Defaults (work/creative/tools) are for brand-new installs only.
    Migrating a v1 store must explicitly opt out."""
    c, _ = client
    r = c.get("/api/tags/projects").get_json()
    assert r["labels"] == []
    assert r["assignments"] == {}


def test_project_tag_routes_work_alongside_convo_tags(client):
    """Creating and assigning project tags must not collide with the
    migrated convo-tag data. The two namespaces are isolated."""
    c, _ = client
    # Create a project tag, assign it to one folder.
    c.put(
        "/api/tags/projects/labels",
        data=json.dumps({"labels": [{"name": "personal", "color": 6}]}),
        content_type="application/json",
    )
    pid = c.get("/api/tags/projects").get_json()["labels"][0]["id"]
    c.post(
        "/api/tags/projects/assign",
        data=json.dumps({"folder": "proj-a", "tags": [pid]}),
        content_type="application/json",
    )
    # Project-tag state updated.
    proj = c.get("/api/tags/projects").get_json()
    assert proj["assignments"] == {"proj-a": [pid]}
    # Convo-tag state for proj-a is unchanged (still the migrated pair).
    convo_a = c.get("/api/projects/proj-a/tags").get_json()
    assert convo_a["assignments"] == {"convo-1": [0, 2], "convo-2": [0]}


# ---------------------------------------------------------------------------
# 3. On-disk format after migration + writes.
# ---------------------------------------------------------------------------

def test_disk_shape_after_migration(client):
    """Flush and re-parse the JSON. Asserts the on-disk schema itself,
    not just what the API returns."""
    c, tmp_path = client
    # Make a single write so flush() has something to do.
    c.put(
        "/api/projects/proj-b/tags/labels",
        data=json.dumps({"labels": [{"id": 0, "name": "feature", "color": 2}]}),
        content_type="application/json",
    )
    tag_store.flush()
    data = json.loads((tmp_path / "tags.json").read_text())
    assert data["schema"] == tag_store.SCHEMA_VERSION
    assert set(data.keys()) >= {"schema", "convos", "projects", "_seeded"}
    # Old top-level folder keys are gone — they live under "convos" now.
    assert "proj-a" not in data
    assert "proj-b" not in data
    assert {"proj-a", "proj-b"}.issubset(data["convos"])
    # Migrated install: projects was marked seeded (no defaults were
    # injected), so reload doesn't resurrect them.
    assert "projects" in data["_seeded"]


def test_reload_is_idempotent(client):
    """After migrating + writing, clearing in-memory state and
    re-loading from disk should produce identical results."""
    c, _ = client
    before = c.get("/api/projects/proj-a/tags").get_json()
    tag_store.flush()
    tag_store._store.clear()
    tag_store._dirty = False
    tag_store._load()
    after = c.get("/api/projects/proj-a/tags").get_json()
    assert before == after


# ---------------------------------------------------------------------------
# 4. Delete-tag still scrubs assignments post-migration.
# ---------------------------------------------------------------------------

def test_delete_migrated_tag_scrubs_assignments(client):
    """The invariant TagSet enforces everywhere (delete-scrub) must
    also hold for labels that came in through the v1 migration path."""
    c, _ = client
    # Delete id=2 ("urgent") by PUT'ing labels without it.
    c.put(
        "/api/projects/proj-a/tags/labels",
        data=json.dumps({"labels": [{"id": 0, "name": "bug", "color": 0}]}),
        content_type="application/json",
    )
    r = c.get("/api/projects/proj-a/tags").get_json()
    # convo-1 had [0, 2] → now just [0]. convo-2 was already [0].
    assert r["assignments"] == {"convo-1": [0], "convo-2": [0]}
