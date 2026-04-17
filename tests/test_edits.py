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

    # Dup must get fresh uuids — same IDs would collide with parent under
    # Claude Code's `/resume` sessionId/message lookup.
    orig_uuids = [e["uuid"] for e in orig]
    dup_uuids = [e["uuid"] for e in dup]
    assert orig_uuids != dup_uuids
    assert set(orig_uuids).isdisjoint(set(dup_uuids))

    # Parent chain inside the dup is internally consistent: each parentUuid
    # either is None or points at a uuid earlier in the same file.
    seen: set = set()
    for e in dup:
        pu = e.get("parentUuid")
        if pu is not None:
            assert pu in seen
        seen.add(e["uuid"])

    # Shape of the chain preserved: same count of null parents, etc.
    assert [p is None for p in (e["parentUuid"] for e in orig)] == \
           [p is None for p in (e["parentUuid"] for e in dup)]

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



# ----------------------------------------------------------------------------
# /messages/<uuid>/edit — in-place text replacement. Preserves usage, uuid,
# and parentUuid (same file-rewrite contract as delete/scrub). Prose-only:
# messages containing tool_use / tool_result / thinking blocks must be
# rejected because their shape carries structural meaning.
# ----------------------------------------------------------------------------


def _assistant_with_usage(uuid, parent, text, in_tokens, out_tokens):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        },
    }


def test_edit_replaces_text_preserves_usage_and_chain(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [
        msg("u1", None, "what is 2+2"),
        _assistant_with_usage("a1", "u1", "The answer is four.", 100, 20),
        msg("u2", "a1", "thanks"),
    ])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/a1/edit",
        json={"text": "redacted"},
    )
    assert resp.status_code == 200

    lines = read_jsonl(path)
    edited = next(e for e in lines if e["uuid"] == "a1")
    # Chain untouched.
    assert edited["parentUuid"] == "u1"
    # Usage (historical billing record) untouched.
    assert edited["message"]["usage"]["input_tokens"] == 100
    assert edited["message"]["usage"]["output_tokens"] == 20
    # Content collapsed to a single text block holding the new text.
    assert edited["message"]["content"] == [{"type": "text", "text": "redacted"}]
    # Neighbours untouched — edit is local.
    u1 = next(e for e in lines if e["uuid"] == "u1")
    assert u1["message"]["content"] == [{"type": "text", "text": "what is 2+2"}]


def test_edit_accepts_empty_string(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "original")])
    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={"text": ""},
    )
    assert resp.status_code == 200
    lines = read_jsonl(path)
    assert lines[0]["message"]["content"] == [{"type": "text", "text": ""}]


def test_edit_rejects_missing_text_field(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "hi")])
    # Body without `text` key.
    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={},
    )
    assert resp.status_code == 400
    # Non-string `text`.
    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={"text": 42},
    )
    assert resp.status_code == 400


def test_edit_allows_message_with_tool_use_block_and_tombstones_counts(client, tmp_path):
    """Edit on non-prose messages is allowed — the tool_use block gets
    collapsed to the new text, and the lost tool_uses count is tombstoned
    into deleted_delta so stats totals survive the re-scan."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [tool_use_msg("t1", None, tool_id="tu1", text="I'll look that up.")])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/t1/edit",
        json={"text": "anything"},
    )
    assert resp.status_code == 200

    # Content collapsed to a single text block.
    lines = read_jsonl(path)
    assert lines[0]["message"]["content"] == [{"type": "text", "text": "anything"}]

    # Tombstone carries the lost tool_use count + messages_edited counter.
    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["tool_uses"] == {"Read": 1}
    assert dd["messages_edited"] == 1


def test_edit_allows_message_with_tool_result_block(client, tmp_path):
    """Tool_result blocks aren't counted in stats, so editing a tool_result-only
    message collapses content but produces an empty stats diff (no tombstoned
    stats) aside from the messages_edited counter."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [tool_result_msg("r1", None, tool_id="tu1", text="result")])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/r1/edit",
        json={"text": "anything"},
    )
    assert resp.status_code == 200
    lines = read_jsonl(path)
    assert lines[0]["message"]["content"] == [{"type": "text", "text": "anything"}]

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["messages_edited"] == 1
    # tool_results aren't counted in stats — diff should have no tool_uses etc.
    assert "tool_uses" not in dd or not dd["tool_uses"]


