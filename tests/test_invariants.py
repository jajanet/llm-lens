"""Invariant tests for the parser and conversation API.

Every test goes through the Flask test client so _parse_messages_cached,
the API response shape, and cache invalidation are exercised end-to-end.

Covered invariants:
  1. Parser count = N - k - m (snapshots + isMeta filtered).
  2. First + last non-filtered entries are at main[0] / main[-1].
  3. Every non-filtered uuid appears exactly once in the response.
  4. Pagination: offset + len(page) <= total; pages concatenate with no
     gaps or overlaps.
  5. Cache invalidation: GET reflects mutations made through the API.

Fixtures deliberately use distinct content per message. _parse_messages_cached
runs a dedup() pass keyed on content strings — repeated text collapses silently.
"""
import json
from pathlib import Path

import pytest

import llm_lens


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    # Isolate the user-curated word lists per test so writes don't leak into
    # the real ~/.cache/llm-lens/word_lists.json or across tests.
    monkeypatch.setattr(
        llm_lens, "_word_lists_path",
        lambda: tmp_path / "word_lists.json",
    )
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def msg(uuid, parent, text, *, role="user", type_="user", is_meta=False):
    """Distinct content per call — dedup() keys on content strings."""
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": type_,
        "isMeta": is_meta,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def snapshot(uuid):
    return {"type": "file-history-snapshot", "uuid": uuid}


# ---------------------------------------------------------------------------
# 1. Parser preserves every real message
# ---------------------------------------------------------------------------

def test_parser_count_excludes_snapshots_and_meta(client, tmp_path):
    """N=5, k=1 (snapshot), m=1 (isMeta) → main = 3.

    NOTE: the parser runs a dedup pass keyed on content (__init__.py:120),
    so the invariant N-k-m only holds when every real message has distinct
    text. All fixtures in this section use unique content.
    """
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "hello"),
        msg("b", "a", "world"),
        snapshot("s1"),                             # k = 1
        msg("c", "b", "side", is_meta=True),        # m = 1
        msg("d", "c", "done"),
    ])

    resp = client.get("/api/projects/proj/conversations/c?limit=100")
    assert resp.status_code == 200
    data = resp.get_json()

    assert len(data["main"]) == 3
    assert data["total"] == 3
    main_uuids = [m["uuid"] for m in data["main"]]
    assert "s1" not in main_uuids
    assert "c" not in main_uuids
    assert data["agent_runs"] == []


