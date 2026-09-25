"""`commit.commit_paths` attaches a `Session-Id:` trailer when the message
carries none and a sid resolves -- the fix for dispatched wave-commit agents
calling `commit_paths` in-process and landing untrailered commits
(`state/bug-backlog/2026-09-24-wave-commit-agent-called-commit-paths-in-
fc03df470bcc.yaml`)."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _last_message(repo):
    return _git(repo, "log", "-1", "--format=%B").stdout


def test_message_without_trailer_gets_exactly_one_when_sid_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_core, "resolve_session_id", lambda cwd=None: "11111111-2222-4333-8444-555555555555"
    )
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    gcommit.commit_paths(repo, ["new.txt"], "add new")

    message = _last_message(repo)
    lines = [l for l in message.splitlines() if l.startswith("Session-Id:")]
    assert lines == ["Session-Id: 11111111-2222-4333-8444-555555555555"], message


def test_message_with_trailer_already_present_keeps_exactly_one(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_core, "resolve_session_id", lambda cwd=None: "11111111-2222-4333-8444-555555555555"
    )
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    gcommit.commit_paths(
        repo, ["new.txt"], "add new\n\nSession-Id: 99999999-2222-4333-8444-555555555555\n"
    )

    message = _last_message(repo)
    lines = [l for l in message.splitlines() if l.startswith("Session-Id:")]
    assert lines == ["Session-Id: 99999999-2222-4333-8444-555555555555"], message


def test_unresolvable_sid_leaves_message_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None: "")
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    gcommit.commit_paths(repo, ["new.txt"], "add new")

    message = _last_message(repo)
    assert "Session-Id:" not in message
    assert message.strip() == "add new"
