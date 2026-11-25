"""Stamp-ancestry gate: verdicts, the trial-then-merge sequence and its refusals, on tmp_path repos."""

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.ops.dispatch_emit import land_stamp_ancestry as lsa


def _g(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _commit(root: Path, name: str, text: str, msg: str) -> str:
    (root / name).write_text(text, encoding="utf-8", newline="\n")
    _g(root, "add", name)
    _g(root, "commit", "-q", "-m", msg)
    return _g(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _g(root, "init", "-q", "-b", "main")
    _g(root, "config", "user.name", "t")
    _g(root, "config", "user.email", "t@example.invalid")
    _g(root, "config", "commit.gpgsign", "false")
    _g(root, "config", "core.autocrlf", "false")
    _commit(root, "a.txt", "1\n2\n3\n", "base a")
    _commit(root, "b.txt", "x\ny\nz\n", "base b")
    return root


def _stamp_on_side(root: Path, name: str, text: str) -> str:
    _g(root, "checkout", "-q", "-b", "side", "HEAD")
    sha = _commit(root, name, text, "stamp")
    _g(root, "checkout", "-q", "main")
    return sha


def test_live_stamp_reads_the_running_engine_root_in_process(monkeypatch):
    from coordinator_core.warm import skew

    seen = []
    monkeypatch.setattr(skew, "read_engine_stamp_sha", lambda root: seen.append(Path(root)) or "ab12")
    monkeypatch.setattr(lsa, "run_git", lambda *a, **k: pytest.fail("spawned"))
    assert lsa.live_stamp_sha() == "ab12"
    assert (seen[0] / "coordinator_core" / "ops" / "dispatch_emit" / "land_stamp_ancestry.py").is_file()


def test_unstamped_tree_yields_none(monkeypatch, tmp_path):
    from coordinator_core.warm import skew

    # An engine root with no `_engine_stamp` is a dev checkout: unknown, never a merge target.
    assert skew.read_engine_stamp_sha(tmp_path) is None
    monkeypatch.setattr(skew, "_engine_stamp_path", lambda root: tmp_path / "absent")
    assert lsa.live_stamp_sha() is None


def test_none_stamp_is_unknown_with_zero_spawns(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(lsa, "run_git", boom)
    assert lsa.check(repo, None) == lsa.Verdict(lsa.UNKNOWN, None)


def test_current_when_stamp_is_ancestor(repo):
    head = _g(repo, "rev-parse", "HEAD")
    assert lsa.check(repo, head).state == lsa.CURRENT


def test_behind_when_stamp_not_in_head(repo):
    stamp = _stamp_on_side(repo, "b.txt", "x\nY\nz\n")
    _commit(repo, "c.txt", "c\n", "main moves")
    assert lsa.check(repo, stamp) == lsa.Verdict(lsa.BEHIND, stamp)


def test_unknown_when_object_absent(repo):
    assert lsa.check(repo, "0" * 40).state == lsa.UNKNOWN


def test_check_is_one_spawn(repo, monkeypatch):
    calls = []
    real = lsa.run_git
    monkeypatch.setattr(lsa, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    lsa.check(repo, _g(repo, "rev-parse", "HEAD"))
    assert len(calls) == 1


def test_clean_merge(repo, monkeypatch):
    stamp = _stamp_on_side(repo, "b.txt", "x\nY\nz\n")
    _commit(repo, "c.txt", "c\n", "main moves")
    calls = []
    real = lsa.run_git
    monkeypatch.setattr(lsa, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    out = lsa.merge_stamp(repo, stamp, "main")
    assert out["merged"] is True and out["sha"] == stamp
    assert out["merge_commit"] == _g(repo, "rev-parse", "HEAD")
    assert len(calls) == 2
    assert lsa.check(repo, stamp).state == lsa.CURRENT
    assert (repo / "b.txt").read_text(encoding="utf-8") == "x\nY\nz\n"


def test_conflicting_trial_leaves_everything_untouched(repo):
    stamp = _stamp_on_side(repo, "a.txt", "1\nSIDE\n3\n")
    head = _commit(repo, "a.txt", "1\nMAIN\n3\n", "main edits same line")
    (repo / "b.txt").write_text("dirty\n", encoding="utf-8")
    before = {p: (repo / p).read_bytes() for p in ("a.txt", "b.txt")}
    out = lsa.merge_stamp(repo, stamp, "main")
    assert out["refused"] == "stamp-merge-conflict"
    assert out["stamp_sha"] == stamp
    assert out["remedy"] == f"git merge {stamp}"
    assert stamp in out["message"] and "main" in out["message"] and out["remedy"] in out["message"]
    assert "re-fire" in out["message"]
    assert _g(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert {p: (repo / p).read_bytes() for p in before} == before


def test_dirt_blocked_merge_refuses_and_keeps_the_dirty_file(repo):
    stamp = _stamp_on_side(repo, "b.txt", "x\nY\nz\n")
    head = _commit(repo, "c.txt", "c\n", "main moves")
    (repo / "b.txt").write_text("x\ny\nmine\n", encoding="utf-8", newline="\n")
    before = (repo / "b.txt").read_bytes()
    out = lsa.merge_stamp(repo, stamp, "main")
    assert out["refused"] == "stamp-merge-conflict" and out["stamp_sha"] == stamp
    assert out["detail"] != "merge-in-progress"
    assert (repo / "b.txt").read_bytes() == before, out
    assert _g(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_staged_change_on_untouched_path_refuses_with_index_unchanged(repo):
    stamp = _stamp_on_side(repo, "b.txt", "x\nY\nz\n")
    head = _commit(repo, "c.txt", "c\n", "main moves")
    (repo / "a.txt").write_text("1\n2\nstaged\n", encoding="utf-8", newline="\n")
    _g(repo, "add", "a.txt")
    index_before = _g(repo, "ls-files", "-s")
    out = lsa.merge_stamp(repo, stamp, "main")
    assert out["refused"] == "stamp-merge-conflict"
    assert _g(repo, "ls-files", "-s") == index_before
    assert _g(repo, "rev-parse", "HEAD") == head


def test_merge_tree_error_rc_is_unknown_not_a_refusal(repo, monkeypatch):
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return GitResult(2, False, "", "fatal: unsupported")

    monkeypatch.setattr(lsa, "run_git", fake)
    out = lsa.merge_stamp(repo, "a" * 40, "main")
    assert out["state"] == lsa.UNKNOWN and "refused" not in out
    assert len(calls) == 1


def test_started_merge_that_leaves_merge_head_is_merge_in_progress(repo, monkeypatch):
    stamp = _stamp_on_side(repo, "b.txt", "x\nY\nz\n")
    _commit(repo, "c.txt", "c\n", "main moves")
    hook = repo / ".git" / "hooks" / "pre-merge-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    out = lsa.merge_stamp(repo, stamp, "main")
    assert out["refused"] == "stamp-merge-conflict"
    assert out["detail"] == "merge-in-progress"
    assert out["remedy"] == "git merge --continue"
    assert (repo / ".git" / "MERGE_HEAD").exists()


def test_refusal_is_registered_under_head_integrity():
    from coordinator_core import deny_registry as dr
    from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg

    assert dr.LANDING_REFUSAL_CLASS["stamp-merge-conflict"] == dr.HEAD_INTEGRITY
    assert reg.LAND_REFUSAL_REGISTRY["stamp-merge-conflict"].harm == dr.HEAD_INTEGRITY