def test_parser_count_multiple_snapshots_and_meta(client, tmp_path):
    """N=8, k=3 snapshots, m=2 isMeta → main == 3."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        snapshot("s0"),
        msg("a", None, "real-1"),
        snapshot("s1"),
        msg("b", "a", "meta-1", is_meta=True),
        msg("c", "b", "real-2"),
        snapshot("s2"),
        msg("d", "c", "meta-2", is_meta=True),
        msg("e", "d", "real-3"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert len(data["main"]) == 3          # 8 - 3 - 2          # 8 - 3 - 2


def test_parser_drops_inline_sidechain_from_main(client, tmp_path):
    """Inline `isSidechain: true` entries are kept out of `main` (they
    surface via `agent_runs` / the agent endpoint instead, covered by the
    inline-agent-run tests further down)."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        snapshot("s0"),                                              # k=1
        msg("b", "a", "main-2"),
        {**msg("sc", "b", "side-only"), "isSidechain": True},        # routed to agent_runs
        msg("mx", "b", "meta", is_meta=True),                        # m=1
        msg("c", "b", "main-3"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()

    main_uuids = [m["uuid"] for m in data["main"]]
    assert main_uuids == ["a", "b", "c"]
    assert "sc" not in main_uuids
    assert data["total"] == 3


# ---------------------------------------------------------------------------
# 2. First + last message present
# ---------------------------------------------------------------------------

def test_parser_preserves_first_and_last_order(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    entries = [
        msg(str(i), str(i - 1) if i > 0 else None, f"msg-{i}")
        for i in range(5)
    ]
    write_jsonl(path, entries)

    # limit >= total to bypass the offset=0,limit<total jump-to-end branch
    resp = client.get("/api/projects/proj/conversations/c?limit=100")
    data = resp.get_json()

    assert data["main"][0]["uuid"] == "0"
    assert data["main"][-1]["uuid"] == "4"


def test_parser_first_last_with_branch(client, tmp_path):
    """Forked graph: A has two children B and C. The parser is order-
    preserving (it does not resolve branches), so main[0] is still the
    first JSONL entry and main[-1] is the last. This test documents that
    policy — if the parser ever starts picking a single path, this will
    fail loudly."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "root"),
        msg("b", "a", "branch-B"),
        msg("c", "a", "branch-C"),   # sibling of b, same parent
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]
    assert uuids[0] == "a"
    assert uuids[-1] == "c"          # last in file order wins
    assert set(uuids) == {"a", "b", "c"}  # both branches preserved


def test_parser_first_last_with_filtered_entries_interleaved(client, tmp_path):
    """A snapshot before the first real msg and meta after the last must
    not shift main[0] / main[-1]."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        snapshot("s0"),
        msg("first", None, "first text"),
        msg("middle", "first", "middle text"),
        snapshot("s1"),
        msg("last", "middle", "last text"),
        msg("m_end", "last", "meta-end", is_meta=True),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert data["main"][0]["uuid"] == "first"
    assert data["main"][-1]["uuid"] == "last"


# ---------------------------------------------------------------------------
# 3. UUID round-trip
# ---------------------------------------------------------------------------

def test_parser_uuid_roundtrip(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "first"),
        msg("b", "a", "second"),
        snapshot("snap1"),
        msg("c", "b", "third", is_meta=True),
        msg("d", "c", "fourth"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    returned = [m["uuid"] for m in data["main"]]

    # complete set check — catches both missing and unexpected UUIDs
    assert set(returned) == {"a", "b", "d"}
    assert len(returned) == 3      # length + set equality ⇒ no duplicates
    assert "snap1" not in returned
    assert "c" not in returned
    # role must survive the round-trip
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert by_uuid["a"]["role"] == "user"
    assert by_uuid["b"]["role"] == "user"
    assert by_uuid["d"]["role"] == "user"


def test_parser_uuid_roundtrip_excludes_inline_sidechain(client, tmp_path):
    """Every non-filtered uuid appears exactly once in `main`. Inline
    `isSidechain: true` entries are dropped outright (not routed elsewhere)
    — the replacement story for agent runs is the separate subagents/ files
    tested below."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "first"),
        msg("b", "a", "second"),
        snapshot("snap1"),
        msg("c", "b", "meta", is_meta=True),
        {**msg("d", "b", "side-only"), "isSidechain": True},
        msg("e", "b", "fourth"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    main_uuids = [m["uuid"] for m in data["main"]]

    # snap1 (snapshot), c (isMeta), d (isSidechain) are all filtered out.
    assert set(main_uuids) == {"a", "b", "e"}
    assert len(main_uuids) == len(set(main_uuids)), "dup uuid in main"
    assert "snap1" not in main_uuids
    assert "c" not in main_uuids
    assert "d" not in main_uuids


# ---------------------------------------------------------------------------
# 4. Pagination math
# ---------------------------------------------------------------------------

def _linear_convo(path: Path, n: int):
    entries = [
        msg(str(i), str(i - 1) if i > 0 else None, f"unique text {i}")
        for i in range(n)
    ]
    write_jsonl(path, entries)


def test_pagination_offset_plus_len_le_total(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    _linear_convo(path, 10)

    for offset, limit in [(0, 3), (0, 10), (0, 100), (2, 3), (5, 10), (9, 5)]:
        data = client.get(
            f"/api/projects/proj/conversations/c?offset={offset}&limit={limit}"
        ).get_json()
        # use the response's own offset — the endpoint rewrites offset=0,
        # limit<total to jump-to-end (see __init__.py:253).
        assert data["offset"] + len(data["main"]) <= data["total"], (
            f"in={offset},{limit} -> out_offset={data['offset']} "
            f"len={len(data['main'])} total={data['total']}"
        )
        # non-zero offsets must be preserved verbatim — the server-side
        # rewrite is restricted to offset==0, limit<total.
        if offset > 0:
            assert data["offset"] == offset, (
                f"server rewrote non-zero offset {offset} -> {data['offset']}"
            )

    # out-of-bounds offset: page is empty and invariant still holds
    oob = client.get(
        "/api/projects/proj/conversations/c?offset=10&limit=5"
    ).get_json()
    assert oob["main"] == []
    assert oob["offset"] + len(oob["main"]) <= oob["total"]


def test_pagination_respects_filtered_count(client, tmp_path):
    """total and offset must be computed on the post-filter count — if a
    snapshot or isMeta entry leaks into total, the offset math is wrong
    even when the page looks right on a clean convo."""
    path = tmp_path / "proj" / "c.jsonl"
    # N=7, k=2 snapshots, m=1 isMeta → 4 real main messages
    write_jsonl(path, [
        snapshot("s0"),
        msg("0", None, "unique 0"),
        snapshot("s1"),
        msg("1", "0", "meta one", is_meta=True),
        msg("2", "1", "unique 2"),
        msg("3", "2", "unique 3"),
        msg("4", "3", "unique 4"),
    ])

    data = client.get(
        "/api/projects/proj/conversations/c?offset=2&limit=2"
    ).get_json()
    assert data["total"] == 4
    assert data["offset"] == 2
    assert data["offset"] + len(data["main"]) <= data["total"]
    assert len(data["main"]) == 2


def test_pagination_single_shot_no_gaps_or_overlaps(client, tmp_path):
    """Single-shot fetch with limit>=total: the only form of pagination
    that works on this API (see the walk test below for why)."""
    path = tmp_path / "proj" / "c.jsonl"
    N = 10
    _linear_convo(path, N)

    bootstrap = client.get("/api/projects/proj/conversations/c?offset=0&limit=9999")
    total = bootstrap.get_json()["total"]
    assert total == N

    data = client.get(
        f"/api/projects/proj/conversations/c?offset=0&limit={total}"
    ).get_json()
    all_msgs = data["main"]

    assert len(all_msgs) == total
    assert data["offset"] + len(all_msgs) <= total
    uuids = [m["uuid"] for m in all_msgs]
    assert len(uuids) == len(set(uuids))
    assert uuids[0] == "0"
    assert uuids[-1] == "9"


def test_pagination_walk_pages_concat_equals_full_list(client, tmp_path):
    """True invariant: walking offsets [0, L, 2L, ...] with chunks of size L
    concatenates to the full list with no gaps or overlaps.

    This test *should* pass on a correctly-paginating API. It fails today
    because the offset=0 jump-to-end branch returns the tail instead of
    the head, so the first page is [7,8,9] and the second page at offset=L
    overlaps it. xfail(strict=True) means this turns into a failure the
    moment the bug is fixed — at which point delete the xfail marker.
    """
    path = tmp_path / "proj" / "c.jsonl"
    N = 10
    _linear_convo(path, N)

    total = client.get(
        f"/api/projects/proj/conversations/c?offset=0&limit=9999"
    ).get_json()["total"]
    assert total == N

    L = 3
    collected = []
    cursor = 0
    while cursor < total:
        page = client.get(
            f"/api/projects/proj/conversations/c?offset={cursor}&limit={L}"
        ).get_json()["main"]
        if not page:
            break
        for m in page:
            collected.append(m["uuid"])
        cursor += len(page)

    expected = [str(i) for i in range(N)]
    assert collected == expected                          # order + completeness
    assert len(collected) == len(set(collected))          # no overlaps
    assert len(collected) == total                        # no gaps


def test_pagination_nonzero_offset_walk_concatenates_correctly(client, tmp_path):
    """Passing multi-page concat test that avoids the offset=0 jump-to-end
    branch. Bootstraps main[0] via a large-limit fetch, then walks forward
    from offset=1 in chunks of L. Verifies no gaps or overlaps across the
    whole range."""
    path = tmp_path / "proj" / "c.jsonl"
    N = 10
    _linear_convo(path, N)

    full = client.get(
        f"/api/projects/proj/conversations/c?offset=0&limit={N}"
    ).get_json()
    expected = [m["uuid"] for m in full["main"]]
    assert len(expected) == N

    L = 3
    collected = [expected[0]]                     # stitch in main[0]
    cursor = 1
    while cursor < N:
        data = client.get(
            f"/api/projects/proj/conversations/c?offset={cursor}&limit={L}"
        ).get_json()
        # server must honor non-zero offsets verbatim
        assert data["offset"] == cursor
        page = data["main"]
        if not page:
            break
        for m in page:
            collected.append(m["uuid"])
        cursor += len(page)

    assert collected == expected                  # order + completeness
    assert len(collected) == len(set(collected))  # no overlaps
    assert len(collected) == N                    # no gaps


# ---------------------------------------------------------------------------
# 5. Cache invalidation
# ---------------------------------------------------------------------------

def test_cache_invalidated_after_delete_message(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "keep"),
        msg("b", "a", "delete me"),
        msg("c", "b", "also keep"),
    ])

    r1 = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert r1["total"] == 3

    resp = client.delete("/api/projects/proj/conversations/c/messages/b")
    assert resp.status_code == 200

    r2 = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert r2["total"] == 2
    uuids = [m["uuid"] for m in r2["main"]]
    assert uuids == ["a", "c"]

    # double-GET: catches stale re-caching where GET₁ on cache miss reads
    # fresh data but accidentally re-populates a stale entry for GET₂.
    r3 = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert r3["total"] == 2
    assert [m["uuid"] for m in r3["main"]] == uuids


def test_cache_invalidated_after_extract(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "one"),
        msg("b", "a", "two"),
        msg("c", "b", "three"),
    ])

    r1 = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert r1["total"] == 3

    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": ["a", "b"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    r2 = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    assert r2["total"] == 2
    assert [m["uuid"] for m in r2["main"]] == ["a", "b"]

    # source must be UNCHANGED — extract is non-destructive
    r_src = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert r_src["total"] == 3
    assert [m["uuid"] for m in r_src["main"]] == ["a", "b", "c"]

    # double-GET on the new conversation — catches stale re-caching.
    r2b = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    assert r2b["total"] == 2
    assert [m["uuid"] for m in r2b["main"]] == ["a", "b"]


def test_cache_invalidated_after_duplicate(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "alpha"),
        msg("b", "a", "bravo"),
    ])

    client.get("/api/projects")  # prime peek cache
    client.get("/api/projects/proj/conversations/c?limit=100")  # prime parse cache

    resp = client.post("/api/projects/proj/conversations/c/duplicate")
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    dup = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    assert dup["total"] == 2
    dup_uuids = [m["uuid"] for m in dup["main"]]
    # IDs must be rewritten so Claude Code /resume doesn't collide with parent.
    assert dup_uuids != ["a", "b"]
    assert len(set(dup_uuids)) == 2

    # source must still exist with its original IDs — it's a copy, not a move
    original = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    assert original["total"] == 2
    assert [m["uuid"] for m in original["main"]] == ["a", "b"]

    # Sidecar exists and points at parent.
    import json as _json
    sidecar = tmp_path / "proj" / f"{new_id}.dup.json"
    assert sidecar.exists()
    meta = _json.loads(sidecar.read_text())
    assert meta["duplicate_of"] == "c"

    # double-GET on the duplicate — catches stale re-caching where GET₁
    # reads fresh data on cache miss but re-populates a stale entry.
    dup2 = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    assert dup2["total"] == 2
    assert [m["uuid"] for m in dup2["main"]] == dup_uuids


def _assistant(uuid, parent, text, in_t, out_t):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": in_t, "output_tokens": out_t,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }


def test_duplicate_subtracts_shared_prefix_while_parent_exists(client, tmp_path):
    path = tmp_path / "proj" / "p.jsonl"
    write_jsonl(path, [
        msg("u1", None, "hi"),
        _assistant("a1", "u1", "reply-one", 10, 5),
        _assistant("a2", "a1", "reply-two", 20, 7),
    ])

    parent = client.get("/api/projects/proj/conversations/p/stats").get_json()
    assert parent["input_tokens"] == 30
    assert parent["output_tokens"] == 12

    resp = client.post("/api/projects/proj/conversations/p/duplicate")
    new_id = resp.get_json()["new_id"]

    # Parent still exists -> shared prefix subtracted from dup's stats so
    # project-level totals don't double-count.
    dup = client.get(
        f"/api/projects/proj/conversations/{new_id}/stats"
    ).get_json()
    assert dup["input_tokens"] == 0
    assert dup["output_tokens"] == 0
    assert dup["duplicate_of"] == "p"

    # Delete parent -> no parent in aggregate anymore, dup must show full stats.
    client.delete("/api/projects/proj/conversations/p")
    dup_after = client.get(
        f"/api/projects/proj/conversations/{new_id}/stats"
    ).get_json()
    assert dup_after["input_tokens"] == 30
    assert dup_after["output_tokens"] == 12


def test_word_lists_get_returns_defaults(client, tmp_path, monkeypatch):
    # Force user-lists file to not exist by pointing HOME at a fresh tmp dir.
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "no_such_dir" / "word_lists.json",
    )
    resp = client.get("/api/word-lists").get_json()
    assert "fuck*" in resp["swears"]
    assert any("absolutely right" in p.lower() for p in resp["filler"])


def test_word_lists_post_persists_user_overrides(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={"swears": ["heck"], "filler": ["my custom phrase"]},
    ).get_json()
    assert saved["swears"] == ["heck"]
    assert saved["filler"] == ["my custom phrase"]
    # Reload via GET — confirms persistence and that defaults are NOT
    # re-merged on top of a user-provided list (user controls the full set).
    again = client.get("/api/word-lists").get_json()
    assert again["swears"] == ["heck"]
    assert "fuck*" not in again["swears"]



def test_word_lists_defaults_includes_custom_filter_and_whitelist(client):
    resp = client.get("/api/word-lists/defaults").get_json()
    assert resp["custom_filter"] == []
    assert "benchmark" in resp["whitelist"]
    assert "claude" in resp["whitelist"]


def test_word_lists_round_trip_custom_filter_and_whitelist(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={
            "swears": ["heck"],
            "filler": ["some phrase"],
            "verbosity": [],
            "custom_filter": ["repeated boilerplate here"],
            "whitelist": ["keepme"],
        },
    ).get_json()
    assert saved["custom_filter"] == ["repeated boilerplate here"]
    assert saved["whitelist"] == ["keepme"]
    # Reload via GET — user-provided whitelist fully replaces the default seed.
    again = client.get("/api/word-lists").get_json()
    assert again["whitelist"] == ["keepme"]
    assert again["custom_filter"] == ["repeated boilerplate here"]


def test_custom_filter_scan_returns_repeated_phrases_above_thresholds(client, tmp_path):
    # Plant the same 3-word phrase three times across messages in one
    # conversation; the scan should surface it (occurrence >= 3,
    # length >= 6, n=1..3).
    folder = tmp_path / "proj"
    write_jsonl(folder / "convo.jsonl", [
        msg("1", None, "hello let me check the status"),
        msg("2", None, "ok let me check again"),
        msg("3", None, "please let me check today"),
    ])
    resp = client.post(
        "/api/projects/proj/conversations/convo/custom-filter/scan",
        json={"min_length_chars": 6, "min_count": 3, "n_min": 1, "n_max": 3},
    ).get_json()
    assert resp["msg_count"] == 3
    assert any("let me check" in c for c in resp["candidates"])


def test_custom_filter_scan_ngram_lowercases_for_counting(client, tmp_path):
    folder = tmp_path / "proj"
    # Mixed case within one convo — should count as the same phrase.
    write_jsonl(folder / "convo.jsonl", [
        msg("1", None, "Let Me Check the LOGS here"),
        msg("2", None, "LET ME check THE logs now"),
        msg("3", None, "let me check the logs again"),
    ])
    resp = client.post(
        "/api/projects/proj/conversations/convo/custom-filter/scan",
        json={"min_length_chars": 8, "min_count": 3, "n_min": 3, "n_max": 5},
    ).get_json()
    assert any("let me check" in c for c in resp["candidates"])


def test_custom_filter_scan_excludes_whitelist_containment(client, tmp_path, monkeypatch):
    # User whitelists "benchmark" → any candidate phrase containing it is
    # excluded, even though it repeats enough times to qualify.
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    client.post(
        "/api/word-lists",
        json={"whitelist": ["benchmark"]},
    )
    folder = tmp_path / "proj"
    write_jsonl(folder / "convo.jsonl", [
        msg(f"u{i}", None, "run the benchmark suite now please")
        for i in range(3)
    ])
    resp = client.post(
        "/api/projects/proj/conversations/convo/custom-filter/scan",
        json={"min_length_chars": 6, "min_count": 3, "n_min": 1, "n_max": 3},
    ).get_json()
    assert not any("benchmark" in c for c in resp["candidates"])


def test_custom_filter_scan_excludes_existing_custom_filter_entries(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    client.post(
        "/api/word-lists",
        json={"custom_filter": ["let me check"]},
    )
    folder = tmp_path / "proj"
    write_jsonl(folder / "convo.jsonl", [
        msg(f"u{i}", None, "ok let me check the logs please")
        for i in range(3)
    ])
    resp = client.post(
        "/api/projects/proj/conversations/convo/custom-filter/scan",
        json={"min_length_chars": 6, "min_count": 3, "n_min": 1, "n_max": 3},
    ).get_json()
    # The exact phrase is in custom_filter, so any candidate containing it
    # is filtered out (containment semantic).
    assert not any("let me check" in c for c in resp["candidates"])


def test_custom_filter_scan_rejects_bad_thresholds(client, tmp_path):
    folder = tmp_path / "proj"
    write_jsonl(folder / "convo.jsonl", [msg("1", None, "hi there")])
    url = "/api/projects/proj/conversations/convo/custom-filter/scan"
    assert client.post(url, json={"min_length_chars": 0, "min_count": 3}).status_code == 400
    assert client.post(url, json={"min_length_chars": 8, "min_count": 1}).status_code == 400
    assert client.post(url, json={"min_length_chars": 8, "min_count": 3, "n_min": 0, "n_max": 3}).status_code == 400
    assert client.post(url, json={"min_length_chars": 8, "min_count": 3, "n_min": 5, "n_max": 3}).status_code == 400


def test_custom_filter_scan_missing_convo_returns_404(client):
    resp = client.post(
        "/api/projects/nope/conversations/nope/custom-filter/scan",
        json={"min_length_chars": 8, "min_count": 3, "n_min": 1, "n_max": 3},
    )
    assert resp.status_code == 404



def test_word_list_defaults_includes_lowercase_user_text(client):
    resp = client.get("/api/word-lists/defaults").get_json()
    assert resp["lowercase_user_text"] is False


def test_word_lists_round_trip_lowercase_user_text(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={"lowercase_user_text": True},
    ).get_json()
    assert saved["lowercase_user_text"] is True
    again = client.get("/api/word-lists").get_json()
    assert again["lowercase_user_text"] is True


def test_word_lists_lowercase_user_text_rejects_non_bool(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    # Non-bool (e.g. the string "true") is coerced to False rather than
    # accepted — matches the existing "invalid lists fall back to empty"
    # posture.
    saved = client.post(
        "/api/word-lists",
        json={"lowercase_user_text": "true"},
    ).get_json()
    assert saved["lowercase_user_text"] is False



def test_word_list_defaults_includes_abbreviations(client):
    resp = client.get("/api/word-lists/defaults").get_json()
    assert resp["apply_abbreviations"] is False
    assert isinstance(resp["abbreviations"], list)
    assert len(resp["abbreviations"]) > 0
    # Every default pair has from + to, from is non-blank.
    for p in resp["abbreviations"]:
        assert isinstance(p, dict)
        assert isinstance(p["from"], str) and p["from"].strip()
        assert isinstance(p["to"], str)
    # Sanity check a couple of expected flips.
    froms = [p["from"] for p in resp["abbreviations"]]
    assert "w/" in froms
    assert "i.e." in froms
    assert "thank you" in froms
    assert "ppl" in froms


def test_word_lists_round_trip_abbreviations(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={
            "abbreviations": [{"from": "imo", "to": "in my opinion"}],
            "apply_abbreviations": True,
        },
    ).get_json()
    assert saved["apply_abbreviations"] is True
    assert saved["abbreviations"] == [{"from": "imo", "to": "in my opinion"}]
    again = client.get("/api/word-lists").get_json()
    assert again["apply_abbreviations"] is True
    assert again["abbreviations"] == [{"from": "imo", "to": "in my opinion"}]


def test_word_lists_abbreviations_drops_malformed_pairs(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={
            "abbreviations": [
                {"from": "valid", "to": "ok"},   # kept
                {"from": "", "to": "blank"},       # dropped (blank from)
                {"from": "missing_to"},             # dropped (no to)
                "not-a-dict",                       # dropped
                {"from": 42, "to": "num"},         # dropped (from not string)
                {"from": "has-to", "to": ""},      # kept (empty to is fine)
            ],
        },
    ).get_json()
    assert saved["abbreviations"] == [
        {"from": "valid", "to": "ok"},
        {"from": "has-to", "to": ""},
    ]



def test_word_list_defaults_includes_custom_filter_enabled(client):
    resp = client.get("/api/word-lists/defaults").get_json()
    assert resp["custom_filter_enabled"] is False


def test_word_lists_round_trip_custom_filter_enabled(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={"custom_filter_enabled": True},
    ).get_json()
    assert saved["custom_filter_enabled"] is True
    again = client.get("/api/word-lists").get_json()
    assert again["custom_filter_enabled"] is True



def test_word_list_defaults_includes_collapse_punct_repeats(client):
    resp = client.get("/api/word-lists/defaults").get_json()
    assert resp["collapse_punct_repeats"] is False


def test_word_lists_round_trip_collapse_punct_repeats(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_lens._word_lists_path",
        lambda: tmp_path / "wl.json",
    )
    saved = client.post(
        "/api/word-lists",
        json={"collapse_punct_repeats": True},
    ).get_json()
    assert saved["collapse_punct_repeats"] is True
    again = client.get("/api/word-lists").get_json()
    assert again["collapse_punct_repeats"] is True


def _bash_assistant(uuid, parent, command, tid="b1"):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [
                {"type": "tool_use", "id": tid, "name": "Bash",
                 "input": {"command": command}},
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }


def test_extract_command_name_handles_wrappers_and_paths():
    from llm_lens import _extract_command_name as ex
    assert ex("grep foo bar") == "grep"
    assert ex("sudo apt install foo") == "apt"
    assert ex("env FOO=1 BAR=2 grep x") == "grep"
    assert ex("/usr/bin/python3 script.py") == "python3"
    # bash -c '...' recurses into the inner script
    assert ex("bash -c 'ls -la | grep foo'") == "ls"
    # pipelines: first command wins
    assert ex("ls -la | wc -l") == "ls"
    assert ex("") == ""
    # Known limitation: `sudo -u user cmd` will mis-attribute to "user"
    # because we don't know per-flag arg-arity. Documented, not fixed —
    # the common `sudo cmd` form covers most cases.


def test_stats_counts_bash_commands_by_name(client, tmp_path):
    path = tmp_path / "proj" / "cmd.jsonl"
    write_jsonl(path, [
        msg("u1", None, "do stuff"),
        _bash_assistant("a1", "u1", "grep foo file.txt", tid="b1"),
        _bash_assistant("a2", "a1", "grep bar other.txt", tid="b2"),
        _bash_assistant("a3", "a2", "sudo apt update", tid="b3"),
        _bash_assistant("a4", "a3", "sed -i 's/x/y/' f", tid="b4"),
    ])

    s = client.get("/api/projects/proj/conversations/cmd/stats").get_json()
    assert s["commands"] == {"grep": 2, "apt": 1, "sed": 1}


def test_messages_endpoint_attaches_bash_commands_to_message(client, tmp_path):
    path = tmp_path / "proj" / "cmd.jsonl"
    write_jsonl(path, [
        msg("u1", None, "search"),
        _bash_assistant("a1", "u1", "grep -r foo .", tid="b1"),
    ])

    data = client.get("/api/projects/proj/conversations/cmd?limit=100").get_json()
    a = next(m for m in data["main"] if m["uuid"] == "a1")
    assert "[Tool: Bash:b1]" in a["content"]
    assert a["commands"] == [{"id": "b1", "command": "grep -r foo ."}]


def test_non_bash_tool_use_doesnt_attach_commands_field(client, tmp_path):
    path = tmp_path / "proj" / "cmd.jsonl"
    write_jsonl(path, [
        msg("u1", None, "read"),
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "r1", "name": "Read",
                     "input": {"path": "/etc/hosts"}},
                ],
            },
        },
    ])

    data = client.get("/api/projects/proj/conversations/cmd?limit=100").get_json()
    a = next(m for m in data["main"] if m["uuid"] == "a1")
    assert "commands" not in a


def test_cache_invalidated_after_delete_conversation(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [msg("a", None, "alpha")])

    assert client.get("/api/projects/proj/conversations/c?limit=100").status_code == 200

    assert client.delete("/api/projects/proj/conversations/c").status_code == 200

    gone = client.get("/api/projects/proj/conversations/c?limit=100")
    assert gone.status_code == 404


def test_offset_zero_with_limit_ge_total_returns_head(client, tmp_path):
    """When limit>=total the jump-to-end branch is skipped and offset stays 0."""
    path = tmp_path / "proj" / "c.jsonl"
    _linear_convo(path, 5)

    data = client.get("/api/projects/proj/conversations/c?offset=0&limit=5").get_json()
    assert data["offset"] == 0
    assert [m["uuid"] for m in data["main"]] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# Dedup behavior (parser collapses true replay duplicates, not repeated content)
# ---------------------------------------------------------------------------

def test_parser_keeps_distinct_uuids_even_with_identical_content(client, tmp_path):
    """Different uuids with the same content must ALL survive. Claude Code
    convos legitimately repeat strings like '[Tool Result]' across dozens
    of messages — collapsing those by content hid hundreds of real messages."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "duplicate text"),
        msg("b", "a", "middle unique"),
        msg("c", "b", "duplicate text"),     # same content, different uuid
        msg("d", "c", "tail unique"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]

    assert data["total"] == 4
    assert uuids == ["a", "b", "c", "d"]


def test_parser_dedups_repeated_uuids(client, tmp_path):
    """Same uuid appearing twice (a /resume replay) collapses to one entry."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "hello"),
        msg("b", "a", "world"),
        msg("a", None, "hello"),             # replay duplicate
        msg("c", "b", "bye"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]

    assert data["total"] == 3
    assert uuids == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Sidechain placement
# ---------------------------------------------------------------------------

def test_inline_sidechain_entry_dropped_from_main(client, tmp_path):
    """Inline sidechain entries are skipped in `main`. Order and content
    of real main entries is preserved around them."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        {**msg("d", "a", "side-only"), "isSidechain": True},
        msg("b", "a", "main-2"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    main_uuids = [m["uuid"] for m in data["main"]]

    assert "d" not in main_uuids
    assert main_uuids == ["a", "b"]



# ---------------------------------------------------------------------------
# Agent runs (subagents/ files)
# ---------------------------------------------------------------------------

def _agent_entry(uuid, parent, text, *, parent_tool_use_id, role="assistant"):
    # Timestamp bucket derived from hash so entries within one fixture sort
    # stably without requiring callers to pass hex-only uuids.
    bucket = abs(hash((uuid, text))) % 60
    entry = {
        "uuid": uuid,
        "parentUuid": parent,
        "type": role,
        "isSidechain": True,
        "timestamp": f"2026-04-16T00:00:{bucket:02d}.000Z",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }
    if parent_tool_use_id is not None:
        entry["parentToolUseID"] = parent_tool_use_id
    return entry


def test_agent_runs_discovered_from_subagents_dir(client, tmp_path):
    """api_conversation lists one agent_run per subagent .jsonl file.
    Name is parsed from the filename token between 'agent-' and the
    trailing hex hash. run_id = the hash itself."""
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [msg("a", None, "main-1")])

    sub = tmp_path / "proj" / "c" / "subagents" / "agent-aside_question-88f7f4fbad621fbe.jsonl"
    write_jsonl(sub, [
        _agent_entry("a1", None, "sub-1", parent_tool_use_id="toolu_ABC"),
        _agent_entry("a2", "a1", "sub-2", parent_tool_use_id="toolu_ABC"),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    assert len(data["agent_runs"]) == 1
    run = data["agent_runs"][0]
    assert run["run_id"] == "88f7f4fbad621fbe"
    assert run["name"] == "aside_question"
    assert run["message_count"] == 2
    assert run["source"] == "subagent"


def test_subagent_run_anchors_to_parent_agent_tool_use(client, tmp_path):
    """If any entry in a subagent file carries a `parentToolUseID` that
    matches a parent tool_use named `Agent` or `Task`, that id becomes the
    run's `anchor_tool_use_id` (where the inline `→` marker attaches).
    Non-Agent ptus (Read/Write/Edit/etc.) are audit-trail noise — they
    must NOT become anchors."""
    parent = tmp_path / "proj" / "c.jsonl"
    # Parent assistant message with two tool_use blocks: one Agent, one Read.
    agent_msg = {
        "uuid": "asst-1", "parentUuid": None, "type": "assistant",
        "isMeta": False,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_AGENT", "name": "Agent", "input": {}},
                {"type": "tool_use", "id": "toolu_READ",  "name": "Read",  "input": {}},
            ],
        },
    }
    write_jsonl(parent, [msg("u1", None, "what's up?"), agent_msg])

    sub = tmp_path / "proj" / "c" / "subagents" / "agent-worker-abcdef01.jsonl"
    write_jsonl(sub, [
        # One entry keyed on the Agent ptu, one on the (noise) Read ptu.
        _agent_entry("a1", None, "real agent output", parent_tool_use_id="toolu_AGENT"),
        _agent_entry("a2", "a1", "spurious progress",   parent_tool_use_id="toolu_READ"),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    assert len(data["agent_runs"]) == 1
    run = data["agent_runs"][0]
    assert run["anchor_tool_use_id"] == "toolu_AGENT"
    assert run["message_count"] == 2  # full file transcript, not ptu-filtered


def test_subagent_run_without_agent_ptu_is_standalone(client, tmp_path):
    """Files with zero Agent/Task-named ptus are still valid runs — they
    just have no parent-side anchor (show in the subagents list only)."""
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [msg("u1", None, "main")])

    sub = tmp_path / "proj" / "c" / "subagents" / "agent-observer-cafebabe.jsonl"
    write_jsonl(sub, [
        _agent_entry("a1", None, "standalone-1", parent_tool_use_id=None),
        _agent_entry("a2", "a1", "standalone-2", parent_tool_use_id=None),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    assert len(data["agent_runs"]) == 1
    run = data["agent_runs"][0]
    assert run["anchor_tool_use_id"] is None
    assert run["run_id"] == "cafebabe"


def test_agent_run_endpoint_returns_whole_file_transcript(client, tmp_path):
    """The agent endpoint returns every formatted entry in the subagent
    file (no ptu filtering). Routed by run_id = filename hash."""
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [msg("a", None, "main-1")])

    sub = tmp_path / "proj" / "c" / "subagents" / "agent-researcher-deadbeef01.jsonl"
    write_jsonl(sub, [
        _agent_entry("a1", None, "entry-one", parent_tool_use_id="toolu_X"),
        _agent_entry("a2", "a1", "entry-two", parent_tool_use_id="toolu_X"),
        _agent_entry("b1", None, "entry-three", parent_tool_use_id="toolu_Y"),
    ])

    resp = client.get("/api/projects/proj/conversations/c/agent/deadbeef01")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["agent_name"] == "researcher"
    assert data["run_id"] == "deadbeef01"
    assert data["parent_convo_id"] == "c"
    contents = [m["content"] for m in data["main"]]
    # Whole-file transcript: all three entries surface, regardless of ptu.
    assert contents == ["entry-one", "entry-two", "entry-three"]


def test_agent_run_endpoint_404_for_unknown_run_id(client, tmp_path):
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [msg("a", None, "main-1")])

    resp = client.get("/api/projects/proj/conversations/c/agent/no_such_run")
    assert resp.status_code == 404


def test_agent_run_name_defaults_to_agent_when_filename_has_no_name_token(client, tmp_path):
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [msg("a", None, "main-1")])

    # Pattern: `agent-<hex>` with no name token between.
    sub = tmp_path / "proj" / "c" / "subagents" / "agent-a6ea9ea898b34a9c.jsonl"
    write_jsonl(sub, [
        _agent_entry("a1", None, "hi", parent_tool_use_id=None),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    assert data["agent_runs"][0]["name"] == "agent"
    assert data["agent_runs"][0]["run_id"] == "a6ea9ea898b34a9c"



def test_inline_agent_run_discovered_from_parent_file(client, tmp_path):
    """Old format: a contiguous cluster of `isSidechain: true` entries in
    the parent .jsonl surfaces as one agent_run with source="inline" and a
    synthetic `inline:<anchor_uuid>` run_id. Anchor = parentUuid of the
    cluster's root."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        msg("b", "a", "main-2-with-Task-call"),
        {**msg("s1", "b", "agent says hi"), "isSidechain": True},
        {**msg("s2", "s1", "agent thinks"),  "isSidechain": True},
        {**msg("s3", "s2", "agent done"),    "isSidechain": True},
        msg("c", "b", "main-3-after-agent"),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    assert len(data["agent_runs"]) == 1
    run = data["agent_runs"][0]
    assert run["source"] == "inline"
    assert run["run_id"] == "inline:b"           # cluster's non-sidechain ancestor
    assert run["anchor_uuid"] == "b"
    assert run["message_count"] == 3
    assert run["name"] == "agent"


def test_inline_agent_run_endpoint_returns_only_clusters_messages(client, tmp_path):
    """Two separate inline clusters rooted at different main messages must
    not bleed into each other's runs."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        {**msg("s1", "a", "cluster-A-msg-1"), "isSidechain": True},
        {**msg("s2", "s1", "cluster-A-msg-2"), "isSidechain": True},
        msg("b", "a", "main-2"),
        {**msg("t1", "b", "cluster-B-msg-1"), "isSidechain": True},
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    run_ids = {r["run_id"] for r in data["agent_runs"]}
    assert run_ids == {"inline:a", "inline:b"}

    # Fetch cluster A — must contain only its two entries.
    resp = client.get("/api/projects/proj/conversations/c/agent/inline:a")
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.get_json()["main"]]
    assert "cluster-A-msg-1" in contents
    assert "cluster-A-msg-2" in contents
    assert "cluster-B-msg-1" not in contents


def test_inline_and_subagent_runs_coexist_in_same_convo(client, tmp_path):
    """Both sources surface together in agent_runs with distinct ids and
    `source` tags so the UI can mark them differently."""
    parent = tmp_path / "proj" / "c.jsonl"
    write_jsonl(parent, [
        msg("a", None, "main-1"),
        {**msg("s1", "a", "old-format-agent"), "isSidechain": True},
    ])
    sub = tmp_path / "proj" / "c" / "subagents" / "agent-worker-beef0001.jsonl"
    write_jsonl(sub, [
        _agent_entry("x1", None, "new-format-agent", parent_tool_use_id=None),
    ])

    data = client.get("/api/projects/proj/conversations/c").get_json()
    by_id = {r["run_id"]: r for r in data["agent_runs"]}
    assert "inline:a" in by_id and by_id["inline:a"]["source"] == "inline"
    assert "beef0001" in by_id and by_id["beef0001"]["source"] == "subagent"



# ---------------------------------------------------------------------------
# System-event badges (slash commands, queued drafts, compactions, …)
# ---------------------------------------------------------------------------

def _top_level_content_entry(type_, subtype, text, uuid="u1"):
    """An entry in Claude Code's 'envelope 3' shape: top-level `content`
    string, no `message` object. Used for slash commands, queue-operations,
    and system meta events."""
    e = {"uuid": uuid, "type": type_, "content": text, "isMeta": False}
    if subtype is not None:
        e["subtype"] = subtype
    return e


def test_slash_command_entry_rendered_as_marker(client, tmp_path):
    """A `system / local_command` entry carries `<command-name>` XML; the
    formatter rewrites it to `[Slash: /btw]` so the frontend renders a pill."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        _top_level_content_entry(
            "system", "local_command",
            "<command-name>/btw</command-name>\n    <command-message>btw</command-message>\n    <command-args></command-args>",
            uuid="cmd1",
        ),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert "cmd1" in by_uuid
    assert by_uuid["cmd1"]["role"] == "system"
    assert by_uuid["cmd1"]["content"] == "[Slash: /btw]"


def test_local_command_stdout_collapsed_into_marker(client, tmp_path):
    """`<local-command-stdout>…</local-command-stdout>` in a regular user
    message collapses to `[SlashOut] <text>`."""
    path = tmp_path / "proj" / "c.jsonl"
    stdout_entry = {
        "uuid": "out1", "parentUuid": None, "type": "user", "isMeta": False,
        "message": {"role": "user", "content": "<local-command-stdout>Set model to Opus</local-command-stdout>"},
    }
    write_jsonl(path, [msg("a", None, "hello"), stdout_entry])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert by_uuid["out1"]["content"] == "[SlashOut] Set model to Opus"


def test_queue_operation_entry_rendered_as_queued_badge(client, tmp_path):
    """Queued drafts (user text typed while auto-mode is busy) surface as
    user-role messages with a `[Queued]` prefix so a badge shows before the
    text."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        _top_level_content_entry("queue-operation", None, "retry with more context", uuid="q1"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert by_uuid["q1"]["role"] == "user"
    assert by_uuid["q1"]["content"].startswith("[Queued] ")
    assert "retry with more context" in by_uuid["q1"]["content"]


def test_compact_and_away_boundaries_get_system_badges(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        _top_level_content_entry("system", "compact_boundary", "Conversation compacted", uuid="c1"),
        _top_level_content_entry("system", "away_summary", "Auto mode built X", uuid="aw1"),
        _top_level_content_entry("system", "informational", "Auto mode tip", uuid="inf1"),
        _top_level_content_entry("system", "scheduled_task_fire", "Firing at 10am", uuid="s1"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert by_uuid["c1"]["content"].startswith("[Compacted] ")
    assert by_uuid["aw1"]["content"].startswith("[Away] ")
    assert by_uuid["inf1"]["content"].startswith("[Info] ")
    assert by_uuid["s1"]["content"].startswith("[Scheduled] ")



def test_stats_counts_slash_commands_and_session_events(client, tmp_path):
    """The per-convo stats endpoint exposes slash-command frequency and
    event counts (queued / compacted / etc.) — powers the Session events
    section of the stats modal without touching token totals."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "hello"),
        # Two /btw invocations + one /clear in the envelope-3 shape.
        _top_level_content_entry("system", "local_command",
            "<command-name>/btw</command-name><command-args></command-args>", uuid="s1"),
        _top_level_content_entry("system", "local_command",
            "<command-name>/btw</command-name><command-args></command-args>", uuid="s2"),
        _top_level_content_entry("system", "local_command",
            "<command-name>/clear</command-name>", uuid="s3"),
        _top_level_content_entry("queue-operation", None, "queued text 1", uuid="q1"),
        _top_level_content_entry("queue-operation", None, "queued text 2", uuid="q2"),
        _top_level_content_entry("system", "compact_boundary", "Conversation compacted", uuid="c1"),
    ])
    resp = client.get("/api/projects/proj/conversations/c/stats")
    assert resp.status_code == 200
    s = resp.get_json()
    assert s["slash_commands"] == {"/btw": 2, "/clear": 1}
    assert s["queued_count"] == 2
    assert s["compact_count"] == 1
    # Token totals must be zero — these entries don't carry `usage`.
    assert s["input_tokens"] == 0
    assert s["output_tokens"] == 0



def test_stats_thinking_prorate_and_compact_estimate(client, tmp_path):
    """Thinking estimate = `output_tokens - text_chars / 4` for each turn
    that contains a thinking block. This survives redacted-thinking turns
    (where the `thinking` field is empty and only a signature is stored)
    because we derive the response side from visible text, not thinking
    chars. `compact_summary_chars` sums the injected summary messages."""
    path = tmp_path / "proj" / "c.jsonl"
    # Turn 1: 100 output_tokens, text = 80 chars → response_est = 20,
    # thinking_est = 80. Redacted thinking (empty content).
    t1 = {
        "uuid": "t1", "parentUuid": None, "type": "assistant", "isMeta": False,
        "message": {
            "role": "assistant", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "output_tokens": 100},
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig1"},
                {"type": "text", "text": "y" * 80},
            ],
        },
    }
    # Turn 2: 200 output_tokens, text = 200 chars → response_est = 50,
    # thinking_est = 150. Non-redacted thinking (content present, ignored).
    t2 = {
        "uuid": "t2", "parentUuid": "t1", "type": "assistant", "isMeta": False,
        "message": {
            "role": "assistant", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 20, "output_tokens": 200},
            "content": [
                {"type": "thinking", "thinking": "x" * 999},
                {"type": "text", "text": "y" * 200},
            ],
        },
    }
    # A compaction: boundary + the injected summary as the next user msg.
    cb = _top_level_content_entry("system", "compact_boundary", "Conversation compacted", uuid="cb")
    summary_text = "S" * 4000
    summary = {
        "uuid": "sum1", "parentUuid": None, "type": "user", "isMeta": False,
        "message": {"role": "user", "content": summary_text},
    }
    write_jsonl(path, [t1, t2, cb, summary])

    s = client.get("/api/projects/proj/conversations/c/stats").get_json()
    assert s["thinking_output_tokens_estimate"] == {"claude-opus-4-7": 230}  # 80 + 150
    assert s["compact_summary_chars"] == 4000


# ---------------------------------------------------------------------------
# Empty / all-filtered conversation
# ---------------------------------------------------------------------------

def test_empty_conversation_returns_empty_lists(client, tmp_path):
    """A file with only snapshots/meta must return a clean empty shape, not 500."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        snapshot("s0"),
        snapshot("s1"),
        msg("m", None, "meta only", is_meta=True),
    ])

    resp = client.get("/api/projects/proj/conversations/c")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["main"] == []
    assert data["agent_runs"] == []
    assert data["total"] == 0


def test_completely_empty_file(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    resp = client.get("/api/projects/proj/conversations/c")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["main"] == []
    assert data["agent_runs"] == []
    assert data["total"] == 0
    assert data["offset"] == 0


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------

def test_bulk_delete_removes_multiple_conversations(client, tmp_path):
    for cid in ("c1", "c2", "c3"):
        write_jsonl(tmp_path / "proj" / f"{cid}.jsonl", [msg("a", None, f"in-{cid}")])

    # prime parse cache for one of them
    assert client.get("/api/projects/proj/conversations/c1?limit=100").status_code == 200

    resp = client.post(
        "/api/projects/proj/conversations/bulk-delete",
        json={"ids": ["c1", "c2"]},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["deleted"] == 2

    # deleted ones 404, survivor still readable
    assert client.get("/api/projects/proj/conversations/c1?limit=100").status_code == 404
    assert client.get("/api/projects/proj/conversations/c2?limit=100").status_code == 404
    assert client.get("/api/projects/proj/conversations/c3?limit=100").status_code == 200


def test_bulk_delete_partial_hit_counts_only_existing(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [msg("a", None, "only one")])

    resp = client.post(
        "/api/projects/proj/conversations/bulk-delete",
        json={"ids": ["c1", "does-not-exist", "also-missing"]},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["deleted"] == 1


def test_bulk_delete_removes_subagent_dir(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [msg("a", None, "x")])
    subagent_dir = tmp_path / "proj" / "c1"
    subagent_dir.mkdir()
    (subagent_dir / "session.jsonl").write_text("{}\n")

    resp = client.post(
        "/api/projects/proj/conversations/bulk-delete",
        json={"ids": ["c1"]},
    )
    assert resp.status_code == 200
    assert not (tmp_path / "proj" / "c1.jsonl").exists()
    assert not subagent_dir.exists()


# ---------------------------------------------------------------------------
# Delete project
# ---------------------------------------------------------------------------

def test_delete_project_removes_folder(client, tmp_path):
    write_jsonl(tmp_path / "proj" / "c1.jsonl", [msg("a", None, "x")])
    write_jsonl(tmp_path / "proj" / "c2.jsonl", [msg("a", None, "y")])

    # prime caches
    client.get("/api/projects")
    client.get("/api/projects/proj/conversations/c1?limit=100")

    resp = client.delete("/api/projects/proj")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert not (tmp_path / "proj").exists()

    # subsequent GET on a convo in the deleted project must 404
    assert client.get("/api/projects/proj/conversations/c1?limit=100").status_code == 404


def test_delete_project_missing_returns_404(client, tmp_path):
    resp = client.delete("/api/projects/does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------

def test_projects_lists_folders_with_conversations(client, tmp_path):
    write_jsonl(tmp_path / "proj1" / "c.jsonl", [msg("a", None, "hello")])
    write_jsonl(tmp_path / "proj2" / "c.jsonl", [msg("b", None, "world")])

    data = client.get("/api/projects").get_json()
    folders = {p["folder"] for p in data}
    assert folders == {"proj1", "proj2"}


def test_projects_excludes_empty_dirs(client, tmp_path):
    write_jsonl(tmp_path / "proj1" / "c.jsonl", [msg("a", None, "content")])
    (tmp_path / "empty-proj").mkdir()  # no .jsonl files

    data = client.get("/api/projects").get_json()
    folders = {p["folder"] for p in data}
    assert "proj1" in folders
    assert "empty-proj" not in folders


def test_projects_empty_when_no_projects_dir(client, tmp_path, monkeypatch):
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", nonexistent)
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.get_json() == []


# ---------------------------------------------------------------------------
# GET /api/projects/<folder>/conversations
# ---------------------------------------------------------------------------

def test_conversations_list_returns_expected_ids(client, tmp_path):
    for cid in ("a", "b", "c"):
        write_jsonl(
            tmp_path / "proj" / f"{cid}.jsonl",
            [msg("x", None, f"text-{cid}")],
        )

    data = client.get("/api/projects/proj/conversations").get_json()
    assert data["total"] == 3
    ids = {c["id"] for c in data["items"]}
    assert ids == {"a", "b", "c"}


def test_conversations_list_missing_project_returns_404(client, tmp_path):
    resp = client.get("/api/projects/no-such-project/conversations")
    assert resp.status_code == 404


def test_conversations_list_pagination(client, tmp_path):
    for i in range(5):
        write_jsonl(
            tmp_path / "proj" / f"c{i}.jsonl",
            [msg("x", None, f"t-{i}")],
        )

    page1 = client.get(
        "/api/projects/proj/conversations?offset=0&limit=2"
    ).get_json()
    page2 = client.get(
        "/api/projects/proj/conversations?offset=2&limit=2"
    ).get_json()
    page3 = client.get(
        "/api/projects/proj/conversations?offset=4&limit=2"
    ).get_json()

    assert page1["total"] == page2["total"] == page3["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert len(page3["items"]) == 1

    all_ids = [c["id"] for c in page1["items"] + page2["items"] + page3["items"]]
    assert len(all_ids) == 5
    assert len(set(all_ids)) == 5  # no overlaps

    # ordering-stability: concatenated page walk must match a single-shot
    # fetch of the whole list. Catches non-deterministic ordering between
    # pages (e.g., mtime ties) that "no overlaps" alone misses.
    full = client.get(
        "/api/projects/proj/conversations?offset=0&limit=100"
    ).get_json()
    full_ids = [c["id"] for c in full["items"]]
    assert all_ids == full_ids


def test_conversations_list_sort_by_size(client, tmp_path):
    write_jsonl(
        tmp_path / "proj" / "small.jsonl",
        [msg("a", None, "x")],
    )
    write_jsonl(
        tmp_path / "proj" / "big.jsonl",
        [msg(str(i), str(i - 1) if i else None, f"long text {i}" * 50) for i in range(5)],
    )

    data = client.get(
        "/api/projects/proj/conversations?sort=size&desc=1"
    ).get_json()
    ids = [c["id"] for c in data["items"]]
    assert ids[0] == "big"
    assert ids[-1] == "small"


def test_conversations_list_sort_by_msgs(client, tmp_path):
    write_jsonl(
        tmp_path / "proj" / "short.jsonl",
        [msg("a", None, "only one")],
    )
    write_jsonl(
        tmp_path / "proj" / "long.jsonl",
        [msg(str(i), str(i - 1) if i else None, f"m{i}") for i in range(4)],
    )

    data = client.get(
        "/api/projects/proj/conversations?sort=msgs&desc=1"
    ).get_json()
    ids = [c["id"] for c in data["items"]]
    assert ids[0] == "long"
    assert ids[-1] == "short"


# ---------------------------------------------------------------------------
# 404s on mutation endpoints for missing conversations
# ---------------------------------------------------------------------------

def test_duplicate_missing_conversation_returns_404(client, tmp_path):
    (tmp_path / "proj").mkdir()
    resp = client.post("/api/projects/proj/conversations/nope/duplicate")
    assert resp.status_code == 404


def test_extract_missing_conversation_returns_404(client, tmp_path):
    (tmp_path / "proj").mkdir()
    resp = client.post(
        "/api/projects/proj/conversations/nope/extract",
        json={"uuids": ["a"]},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Content-type handling in the parser
# ---------------------------------------------------------------------------

def test_parser_drops_assistant_with_empty_content(client, tmp_path):
    """__init__.py:114 filters role==assistant with empty content."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "user ask"),
        {
            "uuid": "b",
            "parentUuid": "a",
            "type": "assistant",
            "message": {"role": "assistant", "content": []},   # empty → filtered
        },
        msg("c", "b", "follow-up"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]
    assert "b" not in uuids
    assert uuids == ["a", "c"]


def test_parser_renders_tool_use_and_tool_result_blocks(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u", None, "please read a file"),
        {
            "uuid": "a",
            "parentUuid": "u",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling tool"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "uuid": "r",
            "parentUuid": "a",
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        },
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    by_uuid = {m["uuid"]: m for m in data["main"]}

    # Marker now embeds the tool_use id so the frontend can correlate the
    # badge with command data attached to the message.
    assert "[Tool: Read:t1]" in by_uuid["a"]["content"]
    assert "calling tool" in by_uuid["a"]["content"]
    assert "[Tool Result]" in by_uuid["r"]["content"]
    assert by_uuid["a"]["role"] == "assistant"
    assert by_uuid["r"]["role"] == "user"


def test_delete_message_strips_orphan_tool_result_and_drops_empty(client, tmp_path):
    """End-to-end via GET: deleting an assistant tool_use message must
    strip the matching tool_result from the next user message. If that
    strips it to empty, the user message is also dropped and its
    children re-parent up. (test_edits.py verifies the JSONL; this
    verifies the parsed API response reflects the same state.)"""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u1", None, "user ask"),
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {   # tool_result-only — will become empty when a1 is deleted
            "uuid": "r1",
            "parentUuid": "a1",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}
                ],
            },
        },
        msg("u2", "r1", "follow-up after tool"),
    ])

    resp = client.delete("/api/projects/proj/conversations/c/messages/a1")
    assert resp.status_code == 200

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]
    assert "a1" not in uuids
    assert "r1" not in uuids                 # stripped to empty → dropped
    assert "u2" in uuids                     # re-parented and preserved
    assert "user ask" in data["main"][0]["content"]


def test_delete_message_strips_orphan_tool_result_but_keeps_message(client, tmp_path):
    """If a user message has tool_result AND text, only the tool_result is
    stripped — the message survives with its remaining content."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u1", None, "user ask"),
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "uuid": "r1",
            "parentUuid": "a1",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
                    {"type": "text", "text": "here is some follow-up text"},
                ],
            },
        },
    ])

    resp = client.delete("/api/projects/proj/conversations/c/messages/a1")
    assert resp.status_code == 200

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    uuids = [m["uuid"] for m in data["main"]]
    assert "a1" not in uuids
    assert "r1" in uuids                     # survived — had non-tool content
    r1 = next(m for m in data["main"] if m["uuid"] == "r1")
    assert "[Tool Result]" not in r1["content"]
    assert "here is some follow-up text" in r1["content"]


def test_delete_message_nonexistent_uuid_returns_404(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [msg("a", None, "hello")])

    resp = client.delete("/api/projects/proj/conversations/c/messages/does-not-exist")
    assert resp.status_code == 404


def test_extract_strips_orphan_tool_use_block(client, tmp_path):
    """Extracting the tool_use side without its paired tool_result strips
    the tool_use block from the extracted copy — verified via GET."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u1", None, "user ask"),
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "uuid": "r1",
            "parentUuid": "a1",
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        },
    ])

    # extract only u1 + a1, NOT r1 → tool_use t1 is orphaned
    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": ["u1", "a1"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    data = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    a1 = next(m for m in data["main"] if m["uuid"] == "a1")
    assert "[Tool: Read]" not in a1["content"]   # orphan tool_use stripped
    assert "let me check" in a1["content"]       # text block survived


def test_extract_strips_orphan_tool_result_block(client, tmp_path):
    """The symmetric case: extract the tool_result side without the
    tool_use — the tool_result block is stripped in the output."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u1", None, "user ask"),
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "uuid": "r1",
            "parentUuid": "a1",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    {"type": "text", "text": "after the tool"},
                ],
            },
        },
    ])

    # extract only r1 → tool_result is orphaned
    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": ["r1"]},
    )
    assert resp.status_code == 200
    new_id = resp.get_json()["new_id"]

    data = client.get(
        f"/api/projects/proj/conversations/{new_id}?limit=100"
    ).get_json()
    r1 = next(m for m in data["main"] if m["uuid"] == "r1")
    assert "[Tool Result]" not in r1["content"]
    assert "after the tool" in r1["content"]


