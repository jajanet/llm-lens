"""Tests for the agent-setting peek + convo-header payload.

Covers the additions made when we started surfacing per-convo agent type
and last-turn context in the project list + convo view:

* `_peek_jsonl_cached` pulls `type=agent-setting` lines from the first 30
  jsonl records.
* `api_conversations` exposes `agent` only when present (keeps payload
  clean for the majority of convos with no explicit agent).
* `api_conversation` returns a `header` block with `agent` + ctx fields
  so the breadcrumb badges in `messages.js` have the data to render.
* Peek cache carries `agent` across calls (important — otherwise the
  short-circuit on warm cache would strip it).
"""
import json
from pathlib import Path

import pytest

import llm_lens
from llm_lens import peek_cache


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_smoke so cache state + temp dir are isolated)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "ARCHIVE_ROOT", tmp_path / "_archive")
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens._stats_cached.cache_clear()
    peek_cache._store.clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def _write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _user(uid, parent, text):
    return {
        "uuid": uid, "parentUuid": parent, "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": text}]},
    }


def _assistant(uid, parent, text, *, in_t=10, out_t=5,
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


# ---------------------------------------------------------------------------
# _peek_jsonl_cached
# ---------------------------------------------------------------------------

def test_peek_extracts_agent_setting(tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        {"type": "agent-setting", "agentSetting": "fixer",
         "sessionId": "c"},
        _user("u1", None, "hi"),
        _assistant("a1", "u1", "hello"),
    ])
    stat = path.stat()
    result = llm_lens._peek_jsonl_cached(
        str(path), stat.st_mtime, stat.st_size,
    )
    assert result["agent"] == "fixer"


def test_peek_no_agent_setting_returns_none(tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        _user("u1", None, "hi"),
        _assistant("a1", "u1", "hello"),
    ])
    stat = path.stat()
    result = llm_lens._peek_jsonl_cached(
        str(path), stat.st_mtime, stat.st_size,
    )
    assert result["agent"] is None


def test_peek_last_agent_setting_wins(tmp_path):
    """Multiple agent-setting lines (user switched mid-session): last one
    encountered within the 30-line peek window is what we report."""
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        {"type": "agent-setting", "agentSetting": "fixer", "sessionId": "c"},
        _user("u1", None, "hi"),
        {"type": "agent-setting", "agentSetting": "unit-test-writer",
         "sessionId": "c"},
        _assistant("a1", "u1", "hello"),
    ])
    stat = path.stat()
    result = llm_lens._peek_jsonl_cached(
        str(path), stat.st_mtime, stat.st_size,
    )
    assert result["agent"] == "unit-test-writer"


def test_peek_ignores_agent_setting_beyond_window(tmp_path):
    """If agent-setting appears past line 30 it's outside the peek window
    — we prefer to keep the peek cheap rather than scan the whole file."""
    path = tmp_path / "proj" / "c.jsonl"
    entries = [_user(f"u{i}", None, f"msg {i}") for i in range(35)]
    entries.append(
        {"type": "agent-setting", "agentSetting": "late-agent",
         "sessionId": "c"}
    )
    _write_jsonl(path, entries)
    stat = path.stat()
    result = llm_lens._peek_jsonl_cached(
        str(path), stat.st_mtime, stat.st_size,
    )
    assert result["agent"] is None


# ---------------------------------------------------------------------------
# Peek cache carries `agent`
# ---------------------------------------------------------------------------

def test_peek_cache_carries_agent_across_calls(tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        {"type": "agent-setting", "agentSetting": "fixer", "sessionId": "c"},
        _user("u1", None, "hi"),
    ])
    stat = path.stat()

    # Cold call populates the cache; warm call must still return the
    # agent field (regression guard: the short-circuit in _peek used to
    # only check for "preview" and would strip agent).
    cold = llm_lens._peek(path, stat)
    warm = llm_lens._peek(path, stat)
    assert cold["agent"] == "fixer"
    assert warm["agent"] == "fixer"


# ---------------------------------------------------------------------------
# api_conversations (list endpoint)
# ---------------------------------------------------------------------------

def test_conversations_list_includes_agent_when_present(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        {"type": "agent-setting", "agentSetting": "fixer", "sessionId": "c"},
        _user("u1", None, "hi"),
    ])
    resp = client.get("/api/projects/proj/conversations").get_json()
    items = resp["items"]
    assert len(items) == 1
    assert items[0]["agent"] == "fixer"


def test_conversations_list_omits_agent_when_absent(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        _user("u1", None, "hi"),
    ])
    resp = client.get("/api/projects/proj/conversations").get_json()
    items = resp["items"]
    assert len(items) == 1
    # Intentionally omitted (not `"agent": null`) — keeps payload tidy
    # since most convos have no explicit agent.
    assert "agent" not in items[0]


# ---------------------------------------------------------------------------
# api_conversation (single-convo endpoint) — header block
# ---------------------------------------------------------------------------

def test_conversation_header_has_agent_and_ctx(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        {"type": "agent-setting", "agentSetting": "fixer", "sessionId": "c"},
        _user("u1", None, "hi"),
        _assistant("a1", "u1", "hello",
                   in_t=100, cache_read=200, cache_creation=50,
                   model="claude-sonnet-4"),
    ])
    resp = client.get("/api/projects/proj/conversations/c").get_json()
    header = resp["header"]
    assert header["agent"] == "fixer"
    # Stats keys populated from the final assistant turn.
    assert header["last_context_input_tokens"] == 100
    assert header["last_context_cache_read_tokens"] == 200
    assert header["last_context_cache_creation_tokens"] == 50
    assert header["last_model_for_context"] == "claude-sonnet-4"


def test_conversation_header_agent_none_when_absent(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _write_jsonl(path, [
        _user("u1", None, "hi"),
        _assistant("a1", "u1", "hello"),
    ])
    resp = client.get("/api/projects/proj/conversations/c").get_json()
    header = resp["header"]
    # Here we *do* want the key present but null, so the frontend can
    # uniformly read `header.agent` without guarding undefined.
    assert "agent" in header
    assert header["agent"] is None
