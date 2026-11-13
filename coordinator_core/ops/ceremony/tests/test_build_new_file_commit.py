"""`build_new_file_commit` builds a new-file commit on a parent sha and writes no ref."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PATH = "cross-repo/inbox/memo.md"


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _msg(tmp_path: Path) -> Path:
    msg = tmp_path / "msg.txt"
    msg.write_text("deliver memo\n", encoding="utf-8")
    return msg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return real_git_repo(tmp_path)


@pytest.fixture
def upstream_tip(repo: Path) -> str:
    """A commit ahead of HEAD that no local branch names (a fetched tracking tip)."""
    _git(["checkout", "-q", "-b", "scratch-up"], repo)
    (repo / "upstream-only.txt").write_text("u\n", encoding="utf-8")
    _git(["add", "upstream-only.txt"], repo)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "up"], repo)
    tip = _git(["rev-parse", "HEAD"], repo)
    _git(["update-ref", "refs/remotes/origin/main-up", tip], repo)
    _git(["checkout", "-q", "-"], repo)
    _git(["branch", "-q", "-D", "scratch-up"], repo)
    return tip


def _snapshot(repo: Path) -> dict:
    return {
        "head": _git(["rev-parse", "HEAD"], repo),
        "head_ref": _git(["symbolic-ref", "HEAD"], repo),
        "heads": _git(["for-each-ref", "refs/heads"], repo),
        "index": (repo / ".git" / "index").read_bytes(),
        "status": _git(["status", "--porcelain"], repo),
        "head_log": (repo / ".git" / "logs" / "HEAD").read_bytes(),
    }


def test_builds_commit_on_parent_and_touches_nothing_else(
    repo: Path, tmp_path: Path, upstream_tip: str
) -> None:
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    before = _snapshot(repo)

    result = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha=upstream_tip
    )

    assert result.ok, result.stderr
    sha = result.stdout.strip()
    assert _git(["rev-parse", f"{sha}^"], repo) == upstream_tip
    assert _git(["diff-tree", "--no-commit-id", "--name-status", "-r", sha], repo) == f"A\t{_PATH}"
    assert _git(["show", f"{sha}:{_PATH}"], repo) == "memo"
    assert _git(["show", f"{sha}:upstream-only.txt"], repo) == "u"
    assert _snapshot(repo) == before
    assert not (repo / _PATH).exists()


def test_zero_spawns(
    repo: Path, tmp_path: Path, upstream_tip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = _msg(tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("build_new_file_commit spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = git_native.build_new_file_commit(_PATH, "memo\n", msg, repo, parent_sha=upstream_tip)
    monkeypatch.undo()
    assert result.ok, result.stderr


def test_calls_no_ref_cas(
    repo: Path, tmp_path: Path, upstream_tip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("build_new_file_commit moved a ref")

    monkeypatch.setattr(git_native, "cas_ref", _boom)
    result = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha=upstream_tip
    )
    assert result.ok, result.stderr


def test_head_branch_lock_does_not_block_the_build(
    repo: Path, tmp_path: Path, upstream_tip: str
) -> None:
    head_ref = _git(["symbolic-ref", "HEAD"], repo)
    (repo / ".git" / (head_ref + ".lock")).write_text("", encoding="utf-8")
    result = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha=upstream_tip
    )
    assert result.ok, result.stderr


def test_path_present_in_parent_is_refused(repo: Path, tmp_path: Path, upstream_tip: str) -> None:
    first = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha=upstream_tip
    )
    assert first.ok, first.stderr
    again = git_native.build_new_file_commit(
        _PATH, "other\n", _msg(tmp_path), repo, parent_sha=first.stdout.strip()
    )
    assert not again.ok
    assert "already exists" in again.stderr


def test_unknown_parent_is_refused(repo: Path, tmp_path: Path) -> None:
    result = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha="0" * 40
    )
    assert not result.ok
    assert "readable commit" in result.stderr


@pytest.mark.parametrize(
    ("path", "content", "needle"),
    [
        ("../escape.md", "x\n", "not a repo-relative"),
        ("/abs.md", "x\n", "not a repo-relative"),
        (_PATH, "a\r\nb\n", "CR byte"),
        (_PATH, None, "content is None"),
    ],
)
def test_clean_pipeline_refusals(
    repo: Path, tmp_path: Path, upstream_tip: str, path: str, content, needle: str
) -> None:
    result = git_native.build_new_file_commit(
        path, content, _msg(tmp_path), repo, parent_sha=upstream_tip
    )
    assert not result.ok
    assert needle in result.stderr


def test_path_matched_clean_filter_is_refused(
    repo: Path, tmp_path: Path, upstream_tip: str
) -> None:
    (repo / ".gitattributes").write_text("*.md filter=lfs\n", encoding="utf-8")
    result = git_native.build_new_file_commit(
        _PATH, "memo\n", _msg(tmp_path), repo, parent_sha=upstream_tip
    )
    assert not result.ok
    assert "clean filter" in result.stderr


def test_commit_authored_new_file_still_lands_on_head(repo: Path, tmp_path: Path) -> None:
    result = git_native.commit_authored_new_file(_PATH, "memo\n", _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert _git(["rev-parse", "HEAD"], repo) == result.stdout.strip()
