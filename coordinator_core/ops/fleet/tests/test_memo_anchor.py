"""Tests for `coordinator_core.ops.fleet._memo_anchor`.

Spawns real `git` (`init`, `commit`, `pack-refs`, `gc`) against `tmp_path`
repos to prove the anchor survives the two operations that would otherwise
reap it -- packing and garbage collection -- which a faked object store
cannot demonstrate. Per the spawn ratchet
(`coordinator_core/tests/test_no_new_spawning_tests.py`), the module-level
form is required here rather than per-function marks: `_git`/`_init_repo`
are non-test spawners, so a per-function mark on the `test_*` functions
alone would be inert against Rule 4's condition (ii).

Spec backlink: docs/plans/2026-09-11-memo-deliveries-survive-the-receiver-s-o.md, chunk C3
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from coordinator_core.ops.fleet._memo_anchor import (  # noqa: E402
    ANCHOR_REF_PREFIX,
    anchor_names,
    resolve_anchor,
    write_anchor,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, *, cwd):
    kwargs = dict(cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())
    return subprocess.run(["git", *args], **kwargs)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.example"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)


def _seed_commit(repo: Path) -> str:
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "seed.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


def test_write_then_list_then_resolve_round_trips(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    blob_sha = write_anchor(repo / ".git", "some-memo.md", commit_sha, b"payload bytes")
    assert blob_sha is not None

    names = anchor_names(repo / ".git")
    assert (("some-memo.md", commit_sha, blob_sha)) in names

    assert resolve_anchor(repo / ".git", blob_sha) == b"payload bytes"


def test_anchor_survives_pack_refs_and_is_still_listed(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    blob_sha = write_anchor(repo / ".git", "packed-memo.md", commit_sha, b"packed payload")
    assert blob_sha is not None

    _git(["pack-refs", "--all"], cwd=repo)

    names = anchor_names(repo / ".git")
    assert ("packed-memo.md", commit_sha, blob_sha) in names
    assert resolve_anchor(repo / ".git", blob_sha) == b"packed payload"


def test_loose_shadows_packed(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    first_sha = write_anchor(repo / ".git", "shadow-memo.md", commit_sha, b"first")
    assert first_sha is not None
    _git(["pack-refs", "--all"], cwd=repo)

    second_sha = write_anchor(repo / ".git", "shadow-memo.md", commit_sha, b"second")
    assert second_sha is not None
    assert second_sha != first_sha

    names = anchor_names(repo / ".git")
    triples = [t for t in names if t[0] == "shadow-memo.md" and t[1] == commit_sha]
    assert triples == [("shadow-memo.md", commit_sha, second_sha)]
    assert resolve_anchor(repo / ".git", second_sha) == b"second"


def test_existing_anchor_same_filename_same_commit_sha_is_replaced(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    first_sha = write_anchor(repo / ".git", "replace-memo.md", commit_sha, b"v1")
    assert first_sha is not None
    second_sha = write_anchor(repo / ".git", "replace-memo.md", commit_sha, b"v2")
    assert second_sha is not None

    names = anchor_names(repo / ".git")
    triples = [t for t in names if t[0] == "replace-memo.md" and t[1] == commit_sha]
    assert len(triples) == 1
    assert triples[0][2] == second_sha
    assert resolve_anchor(repo / ".git", second_sha) == b"v2"


@pytest.mark.parametrize(
    "bad_filename",
    [
        "",
        "has space.md",
        "has\ttab.md",
        "has~tilde.md",
        "has^caret.md",
        "has:colon.md",
        "has?question.md",
        "has*star.md",
        "has[bracket.md",
        "has\\backslash.md",
        "double..dot.md",
        "at@{brace.md",
        ".leading-dot.md",
        "trailing.lock",
    ],
)
def test_rejected_filename_returns_none(tmp_path, bad_filename):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    assert write_anchor(repo / ".git", bad_filename, commit_sha, b"x") is None


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",
        "not-hex-at-all-not-hex-at-all-not-hexxx",
        "abc123",  # too short
        "a" * 41,  # too long
        "A" * 40,  # uppercase, refused rather than normalised
    ],
)
def test_rejected_sha_returns_none(tmp_path, bad_sha):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_commit(repo)

    assert write_anchor(repo / ".git", "some-memo.md", bad_sha, b"x") is None


def test_resolved_anchor_survives_gc_prune_now_with_no_branch_reaching_it(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    blob_sha = write_anchor(repo / ".git", "gc-memo.md", commit_sha, b"survives gc")
    assert blob_sha is not None

    # Confirm no branch reaches the blob: it is reachable ONLY via the
    # anchor ref, not via HEAD/any branch tip. `--branches` excludes the
    # anchor namespace itself, unlike `--all`.
    branch_tree_shas = _git(["rev-list", "--objects", "--branches"], cwd=repo).stdout
    assert blob_sha not in branch_tree_shas

    _git(["gc", "--prune=now"], cwd=repo)

    assert resolve_anchor(repo / ".git", blob_sha) == b"survives gc"
    names = anchor_names(repo / ".git")
    assert ("gc-memo.md", commit_sha, blob_sha) in names


def test_anchor_ref_prefix_shape():
    assert ANCHOR_REF_PREFIX == "refs/coordinator/inbox/"
