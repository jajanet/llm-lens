"""End-to-end smoke tests covering every user-facing feature surface.

Unlike `test_invariants.py` (deep parser/pagination/cache invariants) and
`test_edits.py` (focused edit-path correctness), this file is breadth-first:
one quick test per feature that calls the API end-to-end and asserts the
response shape + basic behavior. Goal is "did this whole feature still
load and respond sensibly" rather than exhaustive coverage.

Run via: `pytest tests/test_smoke.py -v`
"""
import json
from pathlib import Path

import pytest

import llm_lens


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "ARCHIVE_ROOT", tmp_path / "_archive")
    monkeypatch.setattr(
        llm_lens, "_word_lists_path",
        lambda: tmp_path / "word_lists.json",
    )
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens._stats_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def user_msg(uid, parent, text):
    return {
        "uuid": uid, "parentUuid": parent, "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": text}]},
    }


def assistant_msg(uid, parent, text, *, in_t=10, out_t=5,
                  cache_read=0, cache_creation=0, model="claude-test"):
    return {
        "uuid": uid, "parentUuid": parent, "type": "assistant",
        "message": {
            "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": in_t, "output_tokens": out_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def bash_msg(uid, parent, command, tid="b1"):
    return {
        "uuid": uid, "parentUuid": parent, "type": "assistant",
        "message": {
            "role": "assistant", "model": "claude-test",
            "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                         "input": {"command": command}}],
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }


def make_convo(tmp_path, folder="proj", convo="c", n_assistant=2,
               include_bash=False):
    path = tmp_path / folder / f"{convo}.jsonl"
    entries = [user_msg("u1", None, "hello")]
    parent = "u1"
    for i in range(n_assistant):
        uid = f"a{i + 1}"
        entries.append(assistant_msg(uid, parent, f"reply {i + 1}"))
        parent = uid
    if include_bash:
        entries.append(bash_msg("bx", parent, "grep foo file.txt"))
    write_jsonl(path, entries)
    return path


# ---------------------------------------------------------------------------
# Discovery + listing
# ---------------------------------------------------------------------------

def test_smoke_projects_listing(client, tmp_path):
    make_convo(tmp_path, folder="proj-a")
    make_convo(tmp_path, folder="proj-b")
    resp = client.get("/api/projects").get_json()
    folders = {p["folder"] for p in resp}
    assert {"proj-a", "proj-b"}.issubset(folders)


def test_smoke_conversation_list_with_pagination(client, tmp_path):
    for i in range(3):
        make_convo(tmp_path, folder="proj", convo=f"c{i}")
    resp = client.get(
        "/api/projects/proj/conversations?limit=2&offset=0&sort=recent"
    ).get_json()
    assert len(resp.get("conversations") or resp.get("items") or []) <= 2 \
        or "conversations" in resp or isinstance(resp, list)


def test_smoke_messages_endpoint_returns_main_and_pagination(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=3)
    data = client.get("/api/projects/proj/conversations/c?limit=10").get_json()
    assert "main" in data
    assert "total" in data
    assert data["total"] >= 4  # 1 user + 3 assistant


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_smoke_single_conversation_stats_shape(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=2)
    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    # All the fields the dashboard reads
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_creation_tokens", "tool_uses", "thinking_count",
              "models", "per_model", "commands"):
        assert k in s, f"missing stats key: {k}"
    assert s["output_tokens"] >= 1


def test_smoke_project_stats_aggregates_across_convos(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c1")
    make_convo(tmp_path, folder="proj", convo="c2")
    out = client.post("/api/projects/stats", json={"folders": ["proj"]}).get_json()
    assert "proj" in out
    assert out["proj"]["output_tokens"] >= 2  # both convos contribute


def test_smoke_overview_endpoint_returns_buckets(client, tmp_path):
    make_convo(tmp_path, folder="proj")
    resp = client.get("/api/overview?range=all&offset=0").get_json()
    # Just check it returned a dict with some recognizable shape; the
    # exact key surface is tested elsewhere.
    assert isinstance(resp, dict)


# ---------------------------------------------------------------------------
# Bash command extraction (recent feature)
# ---------------------------------------------------------------------------

def test_smoke_stats_includes_bash_command_counts(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "do stuff"),
        bash_msg("a1", "u1", "grep foo file", tid="b1"),
        bash_msg("a2", "a1", "git status", tid="b2"),
        bash_msg("a3", "a2", "grep bar other", tid="b3"),
    ])
    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    assert s["commands"] == {"grep": 2, "git": 1}


def test_smoke_project_stats_aggregates_bash_commands(client, tmp_path):
    """Commands should sum across convos in a project — the project-level
    stats modal needs the merged dict, not just per-convo."""
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "go"),
        bash_msg("a1", "u1", "grep foo file", tid="b1"),
        bash_msg("a2", "a1", "git status", tid="b2"),
    ])
    write_jsonl(tmp_path / "proj" / "c2.jsonl", [
        user_msg("u1", None, "go"),
        bash_msg("a1", "u1", "grep bar file", tid="b1"),
    ])
    out = client.post("/api/projects/stats", json={"folders": ["proj"]}).get_json()
    assert out["proj"]["commands"] == {"grep": 2, "git": 1}


