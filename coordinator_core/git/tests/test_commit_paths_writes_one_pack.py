"""A commit's blobs and trees land as one pack, not one loose file each."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


def _loose(repo):
    objects = repo / ".git" / "objects"
    return {p.parent.name + p.name for p in objects.glob("??/*")}


def test_blobs_and_trees_land_in_one_pack(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "seed.txt").write_bytes(b"seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    paths = []
    for i in range(6):
        p = f"d{i % 2}/sub/f{i}.txt"
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_bytes(f"content {i}\n".encode())
        paths.append(p)
    before = _loose(repo)
    packs_before = set((repo / ".git" / "objects" / "pack").glob("*.idx"))

    out = gcommit.commit_paths(repo, paths, "add files")

    new_loose = _loose(repo) - before
    assert new_loose == {out.sha}, "only the commit object goes loose"
    packs = set((repo / ".git" / "objects" / "pack").glob("*.idx")) - packs_before
    assert len(packs) == 1
    _git(repo, "fsck", "--full", "--strict")
    assert _git(repo, "show", f"HEAD:{paths[3]}") == "content 3\n"
    assert set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()) == {"seed.txt", *paths}


def test_refused_commit_writes_no_objects(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_bytes(b"a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    before = _loose(repo)
    with pytest.raises(gcommit.NothingToCommit):
        gcommit.commit_paths(repo, ["a.txt"], "no change")
    assert _loose(repo) == before
    assert not list((repo / ".git" / "objects" / "pack").glob("*.idx"))