def test_edit_allows_message_with_thinking_block_and_tombstones(client, tmp_path):
    """Editing a thinking-bearing assistant turn is allowed; the thinking_count
    and thinking_output_tokens_estimate move to deleted_delta so modal stats
    stay accurate."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    thinking_msg = {
        "uuid": "th1",
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "text", "text": "answer"},
                {"type": "thinking", "thinking": "let me reason", "signature": "sig"},
            ],
        },
    }
    write_jsonl(path, [thinking_msg])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/th1/edit",
        json={"text": "new"},
    )
    assert resp.status_code == 200

    # Thinking block is gone from content.
    lines = read_jsonl(path)
    assert not any(b.get("type") == "thinking" for b in lines[0]["message"]["content"])

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["thinking_count"] == 1
    assert dd["messages_edited"] == 1
    # Tokens stay at 0 in delta because usage is preserved on the message.
    assert "input_tokens" not in dd


def test_edit_bash_tool_use_tombstones_command_breakdown(client, tmp_path):
    """Editing over a Bash tool_use moves its command-name count and
    per-command token slice into the tombstone, so the Bash command breakdown
    in the stats modal survives the edit (both top-level and per_model)."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    bash_msg = {
        "uuid": "a1",
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "grep -rn foo ."}},
                {"type": "text", "text": "looking"},
            ],
        },
    }
    write_jsonl(path, [bash_msg])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/a1/edit",
        json={"text": "scrubbed"},
    )
    assert resp.status_code == 200

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["tool_uses"] == {"Bash": 1}
    assert dd["commands"] == {"grep": 1}
    assert dd["per_model"]["claude-opus-4-7"]["commands"] == {"grep": 1}
    assert dd["per_model"]["claude-opus-4-7"]["command_turn_tokens"]["grep"]["input_tokens"] > 0


def test_edit_prose_only_produces_empty_stats_diff(client, tmp_path):
    """A prose-only edit shouldn't tombstone stats fields (content shape
    unchanged, usage preserved) — only the messages_edited counter bumps."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "original")])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={"text": "rewritten"},
    )
    assert resp.status_code == 200

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd == {"messages_edited": 1}


def test_delete_message_tombstones_full_per_message_stats(client, tmp_path):
    """Per-message delete should tombstone the full stats of the removed
    message (tokens, tool_uses, commands, thinking_count, per_model) plus
    the messages_deleted counter."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "s.jsonl"
    bash_msg = {
        "uuid": "a1",
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 40, "output_tokens": 20,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                {"type": "text", "text": "listing"},
            ],
        },
    }
    write_jsonl(path, [bash_msg])

    resp = client.delete("/api/projects/proj/conversations/s/messages/a1")
    assert resp.status_code == 200

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["messages_deleted"] == 1
    assert dd["input_tokens"] == 40
    assert dd["output_tokens"] == 20
    assert dd["tool_uses"] == {"Bash": 1}
    assert dd["commands"] == {"ls": 1}
    pm = dd["per_model"]["claude-opus-4-7"]
    assert pm["tool_uses"] == {"Bash": 1}
    assert pm["input_tokens"] == 40


def test_edit_on_subagent_file_succeeds(client, tmp_path):
    """api_edit_message must find messages that live in subagent run files
    (<convo_id>/subagents/agent-*.jsonl), not just in the main convo file.
    Regression: subagent-view edits used to return 404."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    convo = "sess"
    main = tmp_path / "proj" / f"{convo}.jsonl"
    sub = tmp_path / "proj" / convo / "subagents" / "agent-x-abc.jsonl"
    write_jsonl(main, [msg("m1", None, "hi")])
    write_jsonl(sub, [msg("s1", None, "subagent text")])

    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/messages/s1/edit",
        json={"text": "edited in subagent"},
    )
    assert resp.status_code == 200

    # Subagent file updated, main file untouched.
    assert read_jsonl(sub)[0]["message"]["content"] == [{"type": "text", "text": "edited in subagent"}]
    assert read_jsonl(main)[0]["uuid"] == "m1"


def test_delete_on_subagent_file_succeeds(client, tmp_path):
    """api_delete_message resolves subagent file paths the same way as edit."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    convo = "sess"
    main = tmp_path / "proj" / f"{convo}.jsonl"
    sub = tmp_path / "proj" / convo / "subagents" / "agent-x-abc.jsonl"
    write_jsonl(main, [msg("m1", None, "hi")])
    write_jsonl(sub, [msg("s1", None, "subagent a"), msg("s2", "s1", "subagent b")])

    resp = client.delete(f"/api/projects/proj/conversations/{convo}/messages/s2")
    assert resp.status_code == 200

    # s2 removed from subagent file; main file untouched.
    uuids = [e["uuid"] for e in read_jsonl(sub)]
    assert uuids == ["s1"]
    assert read_jsonl(main)[0]["uuid"] == "m1"


