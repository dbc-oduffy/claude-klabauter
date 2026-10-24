"""A merge conclusion's incoming paths outside the touch list are not a sweep: no SCOPE
warning and no strict deny; a staged non-incoming peer path still warns or denies."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _dc_commit, dispatch_checks
from coordinator_core.bash_guards.tests.test_check5_foreign_hunk import _claim
from coordinator_core.bash_guards.tests.test_check5_foreign_hunk_merge_conclusion import (
    _branch_point,
    _deny_text,
    _enveloped,
    _git,
    _session,
    _w,
)
from coordinator_core.session import core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _merge_in_progress(tmp_path):
    root, base = _branch_point(tmp_path)
    _git(root, "checkout", "-q", "-b", "side")
    _w(root, "a.txt", "1\n2\n3\n4\n5\n6\n7\n8\nside\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", base)
    _w(root, "b.txt", "b1\n")
    _git(root, "commit", "-qam", "main")
    _git(root, "merge", "--no-commit", "--no-ff", "side")
    return root


def _text(result) -> str:
    if result is None:
        return ""
    out = result["hookSpecificOutput"]
    return (out.get("permissionDecisionReason") or "") + (out.get("additionalContext") or "")


def _peer_owns(root: str, path: str) -> None:
    assert core.init("peer-sess", cwd=root)
    _claim(root, "peer-sess", path, content_hash="1" * 64)


def test_merge_incoming_path_outside_touch_list_is_not_a_sweep(tmp_path):
    root = _merge_in_progress(tmp_path)
    sid = _session(root)
    _claim(root, sid, "unrelated.txt", content_hash="0" * 64)
    text = _text(_enveloped(dispatch_checks.check_validate_commit)('git commit -m "merge"', sid, cwd=root))
    assert "a.txt" not in text
    assert "BLOCKED" not in text


def test_non_incoming_peer_path_staged_mid_merge_still_flagged(tmp_path):
    root = _merge_in_progress(tmp_path)
    _w(root, "c.txt", "peer edit\n")
    _git(root, "add", "c.txt")
    sid = _session(root)
    _claim(root, sid, "unrelated.txt", content_hash="0" * 64)
    _peer_owns(root, "c.txt")
    text = _text(_enveloped(dispatch_checks.check_validate_commit)('git commit -m "merge"', sid, cwd=root))
    assert "c.txt" in text
    assert "a.txt" not in text


def test_no_merge_in_progress_never_runs_the_diff_probe(tmp_path, monkeypatch):
    root, _ = _branch_point(tmp_path)
    _w(root, "a.txt", "changed\n")
    _git(root, "add", "a.txt")
    sid = _session(root)
    _claim(root, sid, "unrelated.txt", content_hash="0" * 64)
    calls = []
    real = _dc_commit._run_git

    def counting(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(_dc_commit, "_run_git", counting)
    text = _text(_enveloped(dispatch_checks.check_validate_commit)('git commit -m "x"', sid, cwd=root))
    assert "SCOPE" in text and "a.txt" in text
    assert not any(c[:2] == ["diff", "--name-only"] and "MERGE_HEAD" in c for c in calls)
