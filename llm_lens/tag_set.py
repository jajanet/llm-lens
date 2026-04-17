"""Pure-data tag primitive: one labels list + per-key assignments.

Wraps a dict slice `{labels, assignments, next_id}` and mutates it in
place. Zero I/O — persistence is TagStore's job. Not thread-safe on its
own; callers are expected to serialize mutations via whatever lock
guards the backing store.

The invariants this class enforces:

* Labels are `{id: int, name: str, color: int}` with unique, positive,
  monotonically-minted ids. Once allocated, an id is NEVER reused within
  this set — even after delete — so stale clients can't silently rebind
  assignments to a newer label.
* Assignments map opaque string keys (convo_id, folder, …) to sorted
  lists of label ids. Keys with empty id-lists are dropped entirely.
* Deleting a label scrubs its id from every assignment, not just its
  own row — no dangling references survive.
* Legacy dicts (old 5-slot `{name, color}` lists without `id` fields)
  are migrated in place on construction, mapping slot-index → id 1:1.
"""

NUM_COLORS = 8


def _sanitize_label(entry, used_ids: set, next_id_hint: int):
    """Coerce a client-submitted label dict into the canonical shape.

    Returns `(label_or_None, next_id_hint)`. Returns None to signal
    the entry should be dropped (empty/bad name, not a dict).

    `used_ids` is mutated: every minted id is added so duplicates
    within one `set_labels` call are impossible.
    """
    if not isinstance(entry, dict):
        return None, next_id_hint
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, next_id_hint
    name = name.strip()[:30]
    color = entry.get("color", 0)
    if not isinstance(color, int) or color < 0 or color >= NUM_COLORS:
        color = 0
    raw_id = entry.get("id")
    if isinstance(raw_id, int) and raw_id >= 0 and raw_id not in used_ids:
        tag_id = raw_id
    else:
        tag_id = next_id_hint
        next_id_hint += 1
    used_ids.add(tag_id)
    return {"id": tag_id, "name": name, "color": color}, max(next_id_hint, tag_id + 1)


class TagSet:
    """One namespace of tag labels + assignments backed by a dict slice."""

    def __init__(self, data: dict, on_change=None):
        self._data = data
        self._on_change = on_change
        data.setdefault("labels", [])
        data.setdefault("assignments", {})
        data.setdefault("next_id", 0)
        self._migrate_if_needed()

    def _touch(self):
        if self._on_change is not None:
            self._on_change()

    def _migrate_if_needed(self) -> bool:
        """If `labels` is the old 5-slot {name, color} shape (no `id`),
        upgrade in place: id := old slot index, empty-name rows dropped.
        The old convo-assignment JSON stored slot indices, and slot
        index is now id, so assignment data is untouched by the
        migration — exactly the \"no rewriting\" property we want.
        """
        labels = self._data.get("labels") or []
        if not labels:
            return False
        if all(isinstance(l, dict) and "id" in l for l in labels):
            return False
        migrated = []
        for idx, entry in enumerate(labels):
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            color = entry.get("color", idx % NUM_COLORS)
            if not isinstance(color, int) or color < 0 or color >= NUM_COLORS:
                color = idx % NUM_COLORS
            migrated.append({"id": idx, "name": name[:30], "color": color})
        self._data["labels"] = migrated
        self._data["next_id"] = max(
            (l["id"] for l in migrated), default=-1
        ) + 1
        self._touch()
        return True

    # --- read-only views ---------------------------------------------------

    def snapshot(self) -> dict:
        """Shallow-copied view of `{labels, assignments}` for callers
        that shouldn't (and can't) mutate the backing store."""
        return {
            "labels": list(self._data.get("labels", [])),
            "assignments": dict(self._data.get("assignments", {})),
        }

    def valid_ids(self) -> set:
        return {
            l["id"]
            for l in self._data.get("labels", [])
            if isinstance(l, dict) and isinstance(l.get("id"), int)
        }

    # --- mutators ---------------------------------------------------------

    def set_labels(self, labels: list) -> None:
        """Replace the label list. Entries without a valid `id` get a
        freshly-minted monotonic one. Removed ids (present before, absent
        now) are scrubbed from every assignment in this set."""
        if not isinstance(labels, list):
            return
        prior = self._data.get("labels", [])
        prior_ids = {
            l["id"] for l in prior
            if isinstance(l, dict) and isinstance(l.get("id"), int)
        }
        submitted_ids = [
            l["id"] for l in labels
            if isinstance(l, dict) and isinstance(l.get("id"), int) and l["id"] >= 0
        ]
        # next_id only grows — never recycle a deleted id.
        next_hint = max(
            self._data.get("next_id", 0),
            max(prior_ids, default=-1) + 1,
            max(submitted_ids, default=-1) + 1,
        )
        used = set()
        new_labels = []
        for entry in labels:
            label, next_hint = _sanitize_label(entry, used, next_hint)
            if label is not None:
                new_labels.append(label)
        self._data["labels"] = new_labels
        self._data["next_id"] = next_hint
        removed = prior_ids - {l["id"] for l in new_labels}
        if removed:
            assignments = self._data.get("assignments", {})
            empty_keys = []
            for key, ids in list(assignments.items()):
                kept = [i for i in ids if i not in removed]
                if kept:
                    assignments[key] = kept
                else:
                    empty_keys.append(key)
            for k in empty_keys:
                assignments.pop(k, None)
        self._touch()

    def assign(self, key: str, tag_ids: list) -> None:
        """Replace the tag-id list for one key. Unknown ids are dropped."""
        valid = self.valid_ids()
        tag_ids = [i for i in tag_ids if isinstance(i, int) and i in valid]
        assignments = self._data.setdefault("assignments", {})
        if tag_ids:
            assignments[key] = sorted(set(tag_ids))
        else:
            assignments.pop(key, None)
        self._touch()

    def bulk_assign(self, keys: list, tag_id: int, add: bool) -> int:
        """Add/remove a single tag id across many keys. Returns the
        number of keys whose assignment set actually changed."""
        if not isinstance(tag_id, int) or tag_id not in self.valid_ids():
            return 0
        assignments = self._data.setdefault("assignments", {})
        count = 0
        for k in keys:
            cur = set(assignments.get(k, []))
            if add:
                if tag_id not in cur:
                    cur.add(tag_id)
                    count += 1
            else:
                if tag_id in cur:
                    cur.discard(tag_id)
                    count += 1
            if cur:
                assignments[k] = sorted(cur)
            else:
                assignments.pop(k, None)
        if count:
            self._touch()
        return count

    def remove_key(self, key: str) -> None:
        """Drop all tag assignments for a key (e.g., conversation or
        project was deleted). Labels are untouched."""
        assignments = self._data.setdefault("assignments", {})
        if key in assignments:
            del assignments[key]
            self._touch()
