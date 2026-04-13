"""Tests for JSONL edit endpoints: chain re-linking and tool block stripping."""
import json
from pathlib import Path

import pytest

import llm_lens


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def read_jsonl(path: Path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def msg(uuid, parent, text="hi", role="user", type_="user"):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": type_,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def tool_use_msg(uuid, parent, tool_id, text="calling"):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}},
            ],
        },
    }


def tool_result_msg(uuid, parent, tool_id, text="result", extra_text=None):
    content = [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]
    if extra_text:
        content.insert(0, {"type": "text", "text": extra_text})
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "user",
        "message": {"role": "user", "content": content},
    }


# ---------------------------------------------------------------------------
# DELETE tests
# ---------------------------------------------------------------------------

def test_delete_relinks_child_to_grandparent(client, tmp_path):
    convo = "conv1"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "first"),
        msg("b", "a", "middle"),
        msg("c", "b", "last"),
    ])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert resp.status_code == 200

    entries = read_jsonl(path)
    assert [e["uuid"] for e in entries] == ["a", "c"]
    assert entries[1]["parentUuid"] == "a"  # c re-linked to a


def test_delete_root_sets_child_parent_to_none(client, tmp_path):
    convo = "conv2"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "root"),
        msg("b", "a", "child"),
    ])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/a")
    assert resp.status_code == 200

    entries = read_jsonl(path)
    assert [e["uuid"] for e in entries] == ["b"]
    assert entries[0]["parentUuid"] is None


def test_delete_assistant_with_tool_use_strips_matching_tool_result(client, tmp_path):
    convo = "conv3"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "ask"),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1", extra_text="followup"),
        msg("d", "c", "end"),
    ])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert resp.status_code == 200

    entries = read_jsonl(path)
    uuids = [e["uuid"] for e in entries]
    assert "b" not in uuids
    c = next(e for e in entries if e["uuid"] == "c")
    # tool_result block for tool_1 stripped, text block survives
    types = [b["type"] for b in c["message"]["content"]]
    assert "tool_result" not in types
    assert "text" in types
    # parent chain: c should now point to a (b's parent)
    assert c["parentUuid"] == "a"


def test_delete_tool_use_drops_tool_result_message_if_empty(client, tmp_path):
    convo = "conv4"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "ask"),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1"),  # only tool_result, no text
        msg("d", "c", "end"),
    ])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert resp.status_code == 200

    entries = read_jsonl(path)
    uuids = [e["uuid"] for e in entries]
    # c had only the tool_result → becomes empty → dropped; d re-parented
    assert uuids == ["a", "d"]
    d = entries[1]
    assert d["parentUuid"] == "a"


def test_delete_nonexistent_returns_404(client, tmp_path):
    convo = "conv5"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [msg("a", None)])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# EXTRACT tests
# ---------------------------------------------------------------------------

def test_extract_contiguous_preserves_chain(client, tmp_path):
    convo = "conv6"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "first"),
        msg("b", "a", "second"),
        msg("c", "b", "third"),
    ])

    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["b", "c"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    assert [e["uuid"] for e in entries] == ["b", "c"]
    # b's parent was a (not extracted) → should be None
    assert entries[0]["parentUuid"] is None
    assert entries[1]["parentUuid"] == "b"


def test_extract_noncontiguous_remaps_to_nearest_extracted_ancestor(client, tmp_path):
    convo = "conv7"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        msg("b", "a"),
        msg("c", "b"),
        msg("d", "c"),
    ])

    # extract a and d only — d's parent c is skipped, should remap up to a
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["a", "d"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    assert [e["uuid"] for e in entries] == ["a", "d"]
    assert entries[0]["parentUuid"] is None
    assert entries[1]["parentUuid"] == "a"


def test_extract_strips_orphan_tool_use_block(client, tmp_path):
    convo = "conv8"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1", text="thinking"),
        tool_result_msg("c", "b", "tool_1"),
    ])

    # extract b (has tool_use) but not c (has matching tool_result)
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["b"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    assert len(entries) == 1
    types = [b["type"] for b in entries[0]["message"]["content"]]
    assert "tool_use" not in types
    assert "text" in types


def test_extract_strips_orphan_tool_result_block(client, tmp_path):
    convo = "conv9"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1", extra_text="after"),
    ])

    # extract c (has tool_result) but not b (has matching tool_use)
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["c"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    assert len(entries) == 1
    types = [b["type"] for b in entries[0]["message"]["content"]]
    assert "tool_result" not in types
    assert "text" in types


def test_extract_drops_message_when_only_content_was_orphan_block(client, tmp_path):
    convo = "conv10"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1"),  # only tool_result, no text
    ])

    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["c"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    assert entries == []


def assert_resume_safe(entries):
    """A JSONL is resume-safe if every parentUuid points to an existing
    uuid (or is null) and every tool_use has a matching tool_result and
    vice versa. These are the structural invariants Claude Code's API
    needs to replay a session without dropping messages."""
    uuids = {e["uuid"] for e in entries if e.get("uuid")}
    for e in entries:
        p = e.get("parentUuid")
        assert p is None or p in uuids, f"dangling parentUuid {p!r} on {e.get('uuid')}"

    tool_use_ids = set()
    tool_result_ids = set()
    for e in entries:
        content = (e.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                tool_use_ids.add(b["id"])
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                tool_result_ids.add(b["tool_use_id"])
    assert tool_use_ids == tool_result_ids, (
        f"tool_use/tool_result mismatch: "
        f"orphan uses={tool_use_ids - tool_result_ids}, "
        f"orphan results={tool_result_ids - tool_use_ids}"
    )


def test_resume_safe_after_delete_middle(client, tmp_path):
    convo = "r1"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1", extra_text="ok"),
        msg("d", "c"),
        msg("e", "d"),
    ])
    client.delete(f"/api/projects/proj/conversations/{convo}/messages/d")
    assert_resume_safe(read_jsonl(path))


