"""`commit_v2` carries (opt-in) and always reports the committing session's own left-behind paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER = "olb-peer"
SELF = "olb-self"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    (root / "b.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    (root / "new.py").write_text("n\n", encoding="utf-8")
    (root / "b.py").write_text("v2\n", encoding="utf-8")
    session_core.init(SELF, cwd=str(root))
    for name in ("a.py", "new.py", "b.py"):
        session_scope.touch(SELF, name, cwd=str(root))
    return root


def _files(repo: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())


def test_default_commits_only_the_pathspec_and_names_the_rest_left(repo):
    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert _files(repo) == {"a.py"}
    assert result["own_left_behind"] == {"carried": [], "left": ["b.py", "new.py"]}


def test_carry_lands_own_dirty_paths_in_the_same_commit(repo):
    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert result["committed"] is True
    assert _files(repo) == {"a.py", "b.py", "new.py"}
    assert result["own_left_behind"] == {"carried": ["b.py", "new.py"], "left": []}


def test_live_peer_held_path_is_never_carried(repo):
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "new.py", cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert _files(repo) == {"a.py", "b.py"}
    assert result["own_left_behind"] == {"carried": ["b.py"], "left": ["new.py"]}


def test_dead_peer_does_not_contest(repo, monkeypatch):
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "new.py", cwd=str(repo))
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live", lambda sid, cwd=None: False
    )

    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert _files(repo) == {"a.py", "b.py", "new.py"}
    assert result["own_left_behind"]["left"] == []


def test_unanswerable_peer_lookup_counts_as_contested(repo, monkeypatch):
    real = commit_v2.session_claim_index.lookup
    calls = []

    def flaky(paths, cwd=None):
        calls.append(list(paths))
        if len(calls) > 1:
            raise RuntimeError("boom")
        return real(paths, cwd=cwd)

    monkeypatch.setattr(commit_v2.session_claim_index, "lookup", flaky)

    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert _files(repo) == {"a.py"}
    assert "own_left_behind" not in result


def test_non_bool_param_is_refused(repo):
    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": "yes"})

    assert result["committed"] is False
    assert "carry_own_left_behind" in result["error"]


def test_clean_session_reply_has_no_key(repo):
    result = _call(
        repo, {"paths": ["a.py", "b.py", "new.py"], "message": "m", "carry_own_left_behind": True}
    )

    assert "own_left_behind" not in result


def test_a_session_git_scratch_touch_is_never_carried(repo):
    scratch = f".git/coordinator-sessions/{SELF}/review-targets.txt"
    (repo / scratch).parent.mkdir(parents=True, exist_ok=True)
    (repo / scratch).write_text("a.py\n", encoding="utf-8")
    session_scope.touch(SELF, scratch, cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert result["committed"] is True, result
    assert _files(repo) == {"a.py", "b.py", "new.py"}


def test_a_gitignored_untracked_left_behind_path_is_not_carried_under_a_supplied_ignore_set(repo):
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-q", "-m", "ignore"], repo)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "x.txt").write_text("s\n", encoding="utf-8")
    session_scope.touch(SELF, "scratch/x.txt", cwd=str(repo))

    result = _call(
        repo,
        {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True, "ignored_untracked": []},
    )

    assert result["committed"] is True, result
    assert _files(repo) == {"a.py", "b.py", "new.py"}
    assert _git(["ls-files", "scratch"], repo).strip() == ""
