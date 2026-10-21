"""ref_cas_retry: the lost-CAS predicate, the peer-overlap check and the bounded re-attempt loop."""
import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git import ref_cas_retry as rcr

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    (repo / "d").mkdir()
    (repo / "d" / "ours.txt").write_text("ours\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _peer_commit(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"peer {rel}")
    return _git(repo, "rev-parse", "HEAD")


def test_predicate_matches_the_real_commit_paths_text(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(gcommit, "cas_ref", lambda *a, **k: False)
    with pytest.raises(gcommit.CommitRefused) as caught:
        gcommit.commit_paths(repo, ["seed.txt"], "msg")
    assert rcr.is_ref_moved_text(str(caught.value))
    assert rcr.is_ref_moved(caught.value)


def test_predicate_rejects_index_markers_and_subclasses():
    assert not rcr.is_ref_moved_text("index-stat compare-and-swap failed -- index changed")
    assert not rcr.is_ref_moved_text(None)
    text = "compare-and-swap failed -- refs/heads/x moved since abc was captured."

    class Sub(gcommit.CommitRefused):
        pass

    assert rcr.is_ref_moved(gcommit.CommitRefused(text))
    assert not rcr.is_ref_moved(Sub(text))
    assert not rcr.is_ref_moved(ValueError(text))


def test_paths_unchanged_between_disjoint_overlap_and_unreadable(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    disjoint = _peer_commit(repo, "peer/file.txt", "p\n")
    assert rcr.paths_unchanged_between(repo, base, disjoint, ["d/ours.txt", "seed.txt"]) is True
    overlap = _peer_commit(repo, "d/ours.txt", "theirs\n")
    assert rcr.paths_unchanged_between(repo, disjoint, overlap, ["d/ours.txt"]) is False
    added = _peer_commit(repo, "new/x.txt", "x\n")
    assert rcr.paths_unchanged_between(repo, overlap, added, ["new/x.txt"]) is False
    assert rcr.paths_unchanged_between(repo, base, "0" * 40, ["seed.txt"]) is None


def _loop(repo, paths, lose_times, peers):
    calls = []

    def attempt():
        calls.append(rcr.head_sha(repo))
        n = len(calls)
        if n <= len(peers):
            peers[n - 1]()
        return n <= lose_times

    return calls, attempt


def test_lands_on_attempt_two_after_a_disjoint_peer(tmp_path):
    repo = _repo(tmp_path)
    calls, attempt = _loop(repo, ["d/ours.txt"], 1, [lambda: _peer_commit(repo, "p.txt", "p\n")])
    result, refused = rcr.retry_ref_moved(repo, ["d/ours.txt"], attempt, lambda r: r)
    assert (result, refused) == (False, None)
    assert len(calls) == 2 and calls[0] != calls[1]


def test_gives_up_after_the_bound(tmp_path):
    repo = _repo(tmp_path)
    peers = [lambda i=i: _peer_commit(repo, f"p{i}.txt", "p\n") for i in range(10)]
    calls, attempt = _loop(repo, ["d/ours.txt"], 99, peers)
    result, refused = rcr.retry_ref_moved(repo, ["d/ours.txt"], attempt, lambda r: r)
    assert (result, refused) == (True, None)
    assert len(calls) == rcr.REF_CAS_ATTEMPTS == 4


def test_overlap_stops_at_the_first_attempt(tmp_path):
    repo = _repo(tmp_path)
    calls, attempt = _loop(repo, ["d/ours.txt"], 99, [lambda: _peer_commit(repo, "d/ours.txt", "t\n")])
    result, refused = rcr.retry_ref_moved(repo, ["d/ours.txt"], attempt, lambda r: r)
    assert (result, refused) == (True, rcr.REFUSED_OVERLAP)
    assert len(calls) == 1


def test_a_won_attempt_and_an_exception_end_the_loop(tmp_path):
    repo = _repo(tmp_path)
    assert rcr.retry_ref_moved(repo, ["seed.txt"], lambda: "ok", lambda r: False) == ("ok", None)

    def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        rcr.retry_ref_moved(repo, ["seed.txt"], boom, lambda r: True)
