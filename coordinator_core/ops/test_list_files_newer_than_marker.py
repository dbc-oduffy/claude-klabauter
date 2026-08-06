"""Tests for coordinator_core.ops.list_files_newer_than_marker — the
percolate.list_files_newer_than_marker RPC wrapper.

Covers the op-classification audit's contract for this op: `.percolate-ignore`-relative
mtime drift listing, capped at `limit`, silent (no error) when the marker is missing, and
a safe-no-op second invocation (AC7).

Negative-spec: this module's fixtures use only synthetic tmp_path trees — no persona
names, no live codenames, no consumer-home literals.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from coordinator_core.ops.list_files_newer_than_marker import (
    _list_files_newer_than_marker,
    list_files_newer_than_marker,
)


def _touch(path, *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))


class TestMarkerMissing:
    def test_missing_marker_returns_empty_and_flag(self, tmp_path):
        (tmp_path / "some_file.py").write_text("x", encoding="utf-8")

        result = list_files_newer_than_marker(str(tmp_path))

        assert result == {"files": [], "marker_missing": True}


class TestNewerThanMarker:
    def test_only_files_newer_than_marker_are_listed(self, tmp_path):
        base = time.time() - 1000
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        _touch(tmp_path / "older.py", mtime=base - 100)
        _touch(tmp_path / "newer.py", mtime=base + 100)
        _touch(tmp_path / "sub" / "newer_nested.py", mtime=base + 200)

        result = list_files_newer_than_marker(str(tmp_path))

        assert result["marker_missing"] is False
        assert result["files"] == ["newer.py", "sub/newer_nested.py"]

    def test_marker_itself_excluded_from_its_own_listing(self, tmp_path):
        base = time.time()
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        # Bump the marker's own mtime forward-in-comparison scenario is
        # irrelevant here -- the point is it must never appear in `files`
        # even if some pathological clock skew made it compare > itself.
        result = list_files_newer_than_marker(str(tmp_path))

        assert ".percolate-ignore" not in result["files"]

    def test_result_capped_at_limit(self, tmp_path):
        base = time.time() - 1000
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        for i in range(5):
            _touch(tmp_path / f"f{i}.py", mtime=base + 100 + i)

        result = list_files_newer_than_marker(str(tmp_path), limit=2)

        assert len(result["files"]) == 2
        assert result["files"] == sorted(result["files"])

    def test_default_limit_is_twenty(self, tmp_path):
        base = time.time() - 1000
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        for i in range(25):
            _touch(tmp_path / f"f{i:02d}.py", mtime=base + 100 + i)

        result = list_files_newer_than_marker(str(tmp_path))

        assert len(result["files"]) == 20


class TestIdempotency:
    def test_second_invocation_is_a_safe_no_op(self, tmp_path):
        base = time.time() - 1000
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        _touch(tmp_path / "newer.py", mtime=base + 100)

        first = list_files_newer_than_marker(str(tmp_path))
        second = list_files_newer_than_marker(str(tmp_path))

        assert first == second
        assert first == {"files": ["newer.py"], "marker_missing": False}

    def test_second_invocation_after_marker_missing_is_still_safe(self, tmp_path):
        (tmp_path / "some_file.py").write_text("x", encoding="utf-8")

        first = list_files_newer_than_marker(str(tmp_path))
        second = list_files_newer_than_marker(str(tmp_path))

        assert first == second == {"files": [], "marker_missing": True}


class TestOpHandler:
    def test_handler_requires_source_dir(self):
        with pytest.raises(ValueError, match="source_dir"):
            asyncio.run(_list_files_newer_than_marker({}))

    def test_handler_rejects_non_directory_source_dir(self, tmp_path):
        not_a_dir = tmp_path / "nope.txt"
        not_a_dir.write_text("x", encoding="utf-8")

        with pytest.raises(ValueError, match="not a directory"):
            asyncio.run(_list_files_newer_than_marker({"source_dir": str(not_a_dir)}))

    def test_handler_returns_wire_shape(self, tmp_path):
        base = time.time() - 1000
        _touch(tmp_path / ".percolate-ignore", mtime=base)
        _touch(tmp_path / "newer.py", mtime=base + 100)

        result = asyncio.run(
            _list_files_newer_than_marker({"source_dir": str(tmp_path), "limit": 5})
        )

        assert result == {"files": ["newer.py"], "marker_missing": False}
