"""resolve_session_start_time's no-claim-dir ladder spends one `log` per
candidate base and never a separate `merge-base` probe."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_memo_lifecycle as ml

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


def test_ladder_issues_no_merge_base_and_resolves_branch_point(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "work")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "own")
    own = _git(tmp_path, "log", "-1", "--format=%cI").stdout.strip()

    calls: list[list[str]] = []
    real = ml._run_git

    def _spy(root, args):
        calls.append(list(args))
        return real(root, args)

    monkeypatch.setattr(ml, "_run_git", _spy)
    start = ml.resolve_session_start_time(tmp_path, "no-claim-dir-sid")

    assert start is not None and start.isoformat() == ml._parse_iso(own).isoformat()
    assert not any(c[0] == "merge-base" for c in calls), calls
    assert len(calls) <= 4, calls
