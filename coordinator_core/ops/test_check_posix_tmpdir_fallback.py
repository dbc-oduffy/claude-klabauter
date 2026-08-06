"""Tests for coordinator_core.ops.check_posix_tmpdir_fallback.

Covers both detected AST shapes (two-arg `.get()` default, `or`-fallback),
the negative case (`tempfile.gettempdir()` never flags), and a real-tree
wiring check that this repo's own tracked sources are currently clean
(the 2026-07-28 dispatch that authored this guard also fixed both
pre-existing true positives it was measured against).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops.check_posix_tmpdir_fallback import (  # noqa: E402
    scan,
    scan_source,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root)] + list(args),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit_all(repo: Path, message: str = "seed") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# Two-arg `.get(name, "/tmp")` shape.
# ---------------------------------------------------------------------------

def test_scan_source_detects_two_arg_default():
    src = 'import os\ntmp_dir = os.environ.get("TMPDIR", "/tmp")\n'
    hits = scan_source(src)
    assert hits == [(2, "two_arg_default")]


def test_scan_source_detects_two_arg_default_for_temp_and_tmp():
    src = (
        'import os\n'
        'a = os.environ.get("TEMP", "/tmp")\n'
        'b = os.environ.get("TMP", "/tmp")\n'
    )
    hits = scan_source(src)
    assert [k for _, k in hits] == ["two_arg_default", "two_arg_default"]


# ---------------------------------------------------------------------------
# `.get(name) or "/tmp"` shape.
# ---------------------------------------------------------------------------

def test_scan_source_detects_or_fallback():
    src = 'import os\nstate_dir = os.environ.get("TMPDIR") or "/tmp"\n'
    hits = scan_source(src)
    assert hits == [(2, "or_fallback")]


# ---------------------------------------------------------------------------
# `os.getenv(...)` spelling (Review: code-reviewer Finding 1, 2026-07-28 —
# functionally identical to `os.environ.get(...)` and at least as common;
# the original matcher missed it entirely).
# ---------------------------------------------------------------------------

def test_scan_source_detects_getenv_two_arg_default():
    src = 'import os\ntmp_dir = os.getenv("TMPDIR", "/tmp")\n'
    hits = scan_source(src)
    assert hits == [(2, "two_arg_default")]


def test_scan_source_detects_getenv_or_fallback():
    src = 'import os\nstate_dir = os.getenv("TMPDIR") or "/tmp"\n'
    hits = scan_source(src)
    assert hits == [(2, "or_fallback")]


def test_scan_source_detects_bare_getenv_alias_import():
    src = 'from os import getenv\ntmp_dir = getenv("TMPDIR", "/tmp")\n'
    hits = scan_source(src)
    assert hits == [(2, "two_arg_default")]


# ---------------------------------------------------------------------------
# Negative case: tempfile.gettempdir() is never flagged.
# ---------------------------------------------------------------------------

def test_scan_source_ignores_tempfile_gettempdir():
    src = (
        'import tempfile\n'
        'tmp_dir = tempfile.gettempdir()\n'
        'fd, p = __import__("tempfile").mkstemp(dir=tempfile.gettempdir())\n'
    )
    assert scan_source(src) == []


def test_scan_source_ignores_unrelated_env_get_calls():
    src = 'import os\nx = os.environ.get("HOME", "/root")\n'
    assert scan_source(src) == []


def test_scan_source_ignores_unrelated_or_fallback():
    src = 'import os\nx = os.environ.get("HOME") or "/root"\n'
    assert scan_source(src) == []


# ---------------------------------------------------------------------------
# scan() — fixture-repo wiring (git ls-files enumeration).
# ---------------------------------------------------------------------------

def test_scan_detects_violation_in_fixture_repo(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "tool.py"
    victim.write_text('import os\ntmp_dir = os.environ.get("TMPDIR", "/tmp")\n')
    _commit_all(repo)

    result = scan(repo)
    assert len(result) == 1
    assert result[0].relpath == "tool.py"
    assert result[0].lineno == 2


def test_scan_clean_fixture_repo_has_no_violations(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "tool.py"
    victim.write_text('import tempfile\ntmp_dir = tempfile.gettempdir()\n')
    _commit_all(repo)

    assert scan(repo) == []


def test_scan_ignores_untracked_files(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "committed.py").write_text("x = 1\n")
    _commit_all(repo)
    (repo / "scratch.py").write_text(
        'import os\ntmp_dir = os.environ.get("TMPDIR", "/tmp")\n'
    )

    assert scan(repo) == []


# ---------------------------------------------------------------------------
# Real-tree wiring: this repo's own tracked sources are clean.
# ---------------------------------------------------------------------------

def test_real_tree_has_no_posix_tmpdir_fallback_violations():
    result = scan(_REPO_ROOT)
    assert result == [], (
        "New POSIX-only tempdir-fallback violation(s) found; use "
        "tempfile.gettempdir() instead: "
        + ", ".join(f"{v.relpath}:{v.lineno}" for v in result)
    )
