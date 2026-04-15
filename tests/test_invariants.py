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
    """N=5, k=1 (snapshot), m=1 (isMeta) → main+side = 3.

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

    # direct invariant check: len(main)+len(side) == N - k - m
    assert len(data["main"]) + len(data["sidechain"]) == 3
    assert data["total"] == 3  # main-only sanity (no sidechain in this fixture)
    main_uuids = [m["uuid"] for m in data["main"]]
    side_uuids = [m["uuid"] for m in data["sidechain"]]
    assert "s1" not in main_uuids and "s1" not in side_uuids
    assert "c" not in main_uuids and "c" not in side_uuids


def test_parser_count_multiple_snapshots_and_meta(client, tmp_path):
    """N=8, k=3 snapshots, m=2 isMeta → main+side == 3."""
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
    assert len(data["main"]) + len(data["sidechain"]) == 3          # 8 - 3 - 2


def test_parser_count_includes_sidechain(client, tmp_path):
    """N=6, k=1, m=1 → 4 real messages; 1 is sidechain → main=3, side=1.
    total tracks main only, but main+side must equal N-k-m."""
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        snapshot("s0"),                                              # k=1
        msg("b", "a", "main-2"),
        {**msg("sc", "b", "side-only"), "isSidechain": True},        # sidechain
        msg("mx", "b", "meta", is_meta=True),                        # m=1
        msg("c", "b", "main-3"),
    ])
    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()

    assert len(data["main"]) + len(data["sidechain"]) == 4           # N - k - m
    assert data["total"] == 3                                        # main only
    assert len(data["sidechain"]) == 1


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
    side_uuids = [m["uuid"] for m in data["sidechain"]]

    # complete set check — catches both missing and unexpected UUIDs
    assert set(returned) == {"a", "b", "d"}
    assert len(returned) == 3      # length + set equality ⇒ no duplicates
    assert "snap1" not in returned
    assert "c" not in returned
    # filtered entries must not leak into sidechain either
    assert "snap1" not in side_uuids
    assert "c" not in side_uuids
    # role must survive the round-trip — the parser routes (side if
    # is_sidechain else main) and could silently swap/drop role.
    by_uuid = {m["uuid"]: m for m in data["main"]}
    assert by_uuid["a"]["role"] == "user"
    assert by_uuid["b"]["role"] == "user"
    assert by_uuid["d"]["role"] == "user"


def test_parser_uuid_roundtrip_includes_sidechain(client, tmp_path):
    """Every non-filtered uuid appears exactly once across main ∪ sidechain,
    and the two lists are disjoint (a uuid can't be in both)."""
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
    side_uuids = [m["uuid"] for m in data["sidechain"]]
    all_returned = main_uuids + side_uuids

    expected = {"a", "b", "d", "e"}           # snap1 (k=1) and c (m=1) filtered
    assert set(all_returned) == expected
    assert len(all_returned) == len(set(all_returned)), \
        "uuid appears in both main and sidechain"
    assert set(main_uuids).isdisjoint(set(side_uuids))
    assert "snap1" not in all_returned
    assert "c" not in all_returned


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


def test_scrub_redacts_text_preserves_usage_and_chain(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [
        msg("u1", None, "what is 2+2"),
        _assistant("a1", "u1", "The answer is four.", 100, 20),
        msg("u2", "a1", "thanks"),
    ])

    pre = client.get("/api/projects/proj/conversations/s/stats").get_json()

    resp = client.post("/api/projects/proj/conversations/s/messages/a1/scrub")
    assert resp.status_code == 200

    # Stats (including usage-derived tokens) unchanged — `usage` left alone.
    post = client.get("/api/projects/proj/conversations/s/stats").get_json()
    assert post["input_tokens"] == pre["input_tokens"]
    assert post["output_tokens"] == pre["output_tokens"]

    # On-disk: uuid/parentUuid unchanged, text content replaced with ".".
    with open(path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    scrubbed = next(e for e in lines if e["uuid"] == "a1")
    assert scrubbed["parentUuid"] == "u1"
    assert scrubbed["message"]["usage"]["input_tokens"] == 100
    assert scrubbed["message"]["usage"]["output_tokens"] == 20
    assert scrubbed["message"]["content"] == [{"type": "text", "text": "."}]
    # Neighbours untouched — scrub is local.
    assert next(e for e in lines if e["uuid"] == "u1")["message"]["content"] \
        == [{"type": "text", "text": "what is 2+2"}]


def test_scrub_rejects_non_prose_messages(client, tmp_path):
    """Messages containing tool_use / tool_result / thinking blocks can't be
    scrubbed — those blocks carry structural meaning (tool linkage, thinking
    signatures) that a blanket text replace would break."""
    path = tmp_path / "proj" / "s.jsonl"
    tool_msg = {
        "uuid": "t1",
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll look that up."},
                {"type": "tool_use", "id": "tu1", "name": "lookup", "input": {}},
            ],
        },
    }
    write_jsonl(path, [tool_msg])

    resp = client.post("/api/projects/proj/conversations/s/messages/t1/scrub")
    assert resp.status_code == 400
    assert "prose-only" in resp.get_json()["error"]

    # File unchanged.
    with open(path) as f:
        line = json.loads(f.readline())
    assert line["message"]["content"][1]["type"] == "tool_use"


