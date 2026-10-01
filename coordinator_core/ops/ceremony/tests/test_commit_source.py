"""post_commit_reader agrees with the tree commit_paths lands, with zero spawns."""

import subprocess

from coordinator_core.git import commit as gcommit
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.ceremony import commit_source
from coordinator_core.telemetry import spawn_counter

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=True, **_NOWIN
    )


def _write(repo, rel, data: bytes):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo, "a.txt", b"a0\n")
    _write(repo, "b.txt", b"b0\n")
    _write(repo, "sub/c.txt", b"c0\n")
    _write(repo, "gone.txt", b"g0\n")
    _write(repo, ".gitattributes", b"*.bin -text\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _reader(repo, **kw):
    kw.setdefault("deleted_paths", ())
    return commit_source.post_commit_reader(repo, resolve_git_common_dir(repo), **kw)


def _landed(repo, path):
    r = subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=str(repo), capture_output=True, **_NOWIN
    )
    return r.stdout if r.returncode == 0 else None


def test_default_mode_matches_landed_tree(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", b"a1\n")
    _write(repo, "new/n.txt", b"n1\n")
    _write(repo, "b.txt", b"b-unlanded\n")
    read = _reader(repo, paths=["a.txt", "new/n.txt"])
    pre = {p: read(p) for p in ("a.txt", "new/n.txt", "b.txt", "sub/c.txt", "nope")}
    gcommit.commit_paths(repo, ["a.txt", "new/n.txt"], "m")
    for p, got in pre.items():
        assert got == _landed(repo, p), p
    assert pre["b.txt"] == b"b0\n"
    assert pre["nope"] is None


def test_prefer_staged_returns_index_blob(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", b"STAGED\n")
    _git(repo, "add", "--", "a.txt")
    _write(repo, "a.txt", b"WORKTREE\n")
    read = _reader(repo, paths=["a.txt"], prefer_staged=["a.txt"])
    got = read("a.txt")
    gcommit.commit_paths(repo, ["a.txt"], "m", prefer_staged=["a.txt"])
    assert got == b"STAGED\n" == _landed(repo, "a.txt")


def test_prefer_deliberate_stage_only_when_index_differs_from_head(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", b"STAGED\n")
    _git(repo, "add", "--", "a.txt")
    _write(repo, "a.txt", b"WORKTREE\n")
    _write(repo, "b.txt", b"b-edit\n")
    paths = ["a.txt", "b.txt"]
    read = _reader(repo, paths=paths, prefer_deliberate_stage=True)
    pre = {p: read(p) for p in paths}
    gcommit.commit_paths(repo, paths, "m", prefer_deliberate_stage=True)
    assert pre["a.txt"] == b"STAGED\n"
    assert pre["b.txt"] == b"b-edit\n"
    for p in paths:
        assert pre[p] == _landed(repo, p)


def test_deletion_reads_none(tmp_path):
    repo = _repo(tmp_path)
    (repo / "gone.txt").unlink()
    read = _reader(repo, paths=["a.txt"], deleted_paths=["gone.txt"])
    assert read("gone.txt") is None
    gcommit.commit_paths(repo, [], "remove gone.txt", deleted_paths=["gone.txt"])
    assert _landed(repo, "gone.txt") is None
    assert read("a.txt") == _landed(repo, "a.txt")


def test_crlf_bytes_pass_through(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "crlf.bin", b"x\r\ny\r\n")
    read = _reader(repo, paths=["crlf.bin"])
    got = read("crlf.bin")
    gcommit.commit_paths(repo, ["crlf.bin"], "m")
    assert got == b"x\r\ny\r\n" == _landed(repo, "crlf.bin")


def test_backslash_paths_normalised(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "sub/c.txt", b"c1\n")
    read = _reader(repo, paths=["sub\\c.txt"])
    assert read("sub\\c.txt") == b"c1\n"
    assert read("sub/c.txt") == b"c1\n"
    assert read("sub\\nothere.txt") is None


def test_zero_spawns_and_no_subprocess_import(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", b"a1\n")
    _git(repo, "add", "--", "a.txt")
    before = spawn_counter.spawn_count()
    read = _reader(
        repo, paths=["a.txt"], prefer_staged=["a.txt"], prefer_deliberate_stage=True
    )
    for p in ("a.txt", "b.txt", "sub/c.txt", "missing"):
        read(p)
        read(p)
    assert spawn_counter.spawn_count() == before
    assert not hasattr(commit_source, "subprocess")
    assert "subprocess" not in open(commit_source.__file__, encoding="utf-8").read()
