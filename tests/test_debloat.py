"""Tests for the debloat feature.

Written BEFORE implementation — they will fail with ImportError until
`llm_lens/debloat.py` exists and the API routes are wired up.

Rules implemented (subset of the brtkwr.com article, agent_progress rule
deferred as future opt-in):

  1. Delete `normalizedMessages` wherever present (safe no-op if absent).
  2. Top-level `toolUseResult` >10KB → replaced with a compact marker
     dict that preserves byte count for reporting.
  3. Thinking blocks with `.thinking` >20KB → truncated to first 2000
     chars + marker suffix. Block type/structure preserved.
  4. Bash `toolUseResult.stdout` >1000 chars → truncated to first 1000
     chars + marker suffix. Only `stdout`; rest of `toolUseResult`
     (exitCode, stderr, interrupted, etc.) left alone.
  5. Inline `tool_result` blocks inside `message.content` with stringified
     length >10KB → content replaced with a "[truncated — was N bytes]"
     marker. `type` and `tool_use_id` on the block are preserved.

The point of the tests is the invariants — every tracked stat and every
resume-chain invariant must equal before/after. The rule-specific tests
just document what each rule does.
"""
import json
from pathlib import Path

import pytest

import llm_lens


# --------------------------------------------------------------------------
# Harness — mirrors tests/test_edits.py for consistency.
# --------------------------------------------------------------------------

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


def user_msg(uuid, parent, text="hi"):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "user",
        "sessionId": "S",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def assistant_msg(uuid, parent, blocks, in_tok=10, out_tok=5, model="claude-opus-4"):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "sessionId": "S",
        "message": {
            "role": "assistant",
            "model": model,
            "content": blocks,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
    }


def tool_use_block(tool_id, name="Bash", command="ls"):
    if name == "Bash":
        return {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": command}}
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def tool_result_user_msg(uuid, parent, tool_id, content="ok", tool_use_result=None):
    """User turn carrying a tool_result block. `content` may be a string
    (inline), a list, or a pre-built dict. `tool_use_result` goes in the
    top-level `toolUseResult` sibling field."""
    entry = {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "user",
        "sessionId": "S",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        },
    }
    if tool_use_result is not None:
        entry["toolUseResult"] = tool_use_result
    return entry


def stats_snapshot(path: Path) -> dict:
    """Pull every tracked stat via the project's own aggregator. If these
    numbers match pre/post debloat, we preserved what matters."""
    llm_lens._peek_jsonl_cached.cache_clear()
    if hasattr(llm_lens, "_stats_cached"):
        llm_lens._stats_cached.cache_clear()
    st = path.stat()
    return llm_lens._stats(path, st)


