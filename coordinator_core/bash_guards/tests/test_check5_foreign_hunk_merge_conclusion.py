"""A merge conclusion's incoming hunks are not a foreign hunk: while MERGE_HEAD
exists, a session-owned staged path the merge rewrote (HEAD vs MERGE_HEAD) is
not denied; every other recorded-hash disagreement still denies."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards.tests.test_check5_foreign_hunk import (
    _claim,
    _init_repo,
    _push_started_at_to_future,
)
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: str, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=root, check=check, capture_output=True,
        **no_console_creationflags(),
    )


def _w(root: str, name: str, text: str) -> None:
    (Path(root) / name).write_text(text, encoding="utf-8")


def _branch_point(tmp_path: Path):
    root = _init_repo(tmp_path)
    _w(root, "a.txt", "1\n2\n3\n4\n5\n6\n7\n8\n")
    _w(root, "b.txt", "b0\n")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    return root, base


def _session(root: str) -> str:
    sid = "my-sess"
    assert core.init(sid, cwd=root)
    _push_started_at_to_future(root, sid)
    return sid


def _deny_text(result) -> str:
    if result is None:
        return ""
    out = result["hookSpecificOutput"]
    return out["permissionDecisionReason"] if out["permissionDecision"] == "deny" else ""


def test_incoming_rewrite_during_merge_is_not_a_foreign_hunk(tmp_path):
    root, base = _branch_point(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    _w(root, "a.txt", "1\n2\n3\n4\n5\n6\n7\n8\nside\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", base)
    _w(root, "b.txt", "b1\n")
    _git(root, "commit", "-qam", "main")
    _git(root, "merge", "--no-commit", "--no-ff", "side")
    sid = _session(root)
    _claim(root, sid, "a.txt", content_hash="0" * 64)
    result = dispatch_checks.check_validate_commit('git commit -m "merge"', sid, cwd=root)
    assert "foreign hunk" not in _deny_text(result).lower()


def test_untouched_by_merge_path_still_denies(tmp_path):
    root, base = _branch_point(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    _w(root, "a.txt", "1\n2\n3\n4\n5\n6\n7\n8\nside\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", base)
    _w(root, "b.txt", "b1\n")
    _git(root, "commit", "-qam", "main")
    _git(root, "merge", "--no-commit", "--no-ff", "side")
    _w(root, "c.txt", "foreign edit\n")
    _git(root, "add", "c.txt")
    sid = _session(root)
    _claim(root, sid, "c.txt", content_hash="0" * 64)
    result = dispatch_checks.check_validate_commit('git commit -m "merge"', sid, cwd=root)
    reason = _deny_text(result)
    assert "foreign hunk" in reason.lower() and "c.txt" in reason


def test_no_merge_head_denies_as_before(tmp_path):
    root, _ = _branch_point(tmp_path)
    _w(root, "a.txt", "changed\n")
    _git(root, "add", "a.txt")
    sid = _session(root)
    _claim(root, sid, "a.txt", content_hash="0" * 64)
    result = dispatch_checks.check_validate_commit('git commit -m "x"', sid, cwd=root)
    assert "foreign hunk" in _deny_text(result).lower()


def test_conflict_resolved_file_is_not_a_foreign_hunk(tmp_path):
    root, base = _branch_point(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    _w(root, "a.txt", "side\n2\n3\n4\n5\n6\n7\n8\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", base)
    _w(root, "a.txt", "main\n2\n3\n4\n5\n6\n7\n8\n")
    _git(root, "commit", "-qam", "main")
    assert _git(root, "merge", "--no-commit", "--no-ff", "side", check=False).returncode != 0
    _w(root, "a.txt", "both\n2\n3\n4\n5\n6\n7\n8\n")
    _git(root, "add", "a.txt")
    sid = _session(root)
    _claim(root, sid, "a.txt", content_hash="0" * 64)
    result = dispatch_checks.check_validate_commit('git commit -m "merge"', sid, cwd=root)
    assert "foreign hunk" not in _deny_text(result).lower()
