"""
coordinator_core.ops.ceremony.tests.test_commit_v2_retry_reports_landed

Purpose: a `ceremony.commit_v2` retry after a client-side timeout. The first call lands, the caller
never reads the reply, the retry finds nothing to commit. `committed: false` alone reads as failure;
the refusal must name the commit that landed, and only when it is this caller's own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _params(**extra) -> dict:
    return {"paths": ["a.txt"], "message": "add a\n", "session_id": _SID, **extra}


def test_retry_after_landed_call_names_the_landed_sha(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    first = _call(repo, _params())
    assert first["committed"] is True

    retry = _call(repo, _params())

    assert retry["committed"] is False
    assert retry["nothing_to_commit"] is True
    assert retry["already_landed_sha"] == first["sha"]
    assert first["sha"] in retry["error"]


def test_zero_delta_with_a_different_message_names_no_commit(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert _call(repo, _params())["committed"] is True

    other = _call(repo, _params(message="something else\n"))

    assert other["nothing_to_commit"] is True
    assert "already_landed_sha" not in other


def test_zero_delta_under_another_session_names_no_commit(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert _call(repo, _params())["committed"] is True

    peer = _call(repo, _params(session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"))

    assert peer["nothing_to_commit"] is True
    assert "already_landed_sha" not in peer


def test_an_old_matching_commit_is_not_adopted(repo, monkeypatch):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert _call(repo, _params())["committed"] is True
    real = commit_v2.time.time
    monkeypatch.setattr(commit_v2.time, "time", lambda: real() + commit_v2._RETRY_LANDED_WINDOW_SECS + 60)

    late = _call(repo, _params())

    assert late["nothing_to_commit"] is True
    assert "already_landed_sha" not in late
