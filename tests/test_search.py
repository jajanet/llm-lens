"""Search endpoints + `_search_convo_cached` behavior.

Covers:
  - /api/projects/<folder>/search: per-convo counts + top-match snippet
  - /api/projects/<folder>/conversations/<convo_id>/search: full match list
  - Empty query: both endpoints return empty container
  - Archive inclusion: archived convos searched alongside live
  - Cache invalidation on edit/delete bust stale results
  - Case-insensitive matching
  - Snippet ellipsis edges for long content
"""
import json
from pathlib import Path

import pytest

import llm_lens


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(llm_lens, "ARCHIVE_ROOT", tmp_path / "_archive")
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens._stats_cached.cache_clear()
    llm_lens._search_convo_cached.cache_clear()
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


def assistant_msg(uid, parent, text):
    return {
        "uuid": uid, "parentUuid": parent, "type": "assistant",
        "message": {
            "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Project search
# ─────────────────────────────────────────────────────────────────────

def test_project_search_empty_query_returns_empty(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "hello world"),
    ])
    r = client.get("/api/projects/proj/search?q=")
    assert r.status_code == 200
    assert r.get_json() == {}


def test_project_search_returns_count_and_top_match(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "first mention of needle here"),
        assistant_msg("a1", "u1", "response without match"),
        user_msg("u2", "a1", "another needle occurrence"),
    ])
    r = client.get("/api/projects/proj/search?q=needle")
    assert r.status_code == 200
    data = r.get_json()
    assert "c1" in data
    assert data["c1"]["count"] == 2
    top = data["c1"]["top"]
    assert "needle" in top["snippet"].lower()
    assert top["uuid"] == "u1"
    assert top["index"] == 0
    # matches array holds up to PROJECT_SEARCH_MATCHES_PER_CONVO entries;
    # for small convos it carries every hit so the UI can reveal them
    # progressively without a second request.
    assert data["c1"]["matches"][0] == top
    assert [m["uuid"] for m in data["c1"]["matches"]] == ["u1", "u2"]


def test_project_search_omits_convos_with_no_match(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "hit.jsonl", [
        user_msg("u1", None, "contains the token xyzzy"),
    ])
    write_jsonl(tmp_path / "proj" / "miss.jsonl", [
        user_msg("u1", None, "nothing to see"),
    ])
    data = client.get("/api/projects/proj/search?q=xyzzy").get_json()
    assert "hit" in data
    assert "miss" not in data


def test_project_search_is_case_insensitive(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "ALL CAPS NEEDLE"),
    ])
    data = client.get("/api/projects/proj/search?q=needle").get_json()
    assert data["c1"]["count"] == 1


def test_project_search_includes_archived_convos(client, tmp_path):
    # Live convo with no match, archived convo with match.
    write_jsonl(tmp_path / "proj" / "live.jsonl", [
        user_msg("u1", None, "nothing here"),
    ])
    arch_dir = tmp_path / "_archive" / "proj"
    write_jsonl(arch_dir / "old.jsonl", [
        user_msg("u1", None, "needle in the archive"),
    ])
    data = client.get("/api/projects/proj/search?q=needle").get_json()
    assert "old" in data
    assert "live" not in data


def test_project_search_missing_folder_returns_empty(client, tmp_path):
    r = client.get("/api/projects/nonexistent/search?q=needle")
    assert r.status_code == 200
    assert r.get_json() == {}


def test_project_search_caps_matches_but_keeps_true_count(client, tmp_path):
    """Payload caps at PROJECT_SEARCH_MATCHES_PER_CONVO snippets, but
    `count` still reports the real total so the "+N more" label stays
    truthful once the UI has revealed everything the backend sent.
    """
    cap = llm_lens.PROJECT_SEARCH_MATCHES_PER_CONVO
    over = cap + 7  # deliberately blow past the cap
    entries = [user_msg(f"u{i}", None, f"match #{i} needle tag")
               for i in range(over)]
    write_jsonl(tmp_path / "proj" / "big.jsonl", entries)
    data = client.get("/api/projects/proj/search?q=needle").get_json()
    assert data["big"]["count"] == over
    assert len(data["big"]["matches"]) == cap
    # Order is preserved — matches[0] is the earliest hit, equal to `top`.
    assert data["big"]["matches"][0] == data["big"]["top"]


# ─────────────────────────────────────────────────────────────────────
# Per-convo search
# ─────────────────────────────────────────────────────────────────────

