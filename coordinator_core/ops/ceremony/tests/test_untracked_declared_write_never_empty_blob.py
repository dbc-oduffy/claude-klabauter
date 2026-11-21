"""A declared write untracked at HEAD never lands as the empty blob while its worktree file is non-empty.

The shared index can carry an intent-to-add entry (`git add -N`) for the path: the entry's blob
is the empty blob, it differs from HEAD (absent), so the path routes to the staged-source arm and
the empty placeholder used to be committed verbatim. The commit route refuses instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
NEW = "scripts/tests/new_floor.Tests.ps1"
SIBLING = "scripts/leg_drive.ps1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _msg(tmp_path: Path) -> Path:
    path = tmp_path / "msg.txt"
    path.write_text("land declared writes\n", encoding="utf-8")
    return path


def _head_blob(repo: Path, rel: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", f"HEAD:{rel}"], cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _seed_sibling(repo: Path) -> None:
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / SIBLING).write_text("base\n", encoding="utf-8")
    _git(repo, "add", "--", SIBLING)
    _git(repo, "commit", "-q", "-m", "seed sibling")


def _worktree_blob(repo: Path, rel: str) -> str:
    return _git(repo, "hash-object", "--", rel).strip()


def _ita(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--intent-to-add", "--", rel)


def test_intent_to_add_untracked_path_with_content_is_not_committed_empty(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_sibling(repo)
    _ita(repo, NEW, "Describe 'floor' {}\n" * 5)

    result = git_native.commit_scoped([NEW], _msg(tmp_path), repo)

    assert not result.ok
    assert NEW in result.stderr
    assert _head_blob(repo, NEW) == ""


def test_intent_to_add_with_peer_hunks_in_sibling_never_lands_empty(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_sibling(repo)
    _ita(repo, NEW, "Describe 'floor' {}\n" * 5)
    (repo / SIBLING).write_text("base\npeer hunk\nmine\n", encoding="utf-8")
    _git(repo, "add", "--", SIBLING)
    (repo / SIBLING).write_text("base\npeer hunk\nmine\nunstaged\n", encoding="utf-8")

    git_native.commit_scoped([NEW, SIBLING], _msg(tmp_path), repo)

    assert _head_blob(repo, NEW) != EMPTY_BLOB


def test_intent_to_add_refusal_names_the_path_and_commits_nothing(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_sibling(repo)
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    _ita(repo, NEW, "real content\n")

    result = git_native.commit_scoped([NEW], _msg(tmp_path), repo)

    assert not result.ok
    assert NEW in result.stderr
    assert "empty-blob" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before


def test_genuinely_empty_new_file_still_commits_as_the_empty_blob(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_sibling(repo)
    _ita(repo, NEW, "")

    result = git_native.commit_scoped([NEW], _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert _head_blob(repo, NEW) == EMPTY_BLOB


def test_plain_untracked_nonempty_file_still_commits_its_content(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_sibling(repo)
    target = repo / NEW
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("real content\n", encoding="utf-8")

    result = git_native.commit_scoped([NEW], _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert _head_blob(repo, NEW) == _worktree_blob(repo, NEW)
