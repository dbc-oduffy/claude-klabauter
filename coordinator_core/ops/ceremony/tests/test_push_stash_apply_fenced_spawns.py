"""No engine op stashes a shared worktree: the push-ladder merge recovery refuses
a dirty-tree conflict without touching a file, and a ratchet bans stash calls."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod
from coordinator_core.ops.ceremony.tests.test_push_reject_landed_by_peer import (
    _git,
    _init_repo_with_upstream,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CORE = Path(__file__).resolve().parents[3]

#: git stash subcommands that read; every other `stash` use rewrites a tree.
_READ_ONLY_STASH_VERBS = {"list", "show"}

#: A command string spelling a mutating stash (`shell=True` shapes).
_SHELL_STASH = re.compile(r"\s*git\s+(?:-\S+\s+)*stash\s+(?:push|save|pop|apply|create)\b")

#: Named carve-outs: (path relative to coordinator_core, reason).
_STASH_CARVE_OUTS: dict[str, str] = {}


def _commit_file(repo, rel: str, text: str, msg: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", msg], repo)


def _diverged_repo(tmp_path, upstream_edits: str):
    repo = _init_repo_with_upstream(tmp_path)
    _commit_file(repo, "docs/note.md", "v1", "tracked")
    _git(["push", "-q", "origin", "work/x"], repo)
    other = tmp_path / "other"
    _git(["clone", "-q", str(tmp_path / "origin.git"), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    _commit_file(other, upstream_edits, "upstream", "upstream edit")
    _git(["push", "-q", "origin", "work/x"], other)
    _commit_file(repo, "ours.txt", "unpublished", "ours")
    _git(["fetch", "-q", "origin"], repo)
    return repo


def _snapshot(repo: Path, rels):
    return {r: ((repo / r).read_bytes(), (repo / r).stat().st_mtime_ns) for r in rels}


def _assert_no_stash(repo: Path) -> None:
    assert not (repo / ".git" / "refs" / "stash").exists()
    assert _git(["stash", "list"], repo).strip() == ""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dirty_tree_refusal_leaves_every_file_and_mtime_untouched(tmp_path):
    repo = _diverged_repo(tmp_path, "docs/note.md")
    dirty = ["docs/note.md", "peer_wip_a.txt", "peer_wip_b.txt"]
    for rel in dirty:
        (repo / rel).write_text("peer wip " + rel, encoding="utf-8")
        os.utime(repo / rel, ns=(10**18, 10**18))
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    before = _snapshot(repo, dirty)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code != 0
    assert reason.startswith("merge recovery declined, nothing touched: ")
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert _snapshot(repo, dirty) == before
    _assert_no_stash(repo)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_clean_merge_over_unrelated_dirty_files_keeps_their_mtimes(tmp_path):
    repo = _diverged_repo(tmp_path, "docs/other.md")
    dirty = ["peer_wip_a.txt", "peer_wip_b.txt"]
    for rel in dirty:
        (repo / rel).write_text("peer wip " + rel, encoding="utf-8")
        os.utime(repo / rel, ns=(10**18, 10**18))
    before = _snapshot(repo, dirty)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 0, reason
    assert _snapshot(repo, dirty) == before
    _assert_no_stash(repo)
