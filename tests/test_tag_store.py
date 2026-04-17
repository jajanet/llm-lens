"""Integration tests for the TagStore persistence + schema layer.

Unit-level invariants of the tag primitive live in `test_tag_set.py`.
This file covers the pieces TagSet doesn't know about:

* on-disk schema v1 → v2 migration,
* default project-tag seeding on fresh installs only,
* the backward-compat convo-tag module-level API,
* generic namespace API (`get`, `set_labels_ns`, …),
* HTTP round-trips for both convo-tag and project-tag endpoints.
"""

import json
from pathlib import Path

import pytest

import llm_lens
from llm_lens import tag_store
from llm_lens.tag_set import NUM_COLORS


# ---------------------------------------------------------------------------
# Fixture: isolate store state per-test, seed defaults for fresh installs.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_store(monkeypatch, tmp_path):
    monkeypatch.setattr(tag_store, "TAGS_PATH", tmp_path / "tags.json")
    monkeypatch.setattr(tag_store, "CACHE_DIR", tmp_path)
    tag_store._store.clear()
    tag_store._dirty = False
    # Reload from the (non-existent) tmp_path → fresh store + default
    # seeding runs. Tests that want no defaults can manually re-reset.
    tag_store._load()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "ARCHIVE_ROOT", tmp_path / "_archive")
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens._stats_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    # Give the client a real project folder so per-folder routes don't 404.
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "c1.jsonl").write_text("")
    return llm_lens.app.test_client()


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_v1_flat_store_migrates_to_v2(tmp_path):
    """A tags.json written by an older install (flat {folder: ...})
    must load cleanly and nest existing convo-tag data under 'convos'
    without rewriting the assignments table."""
    v1 = {
        "proj-a": {
            "labels": [{"name": "bug", "color": 0}],
            "assignments": {"c1": [0]},
        },
        "proj-b": {
            "labels": [{"name": "feature", "color": 3}],
            "assignments": {},
        },
    }
    (tmp_path / "tags.json").write_text(json.dumps(v1))
    tag_store._store.clear()
    tag_store._load()
    assert tag_store._store["schema"] == tag_store.SCHEMA_VERSION
    assert "proj-a" in tag_store._store["convos"]
    assert "proj-b" in tag_store._store["convos"]
    # Convo data is preserved verbatim through the migration.
    proj_a = tag_store.get_project("proj-a")
    assert proj_a["assignments"] == {"c1": [0]}


def test_v1_migration_marks_projects_as_seeded(tmp_path):
    """Existing installs must NOT suddenly see three unexpected project
    tags. v1→v2 migration pre-populates `_seeded` with 'projects' so
    the default-seeder skips them."""
    (tmp_path / "tags.json").write_text(json.dumps({
        "proj-a": {"labels": [], "assignments": {}},
    }))
    tag_store._store.clear()
    tag_store._load()
    assert tag_store.get(("projects",))["labels"] == []


def test_fresh_install_seeds_default_project_tags():
    """No existing tags.json → the three documented defaults appear
    exactly once, and deleting them doesn't cause them to come back."""
    names = [l["name"] for l in tag_store.get(("projects",))["labels"]]
    assert names == ["work", "creative", "tools"]

    # Delete all defaults and reload — must not reseed.
    tag_store.set_labels_ns(("projects",), [])
    tag_store.flush()
    tag_store._store.clear()
    tag_store._load()
    assert tag_store.get(("projects",))["labels"] == []


def test_v2_on_disk_reloads_idempotent():
    tag_store.set_labels_ns(("convos", "proj"), [{"name": "bug", "color": 0}])
    tag_store.flush()
    before = json.loads(tag_store.TAGS_PATH.read_text())
    tag_store._store.clear()
    tag_store._load()
    tag_store.flush()  # flush does nothing if nothing changed
    after = json.loads(tag_store.TAGS_PATH.read_text())
    assert before == after


# ---------------------------------------------------------------------------
# Generic namespace API: both namespaces share the same surface.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("namespace,key", [
    (("convos", "proj"), "c1"),
    (("projects",),      "proj-a"),
])
def test_namespace_crud_roundtrip(namespace, key):
    tag_store.set_labels_ns(namespace, [
        {"name": "a", "color": 0},
        {"name": "b", "color": 1},
    ])
    labels = tag_store.get(namespace)["labels"]
    ids = [l["id"] for l in labels]

    tag_store.assign_ns(namespace, key, ids)
    assert tag_store.get(namespace)["assignments"][key] == ids

    # Delete id=ids[0] → scrubbed from assignments.
    tag_store.set_labels_ns(namespace, [labels[1]])
    remaining = tag_store.get(namespace)["assignments"][key]
    assert remaining == [ids[1]]


