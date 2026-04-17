"""Unit tests for the TagSet primitive.

TagSet is pure data — no disk, no lock, no globals. Every test here
builds a tiny dict, wraps a TagSet around it, pokes methods, and
asserts on the resulting dict. That keeps these tests fast and makes
them the authoritative spec for tag-data invariants, independent of
any persistence or HTTP wiring.
"""

from llm_lens.tag_set import TagSet, NUM_COLORS


# ---------------------------------------------------------------------------
# NUM_COLORS contract
# ---------------------------------------------------------------------------

def test_num_colors_is_eight():
    assert NUM_COLORS == 8


# ---------------------------------------------------------------------------
# Construction + migration
# ---------------------------------------------------------------------------

def test_empty_slot_initializes_defaults():
    data = {}
    TagSet(data)
    assert data["labels"] == []
    assert data["assignments"] == {}
    assert data["next_id"] == 0


def test_migration_from_old_shape_preserves_slot_index_as_id():
    data = {
        "labels": [
            {"name": "bug", "color": 0},
            {"name": "",    "color": 1},
            {"name": "urgent", "color": 3},
            {"name": "",    "color": 4},
        ],
        "assignments": {"c1": [0, 2]},
    }
    TagSet(data)
    # Empty rows drop; old slot index survives as id.
    assert data["labels"] == [
        {"id": 0, "name": "bug", "color": 0},
        {"id": 2, "name": "urgent", "color": 3},
    ]
    # Assignment table is untouched — the whole reason we preserve
    # slot-index-as-id is so we don't have to rewrite it.
    assert data["assignments"] == {"c1": [0, 2]}
    # next_id seeds above every id we've ever minted.
    assert data["next_id"] == 3


def test_migration_idempotent():
    data = {"labels": [{"name": "bug", "color": 0}]}
    TagSet(data)
    snapshot = list(data["labels"])
    TagSet(data)
    assert data["labels"] == snapshot


def test_migration_clamps_bad_colors():
    data = {
        "labels": [
            {"name": "a", "color": 999},
            {"name": "b", "color": -1},
        ],
    }
    TagSet(data)
    for l in data["labels"]:
        assert 0 <= l["color"] < NUM_COLORS


# ---------------------------------------------------------------------------
# set_labels: id allocation + delete semantics
# ---------------------------------------------------------------------------

def test_set_labels_mints_monotonic_ids():
    data = {}
    ts = TagSet(data)
    ts.set_labels([
        {"name": "a", "color": 0},
        {"name": "b", "color": 1},
    ])
    assert [l["id"] for l in data["labels"]] == [0, 1]

    ts.set_labels([
        {"id": 0, "name": "a", "color": 0},
        {"id": 1, "name": "b", "color": 1},
        {"name": "c", "color": 2},
    ])
    assert [l["id"] for l in data["labels"]] == [0, 1, 2]


def test_deleted_ids_never_reused():
    """The whole point of id stability: after deleting id=1, a newly
    created label must get id=2, not id=1 — otherwise any stale client
    holding id=1 would silently rebind to the new label."""
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}, {"name": "b", "color": 1}])
    ts.set_labels([{"id": 0, "name": "a", "color": 0}])  # delete id=1
    ts.set_labels([
        {"id": 0, "name": "a", "color": 0},
        {"name": "c", "color": 2},
    ])
    assert [l["id"] for l in data["labels"]] == [0, 2]


def test_deleting_label_scrubs_assignments():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}, {"name": "b", "color": 1}])
    ts.assign("k1", [0, 1])
    ts.assign("k2", [1])
    ts.set_labels([{"id": 0, "name": "a", "color": 0}])  # delete id=1
    # k1 kept id=0; k2 had only id=1 so its row is gone entirely.
    assert data["assignments"] == {"k1": [0]}


def test_rename_and_recolor_preserve_id_and_assignments():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "bug", "color": 0}])
    ts.assign("k1", [0])
    ts.set_labels([{"id": 0, "name": "defect", "color": 5}])
    assert data["labels"] == [{"id": 0, "name": "defect", "color": 5}]
    assert data["assignments"] == {"k1": [0]}


def test_set_labels_rejects_empty_name_entries():
    data = {}
    ts = TagSet(data)
    ts.set_labels([
        {"name": "a", "color": 0},
        {"name": "   ", "color": 1},
        {"name": "",  "color": 2},
    ])
    assert [l["name"] for l in data["labels"]] == ["a"]


def test_set_labels_clamps_invalid_colors():
    data = {}
    ts = TagSet(data)
    ts.set_labels([
        {"name": "a", "color": 99},
        {"name": "b", "color": -5},
        {"name": "c", "color": "red"},  # wrong type
    ])
    for l in data["labels"]:
        assert 0 <= l["color"] < NUM_COLORS


def test_set_labels_ignores_non_list_input():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    ts.set_labels("not a list")  # type: ignore[arg-type]
    assert data["labels"] == [{"id": 0, "name": "a", "color": 0}]


# ---------------------------------------------------------------------------
# assign / bulk_assign validation
# ---------------------------------------------------------------------------

def test_assign_drops_unknown_ids():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    ts.assign("k1", [0, 99, 42])
    assert data["assignments"] == {"k1": [0]}


def test_assign_empty_list_removes_key():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    ts.assign("k1", [0])
    ts.assign("k1", [])
    assert data["assignments"] == {}


def test_bulk_assign_returns_zero_for_unknown_tag():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    assert ts.bulk_assign(["k1", "k2"], tag_id=99, add=True) == 0
    assert data["assignments"] == {}


def test_bulk_assign_counts_only_changed_keys():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    assert ts.bulk_assign(["k1", "k2"], tag_id=0, add=True) == 2
    # Already tagged → no change this time.
    assert ts.bulk_assign(["k1", "k2"], tag_id=0, add=True) == 0


def test_remove_key_clears_assignments():
    data = {}
    ts = TagSet(data)
    ts.set_labels([{"name": "a", "color": 0}])
    ts.assign("k1", [0])
    ts.remove_key("k1")
    assert data["assignments"] == {}


# ---------------------------------------------------------------------------
# on_change wiring — lets TagStore mark itself dirty automatically
# ---------------------------------------------------------------------------

def test_on_change_fires_on_mutation():
    calls = {"n": 0}
    ts = TagSet({}, on_change=lambda: calls.__setitem__("n", calls["n"] + 1))
    ts.set_labels([{"name": "a", "color": 0}])
    ts.assign("k1", [0])
    ts.bulk_assign(["k2"], 0, True)
    ts.remove_key("k1")
    assert calls["n"] == 4


def test_on_change_not_fired_for_noops():
    calls = {"n": 0}
    ts = TagSet(
        {"labels": [{"id": 0, "name": "a", "color": 0}], "assignments": {}, "next_id": 1},
        on_change=lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    # These shouldn't touch anything:
    ts.bulk_assign(["k1"], tag_id=99, add=True)  # unknown id → no-op
    ts.remove_key("k1")  # not present → no-op
    assert calls["n"] == 0