def test_edit_on_nonexistent_uuid_in_subagent_still_404(client, tmp_path):
    """If the uuid isn't in the main file or any subagent file, 404."""
    convo = "sess"
    main = tmp_path / "proj" / f"{convo}.jsonl"
    sub = tmp_path / "proj" / convo / "subagents" / "agent-x-abc.jsonl"
    write_jsonl(main, [msg("m1", None, "hi")])
    write_jsonl(sub, [msg("s1", None, "subagent")])

    resp = client.post(
        f"/api/projects/proj/conversations/{convo}/messages/ghost/edit",
        json={"text": "x"},
    )
    assert resp.status_code == 404


def test_edit_returns_404_for_missing_message(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "hi")])
    resp = client.post(
        "/api/projects/proj/conversations/s/messages/does-not-exist/edit",
        json={"text": "x"},
    )
    assert resp.status_code == 404


def test_edit_returns_404_for_missing_conversation(client, tmp_path):
    resp = client.post(
        "/api/projects/proj/conversations/nope/messages/u1/edit",
        json={"text": "x"},
    )
    assert resp.status_code == 404


def test_edit_invalidates_parse_cache(client, tmp_path):
    """After an edit, reading the conversation back must show the new text —
    the mtime-keyed parse cache needs to be invalidated."""
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "original text")])
    # Prime the cache.
    pre = client.get("/api/projects/proj/conversations/s").get_json()
    assert pre["main"][0]["content"] == "original text"

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={"text": "new text"},
    )
    assert resp.status_code == 200

    post = client.get("/api/projects/proj/conversations/s").get_json()
    assert post["main"][0]["content"] == "new text"


def test_edit_on_string_content_keeps_string_shape(client, tmp_path):
    """Messages whose content was stored as a bare string (rather than a
    list of text blocks) should still accept edits. The new text replaces
    the string in place."""
    path = tmp_path / "proj" / "s.jsonl"
    # Build a message with string content directly (msg() uses list shape).
    entry = {
        "uuid": "u1",
        "parentUuid": None,
        "type": "user",
        "message": {"role": "user", "content": "plain string content"},
    }
    write_jsonl(path, [entry])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/u1/edit",
        json={"text": "replaced"},
    )
    assert resp.status_code == 200

    lines = read_jsonl(path)
    assert lines[0]["message"]["content"] == "replaced"