def test_resume_safe_after_delete_tool_use(client, tmp_path):
    convo = "r2"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1", extra_text="followup"),
        msg("d", "c"),
    ])
    client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert_resume_safe(read_jsonl(path))


def test_resume_safe_after_noncontiguous_extract(client, tmp_path):
    convo = "r3"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1"),
        tool_result_msg("c", "b", "tool_1", extra_text="ok"),
        msg("d", "c"),
        tool_use_msg("e", "d", "tool_2"),
        tool_result_msg("f", "e", "tool_2", extra_text="ok2"),
    ])
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["a", "d", "f"]},  # skipping tool pairs on purpose
    )
    new_id = resp.get_json()["new_id"]
    assert_resume_safe(read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl"))


def test_resume_safe_after_extract_partial_tool_pair(client, tmp_path):
    convo = "r4"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        tool_use_msg("b", "a", "tool_1", text="keep me"),
        tool_result_msg("c", "b", "tool_1"),
    ])
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["a", "b"]},  # tool_use without its result
    )
    new_id = resp.get_json()["new_id"]
    assert_resume_safe(read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl"))


# ---------------------------------------------------------------------------
# Branching (forked conversations)
# ---------------------------------------------------------------------------

def test_delete_message_with_multiple_children_relinks_all(client, tmp_path):
    """Deleting B where B has children C and D must re-parent both to A."""
    convo = "fork1"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "root"),
        msg("b", "a", "fork point"),
        msg("c", "b", "branch-1"),
        msg("d", "b", "branch-2"),
    ])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert resp.status_code == 200

    entries = read_jsonl(path)
    uuids = [e["uuid"] for e in entries]
    assert "b" not in uuids
    parents = {e["uuid"]: e["parentUuid"] for e in entries}
    assert parents["c"] == "a"
    assert parents["d"] == "a"


def test_resume_safe_after_delete_forked_message(client, tmp_path):
    convo = "fork-r"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        msg("b", "a"),
        msg("c", "b"),
        msg("d", "b"),  # second child of b
    ])
    client.delete(f"/api/projects/proj/conversations/{convo}/messages/b")
    assert_resume_safe(read_jsonl(path))


# ---------------------------------------------------------------------------
# Duplicate: structural integrity
# ---------------------------------------------------------------------------

def test_duplicate_copies_full_chain_intact(client, tmp_path):
    convo = "dup1"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None, "root"),
        msg("b", "a", "child"),
        tool_use_msg("c", "b", "t1"),
        tool_result_msg("d", "c", "t1"),
    ])

    resp = client.post(f"/api/projects/proj/conversations/{convo}/duplicate")
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]
    assert new_id != convo

    orig = read_jsonl(path)
    dup = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")

    assert [e["uuid"] for e in orig] == [e["uuid"] for e in dup]
    assert [e["parentUuid"] for e in orig] == [e["parentUuid"] for e in dup]
    assert_resume_safe(dup)
    assert_resume_safe(orig)


# ---------------------------------------------------------------------------
# Bulk-delete edge case
# ---------------------------------------------------------------------------

def test_bulk_delete_empty_ids_returns_400_or_zero(client, tmp_path):
    (tmp_path / "proj").mkdir()
    resp = client.post(
        "/api/projects/proj/conversations/bulk-delete",
        json={"ids": []},
    )
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert resp.get_json()["deleted"] == 0


# ---------------------------------------------------------------------------
# Multi-tool-use partial orphaning
# ---------------------------------------------------------------------------

def test_extract_strips_only_orphaned_tool_use_in_multi_block(client, tmp_path):
    """Assistant message has two tool_use blocks. Extract includes the
    result for t2 but not t1 — only t1's block should be stripped."""
    convo = "multi_tool"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [
        msg("a", None),
        {
            "uuid": "b", "parentUuid": "a", "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Write", "input": {}},
            ]},
        },
        tool_result_msg("c", "b", "t1"),
        tool_result_msg("d", "b", "t2"),
    ])

    # extract b and d only — t1 is orphaned, t2 is intact
    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": ["a", "b", "d"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    entries = read_jsonl(tmp_path / "proj" / f"{new_id}.jsonl")
    b_entry = next(e for e in entries if e["uuid"] == "b")
    tool_ids = [
        blk["id"] for blk in b_entry["message"]["content"]
        if blk.get("type") == "tool_use"
    ]
    assert "t1" not in tool_ids  # orphaned → stripped
    assert "t2" in tool_ids      # paired → kept
    assert_resume_safe(entries)


# ---------------------------------------------------------------------------
# Malformed JSONL
# ---------------------------------------------------------------------------

def test_malformed_jsonl_line_is_skipped_not_crashed(client, tmp_path):
    """A truncated / non-JSON line should not crash parse or mutation paths."""
    convo = "bad1"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(msg("a", None, "alpha")) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps(msg("b", "a", "bravo")) + "\n")

    # parse via GET should not 500
    resp = client.get(f"/api/projects/proj/conversations/{convo}?limit=1000")
    assert resp.status_code == 200
    uuids = [m["uuid"] for m in resp.get_json()["main"]]
    assert uuids == ["a", "b"]

    # mutation on a file with a bad line should also not 500
    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/a")
    assert resp.status_code == 200


def test_extract_empty_selection_returns_400(client, tmp_path):
    convo = "conv11"
    path = tmp_path / "proj" / f"{convo}.jsonl"
    write_jsonl(path, [msg("a", None)])

    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/extract",
        json={"uuids": []},
    )
    assert resp.status_code == 400
