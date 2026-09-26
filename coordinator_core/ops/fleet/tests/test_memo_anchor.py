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
        "abc123",
        "a" * 41,
        "A" * 40,
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

    branch_tree_shas = _git(["rev-list", "--objects", "--branches"], cwd=repo).stdout
    assert blob_sha not in branch_tree_shas

    _git(["gc", "--prune=now"], cwd=repo)

    assert resolve_anchor(repo / ".git", blob_sha) == b"survives gc"
    names = anchor_names(repo / ".git")
    assert ("gc-memo.md", commit_sha, blob_sha) in names


def test_anchor_ref_prefix_shape():
    assert ANCHOR_REF_PREFIX == "refs/coordinator/inbox/"


def test_lost_cas_where_ref_already_matches_returns_the_sha(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    first_sha = write_anchor(repo / ".git", "raced-memo.md", commit_sha, b"same payload")
    assert first_sha is not None

    import coordinator_core.ops.fleet._memo_anchor as anchor_module

    monkeypatch.setattr(anchor_module, "cas_ref", lambda *a, **k: False)

    second_sha = write_anchor(repo / ".git", "raced-memo.md", commit_sha, b"same payload")

    assert second_sha == first_sha, (
        "a lost CAS whose ref already equals the intended blob is not a "
        "loss -- it must return the sha, not None"
    )


def test_lost_cas_with_a_genuine_mismatch_still_returns_none(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_sha = _seed_commit(repo)

    import coordinator_core.ops.fleet._memo_anchor as anchor_module

    monkeypatch.setattr(anchor_module, "cas_ref", lambda *a, **k: False)

    result = write_anchor(repo / ".git", "never-written-memo.md", commit_sha, b"x")

    assert result is None