def test_extract_empty_uuids_returns_400(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [msg("a", None, "hello")])

    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": []},
    )
    assert resp.status_code == 400


def test_parser_renders_thinking_block(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("u", None, "hmm"),
        {
            "uuid": "a",
            "parentUuid": "u",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me consider"},
                    {"type": "text", "text": "answer"},
                ],
            },
        },
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    a = next(m for m in data["main"] if m["uuid"] == "a")
    assert "<thinking>let me consider</thinking>" in a["content"]
    assert "answer" in a["content"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_parser_skips_malformed_jsonl_line(client, tmp_path):
    """A truncated or non-JSON line (common with crash-interrupted writes)
    must not 500 the parser — it should skip and continue."""
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(msg("a", None, "valid first")) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps(msg("b", "a", "valid last")) + "\n")

    resp = client.get("/api/projects/proj/conversations/c?limit=100")
    assert resp.status_code == 200
    uuids = [m["uuid"] for m in resp.get_json()["main"]]
    assert "a" in uuids and "b" in uuids


def test_pagination_limit_zero_returns_empty(client, tmp_path):
    """limit=0 must return an empty page; total is still the unsliced count."""
    path = tmp_path / "proj" / "c.jsonl"
    _linear_convo(path, 5)
    data = client.get(
        "/api/projects/proj/conversations/c?offset=0&limit=0"
    ).get_json()
    assert data["main"] == []
    assert data["total"] == 5


