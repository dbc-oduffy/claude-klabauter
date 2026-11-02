"""Engine commit and land-fetch routes leave the pack count unchanged, small batch or large."""

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


def test_batch_over_the_threshold_adds_no_pack(tmp_path):
    repo = _repo(tmp_path)
    paths = _add_files(repo, 50, sub="sub/")
    packs_before = _idx(repo)

    gcommit.commit_paths(repo, paths, "add files")

    # The first over-threshold batch mints the tail pack; every later one folds into it.
    assert len(_idx(repo) - packs_before) <= 1
    packs_after_first = _idx(repo)
    more = _add_files(repo, 50, sub="more/")

    gcommit.commit_paths(repo, more, "add more files")

    assert len(_idx(repo)) == len(packs_after_first)
    _git(repo, "fsck", "--full", "--strict")
    assert set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()) == {
        "seed.txt", *paths, *more,
    }


def _land_fixture(tmp_path, count):
    """A clone that fetches `count` new files pushed by a peer clone."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    seed = _repo(tmp_path)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(origin), str(clone)],
        check=True, capture_output=True, **_NOWIN,
    )
    paths = _add_files(seed, count, sub="landed/")
    _git(seed, "add", *paths)
    _git(seed, "commit", "-q", "-m", "peer work")
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    return clone, paths


@pytest.mark.parametrize("count", [50, 150])
def test_land_fetch_adds_no_pack(tmp_path, count):
    from coordinator_core.ops.ceremony import git_native

    clone, paths = _land_fixture(tmp_path, count)
    packs_before = _idx(clone)

    res = git_native.fetch(clone, "origin")

    assert res.returncode == 0, res.stderr
    assert len(_idx(clone) - packs_before) == 0
    assert set(_git(clone, "ls-tree", "-r", "--name-only", "origin/main").split()) >= set(paths)


def test_signed_commit_keeps_flush_then_loose_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    paths = _add_files(repo, 2)
    monkeypatch.setattr(gcommit, "_gpgsign_enabled", lambda repo: True)
    order = []
    real_flush = gcommit.flush_object_batch

    def flush():
        order.append("flush")
        real_flush()

    def sign(repo_, tree, parent, name, email, when, message, **_kw):
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
