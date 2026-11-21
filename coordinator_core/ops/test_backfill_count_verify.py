"""Tests for coordinator_core.ops.backfill_count_verify (real tmp git repos)."""

from __future__ import annotations


import pytest

from coordinator_core.ops import backfill_count_verify as bcv

BLOCK = "## {day} — all-machines\n\n**Branch:** all refs\n**Commits:** {n} (range: aaaa..bbbb)\n**Backfilled:** yes\n"


def test_header_and_missing_dir_are_ignored(tmp_path):
    (tmp_path / "state" / "week-changelog").mkdir(parents=True)
    (tmp_path / "state" / "week-changelog" / "HEADER.md").write_text("**Commits:** 5\n")
    assert bcv.verify(str(tmp_path)) == ([], 0)
    assert bcv.main(["--repo", str(tmp_path / "nope")]) == 2