def test_extract_nonexistent_uuids_returns_400_or_empty(client, tmp_path):
    """Requesting uuids that don't exist in the source must not 500 — it
    should either validate (4xx) or produce an empty extracted file."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [msg("a", None, "hello")])
    resp = client.post(
        "/api/projects/proj/conversations/c/extract",
        json={"uuids": ["does-not-exist"]},
    )
    assert resp.status_code in (200, 400, 404)
    if resp.status_code == 200:
        new_id = resp.get_json()["new_id"]
        new = client.get(
            f"/api/projects/proj/conversations/{new_id}?limit=100"
        ).get_json()
        assert new["main"] == []
        assert new["total"] == 0


# ---------------------------------------------------------------------------
# Enriched parse payload: model + usage on assistant entries
# ---------------------------------------------------------------------------

def test_parsed_payload_surfaces_model_and_usage_for_assistant(client, tmp_path):
    """_parse_messages_cached now forwards the raw message's `model` and
    `usage` fields so the export/stats paths can emit them without
    re-reading the source file."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        {
            "uuid": "u1",
            "parentUuid": None,
            "type": "user",
            "isMeta": False,
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "type": "assistant",
            "isMeta": False,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "hello"}],
            },
        },
    ])
    resp = client.get("/api/projects/proj/conversations/c?limit=50")
    assert resp.status_code == 200
    main = resp.get_json()["main"]
    by_role = {m["role"]: m for m in main}
    assert "assistant" in by_role
    assert by_role["assistant"].get("model") == "claude-opus-4-7"
    assert by_role["assistant"].get("usage") == {"input_tokens": 10, "output_tokens": 5}
    # User entry should not sprout a synthetic model/usage.
    assert "model" not in by_role["user"]
    assert "usage" not in by_role["user"]


