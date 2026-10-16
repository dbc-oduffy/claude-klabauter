"""session.safe_commit_offer refuses a commit on a remote-backed main unless on_main_reason is given."""

from __future__ import annotations

import pytest

from coordinator_core.ops.session import safe_commit_offer as sco


def _repo(tmp_path, *, branch, remote):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    config = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        config += '[remote "origin"]\n\turl = https://example.invalid/r.git\n'
    (git / "config").write_text(config, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def reached(monkeypatch):
    calls = []

    def fake_commit(session_id, cwd, groups):
        calls.append(cwd)
        return sco._empty_commit_offer_report(
            session_id, [], status="empty", detail="stub"
        )

    monkeypatch.setattr(sco, "commit_session_offer", fake_commit)
    monkeypatch.setattr(sco, "_render_report", lambda report, root=None: "ok")
    for name in (
        "_log_excluded_diagnostic",
        "_log_dropped_groups_diagnostic",
        "_log_failed_groups_diagnostic",
    ):
        monkeypatch.setattr(sco, name, lambda *a, **k: None)
    return calls


def _call(repo, **extra):
    return sco._handler({"cwd": str(repo), "session_id": "s-1", **extra})


def test_refuses_on_remote_backed_main_and_commits_nothing(tmp_path, reached):
    repo = _repo(tmp_path, branch="main", remote=True)
    out = _call(repo)
    assert "workday-start-day-branch-resolve day-branch-assert" in out["error"]
    assert reached == []


def test_override_reason_admits(tmp_path, reached):
    repo = _repo(tmp_path, branch="main", remote=True)
    out = _call(repo, on_main_reason="release-index commit")
    assert "error" not in out
    assert len(reached) == 1


def test_remote_less_main_admits(tmp_path, reached):
    repo = _repo(tmp_path, branch="main", remote=False)
    assert "error" not in _call(repo)
    assert len(reached) == 1


def test_work_branch_with_remote_admits(tmp_path, reached):
    repo = _repo(tmp_path, branch="work/x/2026-10-16", remote=True)
    assert "error" not in _call(repo)
    assert len(reached) == 1
