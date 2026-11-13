"""
coordinator_core.post_run.tests.test_commit_step

Purpose: `commit_scoped` lands exactly the run's touched + transform-output + evidence paths in one
scoped commit and leaves a peer's unrelated dirty file dirty and unstaged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.post_run.commit_step import commit_scoped, scoped_paths
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _porcelain(root: Path) -> dict:
    out = {}
    for line in _git(["status", "--porcelain", "-uall"], root).splitlines():
        if not line[3:].startswith(".coordinator-local/"):
            out[line[3:]] = line[:2]
    return out


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "work/test"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, ".gitignore", "scratch/\n")
    _write(root, "docs/wiki/old.md", "# Old\n")
    _write(root, "docs/wiki/peer.md", "# Peer\n")
    _git(["add", "--", ".gitignore", "docs/wiki/old.md", "docs/wiki/peer.md"], root)
    _git(["commit", "-q", "-m", "base"], root)
    return root


def _head_count(root: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], root).strip())


def test_run_scope_lands_and_a_peer_dirty_file_stays_unstaged(repo):
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")
    _write(repo, "docs/wiki/new.md", "# New\n")
    _write(repo, "docs/wiki/_index.md", "# Wiki\n")
    _write(repo, "state/evidence/run-1.json", "{}\n")
    _write(repo, "docs/wiki/peer.md", "# Peer\n\nmid-edit by a peer\n")

    result = commit_scoped(
        repo,
        touched=["docs/wiki/old.md", "docs/wiki/new.md"],
        transform_outputs=["docs/wiki/_index.md"],
        evidence=["state/evidence/run-1.json"],
        message="post-run: finish run-1",
        session_id=_SID,
    )

    assert result.ok and result.committed and result.sha, result.error
    assert result.process_sha and _git(["rev-parse", "HEAD"], repo).strip() == result.process_sha
    product = set(_git(["show", "--name-only", "--format=", result.sha], repo).split())
    process = set(_git(["show", "--name-only", "--format=", result.process_sha], repo).split())
    assert product == {"docs/wiki/old.md", "docs/wiki/new.md", "docs/wiki/_index.md"}
    assert process == {"state/evidence/run-1.json"}
    assert _porcelain(repo) == {"docs/wiki/peer.md": " M"}


def test_a_peer_staged_file_is_not_swept_into_the_commit(repo):
    _write(repo, "docs/wiki/peer.md", "# Peer\n\nstaged by a peer\n")
    _git(["add", "--", "docs/wiki/peer.md"], repo)
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")

    result = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: old", session_id=_SID)

    assert result.ok and result.committed, result.error
    landed = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert landed == {"docs/wiki/old.md"}
    assert _porcelain(repo) == {"docs/wiki/peer.md": "M "}


def test_a_scoped_deletion_lands_as_a_deletion(repo):
    (repo / "docs/wiki/old.md").unlink()

    result = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: drop old", session_id=_SID)

    assert result.ok and result.committed, result.error
    assert "docs/wiki/old.md" not in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
    assert _porcelain(repo) == {}


def test_a_gitignored_untracked_path_is_dropped_not_committed(repo):
    _write(repo, "docs/wiki/new.md", "# New\n")
    _write(repo, "scratch/ev.json", "{}\n")

    result = commit_scoped(
        repo, ["docs/wiki/new.md"], [], ["scratch/ev.json"], "post-run: new", session_id=_SID
    )

    assert result.ok and result.committed, result.error
    assert result.dropped_ignored == ["scratch/ev.json"]
    landed = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert landed == {"docs/wiki/new.md"}


def test_a_rerun_with_nothing_left_to_commit_is_ok_and_adds_no_commit(repo):
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")
    first = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: old", session_id=_SID)
    assert first.committed
    count = _head_count(repo)

    second = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: old again", session_id=_SID)

    assert second.ok and not second.committed and second.error is None
    assert _head_count(repo) == count


def test_an_empty_scope_commits_nothing(repo):
    count = _head_count(repo)

    result = commit_scoped(repo, [], [], [], "post-run: nothing", session_id=_SID)

    assert result.ok and not result.committed and result.paths == []
    assert _head_count(repo) == count


def test_a_commit_refusal_is_returned_not_swallowed(repo, monkeypatch):
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")
    monkeypatch.setattr(
        commit_v2, "_handler", lambda params, repo_root=None: {"committed": False, "sha": None, "error": "gate X"}
    )

    result = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: old", session_id=_SID)

    assert not result.ok and result.error == "gate X"
    assert _porcelain(repo) == {"docs/wiki/old.md": " M"}


def test_an_exception_in_the_handler_is_returned_as_a_failure(repo, monkeypatch):
    def boom(params, repo_root=None):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(commit_v2, "_handler", boom)
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")

    result = commit_scoped(repo, ["docs/wiki/old.md"], [], [], "post-run: old", session_id=_SID)

    assert not result.ok and "RuntimeError: disk gone" in result.error


def test_the_call_names_explicit_paths_and_never_stages_everything(repo, monkeypatch):
    seen = {}

    def spy(params, repo_root=None):
        seen.update(params)
        return {"committed": False, "sha": None, "nothing_to_commit": True}

    monkeypatch.setattr(commit_v2, "_handler", spy)
    _write(repo, "docs/wiki/old.md", "# Old\n\nedited\n")

    commit_scoped(repo, ["docs/wiki/old.md"], ["docs/wiki/old.md"], [], "m", session_id=_SID)

    assert seen["paths"] == ["docs/wiki/old.md"]
    assert seen["ignored_untracked"] == []


def test_scoped_paths_unions_dedupes_and_normalises(repo):
    _write(repo, "a/b.md", "x\n")
    got = scoped_paths(
        repo,
        touched=["./a/b.md", str(repo / "a" / "b.md")],
        transform_outputs=["docs\\wiki\\old.md"],
        evidence=["a/b.md"],
    )

    assert got == ["a/b.md", "docs/wiki/old.md"]


@pytest.mark.parametrize("bad", ["../escape.md", ".git/config", "docs", "", "/elsewhere/x.md"])
def test_scoped_paths_refuses_non_explicit_entries(repo, bad):
    with pytest.raises(ValueError):
        scoped_paths(repo, [bad], [], [])
