"""Tests for coordinator_core.ops.verify_no_console_flash.

Covers the fail-closed contract on an unreadable candidate file: an
unreadable file must never be silently reported as "0 matches" / clean —
see the module's `_grep_file` docstring and the Tier 2 fence in `main`.
"""
from __future__ import annotations

import io
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from coordinator_core.ops import verify_no_console_flash as vncf


def _write(tmp_path, rel: str, content: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_grep_file_returns_hits_for_readable_file(tmp_path):
    path = _write(tmp_path, "clean.sh", "python3 -c 'print(1)'\n")
    hits = vncf._grep_file(path, vncf._PY_NODE_MATCH)
    assert hits
    assert hits[0].startswith(f"{path}:1:")


def test_grep_file_raises_on_unreadable_file(tmp_path):
    """An unreadable file must raise, not silently return [] (which would
    be indistinguishable from 'read fine, zero matches')."""
    path = _write(tmp_path, "unreadable.sh", "python3 -c 'print(1)'\n")
    os.chmod(path, 0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as root or on a platform where chmod 0o000 doesn't block reads")
        with pytest.raises(OSError):
            vncf._grep_file(path, vncf._PY_NODE_MATCH)
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_scan_collects_unreadable_paths_and_still_scans_readable_siblings(tmp_path):
    good = _write(tmp_path, "good.sh", "python3 -c 'print(1)'\n")
    bad = _write(tmp_path, "bad.sh", "python3 -c 'print(2)'\n")
    os.chmod(bad, 0o000)
    try:
        if os.access(bad, os.R_OK):
            pytest.skip("running as root or on a platform where chmod 0o000 doesn't block reads")
        hits, unreadable = vncf._scan(
            str(tmp_path), vncf._PY_NODE_INCLUDE_GLOBS, vncf._PY_NODE_INCLUDE_EXACT,
            vncf._PY_NODE_MATCH,
        )
        assert unreadable == [bad]
        assert any(h.startswith(f"{good}:") for h in hits)
        assert not any(h.startswith(f"{bad}:") for h in hits)
    finally:
        os.chmod(bad, stat.S_IRUSR | stat.S_IWUSR)


def test_main_fails_closed_when_a_candidate_file_is_unreadable(tmp_path, monkeypatch):
    """The overall gate must report non-clean (exit 1) when a target file
    could not be scanned -- never silently 'OK' because it found 0 matches
    in files it happened to be able to read."""
    coord_root = tmp_path / "coordinator-claude"
    bin_dir = coord_root / "bin"
    bin_dir.mkdir(parents=True)
    unreadable_path = bin_dir / "locked.sh"
    unreadable_path.write_text("echo hello\n", encoding="utf-8")
    os.chmod(str(unreadable_path), 0o000)
    try:
        if os.access(str(unreadable_path), os.R_OK):
            pytest.skip("running as root or on a platform where chmod 0o000 doesn't block reads")

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = vncf.main([str(tmp_path)])

        assert rc == 1
        assert str(unreadable_path) in err.getvalue()
        assert "OK: all console-spawning invocations" not in out.getvalue()
    finally:
        os.chmod(str(unreadable_path), stat.S_IRUSR | stat.S_IWUSR)


def test_main_clean_when_no_violations_and_all_files_readable(tmp_path):
    coord_root = tmp_path / "coordinator-claude"
    bin_dir = coord_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "clean.sh").write_text("echo hello\n", encoding="utf-8")

    out = io.StringIO()
    with redirect_stdout(out):
        rc = vncf.main([str(tmp_path)])

    assert rc == 0
    assert "OK: all console-spawning invocations are suppressed or explicitly allowlisted" in out.getvalue()
