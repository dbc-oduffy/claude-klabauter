"""Regression test for `_needs_copy`'s honest-change-reporting fix (task
brief "Deliverable 3 — honest change reporting").

`_needs_copy` used to treat "src mtime > dst mtime" alone as sufficient
proof a copy was needed. Source rows are materialized from a committed ref
via `git archive` + extraction (`publish.py::_extract_git_archive`), so
every source file's mtime is the extraction timestamp — always "now",
always newer than any prior destination file regardless of whether the
underlying bytes changed. That made a full mirror publish log a wall of
`UPDATE:` lines with zero tracked git diff at the destination. This test
pins the fix: byte-identical content with a much newer source mtime must
report "no copy needed".

Loaded via the `percolate` package (§ sibling `test_publish_sync_renamed_
dir_exemption.py`'s own note on why a bare `spec_from_file_location` can't
resolve `publish_sync.py`'s relative `.ignore` import).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def test_needs_copy_false_when_bytes_identical_despite_newer_src_mtime(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("payload", encoding="utf-8")
    time.sleep(0.05)
    src.write_text("payload", encoding="utf-8")
    assert src.stat().st_mtime >= dst.stat().st_mtime
    assert publish_sync._needs_copy(src, dst) is False


def test_needs_copy_true_when_bytes_differ(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("old", encoding="utf-8")
    src.write_text("new", encoding="utf-8")
    assert publish_sync._needs_copy(src, dst) is True


def test_needs_copy_true_when_dst_missing(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("payload", encoding="utf-8")
    assert publish_sync._needs_copy(src, tmp_path / "absent.txt") is True


def test_needs_copy_false_for_identical_zero_byte_files(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("", encoding="utf-8")
    dst.write_text("", encoding="utf-8")
    assert publish_sync._needs_copy(src, dst) is False
