"""
coordinator_core.ops.ceremony.tests.test_commit_v2_allow_empty

Purpose: `ceremony.commit_v2` `allow_empty` lands one marker commit (tree equal to HEAD's) for a run
whose content already landed, only after NothingToCommit and with no landed predecessor.
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
    _git(["config", "core.autocrlf", "false"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], root)
    _git(["commit", "-q", "-m", "hand-committed a"], root)
    return root


def _params(**extra) -> dict:
    return {"paths": ["a.txt"], "message": "close a\n", "session_id": _SID, **extra}


def _count(repo: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], repo))


def test_allow_empty_lands_one_marker_with_head_tree(repo):
    before_tree = _git(["rev-parse", "HEAD^{tree}"], repo)
    before = _count(repo)

    reply = _call(repo, _params(allow_empty=True))

    assert reply["committed"] is True and reply["sha"]
    assert _count(repo) == before + 1
    assert _git(["rev-parse", "HEAD^{tree}"], repo) == before_tree
    assert _git(["rev-parse", "HEAD"], repo) == reply["sha"]


def test_without_allow_empty_still_nothing_to_commit(repo):
    before = _count(repo)

    reply = _call(repo, _params())

    assert reply["committed"] is False
    assert reply["nothing_to_commit"] is True
    assert _count(repo) == before


def test_retried_allow_empty_reports_the_landed_sha_without_a_second_commit(repo):
    first = _call(repo, _params(allow_empty=True))
    assert first["committed"] is True
    after_first = _count(repo)

    retry = _call(repo, _params(allow_empty=True))

    assert retry["committed"] is False
    assert retry["already_landed_sha"] == first["sha"]
    assert _count(repo) == after_first


@pytest.mark.parametrize("bad", ["true", 1, None, []])
def test_non_boolean_allow_empty_is_refused_with_no_write(repo, bad):
    before = _count(repo)

    reply = _call(repo, _params(allow_empty=bad))

    assert reply["committed"] is False
    assert "allow_empty must be a boolean" in reply["error"]
    assert _count(repo) == before


def test_mixed_process_and_product_no_delta_lands_at_most_one_marker(repo):
    (repo / "state").mkdir()
    (repo / "state" / "p.md").write_text("p\n", encoding="utf-8")
    _git(["add", "--", "state/p.md"], repo)
    _git(["commit", "-q", "-m", "hand-committed p"], repo)
    before = _count(repo)

    reply = _call(repo, _params(paths=["a.txt", "state/p.md"], allow_empty=True))

    assert reply["committed"] is True
    assert _count(repo) <= before + 1