def test_delete_conversation_tombstones_all_live_stats(client, tmp_path):
    """Whole-file delete stores the file's full live stats (tokens, tool_uses,
    commands, per_model, thinking_count, etc) in deleted_delta so totals
    survive in aggregation even after the JSONL is gone."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "g.jsonl"
    write_jsonl(path, [
        {"uuid": "a1", "type": "assistant", "message": {
            "role": "assistant", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 80, "output_tokens": 40,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "thinking", "thinking": "t"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "grep x"}},
                {"type": "text", "text": "done"},
            ]}},
    ])

    resp = client.delete("/api/projects/proj/conversations/g")
    assert resp.status_code == 200
    assert not path.exists()

    dd = llm_lens.peek_cache._store[str(path)]["deleted_delta"]
    assert dd["input_tokens"] == 80
    assert dd["output_tokens"] == 40
    assert dd["tool_uses"] == {"Bash": 1}
    assert dd["commands"] == {"grep": 1}
    assert dd["thinking_count"] == 1
    assert dd["per_model"]["claude-opus-4-7"]["tool_uses"] == {"Bash": 1}


def test_overview_deleted_delta_surfaces_tombstoned_fields(client, tmp_path):
    """api_overview's totals.deleted_delta must carry every field the
    unified schema covers (commands, per_model, messages_edited, etc) so
    tombstones written by edit/delete show up under the 'Deleted' filter."""
    import llm_lens
    llm_lens.peek_cache._store.clear()

    path = tmp_path / "proj" / "ov.jsonl"
    write_jsonl(path, [
        {"uuid": "a1", "type": "assistant", "message": {
            "role": "assistant", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 30, "output_tokens": 10,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                {"type": "text", "text": "x"},
            ]}},
    ])
    resp = client.post(
        "/api/projects/proj/conversations/ov/messages/a1/edit",
        json={"text": "edited"},
    )
    assert resp.status_code == 200

    r = client.get("/api/overview?folder=proj&range=all")
    assert r.status_code == 200
    dd = r.get_json()["totals"]["deleted_delta"]
    assert dd["tool_uses"] == {"Bash": 1}
    assert dd["commands"] == {"ls": 1}
    assert dd["messages_edited"] == 1
    assert dd["per_model"]["claude-opus-4-7"]["commands"] == {"ls": 1}



# ---------------------------------------------------------------------------
# Preview extraction (first + last user message) — edge cases
# ---------------------------------------------------------------------------

def test_preview_handles_string_content(tmp_path):
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user","content":"hi there"}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "hi there"
    assert r["last_preview"] == "hi there"


def test_preview_handles_list_content(tmp_path):
    """Regression: list-shape content (what _replace_content writes after an
    edit) used to fall through to \"(empty)\" because the old check was
    `isinstance(content, str)` only."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    entry = {"uuid":"u1","type":"user",
             "message":{"role":"user","content":[{"type":"text","text":"listy hi"}]}}
    with open(path, "w") as f:
        f.write(json.dumps(entry)+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "listy hi"
    assert r["last_preview"] == "listy hi"


def test_preview_skips_tool_result_only_user_messages(tmp_path):
    """A user message whose content is only a tool_result block shouldn't
    leak as the preview — it's not human prose. Should fall through to the
    next user message with actual text."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"u0","type":"user","message":{"role":"user",
                "content":[{"type":"tool_result","tool_use_id":"x","content":"stdout"}]}})+"\n")
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user",
                "content":"real first prompt"}})+"\n")
        f.write(json.dumps({"uuid":"u2","type":"user","message":{"role":"user",
                "content":"real last prompt"}})+"\n")
        f.write(json.dumps({"uuid":"u3","type":"user","message":{"role":"user",
                "content":[{"type":"tool_result","tool_use_id":"y","content":"more stdout"}]}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "real first prompt"
    # Last user preview should skip the trailing tool_result-only message.
    assert r["last_preview"] == "real last prompt"


def test_preview_skips_assistant_tool_use(tmp_path):
    """Assistant turns (even those containing tool_use) are not user messages
    and never appear in preview."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"a1","type":"assistant","message":{"role":"assistant",
                "content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}})+"\n")
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user",
                "content":"actual user prompt"}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "actual user prompt"
    assert r["last_preview"] == "actual user prompt"