# ---------------------------------------------------------------------------
# Raw conversation endpoint
# ---------------------------------------------------------------------------

def test_raw_endpoint_returns_unmodified_source_bytes(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    entries = [
        {"uuid": "u1", "parentUuid": None, "sessionId": "s1",
         "type": "user", "isMeta": False,
         "message": {"role": "user", "content": [{"type": "text", "text": "raw"}]}},
        {"type": "file-history-snapshot", "uuid": "snap"},
    ]
    write_jsonl(path, entries)
    on_disk = path.read_bytes()
    resp = client.get("/api/projects/proj/conversations/c/raw")
    assert resp.status_code == 200
    assert resp.mimetype == "application/x-ndjson"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert "c.jsonl" in resp.headers.get("Content-Disposition", "")
    # File-history snapshots and any other bytes the parser drops must still
    # appear in the raw download.
    assert resp.data == on_disk
    assert b"file-history-snapshot" in resp.data


def test_raw_endpoint_404_on_missing(client, tmp_path):
    resp = client.get("/api/projects/proj/conversations/missing/raw")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Download field preferences
# ---------------------------------------------------------------------------

@pytest.fixture
def client_isolated_download_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_lens, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(
        llm_lens, "_download_fields_path",
        lambda: tmp_path / "download_fields.json",
    )
    llm_lens._peek_jsonl_cached.cache_clear()
    llm_lens._parse_messages_cached.cache_clear()
    llm_lens.app.config["TESTING"] = True
    return llm_lens.app.test_client()


def test_download_fields_defaults(client_isolated_download_fields):
    """Fresh install returns the shipped defaults; role+content are always
    true."""
    resp = client_isolated_download_fields.get("/api/download-fields")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["role"] is True
    assert data["content"] is True
    assert data["uuid"] is True
    assert data["timestamp"] is True
    assert data["commands"] is False
    assert data["model"] is False
    assert data["usage"] is False


def test_download_fields_round_trip_ignores_required_off(client_isolated_download_fields):
    """Client can toggle optional fields; required fields stay forced on
    even if the payload says otherwise."""
    resp = client_isolated_download_fields.post(
        "/api/download-fields",
        json={"role": False, "content": False, "model": True, "usage": True,
              "uuid": False, "timestamp": False, "commands": True},
    )
    assert resp.status_code == 200
    saved = resp.get_json()
    assert saved["role"] is True
    assert saved["content"] is True
    assert saved["model"] is True
    assert saved["usage"] is True
    assert saved["uuid"] is False
    assert saved["timestamp"] is False
    assert saved["commands"] is True

    # Re-fetching must return the same values.
    again = client_isolated_download_fields.get("/api/download-fields").get_json()
    assert again == saved


def test_download_fields_unknown_keys_ignored(client_isolated_download_fields):
    resp = client_isolated_download_fields.post(
        "/api/download-fields",
        json={"garbage": True, "role": True},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "garbage" not in data


# ---------------------------------------------------------------------------
# _load_download_fields robustness — file-level, not round-tripping through
# the API. Covers the "stale JSON blob" and "partial keys" cases the HTTP
# tests can't easily construct.
# ---------------------------------------------------------------------------

def test_load_download_fields_falls_back_on_malformed_json(tmp_path, monkeypatch):
    """If ~/.cache/llm-lens/download_fields.json gets corrupted (truncated,
    half-written, edited by hand), the loader must not crash — users would
    silently lose export functionality."""
    path = tmp_path / "download_fields.json"
    path.write_text("{this is not json")
    monkeypatch.setattr(llm_lens, "_download_fields_path", lambda: path)
    out = llm_lens._load_download_fields()
    # Every expected key present and role/content still forced on.
    for k in ("uuid", "role", "content", "timestamp", "commands", "model", "usage"):
        assert k in out
    assert out["role"] is True and out["content"] is True


def test_load_download_fields_partial_file_merges_with_defaults(tmp_path, monkeypatch):
    """Older versions / partial writes may only know about a subset of
    keys. Missing keys must fall through to defaults rather than dropping."""
    path = tmp_path / "download_fields.json"
    path.write_text(json.dumps({"model": True}))  # only one optional key
    monkeypatch.setattr(llm_lens, "_download_fields_path", lambda: path)
    out = llm_lens._load_download_fields()
    assert out["model"] is True                      # honored
    assert out["commands"] is False                  # default
    assert out["usage"] is False                     # default
    assert out["role"] is True and out["content"] is True


def test_load_download_fields_ignores_non_bool_values(tmp_path, monkeypatch):
    """A stray string or null in the saved file for an optional field
    shouldn't flip the flag truthy — use the default and move on."""
    path = tmp_path / "download_fields.json"
    path.write_text(json.dumps({"uuid": "yes", "model": None, "usage": 1}))
    monkeypatch.setattr(llm_lens, "_download_fields_path", lambda: path)
    out = llm_lens._load_download_fields()
    # All three fall back to shipped defaults (True / False / False).
    assert out["uuid"] is True      # default on
    assert out["model"] is False    # default off
    assert out["usage"] is False    # default off


def test_save_download_fields_rejects_non_bool_and_rehydrates(tmp_path, monkeypatch):
    path = tmp_path / "download_fields.json"
    monkeypatch.setattr(llm_lens, "_download_fields_path", lambda: path)
    saved = llm_lens._save_download_fields({"uuid": "truthy-string", "model": 1})
    # Non-bool values get replaced by defaults, not coerced through bool().
    assert saved["uuid"] is True       # default
    assert saved["model"] is False     # default
    # File contents match what we returned (no smuggled extra keys).
    on_disk = json.loads(path.read_text())
    assert on_disk == saved


def test_save_download_fields_forces_required_fields_on(tmp_path, monkeypatch):
    path = tmp_path / "download_fields.json"
    monkeypatch.setattr(llm_lens, "_download_fields_path", lambda: path)
    saved = llm_lens._save_download_fields({"role": False, "content": False})
    assert saved["role"] is True and saved["content"] is True
    assert json.loads(path.read_text())["role"] is True