# Mirrors assert_resume_safe from test_edits.py — inlined so a refactor
# over there can't silently regress debloat coverage.
def assert_resume_safe(entries):
    uuids = {e["uuid"] for e in entries if e.get("uuid")}
    for e in entries:
        p = e.get("parentUuid")
        assert p is None or p in uuids, f"dangling parentUuid {p!r} on {e.get('uuid')}"
    tu_ids, tr_ids = set(), set()
    for e in entries:
        content = (e.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                tu_ids.add(b["id"])
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                tr_ids.add(b["tool_use_id"])
    assert tu_ids == tr_ids, f"orphan uses={tu_ids - tr_ids}, orphan results={tr_ids - tu_ids}"


# --------------------------------------------------------------------------
# Fixture builder — a "full-surface" convo that exercises every rule.
# --------------------------------------------------------------------------

BIG_STDOUT = "x" * 5000           # rule 4 hits (>1000)
BIG_TUR_BYTES = {"kind": "WebFetch", "body": "Z" * 12000}   # rule 2 hits (>10KB)
BIG_THINKING = "T" * 25000        # rule 3 hits (>20KB)
BIG_TOOL_RESULT_INLINE = "Q" * 12000  # rule 5 hits (>10KB)


def build_full_surface_convo(path: Path):
    entries = [
        # Prose user turn — debloat must not touch it.
        user_msg("u1", None, "hi, do stuff"),

        # Assistant with Bash tool_use + thinking block (big).
        assistant_msg("a1", "u1", [
            {"type": "thinking", "thinking": BIG_THINKING},
            tool_use_block("tool-A", name="Bash", command="sed -i s/foo/bar/g file.txt"),
        ]),

        # User turn: tool_result block (inline, big) + top-level toolUseResult
        # with a fat stdout. Exercises rules 2, 4, 5 simultaneously.
        tool_result_user_msg(
            "u2", "a1", "tool-A",
            content=BIG_TOOL_RESULT_INLINE,
            tool_use_result={
                "stdout": BIG_STDOUT,
                "stderr": "",
                "exitCode": 0,
                "interrupted": False,
            },
        ),

        # Assistant with a non-Bash tool_use; its toolUseResult is a big
        # structured value (not a Bash stdout).
        assistant_msg("a2", "u2", [
            tool_use_block("tool-B", name="WebFetch"),
        ]),
        tool_result_user_msg("u3", "a2", "tool-B", content="small", tool_use_result=BIG_TUR_BYTES),

        # A progress envelope carrying `normalizedMessages` nested inside
        # `data` — rule 1. This is a `hook_progress` envelope (safe to
        # carry the test dup-field; agent_progress deliberately excluded
        # from the fixture to match our current rule set).
        {
            "uuid": "p1",
            "parentUuid": "u3",
            "type": "progress",
            "sessionId": "S",
            "data": {
                "type": "hook_progress",
                "message": {"hook": "whatever"},
                "normalizedMessages": [{"dup": "of message"}],
            },
        },

        # Final prose assistant.
        assistant_msg("a3", "p1", [{"type": "text", "text": "done."}], in_tok=5, out_tok=3),
    ]
    write_jsonl(path, entries)
    return entries


# --------------------------------------------------------------------------
# INVARIANT TESTS — the core guarantee. If any of these fail, the feature
# is broken regardless of how much it reclaims.
# --------------------------------------------------------------------------

def test_invariant_stats_preserved(client, tmp_path):
    """Every stat the project aggregates must equal before/after."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)

    before = stats_snapshot(path)
    apply_debloat(path)
    after = stats_snapshot(path)

    # Exact equality on every stat key.
    for k in ("tool_uses", "thinking_count", "commands", "slash_commands",
              "per_model", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_creation_tokens"):
        assert after.get(k) == before.get(k), f"{k} changed: {before.get(k)} -> {after.get(k)}"


def test_invariant_line_count_preserved(client, tmp_path):
    """Matches the article's own safety check — rewrite preserves line
    count. Debloat truncates in place; it never adds or drops lines."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    before = sum(1 for _ in open(path))
    apply_debloat(path)
    after = sum(1 for _ in open(path))
    assert before == after


def test_invariant_resume_chain_preserved(client, tmp_path):
    """UUIDs, parentUuids, sessionId, and tool_use/tool_result pairing all
    survive. A debloated convo must still be resume-safe."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    before = build_full_surface_convo(path)

    apply_debloat(path)
    after = read_jsonl(path)

    # UUIDs identical in same order.
    assert [e["uuid"] for e in after] == [e["uuid"] for e in before]
    # parentUuid identical.
    assert [e.get("parentUuid") for e in after] == [e.get("parentUuid") for e in before]
    # sessionId identical.
    for a, b in zip(after, before):
        assert a.get("sessionId") == b.get("sessionId")

    assert_resume_safe(after)


def test_invariant_tool_use_blocks_untouched(client, tmp_path):
    """Tool name, id, and input (including Bash command like `sed …`) MUST
    be preserved verbatim. That is where `commands` and `tool_uses`
    counters are read from."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    before = build_full_surface_convo(path)
    apply_debloat(path)
    after = read_jsonl(path)

    def tool_uses(entries):
        out = []
        for e in entries:
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    out.append({"id": b["id"], "name": b["name"], "input": b.get("input")})
        return out

    assert tool_uses(after) == tool_uses(before)


def test_invariant_usage_fields_untouched(client, tmp_path):
    """`message.usage` is historical billing truth — never modified."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    before = build_full_surface_convo(path)
    apply_debloat(path)
    after = read_jsonl(path)

    def usages(entries):
        return [(e.get("uuid"), (e.get("message") or {}).get("usage"))
                for e in entries if (e.get("message") or {}).get("usage")]

    assert usages(after) == usages(before)


def test_invariant_all_lines_valid_json(client, tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    apply_debloat(path)

    with open(path) as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(f"line {i} is not valid JSON: {e}")


def test_invariant_idempotent(client, tmp_path):
    """Second debloat reclaims 0 bytes — rules are fixpoints."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    apply_debloat(path)
    size_after_first = path.stat().st_size
    apply_debloat(path)
    size_after_second = path.stat().st_size
    assert size_after_first == size_after_second


# --------------------------------------------------------------------------
# PER-RULE BEHAVIOR — proves each rule actually fires.
# --------------------------------------------------------------------------

def test_rule1_normalized_messages_deleted(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [
        {
            "uuid": "p1", "parentUuid": None, "type": "progress", "sessionId": "S",
            "data": {
                "type": "hook_progress",
                "message": {"foo": "bar"},
                "normalizedMessages": [{"dup": "x"}],
            },
        },
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]
    assert "normalizedMessages" not in e["data"]
    # Siblings under `data` preserved.
    assert e["data"]["type"] == "hook_progress"
    assert e["data"]["message"] == {"foo": "bar"}


def test_rule1_missing_normalized_is_noop(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    orig = [{"uuid": "x", "parentUuid": None, "type": "user",
             "message": {"role": "user", "content": "hi"}}]
    write_jsonl(path, orig)
    apply_debloat(path)
    assert read_jsonl(path) == orig


def test_rule2_big_tool_use_result_truncated_with_byte_marker(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    original_tur = {"kind": "WebFetch", "body": "Z" * 12000}
    original_bytes = len(json.dumps(original_tur))
    write_jsonl(path, [
        tool_result_user_msg("u1", None, "tool-X", tool_use_result=original_tur),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]

    tur = e["toolUseResult"]
    # Marker dict with byte count, not the original payload.
    assert isinstance(tur, dict)
    assert tur.get("debloated") is True
    assert tur.get("was_bytes") == original_bytes


def test_rule2_small_tool_use_result_not_touched(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    small = {"stdout": "ok", "exitCode": 0}
    write_jsonl(path, [
        tool_result_user_msg("u1", None, "tool-X", tool_use_result=small),
    ])
    apply_debloat(path)
    assert read_jsonl(path)[0]["toolUseResult"] == small


def test_rule3_thinking_truncated_keeps_first_2000(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big = "T" * 25000
    write_jsonl(path, [
        assistant_msg("a1", None, [{"type": "thinking", "thinking": big}]),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]

    block = e["message"]["content"][0]
    assert block["type"] == "thinking"   # structure preserved
    assert block["thinking"].startswith("T" * 2000)
    assert "truncated" in block["thinking"].lower()
    assert len(block["thinking"]) < 2500  # first 2000 + short marker


def test_rule3_small_thinking_not_touched(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    th = "ok thinking"
    write_jsonl(path, [
        assistant_msg("a1", None, [{"type": "thinking", "thinking": th}]),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]
    assert e["message"]["content"][0]["thinking"] == th


def test_rule4_bash_stdout_truncated_keeps_first_1000(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big = "x" * 5000
    tur = {"stdout": big, "stderr": "", "exitCode": 0, "interrupted": False}
    write_jsonl(path, [
        tool_result_user_msg("u1", None, "tool-B", tool_use_result=tur),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]

    # Rule 2 (whole-toolUseResult >10KB) does NOT fire here because this
    # small dict stringifies to <10KB. Rule 4 (stdout >1000) fires.
    new_tur = e["toolUseResult"]
    assert isinstance(new_tur, dict)
    assert new_tur["stdout"].startswith("x" * 1000)
    assert "truncated" in new_tur["stdout"].lower()
    # Sibling fields preserved.
    assert new_tur["exitCode"] == 0
    assert new_tur["interrupted"] is False


def test_rule5_inline_tool_result_truncated_preserves_type_and_id(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big = "Q" * 12000
    write_jsonl(path, [
        tool_result_user_msg("u1", None, "tool-A", content=big),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]

    block = e["message"]["content"][0]
    # Block type + linkage preserved — resume-chain integrity.
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tool-A"
    # Content replaced by a marker string.
    assert isinstance(block["content"], str)
    assert "truncated" in block["content"].lower()
    assert "12000" in block["content"] or "12" in block["content"]


def test_rule5_small_tool_result_not_touched(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [
        tool_result_user_msg("u1", None, "tool-A", content="small ok"),
    ])
    apply_debloat(path)
    e = read_jsonl(path)[0]
    assert e["message"]["content"][0]["content"] == "small ok"


# --------------------------------------------------------------------------
# SAFETY TESTS — edge cases that must NOT break.
# --------------------------------------------------------------------------

def test_agent_progress_envelope_not_truncated(tmp_path):
    """The article's `agent_progress` rule is deferred. Our debloat must
    leave `data.message` on agent_progress envelopes alone — they carry
    real subagent invocation content in this project's data shape."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big_payload = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "x" * 15000}]},
        "uuid": "inner",
    }
    entry = {
        "uuid": "p1", "parentUuid": None, "type": "progress", "sessionId": "S",
        "parentToolUseID": "tool-A",
        "data": {"type": "agent_progress", "message": big_payload},
    }
    write_jsonl(path, [entry])
    apply_debloat(path)
    e = read_jsonl(path)[0]
    # Full payload preserved verbatim.
    assert e["data"]["message"] == big_payload


def test_subagent_file_apply_does_not_destroy_messages(tmp_path):
    """Real subagent files live at <convo>/subagents/agent-*.jsonl with
    `type: progress` envelopes where data.message.message is a real
    user/assistant turn. Applying debloat on such a file must preserve
    every message's rendered content."""
    from llm_lens.debloat import apply_debloat

    sub_path = tmp_path / "proj" / "c1" / "subagents" / "agent-x-abc.jsonl"
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    big_assistant_text = "A" * 15000   # would trip naive agent_progress truncation
    entries = [
        {
            "uuid": "s1", "parentUuid": None, "type": "progress", "sessionId": "S",
            "parentToolUseID": "tool-A",
            "data": {
                "type": "agent_progress",
                "message": {
                    "type": "assistant",
                    "uuid": "inner-s1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": big_assistant_text}],
                        "usage": {"input_tokens": 50, "output_tokens": 20},
                    },
                },
            },
        },
    ]
    write_jsonl(sub_path, entries)
    apply_debloat(sub_path)

    after = read_jsonl(sub_path)
    inner = after[0]["data"]["message"]["message"]
    assert inner["content"][0]["text"] == big_assistant_text
    assert inner["usage"] == {"input_tokens": 50, "output_tokens": 20}


def test_empty_file_is_noop(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "empty.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    apply_debloat(path)
    assert path.read_text() == ""


def test_malformed_line_is_preserved_not_crashed(tmp_path):
    """A malformed JSONL line must not take down the whole rewrite.
    Matches test_edits.py's `test_malformed_jsonl_line_is_skipped_not_crashed`."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(user_msg("u1", None, "ok")) + "\n")
        f.write("{this is not valid json\n")
        f.write(json.dumps(user_msg("u2", "u1", "also ok")) + "\n")

    apply_debloat(path)
    with open(path) as f:
        lines = f.readlines()
    # Line count preserved; the malformed line passed through verbatim.
    assert len(lines) == 3
    assert "{this is not valid json" in lines[1]


# --------------------------------------------------------------------------
# SCAN (read-only estimate) — equals apply's actual reclamation.
# --------------------------------------------------------------------------

def test_scan_matches_actual_apply_reclamation(tmp_path):
    """The batch scan endpoint's `bytes_reclaimable` MUST equal the actual
    size delta from apply. No fuzzy estimate allowed."""
    from llm_lens.debloat import scan_convo, apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    size_before = path.stat().st_size

    predicted = scan_convo(path)["bytes_reclaimable"]
    apply_debloat(path)
    actual = size_before - path.stat().st_size

    assert predicted == actual, f"scan lied: predicted {predicted} reclaim, got {actual}"


def test_scan_is_read_only(tmp_path):
    from llm_lens.debloat import scan_convo

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    before_bytes = path.read_bytes()
    scan_convo(path)
    assert path.read_bytes() == before_bytes


# --------------------------------------------------------------------------
# API endpoints — batch scan + single apply + bulk apply.
# --------------------------------------------------------------------------

def test_api_debloat_scan_batch(client, tmp_path):
    p1 = tmp_path / "proj" / "a.jsonl"
    p2 = tmp_path / "proj" / "b.jsonl"
    build_full_surface_convo(p1)
    write_jsonl(p2, [user_msg("x", None, "tiny")])

    resp = client.post("/api/projects/proj/conversations/debloat-scan",
                       json={"ids": ["a", "b"]})
    assert resp.status_code == 200
    body = resp.get_json()

    assert set(body.keys()) == {"a", "b"}
    assert body["a"]["bytes_reclaimable"] > 0
    assert body["a"]["current_size"] == p1.stat().st_size
    # Tiny file: nothing to reclaim.
    assert body["b"]["bytes_reclaimable"] == 0


def test_api_debloat_apply_single(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    before = path.stat().st_size

    resp = client.post("/api/projects/proj/conversations/s/debloat")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["bytes_reclaimed"] == before - path.stat().st_size
    assert body["stats_verified"] is True


def test_api_debloat_apply_aborts_on_stats_mismatch(client, tmp_path, monkeypatch):
    """Invariant checker must abort + restore if the post-rewrite stats
    don't match pre. This is the safety net — tests that it works."""
    from llm_lens import debloat as debloat_mod

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    original = path.read_bytes()

    # Force the invariant check to fail by corrupting the verifier.
    def always_mismatch(*a, **kw):
        raise debloat_mod.StatsInvariantError("forced")
    monkeypatch.setattr(debloat_mod, "_verify_stats_equal", always_mismatch)

    resp = client.post("/api/projects/proj/conversations/s/debloat")
    assert resp.status_code == 500
    # File restored byte-for-byte.
    assert path.read_bytes() == original


def test_api_bulk_debloat(client, tmp_path):
    p1 = tmp_path / "proj" / "a.jsonl"
    p2 = tmp_path / "proj" / "b.jsonl"
    build_full_surface_convo(p1)
    build_full_surface_convo(p2)

    resp = client.post("/api/projects/proj/conversations/bulk-debloat",
                       json={"ids": ["a", "b"]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert set(body["results"].keys()) == {"a", "b"}
    for r in body["results"].values():
        assert r["ok"] is True
        assert r["bytes_reclaimed"] > 0


def test_api_debloat_nonexistent_returns_404(client, tmp_path):
    (tmp_path / "proj").mkdir()
    resp = client.post("/api/projects/proj/conversations/missing/debloat")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# TOMBSTONE — stats rollups after debloat still reflect original spend.
# --------------------------------------------------------------------------

def test_tombstone_records_reclaim_without_changing_rollups(client, tmp_path):
    """After debloat, the sidecar must carry a `debloat_delta` entry for
    bytes_reclaimed accounting, but the usage-derived stats (tokens,
    cost, counts) must still report their pre-debloat values — because
    usage itself is preserved, not tombstoned."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)
    before = stats_snapshot(path)
    apply_debloat(path)
    after = stats_snapshot(path)

    # Usage-derived values unchanged (same guarantee as the invariant
    # test, restated at this layer because rollups read from `_peek`).
    for k in ("input_tokens", "output_tokens", "tool_uses", "thinking_count"):
        assert after.get(k) == before.get(k)

    # Tombstone delta present and accurate.
    delta = after.get("debloat_delta") or {}
    assert delta.get("bytes_reclaimed", 0) > 0


# --------------------------------------------------------------------------
# STRIP_IMAGES — experimental opt-in rule. Safe for stats, uncertain for
# /resume (image content is part of what Claude Code replays to the API).
# --------------------------------------------------------------------------

def _image_convo(path):
    """User message carrying a big base64 image block."""
    big_b64 = "iVBOR" + ("A" * 20000)
    write_jsonl(path, [
        {"uuid": "u1", "parentUuid": None, "type": "user", "sessionId": "S",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "look at this"},
             {"type": "image", "source": {
                 "type": "base64", "media_type": "image/png", "data": big_b64,
             }},
         ]}},
    ])
    return big_b64


def test_strip_images_off_by_default_keeps_image_data(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big = _image_convo(path)
    apply_debloat(path)   # default: strip_images=False
    e = read_jsonl(path)[0]
    assert e["message"]["content"][1]["source"]["data"] == big


def test_strip_images_true_blanks_data_preserves_structure(tmp_path):
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    big = _image_convo(path)
    apply_debloat(path, strip_images=True)

    e = read_jsonl(path)[0]
    blocks = e["message"]["content"]
    # Block count + types preserved.
    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image"
    src = blocks[1]["source"]
    # Data blanked; media_type + type preserved; marker stamped.
    assert src["data"] == ""
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert src["__debloated__"] is True
    assert src["was_bytes"] == len(big)


def test_strip_images_preserves_stats(client, tmp_path):
    """Image blocks aren't counted for tool_uses/thinking/commands/usage,
    so stripping them must pass the invariant check."""
    from llm_lens.debloat import apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    _image_convo(path)
    before = stats_snapshot(path)
    apply_debloat(path, strip_images=True)
    after = stats_snapshot(path)
    for k in ("tool_uses", "thinking_count", "commands", "slash_commands",
              "input_tokens", "output_tokens", "per_model"):
        assert after.get(k) == before.get(k), f"{k} changed under strip_images"


def test_scan_returns_both_reclaim_numbers(tmp_path):
    from llm_lens.debloat import scan_convo

    path = tmp_path / "proj" / "s.jsonl"
    build_full_surface_convo(path)   # has toolUseResult/thinking/... but no image
    # Add an image-bearing line so with-images > without.
    _image_convo(tmp_path / "proj" / "img.jsonl")
    combined = tmp_path / "proj" / "combined.jsonl"
    with open(combined, "w") as out:
        with open(path) as a: out.write(a.read())
        with open(tmp_path / "proj" / "img.jsonl") as b: out.write(b.read())

    scan = scan_convo(combined)
    assert scan["bytes_reclaimable"] > 0
    assert scan["bytes_reclaimable_with_images"] > scan["bytes_reclaimable"]
    assert scan["counts"]["images_stripped"] == 0
    assert scan["counts_with_images"]["images_stripped"] == 1


def test_scan_matches_apply_under_strip_images(tmp_path):
    from llm_lens.debloat import scan_convo, apply_debloat

    path = tmp_path / "proj" / "s.jsonl"
    _image_convo(path)
    size_before = path.stat().st_size
    scan = scan_convo(path)
    apply_debloat(path, strip_images=True)
    actual = size_before - path.stat().st_size
    assert actual == scan["bytes_reclaimable_with_images"]


def test_api_debloat_apply_accepts_strip_images_flag(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    big = _image_convo(path)
    r = client.post("/api/projects/proj/conversations/s/debloat",
                    json={"strip_images": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["images_stripped"] is True
    assert body["counts"]["images_stripped"] == 1
    # Verify file actually stripped.
    e = read_jsonl(path)[0]
    assert e["message"]["content"][1]["source"]["data"] == ""


def test_api_bulk_debloat_accepts_strip_images_flag(client, tmp_path):
    p1 = tmp_path / "proj" / "a.jsonl"
    p2 = tmp_path / "proj" / "b.jsonl"
    _image_convo(p1)
    _image_convo(p2)
    r = client.post("/api/projects/proj/conversations/bulk-debloat",
                    json={"ids": ["a", "b"], "strip_images": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["images_stripped"] is True
    for rid in ("a", "b"):
        assert body["results"][rid]["ok"] is True
        assert body["results"][rid]["counts"]["images_stripped"] == 1
