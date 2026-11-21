"""A split index (core.splitIndex / sharedindex.*) and a v4 index must never be spliced in place:
the writer refuses typed with the index untouched, and every caller either falls back to git's own
writer or reports a typed refusal. Each caller must leave the landed paths at the landed blobs and a
peer's staged change intact, so a plain peer `git commit` cannot revert the land."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import index_write
from coordinator_core.git.commit import commit_paths, stage_paths_in_process
from coordinator_core.git.git_index import IndexParseError
from coordinator_core.git.index_sync import sync_shared_index
from coordinator_core.git.tests.test_index_sync import (
    _assert_peer_commit_preserves,
    _git,
    _plumbing_land,
    _stage_peer,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SHAPES = ["split", "v4"]


def _shape_index(repo: Path, shape: str) -> None:
    if shape == "split":
        _git(repo, "update-index", "--split-index")
        assert list((repo / ".git").glob("sharedindex.*"))
    else:
        _git(repo, "update-index", "--index-version", "4")
        assert (repo / ".git" / "index").read_bytes()[4:8] == b"\x00\x00\x00\x04"


@pytest.fixture(params=_SHAPES)
def repo(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    for name, text in (("a.txt", "a0\n"), ("b.txt", "b0\n"), ("gone.txt", "g0\n"), ("peer.txt", "p0\n")):
        (tmp_path / name).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _shape_index(tmp_path, request.param)
    return tmp_path


def _is_v4(repo: Path) -> bool:
    return (repo / ".git" / "index").read_bytes()[4:8] == b"\x00\x00\x00"


def _blob(repo: Path, text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=text,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def test_prepare_splice_refuses_typed_and_leaves_index_bytes(repo: Path) -> None:
    before = (repo / ".git" / "index").read_bytes()
    with pytest.raises(index_write.UnsupportedIndexShape):
        index_write.prepare_splice(repo, {"a.txt": (0o100644, _blob(repo, "a1\n"))})
    assert (repo / ".git" / "index").read_bytes() == before
    assert not (repo / ".git" / "index.lock").exists()


def test_splice_index_falls_back_and_keeps_every_entry(repo: Path) -> None:
    _stage_peer(repo)
    index_write.splice_index(
        repo,
        {"a.txt": (0o100644, _blob(repo, "a1\n")), "new.txt": (0o100644, _blob(repo, "n\n")),
         "gone.txt": index_write.ABSENT},
    )
    names = _git(repo, "ls-files").split()
    assert names == ["a.txt", "b.txt", "new.txt", "peer.txt"]
    assert _git(repo, "diff", "--cached", "--name-only", "--", "a.txt", "new.txt", "gone.txt").split() == [
        "a.txt", "gone.txt", "new.txt",
    ]
    assert _git(repo, "ls-files", "-s", "--", "peer.txt").split()[1] == _blob(repo, "p1 staged by peer\n")


def test_splice_index_without_fallback_reraises_typed(repo: Path) -> None:
    with pytest.raises(index_write.UnsupportedIndexShape):
        index_write.splice_index(repo, {"a.txt": (0o100644, _blob(repo, "a1\n"))}, shape_fallback=False)


def test_stage_paths_in_process_stages_and_keeps_peer(repo: Path) -> None:
    _stage_peer(repo)
    (repo / "a.txt").write_text("a-staged\n")
    if _is_v4(repo):
        # The index READER refuses v4 before any write: typed, index untouched.
        before = (repo / ".git" / "index").read_bytes()
        with pytest.raises(IndexParseError):
            stage_paths_in_process(repo, ["a.txt"])
        assert (repo / ".git" / "index").read_bytes() == before
        return
    stage_paths_in_process(repo, ["a.txt"])
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["a.txt", "peer.txt"]
    assert _git(repo, "ls-files").split() == ["a.txt", "b.txt", "gone.txt", "peer.txt"]


def test_commit_paths_lands_and_peer_commit_preserves(repo: Path) -> None:
    _stage_peer(repo)
    (repo / "b.txt").write_text("b-committed\n")
    (repo / "gone.txt").unlink()
    head = _git(repo, "rev-parse", "HEAD")
    if _is_v4(repo):
        with pytest.raises(IndexParseError):
            commit_paths(repo, ["b.txt"], "land b, remove gone.txt", deleted_paths=["gone.txt"])
        assert _git(repo, "rev-parse", "HEAD") == head
        return
    outcome = commit_paths(repo, ["b.txt"], "land b, remove gone.txt", deleted_paths=["gone.txt"])
    assert outcome.sha
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", "HEAD:b.txt") == "b-committed"
    assert _git(repo, "ls-tree", "HEAD", "--", "gone.txt") == ""
    assert _git(repo, "show", "HEAD:peer.txt") == "p1 staged by peer"


@pytest.mark.parametrize(
    "landed",
    [{"a.txt": "a1\n"}, {"new.txt": "n1\n"}, {"gone.txt": None},
     {"a.txt": "a2\n", "b.txt": "b2\n", "new.txt": "n2\n", "gone.txt": None}],
    ids=["modify", "add", "delete", "mixed"],
)
def test_sync_helper_preserves_land_and_peer_staging(repo: Path, landed: dict) -> None:
    _stage_peer(repo)
    new = _plumbing_land(repo, landed)
    sync_shared_index(repo, new, list(landed))
    assert _git(repo, "diff", "--cached", "--name-only", "--", *landed) == ""
    _assert_peer_commit_preserves(repo, new, landed)


def test_site_propagate_delivery(repo: Path) -> None:
    from coordinator_core.ops import propagate_body

    rel = "state/handoffs/h.md"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / rel).write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "handoff")
    _stage_peer(repo)
    (repo / rel).write_text("one\ntwo\n")
    sha, err = propagate_body._commit_delivery(repo, rel, "slug", "summary")
    assert err is None and sha
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", f"HEAD:{rel}") == "one\ntwo"


def test_site_commit_scoped(repo: Path) -> None:
    from coordinator_core.ops.ceremony import git_native

    _stage_peer(repo)
    (repo / "b.txt").write_text("b-scoped\n")
    (repo / "gone.txt").unlink()
    msg = repo / ".git" / "msg.txt"
    msg.write_text("scoped\n")
    head, before = _git(repo, "rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes()
    if _is_v4(repo):
        with pytest.raises(IndexParseError):
            git_native.commit_scoped(["b.txt", "gone.txt"], msg, repo)
        assert _git(repo, "rev-parse", "HEAD") == head
        assert (repo / ".git" / "index").read_bytes() == before
        return
    res = git_native.commit_scoped(["b.txt", "gone.txt"], msg, repo)
    if res.returncode != 0:
        # The divergence reader refuses a split/v4 index before any write: typed, nothing moved.
        assert "indeterminate" in res.stderr
        assert _git(repo, "rev-parse", "HEAD") == head
        assert (repo / ".git" / "index").read_bytes() == before
        return
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", "HEAD:b.txt") == "b-scoped"
    assert _git(repo, "ls-tree", "HEAD", "--", "gone.txt") == ""