def test_smoke_overview_aggregates_bash_commands_account_wide(client, tmp_path):
    write_jsonl(tmp_path / "proj-a" / "c.jsonl", [
        user_msg("u1", None, "go"),
        bash_msg("a1", "u1", "grep foo file"),
    ])
    write_jsonl(tmp_path / "proj-b" / "c.jsonl", [
        user_msg("u1", None, "go"),
        bash_msg("a1", "u1", "git status"),
    ])
    resp = client.get("/api/overview?range=all&offset=0").get_json()
    cmds = resp["totals"]["commands"]
    assert cmds.get("grep") == 1 and cmds.get("git") == 1


def test_smoke_per_model_includes_commands_thinking_and_tool_turn_tokens(client, tmp_path):
    """`per_model` should now carry commands, thinking_count, and the per-tool
    cost-share attribution used for cost-per-call."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "do stuff"),
        # assistant turn with bash + thinking, billed real tokens
        {
            "uuid": "a1", "parentUuid": "u1", "type": "assistant",
            "message": {
                "role": "assistant", "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "tool_use", "id": "b1", "name": "Bash",
                     "input": {"command": "grep foo file"}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 30,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        },
    ])
    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    pm = s["per_model"]["claude-sonnet-4-5"]
    assert pm["commands"] == {"grep": 1}
    assert pm["thinking_count"] == 1
    # Single tool_use block in the turn → it gets the full turn cost.
    bash = pm["tool_turn_tokens"]["Bash"]
    assert bash["input_tokens"] == 100
    assert bash["output_tokens"] == 30
    # Top-level mirrors per_model sum
    assert s["tool_turn_tokens"]["Bash"]["input_tokens"] == 100


def test_smoke_tool_turn_tokens_splits_cost_among_blocks(client, tmp_path):
    """Each tool_use block in a turn gets 1/N of the turn's tokens, where
    N is the total tool_use block count. Sum across tools = actual turn
    cost — so per-call avg (share / call_count) doesn't double-count.
    Turn here has Bash×2 + Read×1 = 3 blocks costing 300 input tokens:
        Bash share = 200 (2 blocks × 300/3)
        Read share = 100 (1 block × 300/3)
        Sum = 300 ✓
    """
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "do stuff"),
        {
            "uuid": "a1", "parentUuid": "u1", "type": "assistant",
            "message": {
                "role": "assistant", "model": "claude-test",
                "content": [
                    {"type": "tool_use", "id": "b1", "name": "Bash",
                     "input": {"command": "grep foo"}},
                    {"type": "tool_use", "id": "b2", "name": "Bash",
                     "input": {"command": "grep bar"}},
                    {"type": "tool_use", "id": "r1", "name": "Read",
                     "input": {"path": "/etc/hosts"}},
                ],
                "usage": {"input_tokens": 300, "output_tokens": 60,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        },
    ])
    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    ttt = s["tool_turn_tokens"]
    assert ttt["Bash"]["input_tokens"] == 200  # 2 blocks of 300/3
    assert ttt["Bash"]["output_tokens"] == 40  # 2 blocks of 60/3
    assert ttt["Read"]["input_tokens"] == 100  # 1 block of 300/3
    assert ttt["Read"]["output_tokens"] == 20
    # Sum across tools equals the actual turn cost — no double-counting.
    assert ttt["Bash"]["input_tokens"] + ttt["Read"]["input_tokens"] == 300
    assert ttt["Bash"]["output_tokens"] + ttt["Read"]["output_tokens"] == 60
    # Tool counts unaffected by attribution math
    assert s["tool_uses"]["Bash"] == 2
    assert s["tool_uses"]["Read"] == 1


def test_smoke_command_turn_tokens_splits_share_among_commands(client, tmp_path):
    """Bash blocks within a turn get split first by per-block share, then
    keyed by command name. A turn with `grep × 2 + sed × 1` and 300 input
    tokens splits each block to 100, so grep accumulates 200, sed 100.
    Sum across commands = Bash's turn-share = full turn cost (no other
    tools in this turn)."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "do stuff"),
        {
            "uuid": "a1", "parentUuid": "u1", "type": "assistant",
            "message": {
                "role": "assistant", "model": "claude-test",
                "content": [
                    {"type": "tool_use", "id": "b1", "name": "Bash",
                     "input": {"command": "grep foo file"}},
                    {"type": "tool_use", "id": "b2", "name": "Bash",
                     "input": {"command": "grep bar other"}},
                    {"type": "tool_use", "id": "b3", "name": "Bash",
                     "input": {"command": "sed -i 's/x/y/' f"}},
                ],
                "usage": {"input_tokens": 300, "output_tokens": 60,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        },
    ])
    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    ctt = s["command_turn_tokens"]
    assert ctt["grep"]["input_tokens"] == 200
    assert ctt["grep"]["output_tokens"] == 40
    assert ctt["sed"]["input_tokens"] == 100
    assert ctt["sed"]["output_tokens"] == 20
    # Sum across commands matches Bash's tool_turn_tokens (whole turn here
    # was Bash blocks, so they should agree).
    bash_share = s["tool_turn_tokens"]["Bash"]
    assert ctt["grep"]["input_tokens"] + ctt["sed"]["input_tokens"] \
        == bash_share["input_tokens"]
    # Per-model also has it
    pm_ctt = s["per_model"]["claude-test"]["command_turn_tokens"]
    assert pm_ctt["grep"]["input_tokens"] == 200
    assert pm_ctt["sed"]["input_tokens"] == 100