def test_convo_search_returns_every_match_with_index(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "first needle"),
        assistant_msg("a1", "u1", "no match"),
        user_msg("u2", "a1", "second needle"),
        user_msg("u3", "u2", "third needle occurrence"),
    ])
    r = client.get("/api/projects/proj/conversations/c1/search?q=needle")
    assert r.status_code == 200
    matches = r.get_json()
    assert len(matches) == 3
    # Ordered by message index.
    assert [m["uuid"] for m in matches] == ["u1", "u2", "u3"]
    assert [m["index"] for m in matches] == [0, 2, 3]
    # Role exposed so the frontend can style user vs assistant hits.
    assert matches[0]["role"] == "user"
    assert matches[1]["role"] == "user"  # user msg u2


def test_convo_search_empty_query_returns_empty_list(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "hello"),
    ])
    r = client.get("/api/projects/proj/conversations/c1/search?q=")
    assert r.get_json() == []


def test_convo_search_missing_convo_returns_empty(client, tmp_path):
    r = client.get("/api/projects/proj/conversations/does-not-exist/search?q=x")
    assert r.status_code == 200
    assert r.get_json() == []


def test_convo_search_finds_archived(client, tmp_path):
    arch_dir = tmp_path / "_archive" / "proj"
    write_jsonl(arch_dir / "c1.jsonl", [
        user_msg("u1", None, "archived needle"),
    ])
    matches = client.get(
        "/api/projects/proj/conversations/c1/search?q=needle"
    ).get_json()
    assert len(matches) == 1


# ─────────────────────────────────────────────────────────────────────
# Snippet shape
# ─────────────────────────────────────────────────────────────────────

def test_snippet_adds_ellipsis_when_truncated_on_both_sides(client, tmp_path):
    long_prefix = "filler " * 30
    long_suffix = " trailing" * 30
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, long_prefix + "TARGET" + long_suffix),
    ])
    data = client.get("/api/projects/proj/search?q=TARGET").get_json()
    snippet = data["c1"]["top"]["snippet"]
    assert snippet.startswith("…")
    assert snippet.endswith("…")
    assert "TARGET" in snippet


def test_snippet_no_leading_ellipsis_when_match_is_at_start(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [
        user_msg("u1", None, "needle at the very start of this short msg"),
    ])
    data = client.get("/api/projects/proj/search?q=needle").get_json()
    assert not data["c1"]["top"]["snippet"].startswith("…")


# ─────────────────────────────────────────────────────────────────────
# Cache invalidation
# ─────────────────────────────────────────────────────────────────────

def test_search_cache_invalidated_on_message_delete(client, tmp_path):
    fp = tmp_path / "proj" / "c1.jsonl"
    write_jsonl(fp, [
        user_msg("u1", None, "needle before"),
        user_msg("u2", "u1", "after"),
    ])
    # Prime the cache.
    data = client.get("/api/projects/proj/search?q=needle").get_json()
    assert data["c1"]["count"] == 1

    # Delete the matching message via the real endpoint so invalidation
    # runs through the production path, not just a manual cache_clear.
    r = client.delete("/api/projects/proj/conversations/c1/messages/u1")
    assert r.status_code in (200, 204)

    data2 = client.get("/api/projects/proj/search?q=needle").get_json()
    # After the delete, no convo matches anymore.
    assert data2 == {} or "c1" not in data2


def test_search_cache_invalidated_on_conversation_delete(client, tmp_path):
    fp = tmp_path / "proj" / "c1.jsonl"
    write_jsonl(fp, [user_msg("u1", None, "needle")])
    assert client.get("/api/projects/proj/search?q=needle").get_json().get("c1")
    client.delete("/api/projects/proj/conversations/c1")
    assert client.get("/api/projects/proj/search?q=needle").get_json() == {}


def test_search_invalidate_fn_clears_search_cache(client, tmp_path):
    """Unit-level: _invalidate_cache_for must drop search entries too —
    otherwise any mutation path that goes through it (edit/delete/debloat)
    would leak stale search results until the LRU evicts."""
    fp = tmp_path / "proj" / "c1.jsonl"
    write_jsonl(fp, [user_msg("u1", None, "needle")])
    # Prime.
    client.get("/api/projects/proj/search?q=needle")
    assert llm_lens._search_convo_cached.cache_info().currsize > 0
    llm_lens._invalidate_cache_for(fp)
    assert llm_lens._search_convo_cached.cache_info().currsize == 0