def test_scrub_returns_404_for_missing_message(client, tmp_path):
    path = tmp_path / "proj" / "s.jsonl"
    write_jsonl(path, [msg("u1", None, "hi")])

    resp = client.post(
        "/api/projects/proj/conversations/s/messages/does-not-exist/scrub"
    )
    assert resp.status_code == 404


def test_normalize_whitespace_collapses_runs_and_blank_lines(client, tmp_path):
    path = tmp_path / "proj" / "w.jsonl"
    write_jsonl(path, [
        msg("u1", None, "hello    world\n\n\n\nnext   paragraph   "),
    ])

    resp = client.post(
        "/api/projects/proj/conversations/w/messages/u1/scrub",
        json={"kind": "normalize_whitespace"},
    )
    assert resp.status_code == 200

    with open(path) as f:
        entry = json.loads(f.readline())
    text = entry["message"]["content"][0]["text"]
    assert text == "hello world\n\nnext paragraph"


def test_transform_rejects_unknown_kind(client, tmp_path):
    path = tmp_path / "proj" / "w.jsonl"
    write_jsonl(path, [msg("u1", None, "hi")])

    resp = client.post(
        "/api/projects/proj/conversations/w/messages/u1/scrub",
        json={"kind": "bogus"},
    )
    assert resp.status_code == 400
    assert "Unknown transform kind" in resp.get_json()["error"]


def test_remove_swears_handles_stem_variations(client, tmp_path):
    """`fuck*` should match fuck/fucks/fucker/fucking/etc. via the closed
    safe-suffix list — but plain `ass` must not blow up on `assistant`."""
    path = tmp_path / "proj" / "sw.jsonl"
    write_jsonl(path, [
        msg("u1", None,
            "fuck this fucking fucker, fucks and fucked. ass alone here. "
            "but the assistant is fine and assess is fine."),
    ])

    resp = client.post(
        "/api/projects/proj/conversations/sw/messages/u1/scrub",
        json={"kind": "remove_swears"},
    )
    assert resp.status_code == 200

    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    # All fuck variants gone
    assert "fuck" not in text.lower()
    # Plain `ass` removed (it's in the default list)
    assert " ass " not in f" {text} "
    # `assistant` and `assess` survive — false positives would mean the
    # regex was too greedy.
    assert "assistant" in text
    assert "assess" in text


def test_remove_filler_drops_ai_sycophancy_phrases(client, tmp_path):
    path = tmp_path / "proj" / "fl.jsonl"
    write_jsonl(path, [
        msg("u1", None,
            "You're absolutely right! Here is the answer. "
            "Let me think step by step. The result is 4."),
    ])

    resp = client.post(
        "/api/projects/proj/conversations/fl/messages/u1/scrub",
        json={"kind": "remove_filler"},
    )
    assert resp.status_code == 200

    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    assert "absolutely right" not in text.lower()
    assert "step by step" not in text.lower()
    assert "Here is the answer." in text
    assert "The result is 4." in text


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


def test_swears_match_case_insensitive(client, tmp_path):
    path = tmp_path / "proj" / "ci.jsonl"
    write_jsonl(path, [msg("u1", None, "FUCK this and Fucker")])

    client.post(
        "/api/projects/proj/conversations/ci/messages/u1/scrub",
        json={"kind": "remove_swears"},
    )
    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    assert "fuck" not in text.lower()