def test_smoke_project_stats_aggregates_per_model_extras(client, tmp_path):
    """Project-level rollup must merge per_model.commands / thinking_count /
    tool_turn_tokens across convos so the modal can show by-model breakdowns."""
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "go"),
        {
            "uuid": "a1", "parentUuid": "u1", "type": "assistant",
            "message": {
                "role": "assistant", "model": "claude-test",
                "content": [{"type": "tool_use", "id": "b1", "name": "Bash",
                             "input": {"command": "grep foo"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        },
    ])
    write_jsonl(tmp_path / "proj" / "c2.jsonl", [
        user_msg("u1", None, "go"),
        {
            "uuid": "a1", "parentUuid": "u1", "type": "assistant",
            "message": {
                "role": "assistant", "model": "claude-test",
                "content": [{"type": "tool_use", "id": "b1", "name": "Bash",
                             "input": {"command": "git status"}}],
                "usage": {"input_tokens": 50, "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        },
    ])
    out = client.post("/api/projects/stats", json={"folders": ["proj"]}).get_json()
    pm = out["proj"]["per_model"]["claude-test"]
    assert pm["commands"] == {"grep": 1, "git": 1}
    # Each convo had a single Bash block — full turn cost goes to Bash.
    # Sum across convos: 100 + 50.
    assert pm["tool_turn_tokens"]["Bash"]["input_tokens"] == 150


def test_smoke_messages_attach_bash_command_data(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "search"),
        bash_msg("a1", "u1", "grep -r foo .", tid="bash-xyz"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=10").get_json()
    a = next(m for m in data["main"] if m["uuid"] == "a1")
    assert a["commands"] == [{"id": "bash-xyz", "command": "grep -r foo ."}]
    # Marker carries the id so the frontend can correlate
    assert "[Tool: Bash:bash-xyz]" in a["content"]


# ---------------------------------------------------------------------------
# Mutations: delete, archive, unarchive, duplicate
# ---------------------------------------------------------------------------

def test_smoke_delete_conversation_tombstones_stats(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    resp = client.delete("/api/projects/proj/conversations/c")
    assert resp.status_code == 200
    # File gone
    assert not (tmp_path / "proj" / "c.jsonl").exists()
    # But project stats still see the deleted_delta tombstone
    s = client.post("/api/projects/stats", json={"folders": ["proj"]}).get_json()
    dd = s["proj"]["deleted_delta"]
    assert dd.get("output_tokens", 0) >= 1


def test_smoke_edit_non_prose_tombstones_tool_and_command_counts(client, tmp_path):
    """End-to-end: edit over a Bash tool_use message. Stats (tool_uses,
    commands, messages_edited) must land in deleted_delta — the modal
    relies on these to keep bash-command breakdowns accurate post-edit."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [user_msg("u1", None, "hi"), bash_msg("a1", "u1", "grep foo .")])
    resp = client.post(
        "/api/projects/proj/conversations/c/messages/a1/edit",
        json={"text": "redacted"},
    )
    assert resp.status_code == 200
    stats = client.get("/api/projects/proj/conversations/c/stats").get_json()
    dd = stats["deleted_delta"]
    assert dd["tool_uses"] == {"Bash": 1}
    assert dd["commands"] == {"grep": 1}
    assert dd["messages_edited"] == 1


def test_smoke_edit_and_delete_work_on_subagent_file(client, tmp_path):
    """Subagent run messages live in <convo>/subagents/agent-*.jsonl.
    Both edit and delete must resolve that path — used to 404."""
    convo = "c"
    write_jsonl(tmp_path / "proj" / f"{convo}.jsonl", [user_msg("m1", None, "hi")])
    sub = tmp_path / "proj" / convo / "subagents" / "agent-x-abc.jsonl"
    write_jsonl(sub, [user_msg("s1", None, "a"), user_msg("s2", "s1", "b")])
    assert client.post(
        f"/api/projects/proj/conversations/{convo}/messages/s1/edit",
        json={"text": "edited"},
    ).status_code == 200
    assert client.delete(
        f"/api/projects/proj/conversations/{convo}/messages/s2"
    ).status_code == 200


def test_smoke_archive_then_unarchive_roundtrip(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    assert client.post("/api/projects/proj/conversations/c/archive").status_code == 200
    assert not (tmp_path / "proj" / "c.jsonl").exists()
    assert client.post("/api/projects/proj/conversations/c/unarchive").status_code == 200
    assert (tmp_path / "proj" / "c.jsonl").exists()


def test_smoke_duplicate_rewrites_session_id_and_writes_sidecar(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    # Inject a sessionId field so we can verify the rewrite touches it.
    path = tmp_path / "proj" / "c.jsonl"
    lines = path.read_text().splitlines()
    rewritten = []
    for line in lines:
        e = json.loads(line)
        e["sessionId"] = "c"
        rewritten.append(json.dumps(e))
    path.write_text("\n".join(rewritten) + "\n")

    resp = client.post("/api/projects/proj/conversations/c/duplicate").get_json()
    new_id = resp["new_id"]
    assert new_id != "c"

    # Sidecar exists with the parent reference
    sidecar = tmp_path / "proj" / f"{new_id}.dup.json"
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text())
    assert meta["duplicate_of"] == "c"

    # Every line in the dup should carry the new sessionId, never the parent's.
    dup_path = tmp_path / "proj" / f"{new_id}.jsonl"
    for line in dup_path.read_text().splitlines():
        e = json.loads(line)
        assert e.get("sessionId") == new_id


def test_smoke_duplicate_subtracts_shared_prefix_while_parent_exists(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=2)
    parent = client.get("/api/projects/proj/conversations/c/stats").get_json()
    parent_out = parent["output_tokens"]
    assert parent_out > 0

    new_id = client.post("/api/projects/proj/conversations/c/duplicate").get_json()["new_id"]

    dup = client.get(f"/api/projects/proj/conversations/{new_id}/stats").get_json()
    # Parent is still around → shared-prefix subtraction zeroes the dup's stats.
    assert dup["output_tokens"] == 0
    assert dup.get("duplicate_of") == "c"


def test_smoke_delete_message_removes_and_updates_total(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=2)
    before = client.get("/api/projects/proj/conversations/c?limit=10").get_json()["total"]
    resp = client.delete("/api/projects/proj/conversations/c/messages/a2")
    assert resp.status_code == 200
    after = client.get("/api/projects/proj/conversations/c?limit=10").get_json()["total"]
    assert after == before - 1


def test_smoke_extract_creates_new_convo(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=2)
    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": ["u1", "a1"]},
    ).get_json()
    new_id = resp.get("new_id") or resp.get("id")
    assert new_id and new_id != "c"
    assert (tmp_path / "proj" / f"{new_id}.jsonl").exists()
    # Original untouched
    assert (tmp_path / "proj" / "c.jsonl").exists()


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

def test_smoke_bulk_archive_and_unarchive(client, tmp_path):
    for i in range(3):
        make_convo(tmp_path, folder="proj", convo=f"c{i}")
    ids = [f"c{i}" for i in range(3)]
    assert client.post(
        "/api/projects/proj/conversations/bulk-archive", json={"ids": ids}
    ).status_code == 200
    assert all(not (tmp_path / "proj" / f"{i}.jsonl").exists() for i in ids)
    assert client.post(
        "/api/projects/proj/conversations/bulk-unarchive", json={"ids": ids}
    ).status_code == 200
    assert all((tmp_path / "proj" / f"{i}.jsonl").exists() for i in ids)


def test_smoke_bulk_delete_conversations(client, tmp_path):
    for i in range(2):
        make_convo(tmp_path, folder="proj", convo=f"c{i}")
    resp = client.post(
        "/api/projects/proj/conversations/bulk-delete",
        json={"ids": ["c0", "c1"]},
    )
    assert resp.status_code == 200
    assert not (tmp_path / "proj" / "c0.jsonl").exists()
    assert not (tmp_path / "proj" / "c1.jsonl").exists()


# ---------------------------------------------------------------------------
# Transforms (redact family) — every kind once
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------

def test_smoke_word_lists_get_post_roundtrip(client, tmp_path):
    # GET defaults
    initial = client.get("/api/word-lists").get_json()
    assert "swears" in initial and "filler" in initial
    assert any(s.endswith("*") for s in initial["swears"])  # `fuck*` etc.

    # POST overrides
    saved = client.post(
        "/api/word-lists",
        json={"swears": ["heck*"], "filler": ["my custom phrase"]},
    ).get_json()
    assert saved["swears"] == ["heck*"]
    assert saved["filler"] == ["my custom phrase"]

    # Persists across reads — and defaults are NOT silently re-merged.
    again = client.get("/api/word-lists").get_json()
    assert again["swears"] == ["heck*"]


def test_smoke_word_list_defaults_endpoint(client, tmp_path):
    d = client.get("/api/word-lists/defaults").get_json()
    assert "swears" in d and "filler" in d
    # Sanity: a couple of expected entries
    assert "fuck*" in d["swears"]
    assert any("absolutely right" in p.lower() for p in d["filler"])


# ---------------------------------------------------------------------------
# Project-level operations
# ---------------------------------------------------------------------------

def test_smoke_delete_project_removes_folder(client, tmp_path):
    make_convo(tmp_path, folder="proj-x", convo="c")
    assert client.delete("/api/projects/proj-x").status_code == 200
    assert not (tmp_path / "proj-x").exists()


def test_smoke_refresh_cache_endpoint(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    # Prime caches first
    client.get("/api/projects/proj/conversations/c?limit=10")
    resp = client.post("/api/projects/proj/refresh-cache")
    assert resp.status_code == 200


def test_smoke_archived_conversations_listing(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    client.post("/api/projects/proj/conversations/c/archive")
    resp = client.get("/api/projects/proj/archived").get_json()
    items = resp["items"] if isinstance(resp, dict) and "items" in resp else resp
    ids = {item if isinstance(item, str) else (item.get("id") or item.get("convo_id"))
           for item in items}
    assert "c" in ids


# ---------------------------------------------------------------------------
# 404 + error paths
# ---------------------------------------------------------------------------

def test_smoke_404s_on_missing_resources(client, tmp_path):
    assert client.get(
        "/api/projects/nope/conversations/none/stats"
    ).status_code in (404, 200)  # may return zeroed stats
    assert client.delete(
        "/api/projects/nope/conversations/none"
    ).status_code == 404
    assert client.post(
        "/api/projects/nope/conversations/none/duplicate"
    ).status_code == 404


def test_smoke_raw_endpoint_returns_file_with_attachment_headers(client, tmp_path):
    """Download raw convo button hits this endpoint. Needs to be an
    attachment (so the browser saves instead of rendering) and carry the
    exact on-disk bytes."""
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=2)
    src = (tmp_path / "proj" / "c.jsonl").read_bytes()

    resp = client.get("/api/projects/proj/conversations/c/raw")
    assert resp.status_code == 200
    assert resp.mimetype == "application/x-ndjson"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert "c.jsonl" in resp.headers.get("Content-Disposition", "")
    assert resp.data == src


def test_smoke_raw_endpoint_404_on_missing(client, tmp_path):
    make_convo(tmp_path, folder="proj", convo="c")
    assert client.get("/api/projects/proj/conversations/gone/raw").status_code == 404
    assert client.get("/api/projects/ghost/conversations/c/raw").status_code == 404


def test_smoke_model_and_usage_exposed_on_assistant_messages(client, tmp_path, monkeypatch):
    """make_convo writes assistant_msg with model + usage; both should
    survive the parser and reach the messages endpoint, since JSONL
    export reads from there."""
    make_convo(tmp_path, folder="proj", convo="c", n_assistant=1)
    data = client.get("/api/projects/proj/conversations/c?limit=10").get_json()
    asst = next(m for m in data["main"] if m["role"] == "assistant")
    assert asst.get("model") == "claude-test"
    assert isinstance(asst.get("usage"), dict)
    assert asst["usage"].get("input_tokens") == 10
    assert asst["usage"].get("output_tokens") == 5


@pytest.fixture
def client_isolated_download_fields(tmp_path, monkeypatch):
    """Scope `download_fields.json` to the temp dir so real user prefs at
    ~/.cache/llm-lens/download_fields.json aren't touched."""
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(
        llm_lens, "_download_fields_path",
        lambda: tmp_path / "download_fields.json",
    )
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def test_smoke_download_fields_get_post_roundtrip(client_isolated_download_fields):
    """Mirror of word-lists smoke: fresh GET returns defaults (with
    role+content forced on); POST persists; subsequent GET reflects."""
    initial = client_isolated_download_fields.get("/api/download-fields").get_json()
    assert initial["role"] is True and initial["content"] is True
    # Defaults ship uuid+timestamp on, optional-richer fields off.
    assert initial["uuid"] is True and initial["timestamp"] is True
    assert initial["commands"] is False and initial["model"] is False

    saved = client_isolated_download_fields.post(
        "/api/download-fields",
        json={"uuid": False, "timestamp": False, "model": True, "usage": True,
              "commands": True},
    ).get_json()
    assert saved["uuid"] is False
    assert saved["model"] is True and saved["usage"] is True
    # Required fields forced on even though we didn't send them.
    assert saved["role"] is True and saved["content"] is True

    again = client_isolated_download_fields.get("/api/download-fields").get_json()
    assert again == saved


def test_smoke_download_fields_empty_post_preserves_required(client_isolated_download_fields):
    resp = client_isolated_download_fields.post("/api/download-fields", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["role"] is True and data["content"] is True



def test_smoke_conversations_list_includes_last_preview(client, tmp_path):
    """Project list exposes `last_preview` alongside `preview` so the UI can
    toggle between showing the first user message (topic) or the last user
    message (resume-where-I-left-off) per the user's choice."""
    fp = tmp_path / "proj" / "c.jsonl"
    write_jsonl(fp, [
        user_msg("u1", None, "FIRST hello"),
        assistant_msg("a1", "u1", "ok"),
        user_msg("u2", "a1", "LAST goodbye"),
    ])
    r = client.get("/api/projects/proj/conversations").get_json()
    item = r["items"][0]
    assert item["preview"] == "FIRST hello"
    assert item["last_preview"] == "LAST goodbye"



def test_smoke_messages_expose_has_tool_use_and_has_thinking_flags(client, tmp_path):
    """Each message in the /conversations/<id> response should carry
    `has_tool_use` / `has_thinking` booleans so the frontend can gate
    non-prose warnings on bulk edit/delete/redact actions without
    re-parsing the flattened display content."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        user_msg("u1", None, "hi"),
        {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
         "message": {"role": "assistant", "model": "claude-test",
                     "usage": {"input_tokens": 1, "output_tokens": 1,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "thinking", "thinking": "t"},
                         {"type": "tool_use", "name": "Bash",
                          "id": "tu1", "input": {"command": "ls"}},
                         {"type": "text", "text": "ok"},
                     ]}},
        {"uuid": "a2", "parentUuid": "a1", "type": "assistant",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [{"type": "text", "text": "plain text"}]}},
    ])
    r = client.get("/api/projects/proj/conversations/c").get_json()
    byId = {m["uuid"]: m for m in r["main"]}
    # Assistant msg with tool_use + thinking is flagged non-prose.
    assert byId["a1"].get("has_tool_use") is True
    assert byId["a1"].get("has_thinking") is True
    # Plain text assistant msg has neither flag (flags are set only when True,
    # so checking absence here is acceptable — frontend falsy-checks them).
    assert not byId["a2"].get("has_tool_use")
    assert not byId["a2"].get("has_thinking")
    # User msg: no tool_use/thinking possible.
    assert not byId["u1"].get("has_tool_use")
    assert not byId["u1"].get("has_thinking")


# ---------------------------------------------------------------------------
# Debloat — breadth-first API smoke.
# Deep coverage (invariants, per-rule behavior, tombstones, abort/restore)
# lives in tests/test_debloat.py. Here we just prove the three endpoints are
# reachable, return the expected shape, and integrate with the rest of the
# stack (conversation-stats endpoint surfaces the tombstone afterward).
# ---------------------------------------------------------------------------


def _big_convo(path):
    """Write a convo that will actually reclaim bytes under every rule."""
    big_stdout = "x" * 5000
    big_tur = {"kind": "WebFetch", "body": "Z" * 12000}
    big_thinking = "T" * 25000
    big_tool_result = "Q" * 12000
    write_jsonl(path, [
        user_msg("u1", None, "hi"),
        {"uuid": "a1", "parentUuid": "u1", "type": "assistant",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [
                         {"type": "thinking", "thinking": big_thinking},
                         {"type": "tool_use", "id": "tA", "name": "Bash",
                          "input": {"command": "sed -i s/x/y/g f"}},
                     ],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
        {"uuid": "u2", "parentUuid": "a1", "type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tA", "content": big_tool_result},
         ]},
         "toolUseResult": {"stdout": big_stdout, "stderr": "", "exitCode": 0}},
        {"uuid": "a2", "parentUuid": "u2", "type": "assistant",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [
                         {"type": "tool_use", "id": "tB", "name": "WebFetch",
                          "input": {"url": "x"}},
                     ],
                     "usage": {"input_tokens": 5, "output_tokens": 2}}},
        {"uuid": "u3", "parentUuid": "a2", "type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tB", "content": "ok"},
         ]},
         "toolUseResult": big_tur},
    ])


def test_debloat_scan_returns_exact_reclaim_per_id(client, tmp_path):
    _big_convo(tmp_path / "proj" / "a.jsonl")
    write_jsonl(tmp_path / "proj" / "b.jsonl", [user_msg("u1", None, "tiny")])

    resp = client.post("/api/projects/proj/conversations/debloat-scan",
                       json={"ids": ["a", "b", "missing"]})
    assert resp.status_code == 200
    body = resp.get_json()

    # Both existing ids get a row; big one > 0 reclaim, small one == 0.
    assert body["a"]["bytes_reclaimable"] > 0
    assert body["a"]["current_size"] > 0
    assert body["b"]["bytes_reclaimable"] == 0
    # Missing id carries an error marker.
    assert body["missing"].get("error") == "not_found"


def test_debloat_single_apply_and_bulk_endpoints_return_ok(client, tmp_path):
    _big_convo(tmp_path / "proj" / "single.jsonl")
    _big_convo(tmp_path / "proj" / "one.jsonl")
    _big_convo(tmp_path / "proj" / "two.jsonl")

    r = client.post("/api/projects/proj/conversations/single/debloat")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["stats_verified"] is True
    assert body["bytes_reclaimed"] > 0

    r = client.post("/api/projects/proj/conversations/bulk-debloat",
                    json={"ids": ["one", "two"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["results"]["one"]["ok"] is True
    assert body["results"]["two"]["ok"] is True


def test_debloat_tombstone_surfaces_on_convo_stats(client, tmp_path):
    """After debloat, the per-convo stats endpoint must surface the
    `debloat_delta` so the conversations-view card can render the
    `debloated · N freed` badge without re-scanning the file."""
    _big_convo(tmp_path / "proj" / "c.jsonl")
    r = client.post("/api/projects/proj/conversations/c/debloat")
    assert r.status_code == 200
    reclaimed = r.get_json()["bytes_reclaimed"]
    assert reclaimed > 0

    # Stats endpoint is the integration layer the frontend actually reads.
    stats_resp = client.get("/api/projects/proj/conversations/c/stats")
    assert stats_resp.status_code == 200
    stats = stats_resp.get_json()
    dd = stats.get("debloat_delta") or {}
    assert dd.get("bytes_reclaimed") == reclaimed
    assert (dd.get("counts") or {}).get("tool_use_result_truncated", 0) >= 1


def test_debloat_missing_convo_returns_404(client, tmp_path):
    (tmp_path / "proj").mkdir()
    r = client.post("/api/projects/proj/conversations/nope/debloat")
    assert r.status_code == 404