def test_namespaces_are_isolated():
    """Changing convo tags in one namespace must not touch project tags
    (or vice versa). The whole point of the refactor."""
    tag_store.set_labels_ns(("convos", "proj"), [{"name": "convo-only", "color": 0}])
    tag_store.set_labels_ns(("projects",), [{"name": "proj-only", "color": 1}])

    convo_names = [l["name"] for l in tag_store.get(("convos", "proj"))["labels"]]
    proj_names  = [l["name"] for l in tag_store.get(("projects",))["labels"]]
    assert convo_names == ["convo-only"]
    assert proj_names == ["proj-only"]


def test_unknown_namespace_raises():
    with pytest.raises(ValueError):
        tag_store.get(("bogus",))


# ---------------------------------------------------------------------------
# Backward-compat convo-tag shortcuts
# ---------------------------------------------------------------------------

def test_backcompat_get_project_wraps_new_api():
    tag_store.set_labels_ns(("convos", "proj"), [{"name": "a", "color": 0}])
    assert tag_store.get_project("proj")["labels"][0]["name"] == "a"


def test_remove_folder_cleans_convo_and_project_tags():
    """Deleting a project folder should drop its convo-tag set AND
    scrub its id from any project-tag assignments that referenced it."""
    tag_store.set_labels_ns(("convos", "proj"), [{"name": "a", "color": 0}])
    tag_store.set_labels_ns(("projects",),     [{"name": "work", "color": 1}])
    pid = tag_store.get(("projects",))["labels"][0]["id"]
    tag_store.assign_ns(("projects",), "proj", [pid])

    tag_store.remove_folder("proj")
    assert "proj" not in tag_store._store["convos"]
    assert tag_store.get(("projects",))["assignments"] == {}


# ---------------------------------------------------------------------------
# HTTP round-trips
# ---------------------------------------------------------------------------

def test_http_convo_tag_flow(client):
    # Old per-folder routes continue to work unchanged.
    r = client.get("/api/projects/proj/tags").get_json()
    assert r["labels"] == []

    client.put(
        "/api/projects/proj/tags/labels",
        data=json.dumps({"labels": [{"name": "bug", "color": 0}]}),
        content_type="application/json",
    )
    r = client.get("/api/projects/proj/tags").get_json()
    bug_id = r["labels"][0]["id"]

    client.post(
        "/api/projects/proj/tags/assign",
        data=json.dumps({"convo_id": "c1", "tags": [bug_id]}),
        content_type="application/json",
    )
    assert client.get("/api/projects/proj/tags").get_json()["assignments"]["c1"] == [bug_id]


def test_http_project_tag_flow(client):
    # Fresh install → defaults present.
    r = client.get("/api/tags/projects").get_json()
    names = [l["name"] for l in r["labels"]]
    assert names == ["work", "creative", "tools"]

    # Add a new tag.
    labels = r["labels"] + [{"name": "research", "color": 5}]
    client.put(
        "/api/tags/projects/labels",
        data=json.dumps({"labels": labels}),
        content_type="application/json",
    )
    r = client.get("/api/tags/projects").get_json()
    research_id = [l for l in r["labels"] if l["name"] == "research"][0]["id"]

    # Assign it to two project folders via bulk.
    client.post(
        "/api/tags/projects/bulk-assign",
        data=json.dumps({"folders": ["proj-a", "proj-b"], "tag": research_id, "add": True}),
        content_type="application/json",
    )
    r = client.get("/api/tags/projects").get_json()
    assert r["assignments"]["proj-a"] == [research_id]
    assert r["assignments"]["proj-b"] == [research_id]

    # Per-folder assign overwrites.
    client.post(
        "/api/tags/projects/assign",
        data=json.dumps({"folder": "proj-a", "tags": []}),
        content_type="application/json",
    )
    r = client.get("/api/tags/projects").get_json()
    assert "proj-a" not in r["assignments"]


def test_http_project_tag_delete_scrubs_assignments(client):
    r = client.get("/api/tags/projects").get_json()
    work_id = [l for l in r["labels"] if l["name"] == "work"][0]["id"]
    client.post(
        "/api/tags/projects/bulk-assign",
        data=json.dumps({"folders": ["proj-a"], "tag": work_id, "add": True}),
        content_type="application/json",
    )
    # Delete 'work' by PUT-ing labels without it.
    keep = [l for l in r["labels"] if l["name"] != "work"]
    client.put(
        "/api/tags/projects/labels",
        data=json.dumps({"labels": keep}),
        content_type="application/json",
    )
    r = client.get("/api/tags/projects").get_json()
    # Deleted id is gone, assignment is empty (not a dangling reference).
    remaining_ids = {l["id"] for l in r["labels"]}
    assert work_id not in remaining_ids
    assert r["assignments"] == {}
