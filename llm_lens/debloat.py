"""File-level debloat for Claude Code JSONL conversations.

Removes redundant / oversized sidecar metadata that Claude Code writes to
disk but doesn't need on `/resume`. Lossy, in-place, and gated behind an
invariant check: every tracked stat (tokens, tool counts, thinking
counts, etc.) must equal before/after or the rewrite is rolled back.

Ported from the approach at brtkwr.com/posts/2026-01-22-pruning-claude-code-conversation-history,
minus the `agent_progress` rule — that rule's `.data.message` target
carries real subagent invocation content in this project's data shape
(see `_format_entry_message` in `__init__.py`), so we defer it as future
opt-in. Everything we keep is structurally safe.

Rules:
  1. Delete `normalizedMessages` nested under any `data` dict (safe
     no-op when absent).
  2. Top-level `toolUseResult` with stringified length >10KB → replaced
     with `{"debloated": True, "was_bytes": N}`.
  3. Thinking block text (`.thinking`) >20KB → truncated to first 2000
     chars + marker suffix. Block `type` preserved so `thinking_count`
     stays correct.
  4. `toolUseResult.stdout` >1000 chars (when the whole object hasn't
     already been replaced by rule 2) → first 1000 chars + marker.
     Sibling fields (exitCode, stderr, interrupted) left alone.
  5. Inline `tool_result` blocks inside `message.content` with
     stringified length >10KB → `content` replaced with a marker string.
     `type` and `tool_use_id` preserved so `has_tool_result`, the resume
     chain, and tool_use↔tool_result pairing all survive.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# Thresholds copied verbatim from the article.
TOOL_USE_RESULT_LIMIT = 10_000
BASH_STDOUT_LIMIT = 1_000
THINKING_LIMIT = 20_000
THINKING_KEEP = 2_000
INLINE_TOOL_RESULT_LIMIT = 10_000


# Every stat key the aggregator exposes that should be invariant across
# debloat. Matches the non-identity fields of `_empty_stats()` in
# `__init__.py` — if that schema grows a new countable field, add it
# here too.
_INVARIANT_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "tool_uses",
    "commands",
    "thinking_count",
    "slash_commands",
    "tool_turn_tokens",
    "command_turn_tokens",
    "thinking_output_tokens_estimate",
    "compact_summary_chars",
    "queued_count",
    "compact_count",
    "away_count",
    "info_count",
    "scheduled_count",
    "per_model",
)


class StatsInvariantError(Exception):
    """Raised when a debloat rewrite changed a stat that must never change.
    The caller is responsible for restoring the original file bytes."""


# --------------------------------------------------------------------------
# Rule engine.
# --------------------------------------------------------------------------


def _debloat_entry(entry: dict, strip_images: bool = False) -> tuple[dict, dict]:
    """Apply every debloat rule in place on one parsed JSONL entry.

    Returns (entry, counts). `entry` is the same dict reference mutated
    in place for performance on large files.

    When `strip_images=True`, the experimental image-strip rule also
    fires: every `image` block's `source.data` is replaced with an
    empty string (valid-but-empty base64) and `__debloated__: true` is
    stamped on the source so the UI can surface which blocks lost
    their pixels. This rule is OPT-IN per apply because base64 images
    in user content may be part of what Claude Code replays on
    `/resume` — unlike the other rules, which only touch local-only
    sidecar metadata.
    """
    counts = {
        "normalized_dropped": 0,
        "tool_use_result_truncated": 0,
        "thinking_truncated": 0,
        "bash_stdout_truncated": 0,
        "inline_tool_result_truncated": 0,
        "images_stripped": 0,
    }

    # Rule 1.
    data = entry.get("data")
    if isinstance(data, dict) and "normalizedMessages" in data:
        del data["normalizedMessages"]
        counts["normalized_dropped"] = 1

    # Rule 2 + 4. Order matters: rule 2 replaces the whole dict with a
    # marker, so rule 4 only runs when rule 2 doesn't fire.
    tur = entry.get("toolUseResult")
    if tur is not None:
        try:
            tur_len = len(json.dumps(tur, ensure_ascii=False))
        except (TypeError, ValueError):
            tur_len = 0
        if tur_len > TOOL_USE_RESULT_LIMIT:
            entry["toolUseResult"] = {"debloated": True, "was_bytes": tur_len}
            counts["tool_use_result_truncated"] = 1
        elif isinstance(tur, dict):
            stdout = tur.get("stdout")
            if isinstance(stdout, str) and len(stdout) > BASH_STDOUT_LIMIT:
                tur["stdout"] = (
                    stdout[:BASH_STDOUT_LIMIT]
                    + f"\n[truncated — was {len(stdout)} bytes]"
                )
                counts["bash_stdout_truncated"] = 1

    # Rules 3 + 5 + (6, experimental).
    msg = entry.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "thinking":
                    th = block.get("thinking")
                    if isinstance(th, str) and len(th) > THINKING_LIMIT:
                        block["thinking"] = (
                            th[:THINKING_KEEP]
                            + f"\n[truncated — was {len(th)} bytes]"
                        )
                        counts["thinking_truncated"] += 1
                elif t == "tool_result":
                    try:
                        block_len = len(json.dumps(block, ensure_ascii=False))
                    except (TypeError, ValueError):
                        block_len = 0
                    if block_len > INLINE_TOOL_RESULT_LIMIT:
                        tool_use_id = block.get("tool_use_id")
                        block.clear()
                        block["type"] = "tool_result"
                        if tool_use_id is not None:
                            block["tool_use_id"] = tool_use_id
                        block["content"] = (
                            f"[truncated — was {block_len} bytes]"
                        )
                        counts["inline_tool_result_truncated"] += 1
                elif t == "image" and strip_images:
                    src = block.get("source")
                    if isinstance(src, dict):
                        data_str = src.get("data")
                        if isinstance(data_str, str) and len(data_str) > 0:
                            # Empty-string base64 is still valid base64;
                            # preserves the block's shape so parsers that
                            # validate won't choke. Flag it so the UI can
                            # show which blocks are stripped.
                            src["data"] = ""
                            src["__debloated__"] = True
                            src["was_bytes"] = len(data_str)
                            counts["images_stripped"] += 1

    return entry, counts


# --------------------------------------------------------------------------
# Public API.
# --------------------------------------------------------------------------


def scan_convo(path) -> dict:
    """Read-only pass: compute exact reclaim numbers for both modes.

    Returns:
        current_size:                 bytes in the file as it stands
        bytes_reclaimable:            reclaim under the default (no-image) rules
        bytes_reclaimable_with_images: reclaim when the experimental image-strip
                                       rule is also applied
        counts:                       per-rule counts for the default mode
        counts_with_images:           per-rule counts when images are stripped too

    Both numbers are exact byte deltas (not estimates) — same engine the
    apply uses, minus the write.
    """
    path = Path(path)
    if not path.exists():
        return {
            "bytes_reclaimable": 0,
            "bytes_reclaimable_with_images": 0,
            "counts": _empty_counts(),
            "counts_with_images": _empty_counts(),
            "current_size": 0,
        }

    size_before = path.stat().st_size
    if size_before == 0:
        return {
            "bytes_reclaimable": 0,
            "bytes_reclaimable_with_images": 0,
            "counts": _empty_counts(),
            "counts_with_images": _empty_counts(),
            "current_size": 0,
        }

    # Walk the file once; for each line compute the serialized length
    # under both rule sets. Cheap: per-line extra work is a second
    # pass through the rules + a second json.dumps.
    new_bytes_default = 0
    new_bytes_with_images = 0
    total_default = _empty_counts()
    total_with_images = _empty_counts()

    with open(path, "rb") as fp:
        for raw in fp:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                new_bytes_default += len(raw)
                new_bytes_with_images += len(raw)
                continue
            stripped = text.rstrip("\n")
            if not stripped:
                new_bytes_default += len(raw)
                new_bytes_with_images += len(raw)
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                new_bytes_default += len(raw)
                new_bytes_with_images += len(raw)
                continue

            # Default: no image strip.
            entry_a = json.loads(stripped)  # fresh copy
            _, counts_a = _debloat_entry(entry_a, strip_images=False)
            for k, v in counts_a.items():
                total_default[k] += v
            new_bytes_default += len(
                (json.dumps(entry_a, ensure_ascii=False) + "\n").encode("utf-8")
            )

            # With images.
            entry_b = entry
            _, counts_b = _debloat_entry(entry_b, strip_images=True)
            for k, v in counts_b.items():
                total_with_images[k] += v
            new_bytes_with_images += len(
                (json.dumps(entry_b, ensure_ascii=False) + "\n").encode("utf-8")
            )

    return {
        "bytes_reclaimable": max(0, size_before - new_bytes_default),
        "bytes_reclaimable_with_images": max(0, size_before - new_bytes_with_images),
        "counts": total_default,
        "counts_with_images": total_with_images,
        "current_size": size_before,
    }


def apply_debloat(path, strip_images: bool = False) -> dict:
    """Rewrite the file in place with debloat rules applied, verify
    stats are unchanged, and record a tombstone. On invariant failure,
    restore the original byte-for-byte and raise `StatsInvariantError`.

    `strip_images=True` opts into the experimental image-strip rule.
    Safe for stats (images aren't counted), but may affect `/resume`
    because base64 images in user content are part of what Claude Code
    replays to the API — unlike every other rule, which only touches
    local-only sidecar metadata. Off by default; callers must explicitly
    pass True.
    """
    path = Path(path)
    if not path.exists():
        return {"bytes_reclaimed": 0, "counts": _empty_counts()}
    size_before = path.stat().st_size
    if size_before == 0:
        return {"bytes_reclaimed": 0, "counts": _empty_counts()}

    # Import here to dodge circularity: __init__.py imports us on demand
    # from the route handlers.
    from llm_lens import _stats, _peek_jsonl_cached, _stats_cached, peek_cache

    # Snapshot stats before.
    _peek_jsonl_cached.cache_clear()
    _stats_cached.cache_clear()
    before_stats = _stats(path, path.stat())

    backup_bytes = path.read_bytes()

    total = _empty_counts()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".debloat.", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as out, open(path, "rb") as inp:
            for raw in inp:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    out.write(raw.decode("utf-8", "replace"))
                    continue
                stripped = text.rstrip("\n")
                if not stripped:
                    out.write(text)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    out.write(text)
                    if not text.endswith("\n"):
                        out.write("\n")
                    continue
                new_entry, counts = _debloat_entry(entry, strip_images=strip_images)
                for k, v in counts.items():
                    total[k] += v
                out.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # Invariant check. If this fails, we restore and re-raise.
    _peek_jsonl_cached.cache_clear()
    _stats_cached.cache_clear()
    try:
        after_stats = _stats(path, path.stat())
        _verify_stats_equal(before_stats, after_stats)
    except StatsInvariantError:
        path.write_bytes(backup_bytes)
        _peek_jsonl_cached.cache_clear()
        _stats_cached.cache_clear()
        raise

    bytes_reclaimed = size_before - path.stat().st_size

    try:
        peek_cache.set(
            path, path.stat(),
            debloat_delta={
                "bytes_reclaimed": bytes_reclaimed,
                "counts": dict(total),
                "images_stripped": bool(strip_images),
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        pass

    return {"bytes_reclaimed": bytes_reclaimed, "counts": total}


# --------------------------------------------------------------------------
# Internals.
# --------------------------------------------------------------------------


def _empty_counts() -> dict:
    return {
        "normalized_dropped": 0,
        "tool_use_result_truncated": 0,
        "thinking_truncated": 0,
        "bash_stdout_truncated": 0,
        "inline_tool_result_truncated": 0,
        "images_stripped": 0,
    }


def _verify_stats_equal(before: dict, after: dict) -> None:
    for k in _INVARIANT_KEYS:
        b = before.get(k)
        a = after.get(k)
        if b != a:
            raise StatsInvariantError(
                f"stat {k!r} changed: {b!r} -> {a!r}"
            )
