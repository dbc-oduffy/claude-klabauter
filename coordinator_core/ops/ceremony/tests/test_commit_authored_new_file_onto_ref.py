"""`commit_authored_new_file(onto_ref=...)` lands a new file on a ref HEAD does not name."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REF = "refs/heads/work/day"
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
    root = real_git_repo(tmp_path)
    _git(["branch", "work/day"], root)
    return root


def test_lands_on_target_ref_and_leaves_head_untouched(repo: Path, tmp_path: Path) -> None:
    head_before = _git(["rev-parse", "HEAD"], repo)
    head_ref = _git(["symbolic-ref", "HEAD"], repo)
    tip_before = _git(["rev-parse", _REF], repo)
    porcelain_before = _git(["status", "--porcelain"], repo)
    index_before = (repo / ".git" / "index").read_bytes()
    head_log = (repo / ".git" / "logs" / "HEAD").read_bytes()

    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=_REF
    )

    assert result.ok, result.stderr
    new_sha = result.stdout.strip()
    assert _git(["rev-parse", _REF], repo) == new_sha
    assert _git(["rev-parse", f"{new_sha}^"], repo) == tip_before
    changed = _git(["diff-tree", "--no-commit-id", "--name-status", "-r", new_sha], repo)
    assert changed == f"A\t{_PATH}"
    assert _git(["rev-parse", "HEAD"], repo) == head_before
    assert _git(["symbolic-ref", "HEAD"], repo) == head_ref
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / ".git" / "logs" / "HEAD").read_bytes() == head_log
    assert not (repo / _PATH).exists()
    assert _git(["status", "--porcelain"], repo) == porcelain_before


def test_path_present_on_target_ref_is_refused(repo: Path, tmp_path: Path) -> None:
    first = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=_REF
    )
    assert first.ok, first.stderr
    again = git_native.commit_authored_new_file(
        _PATH, "other\n", _msg(tmp_path), repo, onto_ref=_REF
    )
    assert not again.ok
    assert "already exists" in again.stderr


def test_missing_target_ref_fails_without_ladder(repo: Path, tmp_path: Path) -> None:
    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref="refs/heads/work/nope"
    )
    assert not result.ok
    assert "no tip" in result.stderr


def test_lost_cas_returns_failing_result(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_native, "cas_ref", lambda *a, **k: False)
    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=_REF
    )
    assert not result.ok
    assert "compare-and-swap failed" in result.stderr


def test_zero_spawns_on_onto_ref_arm(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = _msg(tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("onto_ref arm spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = git_native.commit_authored_new_file(_PATH, "memo\n", msg, repo, onto_ref=_REF)
    monkeypatch.undo()
    assert result.ok, result.stderr


def test_packed_target_ref_resolves(repo: Path, tmp_path: Path) -> None:
    _git(["pack-refs", "--all", "--prune"], repo)
    assert not (repo / ".git" / "refs" / "heads" / "work" / "day").exists()
    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=_REF
    )
    assert result.ok, result.stderr
    assert _git(["rev-parse", _REF], repo) == result.stdout.strip()


@pytest.mark.parametrize(
    "bad_ref", ["refs/heads/../../HEAD", "work/day", "refs/tags/v1", "HEAD"]
)
def test_malformed_onto_ref_is_refused(repo: Path, tmp_path: Path, bad_ref: str) -> None:
    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=bad_ref
    )
    assert not result.ok
    assert "must be a" in result.stderr


def test_onto_ref_naming_heads_branch_is_refused(repo: Path, tmp_path: Path) -> None:
    head_ref = _git(["symbolic-ref", "HEAD"], repo)
    tip = _git(["rev-parse", head_ref], repo)
    result = git_native.commit_authored_new_file(
        _PATH, "memo\n", _msg(tmp_path), repo, onto_ref=head_ref
    )
    assert not result.ok
    assert "HEAD does not name" in result.stderr
    assert _git(["rev-parse", head_ref], repo) == tip
