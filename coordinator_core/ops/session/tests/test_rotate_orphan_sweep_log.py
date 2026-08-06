"""Tests for coordinator_core.ops.session.rotate_orphan_sweep_log.

Covers the A2 settlement contract (docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A2):
  - >4-line file → rotated back to its 4-line header, tail surfaced verbatim.
  - ≤4-line file → {rotated: false} no-op, bytes untouched.
  - Absent file → healthy no-op (header_lines_kept: 0), file NOT created.
  - Double invocation (CC-4/AC7): second identical call is the documented
    {rotated: false} no-op — the ≤4-line predicate is the idempotency mechanism.
  - Setup errors: repo_root None, D3 mismatch → exit_code:1, nothing written.

All git activity is confined to the session_repo tmp_path fixture repo — no
mutation of the working repository (conftest.py owns the throwaway git init).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core.ops.session.rotate_orphan_sweep_log import (
    _handler,
    _rotate_text,
)

# Mirrors boot_sweep._append_warn_marker's create-with-header branch — the
# 4-line header (title, blank, purpose, blank) the rotation must preserve.
_HEADER = (
    "# Orphan sweep notes\n"
    "\n"
    "Archive events from session.boot_sweep. "
    "/workday-start Step 0.8 reads and rotates.\n"
    "\n"
)

_MARKERS = (
    "- 2026-07-22T10:00:00Z | archived 2026-07-20_a.md (consumed_by=sid-1, no deployment_state change)\n"
    "- 2026-07-22T10:00:01Z | skipped 2026-07-21_b.md (consumed_by=sid-2, awaiting human adjudication or DR-084 continued semantics)\n"
    "- 2026-07-22T10:00:02Z | archived 2026-07-22_c.md (consumed_by=sid-3, succeeded by a live successor, deployment_state stamped shipped)\n"
)


def _marker_path(session_repo) -> Path:
    return session_repo.root / "tasks" / "orphan-sweep-notes.md"


def _seed(session_repo, text: str) -> Path:
    path = _marker_path(session_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _invoke(session_repo, params: dict | None = None) -> dict:
    return asyncio.run(_handler(params or {}, repo_root=session_repo.common_dir))


# ---------------------------------------------------------------------------
# Rotation path
# ---------------------------------------------------------------------------


def test_rotates_tail_and_keeps_four_line_header(session_repo):
    path = _seed(session_repo, _HEADER + _MARKERS)

    result = _invoke(session_repo)

    assert result["exit_code"] == 0
    assert result["rotated"] is True
    assert result["header_lines_kept"] == 4
    assert result["tail_surfaced"] == [
        line for line in _MARKERS.splitlines()
    ]
    # Disk truth: exactly the header bytes survive.
    assert path.read_text(encoding="utf-8") == _HEADER


def test_rotation_preserves_first_four_lines_verbatim(session_repo):
    # Non-standard content: rotation keeps the first 4 lines byte-for-byte,
    # whatever they are — it never re-synthesizes the boot_sweep header.
    custom = "line1\nline2\nline3\nline4\nmarker-a\nmarker-b"
    path = _seed(session_repo, custom)

    result = _invoke(session_repo)

    assert result["rotated"] is True
    assert result["tail_surfaced"] == ["marker-a", "marker-b"]
    assert path.read_text(encoding="utf-8") == "line1\nline2\nline3\nline4\n"


def test_five_line_boundary_rotates(session_repo):
    path = _seed(session_repo, _HEADER + "- one marker\n")

    result = _invoke(session_repo)

    assert result["rotated"] is True
    assert result["tail_surfaced"] == ["- one marker"]
    assert path.read_text(encoding="utf-8") == _HEADER


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------


def test_header_only_file_is_noop(session_repo):
    path = _seed(session_repo, _HEADER)

    result = _invoke(session_repo)

    assert result["exit_code"] == 0
    assert result["rotated"] is False
    assert result["tail_surfaced"] == []
    assert result["header_lines_kept"] == 4
    assert path.read_text(encoding="utf-8") == _HEADER


def test_short_file_is_noop_and_untouched(session_repo):
    path = _seed(session_repo, "just\ntwo lines\n")

    result = _invoke(session_repo)

    assert result["rotated"] is False
    assert result["header_lines_kept"] == 2
    assert path.read_text(encoding="utf-8") == "just\ntwo lines\n"


def test_absent_file_is_healthy_noop_and_not_created(session_repo):
    result = _invoke(session_repo)

    assert result["exit_code"] == 0
    assert result["rotated"] is False
    assert result["tail_surfaced"] == []
    assert result["header_lines_kept"] == 0
    assert not _marker_path(session_repo).exists()


# ---------------------------------------------------------------------------
# CC-4 / AC7: double invocation
# ---------------------------------------------------------------------------


def test_double_invocation_second_call_is_noop(session_repo):
    path = _seed(session_repo, _HEADER + _MARKERS)

    first = _invoke(session_repo)
    assert first["rotated"] is True

    second = _invoke(session_repo)
    assert second["exit_code"] == 0
    assert second["rotated"] is False
    assert second["tail_surfaced"] == []
    assert second["header_lines_kept"] == 4
    assert path.read_text(encoding="utf-8") == _HEADER


def test_rerun_after_fresh_appends_rotates_only_new_tail(session_repo):
    # Rotation → boot_sweep appends again → next rotation surfaces ONLY the
    # new markers (the previous tail was handed off in the first result).
    path = _seed(session_repo, _HEADER + _MARKERS)
    _invoke(session_repo)

    with path.open("a", encoding="utf-8") as fh:
        fh.write("- new marker after rotation\n")

    result = _invoke(session_repo)
    assert result["rotated"] is True
    assert result["tail_surfaced"] == ["- new marker after rotation"]
    assert path.read_text(encoding="utf-8") == _HEADER


# ---------------------------------------------------------------------------
# Setup errors
# ---------------------------------------------------------------------------


def test_repo_root_none_is_setup_error():
    result = asyncio.run(_handler({}, repo_root=None))
    assert result["exit_code"] == 1
    assert result["rotated"] is False
    assert "repo_root" in result["error"]


def test_d3_mismatch_is_setup_error_and_writes_nothing(session_repo, tmp_path):
    path = _seed(session_repo, _HEADER + _MARKERS)
    other = tmp_path / "unrelated"
    other.mkdir()

    result = _invoke(session_repo, params={"repo_root": str(other)})

    assert result["exit_code"] == 1
    assert result["rotated"] is False
    assert path.read_text(encoding="utf-8") == _HEADER + _MARKERS


# ---------------------------------------------------------------------------
# Pure rotation core
# ---------------------------------------------------------------------------


def test_rotate_text_handles_missing_trailing_newline():
    new_text, tail = _rotate_text("a\nb\nc\nd\ne")
    assert new_text == "a\nb\nc\nd\n"
    assert tail == ["e"]


def test_rotate_text_empty_snapshot_is_noop():
    new_text, tail = _rotate_text("")
    assert new_text == ""
    assert tail == []