def test_swears_empty_list_is_noop(client, tmp_path, monkeypatch):
    """Empty user-saved swears list must mean 'don't strip anything' — not
    'fall back to defaults'. User opt-out has to be possible."""
    monkeypatch.setattr("llm_lens._word_lists_path", lambda: tmp_path / "wl.json")
    client.post("/api/word-lists", json={"swears": [], "filler": []})

    path = tmp_path / "proj" / "ne.jsonl"
    write_jsonl(path, [msg("u1", None, "fuck this shit")])

    client.post(
        "/api/projects/proj/conversations/ne/messages/u1/scrub",
        json={"kind": "remove_swears"},
    )
    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    assert text == "fuck this shit"


def test_swear_stem_doesnt_match_inside_other_words(client, tmp_path):
    """Regression: `ass*` would match `assistant` if we used `\\w*` instead
    of the closed safe-suffix list. This test guards that fix."""
    path = tmp_path / "proj" / "fp.jsonl"
    write_jsonl(path, [msg("u1", None,
        "the assistant assessed the assignment in the assembly")])

    # Use a custom list with `ass*` to force stem matching of a short stem.
    client.post(
        "/api/word-lists",
        json={"swears": ["ass*"], "filler": []},
    )
    client.post(
        "/api/projects/proj/conversations/fp/messages/u1/scrub",
        json={"kind": "remove_swears"},
    )
    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    # All four longer words must survive
    for w in ("assistant", "assessed", "assignment", "assembly"):
        assert w in text, f"stem matcher false-positive on '{w}': {text!r}"


def test_filler_handles_multi_block_content(client, tmp_path):
    """List-shaped content (multiple text blocks) must have each block
    transformed independently, not collapsed into one."""
    path = tmp_path / "proj" / "mb.jsonl"
    multi = {
        "uuid": "m1",
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Certainly! First part."},
                {"type": "text", "text": "Of course! Second part."},
            ],
        },
    }
    write_jsonl(path, [multi])

    client.post(
        "/api/projects/proj/conversations/mb/messages/m1/scrub",
        json={"kind": "remove_filler"},
    )
    with open(path) as f:
        blocks = json.loads(f.readline())["message"]["content"]
    assert len(blocks) == 2
    assert "Certainly" not in blocks[0]["text"]
    assert "Of course" not in blocks[1]["text"]
    assert "First part" in blocks[0]["text"]
    assert "Second part" in blocks[1]["text"]


def test_swear_strip_tidies_punctuation_and_double_spaces(client, tmp_path):
    """After removal, leading space before `,` / `.` should collapse so the
    sentence still reads cleanly — not 'this  , then'."""
    path = tmp_path / "proj" / "tidy.jsonl"
    write_jsonl(path, [msg("u1", None, "this fucking , then shit . done")])

    client.post(
        "/api/projects/proj/conversations/tidy/messages/u1/scrub",
        json={"kind": "remove_swears"},
    )
    with open(path) as f:
        text = json.loads(f.readline())["message"]["content"][0]["text"]
    # No "  " (double space) and no " ," / " ." left behind
    assert "  " not in text
    assert " ," not in text
    assert " ." not in text


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

def test_sidechain_entry_lands_in_sidechain_not_main(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    write_jsonl(path, [
        msg("a", None, "main-1"),
        {**msg("d", "a", "side-only"), "isSidechain": True},
        msg("b", "a", "main-2"),
    ])

    data = client.get("/api/projects/proj/conversations/c?limit=100").get_json()
    main_uuids = [m["uuid"] for m in data["main"]]
    side_uuids = [m["uuid"] for m in data["sidechain"]]

    assert "d" in side_uuids
    assert "d" not in main_uuids
    assert main_uuids == ["a", "b"]


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
    assert data["sidechain"] == []
    assert data["total"] == 0


def test_completely_empty_file(client, tmp_path):
    path = tmp_path / "proj" / "c.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    resp = client.get("/api/projects/proj/conversations/c")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["main"] == []
    assert data["sidechain"] == []
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

    assert "[Tool: Read]" in by_uuid["a"]["content"]
    assert "calling tool" in by_uuid["a"]["content"]
    assert "[Tool Result]" in by_uuid["r"]["content"]
    # role round-trip for both assistant and user paths
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
