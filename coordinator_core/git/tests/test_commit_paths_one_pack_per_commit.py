"""An unsigned commit object rides the commit's object-batch pack, not a loose write."""

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


def _add_files(repo):
    paths = []
    for i in range(4):
        p = f"d{i % 2}/f{i}.txt"
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_bytes(f"content {i}\n".encode())
        paths.append(p)
    return paths


def _count_replaces(monkeypatch):
    calls = []
    real = git_objects._replace_with_retry

    def spy(src, dst, *args, **kwargs):
        calls.append(str(dst))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(git_objects, "_replace_with_retry", spy)
    return calls


def test_unsigned_commit_lands_in_the_pack_with_no_loose_object(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    paths = _add_files(repo)
    before = _loose(repo)
    packs_before = set((repo / ".git" / "objects" / "pack").glob("*.idx"))
    replaces = _count_replaces(monkeypatch)

    out = gcommit.commit_paths(repo, paths, "add files")

    assert _loose(repo) == before
    assert len(set((repo / ".git" / "objects" / "pack").glob("*.idx")) - packs_before) == 1
    object_replaces = [r for r in replaces if "objects" in r.replace("\\", "/")]
    assert len(object_replaces) == 2, "the .pack and the .idx; no loose commit replace"
    assert _git(repo, "cat-file", "-t", out.sha).strip() == "commit"
    assert _git(repo, "rev-parse", "HEAD").strip() == out.sha
    _git(repo, "fsck", "--no-dangling")


def test_signed_commit_keeps_flush_then_loose_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    paths = _add_files(repo)
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
    assert _loose(repo) - before == {out.sha}
    _git(repo, "fsck", "--no-dangling")