def test_preview_handles_mixed_text_and_tool_result(tmp_path):
    """Content with both a text block AND a tool_result should extract only
    the text portion."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    entry = {"uuid":"u1","type":"user","message":{"role":"user","content":[
        {"type":"tool_result","tool_use_id":"x","content":"ignore me"},
        {"type":"text","text":"visible part"},
    ]}}
    with open(path, "w") as f:
        f.write(json.dumps(entry)+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "visible part"
    assert r["last_preview"] == "visible part"


def test_preview_empty_when_no_user_messages(tmp_path):
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"a1","type":"assistant","message":{"role":"assistant",
                "content":[{"type":"text","text":"hi"}]}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "(empty)"
    assert r["last_preview"] == "(empty)"


def test_preview_last_differs_from_first_after_multiple_user_msgs(tmp_path):
    """In multi-turn convos, first_preview = opening prompt, last_preview =
    most recent user turn. UI toggles between them."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user","content":"opener"}})+"\n")
        f.write(json.dumps({"uuid":"a1","type":"assistant","message":{"role":"assistant",
                "content":[{"type":"text","text":"ack"}]}})+"\n")
        f.write(json.dumps({"uuid":"u2","type":"user","message":{"role":"user","content":"middle"}})+"\n")
        f.write(json.dumps({"uuid":"a2","type":"assistant","message":{"role":"assistant",
                "content":[{"type":"text","text":"ok"}]}})+"\n")
        f.write(json.dumps({"uuid":"u3","type":"user","message":{"role":"user","content":"closer"}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "opener"
    assert r["last_preview"] == "closer"


def test_preview_last_works_on_large_file_via_tail(tmp_path):
    """_tail_user_preview reads only the last ~64KB. A very long file with a
    real closing message well before its end should still return that text
    as long as it's in the tail window."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user","content":"opener"}})+"\n")
        # Pile on many assistant turns to push past trivial sizes
        for i in range(50):
            f.write(json.dumps({"uuid":f"a{i}","type":"assistant","message":{"role":"assistant",
                    "content":[{"type":"text","text":"x" * 200}]}})+"\n")
        f.write(json.dumps({"uuid":"ulast","type":"user","message":{"role":"user","content":"closer"}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "opener"
    assert r["last_preview"] == "closer"


def test_preview_survives_edit_of_first_user_message(client, tmp_path):
    """Concrete regression: edit the first user message. The preview should
    now show the edited text (not \"(empty)\", which was the old bug when
    list-shape content was introduced by _replace_content)."""
    import llm_lens
    llm_lens.peek_cache._store.clear()
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [msg("u1", None, "original opening")])

    r = client.post(
        "/api/projects/proj/conversations/c/messages/u1/edit",
        json={"text": "replaced opening"},
    )
    assert r.status_code == 200

    r2 = client.get("/api/projects/proj/conversations").get_json()
    items = r2["items"]
    assert len(items) == 1
    assert items[0]["preview"] == "replaced opening"
    assert items[0]["last_preview"] == "replaced opening"



def test_preview_last_falls_back_to_first_when_tail_cant_find(tmp_path, monkeypatch):
    """When the tail reader can't locate a user message (very-long-file
    pathology, or the tail window is all assistant / tool_result), the
    returned `last_preview` falls back to `preview` rather than "(empty)".
    This keeps the UI from showing empty strings for real convos just
    because the last user turn lives beyond the tail read window."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    # Force the tail reader to miss.
    monkeypatch.setattr(llm_lens, "_tail_user_preview", lambda fp: None)
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"uuid":"u1","type":"user","message":{"role":"user","content":"first only"}})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "first only"
    assert r["last_preview"] == "first only"


def test_preview_both_empty_only_when_no_user_content(tmp_path):
    """Both preview and last_preview stay "(empty)" only when a file has
    no user messages at all (e.g. a file with just agent-setting / system
    entries). Distinct signal for the UI so it can render accordingly."""
    import llm_lens
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write(json.dumps({"type":"agent-setting","agentSetting":"fixer","sessionId":"c"})+"\n")
        f.write(json.dumps({"type":"system","subtype":"informational","content":"x"})+"\n")
    stat = path.stat()
    r = llm_lens._peek_jsonl_cached(str(path), stat.st_mtime, stat.st_size)
    assert r["preview"] == "(empty)"
    assert r["last_preview"] == "(empty)"


def test_preview_after_delete_of_last_user_message(client, tmp_path):
    """End-to-end: deleting the last user message should update last_preview
    to whatever the new last user message is (fallback to first if single-turn)."""
    import llm_lens
    llm_lens.peek_cache._store.clear()
    llm_lens._peek_jsonl_cached.cache_clear()
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u1", None, "opening"),
        msg("u2", "u1", "closing"),
    ])

    assert client.delete("/api/projects/proj/conversations/c/messages/u2").status_code == 200

    r = client.get("/api/projects/proj/conversations").get_json()
    item = r["items"][0]
    assert item["preview"] == "opening"
    # Only u1 remains — last should equal first (fallback path or direct match).
    assert item["last_preview"] == "opening"
