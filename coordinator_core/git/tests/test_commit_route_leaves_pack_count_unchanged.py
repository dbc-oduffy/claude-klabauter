"""A small engine commit leaves the pack count unchanged; a large batch adds exactly one pack."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git import git_objects

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


def _loose(repo):
    return {p.parent.name + p.name for p in (repo / ".git" / "objects").glob("??/*")}


def _idx(repo):
    return set((repo / ".git" / "objects" / "pack").glob("*.idx"))


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "seed.txt").write_bytes(b"seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _add_files(repo, count, sub=""):
    paths = []
    for i in range(count):
        p = f"d{i % 2}/{sub}f{i}.txt"
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_bytes(f"content {i}\n".encode())
        paths.append(p)
    return paths


def test_small_unsigned_commit_adds_no_pack_and_stays_loose(tmp_path):
    repo = _repo(tmp_path)
    paths = _add_files(repo, 2)
    before, packs_before = _loose(repo), _idx(repo)

    out = gcommit.commit_paths(repo, paths, "add files")

    assert _idx(repo) == packs_before
    assert out.sha in _loose(repo) - before
    assert _git(repo, "rev-parse", "HEAD").strip() == out.sha
    _git(repo, "fsck", "--no-dangling")


def test_batch_over_the_threshold_writes_exactly_one_pack(tmp_path):
    repo = _repo(tmp_path)
    paths = _add_files(repo, git_objects._LOOSE_BATCH_MAX + 2, sub="sub/")
    packs_before = _idx(repo)

    gcommit.commit_paths(repo, paths, "add files")

    assert len(_idx(repo) - packs_before) == 1
    _git(repo, "fsck", "--full", "--strict")
    assert set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()) == {"seed.txt", *paths}


def test_signed_commit_keeps_flush_then_loose_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    paths = _add_files(repo, 2)
    monkeypatch.setattr(gcommit, "_gpgsign_enabled", lambda repo: True)
    order = []
    real_flush = gcommit.flush_object_batch

    def flush():
        order.append("flush")
        real_flush()

    def sign(repo_, tree, parent, name, email, when, message):
        order.append("sign")
        return None, "stub signer declined"

    monkeypatch.setattr(gcommit, "flush_object_batch", flush)
    monkeypatch.setattr(gcommit, "_sign_commit_tree", sign)
    before = _loose(repo)

    out = gcommit.commit_paths(repo, paths, "add files")

    assert order[:2] == ["flush", "sign"]
    assert out.sha in _loose(repo) - before
    _git(repo, "fsck", "--no-dangling")


def test_refused_commit_writes_no_objects(tmp_path):
    repo = _repo(tmp_path)
    before, packs_before = _loose(repo), _idx(repo)
    with pytest.raises(gcommit.NothingToCommit):
        gcommit.commit_paths(repo, ["seed.txt"], "no change")
    assert _loose(repo) == before
    assert _idx(repo) == packs_before
