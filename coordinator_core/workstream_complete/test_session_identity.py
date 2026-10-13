
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import session_identity

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(*args: str, cwd, input: str | None = None) -> subprocess.CompletedProcess:  # noqa: A002 - shadow ok, local helper
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        input=input,
        **no_console_creationflags(),
    )


def _init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_with_message(path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=path)
    proc = _git("commit", "-q", "-F", "-", cwd=path, input=message)
    if proc.returncode != 0:
        pytest.skip(f"git unavailable/failed committing fixture: {proc.stderr}")
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    sha = _commit_with_message(root, "seed.txt", "seed\n", "seed\n")
    if not sha:
        pytest.skip("git unavailable — cannot build a fixture repo with history")
    return root


def test_single_trailer_resolves(repo):
    _commit_with_message(
        repo,
        "a.txt",
        "a\n",
        "chunk A\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n",
    )
    result = session_identity.session_deliverable_ids(repo, "sid-1")
    assert result.ok
    assert result.deliverable_ids == frozenset({"dlv-alpha"})
    assert len(result.commits) == 1
    assert result.commits[0].source == "trailer"


def test_several_commits_share_one_trailer(repo):
    _commit_with_message(
        repo, "a.txt", "a\n", "chunk A\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n"
    )
    _commit_with_message(
        repo, "b.txt", "b\n", "chunk B\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n"
    )
    result = session_identity.session_deliverable_ids(repo, "sid-1")
    assert result.ok
    assert result.deliverable_ids == frozenset({"dlv-alpha"})
    assert len(result.commits) == 2
    assert all(c.deliverable_id == "dlv-alpha" for c in result.commits)


def test_conflicting_trailers_are_representable_not_first_wins(repo):
    _commit_with_message(
        repo, "a.txt", "a\n", "chunk A\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n"
    )
    _commit_with_message(
        repo, "b.txt", "b\n", "chunk B\n\nSession-Id: sid-1\nDeliverable-Id: dlv-beta\n"
    )
    result = session_identity.session_deliverable_ids(repo, "sid-1")
    assert result.ok
    assert result.deliverable_ids == frozenset({"dlv-alpha", "dlv-beta"})
    assert len(result.commits) == 2
    assert "CONFLICTING" in result.reason


def test_zero_commits_for_session_is_ok_but_empty(repo):
    _commit_with_message(
        repo, "a.txt", "a\n", "chunk A\n\nSession-Id: sid-other\nDeliverable-Id: dlv-alpha\n"
    )
    result = session_identity.session_deliverable_ids(repo, "sid-1")
    assert result.ok
    assert result.deliverable_ids == frozenset()
    assert result.commits == ()
    assert "sid-1" in result.reason


def test_trailer_demoted_outside_last_paragraph_uses_body_fallback(repo):
    message = "chunk A\n\nDeliverable-Id: dlv-gamma\n\nSession-Id: sid-1\n"
    _commit_with_message(repo, "a.txt", "a\n", message)

    trailer_check = _git(
        "log", "-1", "--format=%(trailers:key=Deliverable-Id,valueonly)", cwd=repo
    )
    assert trailer_check.stdout.strip() == ""

    result = session_identity.session_deliverable_ids(repo, "sid-1")
    assert result.ok
    assert result.deliverable_ids == frozenset({"dlv-gamma"})
    assert len(result.commits) == 1
    assert result.commits[0].source == "body-fallback"


def test_empty_session_id_returns_ok_empty_without_git_spawn(repo, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("git must not be spawned for an empty session_id")

    monkeypatch.setattr(session_identity.subprocess, "run", _boom)
    result = session_identity.session_deliverable_ids(repo, "")
    assert result.ok
    assert result.deliverable_ids == frozenset()
    assert result.commits == ()


def test_git_failure_reports_ok_false(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = session_identity.session_deliverable_ids(not_a_repo, "sid-1")
    assert result.ok is False
    assert result.deliverable_ids == frozenset()


def _history_for(repo, sid, floor, monkeypatch):
    from coordinator_core.workstream_complete import session_history

    monkeypatch.setattr(session_history, "_resolve_floor", lambda root, s: (floor, ""))
    return session_history.read_session_history(repo, sid)


def _forbid_spawn(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("a handed history must not spawn")

    monkeypatch.setattr(session_identity.subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def test_injected_history_matches_walk_and_spawns_nothing(repo, monkeypatch):
    floor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _commit_with_message(
        repo, "a.txt", "a\n", "chunk A\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n"
    )
    _commit_with_message(
        repo, "p.txt", "p\n", "peer\n\nSession-Id: sid-peer\nDeliverable-Id: dlv-peer\n"
    )
    _commit_with_message(
        repo, "g.txt", "g\n", "chunk G\n\nDeliverable-Id: dlv-gamma\n\nSession-Id: sid-1\n"
    )
    walked = session_identity.session_deliverable_ids(repo, "sid-1")
    history = _history_for(repo, "sid-1", floor, monkeypatch)
    assert history.resolved

    _forbid_spawn(monkeypatch)
    injected = session_identity.session_deliverable_ids(repo, "sid-1", history=history)

    assert injected.ok and walked.ok
    assert injected.deliverable_ids == walked.deliverable_ids == frozenset({"dlv-alpha", "dlv-gamma"})
    assert {(c.sha, c.deliverable_id) for c in injected.commits} == {
        (c.sha, c.deliverable_id) for c in walked.commits
    }
    assert injected.reason == walked.reason


def test_injected_history_excludes_merges(repo, monkeypatch):
    from coordinator_core.workstream_complete.session_history import (
        SessionCommit,
        SessionHistory,
    )

    def _commit(sha, parents):
        return SessionCommit(sha, parents, 0, "", "s", ("sid-1",), "dlv-x", ())

    history = SessionHistory(
        "sid-1", "f", "h", (_commit("a" * 40, ("f",)), _commit("b" * 40, ("a" * 40, "c" * 40))), True, ""
    )
    result = session_identity.session_deliverable_ids(repo, "sid-1", history=history)
    assert [c.sha for c in result.commits] == ["a" * 40]


def test_unresolved_history_is_degraded_not_a_walk(repo, monkeypatch):
    from coordinator_core.workstream_complete.session_history import SessionHistory

    _commit_with_message(
        repo, "a.txt", "a\n", "chunk A\n\nSession-Id: sid-1\nDeliverable-Id: dlv-alpha\n"
    )
    _forbid_spawn(monkeypatch)
    history = SessionHistory("sid-1", None, None, (), False, "floor-missing")
    result = session_identity.session_deliverable_ids(repo, "sid-1", history=history)
    assert result.ok is False
    assert result.reason.endswith("floor-missing")
    assert result.deliverable_ids == frozenset()
    assert result.commits == ()
