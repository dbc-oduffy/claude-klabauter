"""IndexView: index-keyed fingerprints, untracked classification, one-spawn blob reads."""
import hashlib
import os
import subprocess

import pytest

from coordinator_core.git import git_state
from coordinator_core.roadmap import census_index
from coordinator_core.roadmap.census_index import IndexView
from coordinator_core.roadmap.census_store import CensusStore

pytestmark = pytest.mark.spawns_process


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert p.returncode == 0, p.stderr.decode(errors="replace")


def _write(repo, rel, data):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _write(r, ".gitignore", "ign/\n")
    _write(r, "a/one.txt", "one\n")
    _write(r, "a/two.txt", "two\n")
    _write(r, "top.txt", "top\n")
    _write(r, "ign/x.txt", "x\n")
    _write(r, "ign/y.txt", "y\n")
    _write(r, "live/tracked.txt", "t\n")
    _write(r, "live/loose.txt", "l\n")
    _write(r, "live/sub/kept.txt", "k\n")
    _git(r, "add", ".gitignore", "a", "top.txt")
    _git(r, "add", "live/tracked.txt", "live/sub/kept.txt")
    return r


def test_classify_file_dir_untracked_absent(repo, monkeypatch):
    real_scandir = os.scandir

    def guarded(path=".", *a, **k):
        if ".git" not in os.fspath(path):
            raise AssertionError("walk")
        return real_scandir(path, *a, **k)

    def boom(*a, **k):
        raise AssertionError("walk")

    with monkeypatch.context() as mp:
        mp.setattr(os, "scandir", guarded)
        mp.setattr(os, "walk", boom)
        v = IndexView(repo)
        assert v.classify("top.txt") == "file"
        assert v.classify("a") == "dir"
        assert v.classify("ign") == "untracked"
        assert v.classify("nope/gone") == "absent"
        assert v.fingerprint("ign") is None
        assert v.fingerprint("nope") == "absent"


def test_live_state_root_is_untracked_even_with_tracked_files(repo):
    v = IndexView(repo, live_state_roots=("live",))
    assert v.classify("live") == "untracked"
    assert v.fingerprint("live") is None
    assert v.classify("live/loose.txt") == "untracked"
    assert v.classify("live/tracked.txt") == "file"
    assert v.classify("live/sub") == "dir"


def test_fingerprint_moves_on_staged_content_only(repo):
    before = IndexView(repo).fingerprint("a")
    _write(repo, "a/new_untracked.txt", "u\n")
    assert IndexView(repo).fingerprint("a") == before
    _write(repo, "a/one.txt", "edited unstaged\n")
    assert IndexView(repo).fingerprint("a") == before
    _git(repo, "add", "a/one.txt")
    assert IndexView(repo).fingerprint("a") != before


def test_dir_fingerprint_is_the_documented_digest(repo):
    v = IndexView(repo)
    snap = git_state.read_index(repo, fresh=True)
    h = hashlib.sha1()
    for p in sorted(x for x in snap if x.startswith("a/")):
        h.update(f"{p}\0{snap[p].mode:o}\0{snap[p].sha}\n".encode())
    assert v.fingerprint("a") == h.hexdigest()
    one = snap["a/one.txt"]
    assert v.fingerprint("a/one.txt") == f"{one.sha}:{one.mode:o}"


def test_unchanged_checksum_serves_fingerprints_without_parsing(repo, monkeypatch):
    store = CensusStore()
    first = IndexView(repo, store)
    want = {r: first.fingerprint(r) for r in ("a", "top.txt", "a/one.txt")}
    assert first.parse_ms >= 0 and first._entries is not None

    calls = []
    real = git_state.read_index
    monkeypatch.setattr(git_state, "read_index", lambda *a, **k: calls.append(1) or real(*a, **k))
    second = IndexView(repo, store)
    got = {r: second.fingerprint(r) for r in ("a", "top.txt", "a/one.txt")}
    assert got == want
    assert calls == []
    assert second.parse_ms == 0


def test_parse_error_falls_back_to_one_spawn_with_equal_fingerprints(repo, monkeypatch):
    want = {r: IndexView(repo).fingerprint(r) for r in ("a", "top.txt", "a/two.txt")}

    def bad(*a, **k):
        raise git_state.IndexParseError("forced")

    monkeypatch.setattr(git_state, "read_index", bad)
    from coordinator_core.git import run as run_mod

    spawned = []
    real = run_mod.run_git
    monkeypatch.setattr(run_mod, "run_git", lambda *a, **k: spawned.append(a[0]) or real(*a, **k))
    v = IndexView(repo)
    got = {r: v.fingerprint(r) for r in ("a", "top.txt", "a/two.txt")}
    assert got == want
    assert len(spawned) == 1 and "ls-files" in spawned[0]


def test_files_lists_tracked_under_rel_sorted(repo):
    v = IndexView(repo)
    got = v.files("a")
    assert [p for p, _ in got] == ["a/one.txt", "a/two.txt"]
    assert v.files("top.txt")[0][0] == "top.txt"
    assert v.files("ign") == []


def test_read_blobs_is_one_spawn_and_byte_exact(repo, monkeypatch):
    _write(repo, "empty.bin", b"")
    _write(repo, "zero.bin", b"a\0b\n\0")
    _git(repo, "add", "empty.bin", "zero.bin")
    v = IndexView(repo)
    oids = {p: o for p, o in v.files("")}
    ask = [oids["empty.bin"], oids["zero.bin"], oids["a/one.txt"]]

    class Cost:
        spawns = 0

    spawned = []
    from coordinator_core.git import run as run_mod

    real = run_mod.run_git
    monkeypatch.setattr(run_mod, "run_git", lambda *a, **k: spawned.append(a[0]) or real(*a, **k))
    cost = Cost()
    got = v.read_blobs(ask + ["0" * 40], cost=cost)
    assert got == {ask[0]: b"", ask[1]: b"a\0b\n\0", ask[2]: b"one\n"}
    assert len(spawned) == 1 and cost.spawns == 1
    assert v.read_blobs([], cost=cost) == {} and cost.spawns == 1


def test_no_index_has_no_checksum(tmp_path):
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    v = IndexView(r)
    assert v.checksum is None
    assert v.classify("x") == "absent"
    assert census_index.IndexView is IndexView
