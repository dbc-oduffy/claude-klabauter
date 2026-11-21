"""Pure tests for ops/_anchor_check.py."""

import subprocess

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import _anchor_check
from coordinator_core.ops._anchor_check import evaluate


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """a.py holds PAT at commit X only; the tree has it removed."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_bytes(b"PAT one\r\nPAT two\nother\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-q", "-m", "x")
    (tmp_path / "a.py").write_text("nothing\n", encoding="utf-8")
    return tmp_path


def test_read_at_ref_one_spawn_and_shapes(repo, monkeypatch):
    from coordinator_core.git import run as git_run

    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    got = _anchor_check.read_at_ref(
        repo, [("HEAD", "a.py"), ("HEAD", "nope.py"), ("nonesuch", "a.py"), ("HEAD", "a.py")]
    )
    assert len(calls) == 1
    assert got[("HEAD", "a.py")] == b"PAT one\r\nPAT two\nother\n"
    assert got[("HEAD", "nope.py")] is None
    assert ("nonesuch", "a.py") not in got
    calls.clear()
    assert _anchor_check.read_at_ref(repo, []) == {} and not calls
    assert _anchor_check.read_at_ref(repo, [("a..b", "a.py")]) == {} and not calls


def test_evaluate_base_ref_true_at_ref_absent_in_tree(repo):
    cmd = "grep -c -F PAT a.py"
    blobs = _anchor_check.read_at_ref(repo, _anchor_check.blob_pairs(cmd, "HEAD"))
    held = evaluate(cmd, "2", repo, "HEAD", blobs)
    assert held["state"] == "MATCH" and held["observed"] == "2"
    assert evaluate(cmd, "2", repo)["state"] == "MISMATCH"


def test_evaluate_stage_ref_wins_over_base_ref(repo):
    cmd = "git grep -c -F PAT HEAD -- a.py"
    pairs = _anchor_check.blob_pairs(cmd, "other")
    assert pairs == [("HEAD", "a.py")]
    blobs = _anchor_check.read_at_ref(repo, pairs)
    assert evaluate(cmd, "2", repo, "other", blobs)["state"] == "MATCH"
    assert evaluate(cmd, "2", repo, None, blobs)["state"] == "MATCH"


def test_evaluate_missing_path_at_ref_is_mismatch(repo):
    cmd = "grep -c PAT nope.py"
    blobs = _anchor_check.read_at_ref(repo, _anchor_check.blob_pairs(cmd, "HEAD"))
    r = evaluate(cmd, "1", repo, "HEAD", blobs)
    assert r["state"] == "MISMATCH" and "no such file at HEAD" in r["detail"]


def test_evaluate_unreadable_ref_is_not_evaluable(repo):
    cmd = "grep -c PAT a.py"
    blobs = _anchor_check.read_at_ref(repo, _anchor_check.blob_pairs(cmd, "nonesuch"))
    r = evaluate(cmd, "1", repo, "nonesuch", blobs)
    assert r["state"] == "NOT_EVALUABLE" and r["detail"] == "base ref nonesuch unreadable"
    assert evaluate(cmd, "1", repo, "HEAD")["state"] == "NOT_EVALUABLE"


def test_default_without_base_ref_reads_tree_and_spawns_nothing(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn attempted")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert evaluate("grep -c nothing a.py", "1", repo)["state"] == "MATCH"
