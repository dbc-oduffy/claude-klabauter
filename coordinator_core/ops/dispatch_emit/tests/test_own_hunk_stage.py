"""own_hunk_stage: the own-hunk blob carries only the chosen hunks, in process."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import own_hunk_stage as ohs
from coordinator_core.win_portability import no_console_creationflags

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(root: Path, *args: str) -> None:
    import os

    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env={**os.environ, **_ENV}, **no_console_creationflags())


def _repo(tmp_path: Path, body: bytes, name: str = "f.txt", autocrlf: bool = False) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "true" if autocrlf else "false")
    (root / name).write_bytes(body)
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _lines(n: int) -> bytes:
    return b"".join(b"line%d\n" % i for i in range(1, n + 1))


def _blob(root: Path, sha: str) -> bytes:
    got = git_objects.read_object(resolve_git_common_dir(root), sha)
    assert got is not None and got[0] == "blob"
    return got[1]


def _edit(root: Path, replace: dict[int, bytes]) -> None:
    ls = (root / "f.txt").read_bytes().splitlines(keepends=True)
    for n, new in replace.items():
        ls[n - 1] = new
    (root / "f.txt").write_bytes(b"".join(ls))


def test_read_diff_reports_hunks_in_head_coordinates(tmp_path):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n", 8: b"B\n"})
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    assert [(h.old_start, h.old_len, h.new_start, h.new_len) for h in d.hunks] == [(2, 1, 2, 1), (8, 1, 8, 1)]
    assert d.head_bytes == _lines(10)


def test_own_blob_applies_only_chosen_hunk_other_stays_in_worktree(tmp_path):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n", 8: b"B\n"})
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    sha = ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[0]])
    assert _blob(root, sha) == _lines(10).replace(b"line2\n", b"A\n")
    assert b"B\n" in (root / "f.txt").read_bytes()
    sha2 = ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[1]])
    assert _blob(root, sha2) == _lines(10).replace(b"line8\n", b"B\n")


def test_insertion_and_deletion_hunks(tmp_path):
    root = _repo(tmp_path, _lines(6))
    ls = _lines(6).splitlines(keepends=True)
    new = ls[:1] + [b"ins\n"] + ls[1:4] + ls[5:]
    (root / "f.txt").write_bytes(b"".join(new))
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    assert len(d.hunks) == 2
    only_ins = ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[0]])
    assert _blob(root, only_ins) == b"".join(ls[:1] + [b"ins\n"] + ls[1:])
    both = ohs.own_blob(root, "f.txt", d.head_bytes, d.hunks)
    assert _blob(root, both) == b"".join(new)


def test_stale_hunk_refuses(tmp_path):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n"})
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    (root / "f.txt").write_bytes(_lines(10))
    with pytest.raises(ohs.OwnHunkRefused):
        ohs.own_blob(root, "f.txt", d.head_bytes, d.hunks)


def test_crlf_worktree_is_checked_in_normalised(tmp_path):
    root = _repo(tmp_path, _lines(6), autocrlf=True)
    (root / "f.txt").write_bytes(_lines(6).replace(b"line2\n", b"A\n").replace(b"line5\n", b"B\n").replace(b"\n", b"\r\n"))
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    assert [(h.old_start, h.old_len) for h in d.hunks] == [(2, 1), (5, 1)]
    sha = ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[0]])
    assert _blob(root, sha) == _lines(6).replace(b"line2\n", b"A\n")


def test_head_drift_is_detected(tmp_path):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n"})
    diffs = ohs.read_diff(root, ["f.txt"])
    assert ohs.check_head_unchanged(root, diffs) is None
    (root / "g.txt").write_bytes(b"x\n")
    _git(root, "add", "g.txt")
    _git(root, "commit", "-q", "-m", "other")
    assert ohs.check_head_unchanged(root, diffs) is None  # f.txt's blob did not move
    sav = (root / "f.txt").read_bytes()
    (root / "f.txt").write_bytes(_lines(10).replace(b"line9\n", b"Z\n"))
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "peer edit")
    (root / "f.txt").write_bytes(sav)
    assert ohs.check_head_unchanged(root, diffs) == "f.txt"


def test_untracked_path_is_omitted(tmp_path):
    root = _repo(tmp_path, _lines(3))
    (root / "new.txt").write_bytes(b"n\n")
    assert ohs.read_diff(root, ["new.txt"]) == {}


def test_commit_paths_with_supplied_blob_commits_only_own_hunk(tmp_path):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n", 8: b"B\n"})
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    sha = ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[0]])
    git_commit.commit_paths(root, ["f.txt"], "own hunk", supplied_blobs={"f.txt": sha})
    head = subprocess.run(["git", "show", "HEAD:f.txt"], cwd=root, capture_output=True, check=True, **no_console_creationflags()).stdout
    assert head == _lines(10).replace(b"line2\n", b"A\n")
    assert b"B\n" in (root / "f.txt").read_bytes()
    status = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=root, capture_output=True, text=True, **no_console_creationflags()).stdout
    assert status.strip() == ""


def test_staging_spawns_no_process(tmp_path, monkeypatch):
    root = _repo(tmp_path, _lines(10))
    _edit(root, {2: b"A\n", 8: b"B\n"})

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    d = ohs.read_diff(root, ["f.txt"])["f.txt"]
    ohs.own_blob(root, "f.txt", d.head_bytes, [d.hunks[0]])
    assert ohs.check_head_unchanged(root, {"f.txt": d}) is None
