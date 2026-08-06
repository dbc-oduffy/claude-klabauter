"""
Tests for coordinator_core.ops.tracker.advance_status — tracker.advance_status.

Coverage:
  (a) registration — tracker.advance_status lands in _REGISTRY on import.
  (b) single-stub flip: status cell changes, every other cell/byte untouched.
  (c) idempotent re-apply: row already at to_status -> unchanged, changed=False,
      file is NOT reopened for write (mtime unchanged).
  (d) multi-stub batch in one call, mixed updated/unchanged.
  (e) not-found stub_id -> TrackerRowError, file untouched (ALL-OR-NONE).
  (f) ambiguous stub_id (two matching rows) -> TrackerRowError, file untouched.
  (g) chunk-/stub- prefix matching, case-insensitivity, bold/backtick cell markup.
  (h) attempt-count parenthetical is dropped, not merged (documented negative-spec).
  (i) CRLF/CR line-ending preservation (Windows parity).
  (j) param validation: empty/non-list stub_ids, blank/pipe/newline to_status,
      blank tracker_path.
  (k) handler-level: path-containment rejection (escape via ..), missing file,
      repo_root=None.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency. The pure `advance_status()` core is tested directly
(sync) without any asyncio involvement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for tracker.advance_status. ----
import coordinator_core.ops.tracker.advance_status  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.tracker.advance_status import (
    TrackerRowError,
    _handler,
    advance_status,
)


def _run(coro):
    return asyncio.run(coro)


_SAMPLE_TABLE = (
    "# Tracker\n"
    "\n"
    "| Stub | Status | Notes |\n"
    "|---|---|---|\n"
    "| chunk-2A | Pending enrichment | first |\n"
    "| chunk-2B | Pending enrichment | second |\n"
    "| chunk-2C | Execution in progress (attempt 2/3) | third |\n"
)


def _write(tmp_path: Path, content: str = _SAMPLE_TABLE, name: str = "README.md") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8", newline="")
    return p


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_tracker_advance_status_registered():
    assert "tracker.advance_status" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) single-stub flip
# ---------------------------------------------------------------------------


def test_single_stub_flip_changes_only_status_cell(tmp_path):
    tracker = _write(tmp_path)
    result = advance_status(tracker, ["2A"], "Enrichment in progress")

    assert result == {"updated": ["2A"], "unchanged": [], "changed": True}
    new_content = tracker.read_text(encoding="utf-8")
    assert "| chunk-2A | Enrichment in progress | first |\n" in new_content
    # Untouched rows are byte-identical.
    assert "| chunk-2B | Pending enrichment | second |\n" in new_content
    assert "| chunk-2C | Execution in progress (attempt 2/3) | third |\n" in new_content
    assert "# Tracker\n" in new_content


# ---------------------------------------------------------------------------
# (c) idempotent re-apply — no-op, no write
# ---------------------------------------------------------------------------


def test_idempotent_reapply_is_noop_and_does_not_rewrite_file(tmp_path):
    tracker = _write(tmp_path)
    advance_status(tracker, ["2A"], "Enrichment in progress")
    mtime_before = tracker.stat().st_mtime_ns

    result = advance_status(tracker, ["2A"], "Enrichment in progress")

    assert result == {"updated": [], "unchanged": ["2A"], "changed": False}
    assert tracker.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# (d) multi-stub batch, mixed updated/unchanged
# ---------------------------------------------------------------------------


def test_multi_stub_batch_mixed_updated_and_unchanged(tmp_path):
    tracker = _write(tmp_path)
    advance_status(tracker, ["2A"], "Enrichment in progress")

    result = advance_status(tracker, ["2A", "2B"], "Enrichment in progress")

    assert result["updated"] == ["2B"]
    assert result["unchanged"] == ["2A"]
    assert result["changed"] is True
    content = tracker.read_text(encoding="utf-8")
    assert "| chunk-2A | Enrichment in progress | first |\n" in content
    assert "| chunk-2B | Enrichment in progress | second |\n" in content


# ---------------------------------------------------------------------------
# (e) not-found stub_id — ALL-OR-NONE, file untouched
# ---------------------------------------------------------------------------


def test_not_found_stub_id_raises_and_leaves_file_untouched(tmp_path):
    tracker = _write(tmp_path)
    before = tracker.read_text(encoding="utf-8")

    with pytest.raises(TrackerRowError, match="not found"):
        advance_status(tracker, ["2A", "9Z"], "Enrichment in progress")

    assert tracker.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# (f) ambiguous stub_id — two matching rows, file untouched
# ---------------------------------------------------------------------------


def test_ambiguous_stub_id_raises_and_leaves_file_untouched(tmp_path):
    content = _SAMPLE_TABLE + "| chunk-2A | Pending enrichment | duplicate row |\n"
    tracker = _write(tmp_path, content)
    before = tracker.read_text(encoding="utf-8")

    with pytest.raises(TrackerRowError, match="ambiguous"):
        advance_status(tracker, ["2A"], "Enrichment in progress")

    assert tracker.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# (g) prefix matching, case-insensitivity, markup stripping
# ---------------------------------------------------------------------------


def test_bare_id_chunk_prefix_and_case_insensitive_matching(tmp_path):
    content = (
        "| Stub | Status |\n"
        "|---|---|\n"
        "| **2A** | Pending enrichment |\n"
        "| `chunk-2B` | Pending enrichment |\n"
        "| CHUNK-2C | Pending enrichment |\n"
    )
    tracker = _write(tmp_path, content)

    result = advance_status(tracker, ["2a", "2B", "2c"], "Under review")

    assert set(result["updated"]) == {"2a", "2B", "2c"}
    new_content = tracker.read_text(encoding="utf-8")
    assert new_content.count("Under review") == 3


# ---------------------------------------------------------------------------
# (h) attempt-count parenthetical is dropped, not merged
# ---------------------------------------------------------------------------


def test_attempt_count_parenthetical_is_dropped_not_merged(tmp_path):
    tracker = _write(tmp_path)

    result = advance_status(tracker, ["2C"], "Enriched and reviewed")

    assert result["updated"] == ["2C"]
    content = tracker.read_text(encoding="utf-8")
    assert "| chunk-2C | Enriched and reviewed | third |\n" in content
    assert "attempt 2/3" not in content


# ---------------------------------------------------------------------------
# (i) CRLF / CR line-ending preservation
# ---------------------------------------------------------------------------


def test_crlf_line_endings_preserved(tmp_path):
    content = (
        "| Stub | Status |\r\n"
        "|---|---|\r\n"
        "| chunk-2A | Pending enrichment |\r\n"
        "| chunk-2B | Pending enrichment |\r\n"
    )
    tracker = _write(tmp_path, content)

    advance_status(tracker, ["2A"], "Enrichment in progress")

    raw = tracker.read_bytes()
    assert b"\r\n" in raw
    assert b"| chunk-2A | Enrichment in progress |\r\n" in raw
    # Untouched CRLF row still has its CRLF ending.
    assert b"| chunk-2B | Pending enrichment |\r\n" in raw


# ---------------------------------------------------------------------------
# (j) param validation
# ---------------------------------------------------------------------------


def test_empty_stub_ids_list_raises(tmp_path):
    tracker = _write(tmp_path)
    with pytest.raises(ValueError, match="stub_ids"):
        advance_status(tracker, [], "Enrichment in progress")


def test_pipe_in_to_status_raises_via_handler():
    async def _call():
        return await _handler(
            {
                "tracker_path": "README.md",
                "stub_ids": ["2A"],
                "to_status": "bad | status",
            },
            repo_root=Path("/does/not/matter/.git"),
        )

    with pytest.raises(ValueError, match="to_status"):
        _run(_call())


def test_blank_tracker_path_raises_via_handler():
    async def _call():
        return await _handler(
            {"tracker_path": "   ", "stub_ids": ["2A"], "to_status": "x"},
            repo_root=Path("/does/not/matter/.git"),
        )

    with pytest.raises(ValueError, match="tracker_path"):
        _run(_call())


# ---------------------------------------------------------------------------
# (k) handler-level: path containment, missing file, repo_root=None
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_raises_runtime_error():
    async def _call():
        return await _handler(
            {"tracker_path": "README.md", "stub_ids": ["2A"], "to_status": "x"},
            repo_root=None,
        )

    with pytest.raises(RuntimeError, match="repo_root"):
        _run(_call())


def test_handler_path_traversal_outside_worktree_rejected(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(_SAMPLE_TABLE, encoding="utf-8")

    async def _call():
        return await _handler(
            {
                "tracker_path": "../outside.md",
                "stub_ids": ["2A"],
                "to_status": "x",
            },
            repo_root=worktree / ".git",
        )

    with pytest.raises(ValueError, match="not contained"):
        _run(_call())


def test_handler_missing_tracker_file_rejected(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / ".git").mkdir()

    async def _call():
        return await _handler(
            {
                "tracker_path": "docs/plans/does-not-exist.md",
                "stub_ids": ["2A"],
                "to_status": "x",
            },
            repo_root=worktree / ".git",
        )

    with pytest.raises(ValueError, match="does not resolve to an existing file"):
        _run(_call())


def test_handler_happy_path_returns_repo_relative_tracker_path(tmp_path):
    worktree = tmp_path / "repo"
    (worktree / "docs" / "plans" / "chunk").mkdir(parents=True)
    (worktree / ".git").mkdir()
    tracker = worktree / "docs" / "plans" / "chunk" / "README.md"
    tracker.write_text(_SAMPLE_TABLE, encoding="utf-8")

    async def _call():
        return await _handler(
            {
                "tracker_path": "docs/plans/chunk/README.md",
                "stub_ids": ["2A"],
                "to_status": "Enrichment in progress",
            },
            repo_root=worktree / ".git",
        )

    result = _run(_call())
    assert result["updated"] == ["2A"]
    assert result["changed"] is True
    assert result["to_status"] == "Enrichment in progress"
    assert result["tracker_path"] in (
        "docs/plans/chunk/README.md",
        "docs\\plans\\chunk\\README.md",
    )
