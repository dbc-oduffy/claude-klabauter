"""dispatch.commit_merge: a resolved two-parent merge commits; stale or unresolved input refuses; the sync runs before HEAD moves."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.ops.dispatch_emit import merge_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _build(tmp_path: Path, n_conflicted: int = 2, n_deleted: int = 1, executable: bool = False):
    """A real conflicting merge: HEAD is ``ours``; returns ``(root, conflicted, params)``."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "false")):
        _git(["config", key, value], root)
    conflicted = [f"src/c{i}.txt" for i in range(n_conflicted)]
    deleted = [f"gone{i}.txt" for i in range(n_deleted)]
    if executable:
        conflicted.append("run.sh")
    for name in conflicted:
        _write(root, name, "base\n")
    for name in deleted:
        _write(root, name, "doomed\n")
    _write(root, "clean.txt", "base\n")
    _write(root, "unrelated.txt", "base\n")
    if executable:
        (root / "run.sh").chmod(0o755)
        _git(["add", "--chmod=+x", "run.sh"], root)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "base"], root)
    _git(["checkout", "-q", "-b", "theirs"], root)
    for name in conflicted:
        _write(root, name, "theirs\n")
    for name in deleted:
        (root / name).unlink()
    _write(root, "clean.txt", "from theirs\n")
    _write(root, "added.txt", "new on theirs\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "theirs"], root)
    _git(["checkout", "-q", "main"], root)
    for name in conflicted:
        _write(root, name, "ours\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "ours"], root)
    for name in conflicted:
        _write(root, f"resolved/{name}", f"resolved {name}\n")
    params = {
        "theirs_ref": "theirs",
        "resolved_dir": "resolved",
        "conflicted": sorted(conflicted),
        "message": "Merge theirs into main\n\nbody line",
        "expected_head": _git(["rev-parse", "HEAD"], root).strip(),
    }
    return root, conflicted, params


def _call(root: Path, params: dict) -> dict:
    return merge_commit._handler(params, repo_root=root / ".git")


def _head(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).strip()


def _index(root: Path) -> str:
    return _git(["ls-files", "-s"], root)


def test_two_parent_merge_commits_with_resolved_bytes_and_keeps_modes(tmp_path):
    root, conflicted, params = _build(tmp_path, n_deleted=3, executable=True)
    old = params["expected_head"]
    theirs = _git(["rev-parse", "theirs"], root).strip()

    out = _call(root, params)

    assert out["ok"] is True, out
    assert out["parents"] == [old, theirs]
    assert _head(root) == out["commit"]
    assert _git(["rev-list", "--parents", "-n1", "HEAD"], root).split()[1:] == [old, theirs]
    for name in conflicted:
        assert _git(["show", f"HEAD:{name}"], root) == f"resolved {name}\n"
    assert _git(["ls-tree", "HEAD", "run.sh"], root).startswith("100755")
    assert _git(["show", "HEAD:clean.txt"], root) == "from theirs\n"
    assert _git(["show", "-s", "--format=%B", "HEAD"], root).startswith("Merge theirs into main\n\nbody line")
    assert "commit (merge): Merge theirs into main" in _git(["reflog", "-n1", "HEAD"], root)
    assert _git(["status", "--porcelain", "--untracked-files=no"], root) == ""
    assert list((root / ".git").glob("commit-merge-*")) == []


def test_clean_paths_theirs_deleted_leave_index_and_worktree(tmp_path):
    root, _, params = _build(tmp_path, n_deleted=3)

    out = _call(root, params)

    assert out["ok"] is True, out
    assert sorted(out["synced"]["removed"]) == ["gone0.txt", "gone1.txt", "gone2.txt"]
    for name in ("gone0.txt", "gone1.txt", "gone2.txt"):
        assert not (root / name).exists()
        assert name not in _git(["ls-files"], root).split()
    assert (root / "added.txt").read_text() == "new on theirs\n"


@pytest.mark.parametrize("code", ["conflict-set-changed", "head-moved", "conflict-marker", "resolved-missing"])
def test_refusal_leaves_head_and_shared_index_unchanged(tmp_path, code):
    root, conflicted, params = _build(tmp_path)
    old, index = _head(root), _index(root)
    if code == "conflict-set-changed":
        params["conflicted"] = [conflicted[0], "ghost.txt"]
    elif code == "head-moved":
        params["expected_head"] = _git(["rev-parse", "HEAD~1"], root).strip()
    elif code == "conflict-marker":
        _write(root, f"resolved/{conflicted[0]}", "ok\n<<<<<<< HEAD\nours\n")
    else:
        (root / "resolved" / conflicted[0]).unlink()

    out = _call(root, params)

    assert out["ok"] is False and out["refused"] == code, out
    if code == "conflict-set-changed":
        assert out["detail"] == {"new": [conflicted[1]], "gone": ["ghost.txt"]}
    if code == "conflict-marker":
        assert out["detail"] == [conflicted[0]]
    assert _head(root) == old
    assert _index(root) == index


def test_rule_line_in_a_resolved_file_is_not_a_marker(tmp_path):
    root, conflicted, params = _build(tmp_path)
    _write(root, f"resolved/{conflicted[0]}", "Title\n=======\ntext\n")

    out = _call(root, params)

    assert out["ok"] is True, out
    assert _git(["show", f"HEAD:{conflicted[0]}"], root) == "Title\n=======\ntext\n"


def test_bad_params_refuse(tmp_path):
    root, _, params = _build(tmp_path)
    for patch in ({"expected_head": "abc"}, {"conflicted": ["../x"]}, {"resolved_dir": "../outside"}, {"message": " "}):
        out = _call(root, {**params, **patch})
        assert out["refused"] == "bad-params", (patch, out)


def test_worktree_dirty_path_keeps_its_bytes_and_index_moves_to_new(tmp_path):
    root, _, params = _build(tmp_path)
    _write(root, "clean.txt", "local edit\n")

    out = _call(root, params)

    assert out["ok"] is True, out
    assert out["synced"]["index_only"] == ["clean.txt"]
    assert (root / "clean.txt").read_text() == "local edit\n"
    staged = _git(["ls-files", "-s", "clean.txt"], root).split()[1]
    assert staged == _git(["rev-parse", "HEAD:clean.txt"], root).strip()


def test_path_staged_differently_before_the_call_is_left_staged(tmp_path):
    root, _, params = _build(tmp_path)
    _write(root, "clean.txt", "staged edit\n")
    _git(["add", "clean.txt"], root)
    before = _git(["ls-files", "-s", "clean.txt"], root)

    out = _call(root, params)

    assert out["ok"] is True, out
    assert out["left_staged"] == ["clean.txt"]
    assert _git(["ls-files", "-s", "clean.txt"], root) == before
    assert (root / "clean.txt").read_text() == "staged edit\n"


def _spy(monkeypatch, replace=None, after=None):
    real = merge_commit.run_git
    calls: list = []

    def wrapper(args, **kw):
        calls.append(list(args))
        if replace is not None:
            swapped = replace(list(args))
            if swapped is not None:
                return swapped
        result = real(args, **kw)
        if after is not None:
            after(list(args))
        return result

    monkeypatch.setattr(merge_commit, "run_git", wrapper)
    return calls


def test_unexpected_staged_path_refuses_with_head_unmoved_and_sync_restored(tmp_path, monkeypatch):
    root, _, params = _build(tmp_path)
    _write(root, "extra.txt", "intruder\n")
    old = _head(root)

    def stage_extra(args):
        if "checkout" in args:
            subprocess.run(["git", "-C", str(root), "add", "extra.txt"], check=True, **no_console_creationflags())

    _spy(monkeypatch, after=stage_extra)

    out = _call(root, params)

    assert out["ok"] is False and out["refused"] == "unexpected-staged", out
    assert out["detail"] == ["extra.txt"]
    assert _head(root) == old
    assert (root / "clean.txt").read_text() == "base\n"
    assert _git(["diff", "--cached", "--name-only", old], root).split() == ["extra.txt"]


def test_sync_failure_before_head_moves_leaves_head_and_deletes_untouched(tmp_path, monkeypatch):
    root, _, params = _build(tmp_path, n_deleted=3)
    old, index = _head(root), _index(root)

    def fail_rm(args):
        if "rm" in args:
            return GitResult(returncode=1, stdout="", stderr="refused", timed_out=False)
        return None

    _spy(monkeypatch, replace=fail_rm)

    out = _call(root, params)

    assert out["ok"] is False and out["refused"] == "sync-failed", out
    assert _head(root) == old
    assert _index(root) == index
    assert all((root / f"gone{i}.txt").exists() for i in range(3))
    assert (root / "clean.txt").read_text() == "base\n"
    assert not (root / "added.txt").exists()


def test_spawn_count_is_independent_of_conflicted_path_count(tmp_path, monkeypatch):
    counts = []
    for n in (1, 6):
        root, _, params = _build(tmp_path / f"n{n}", n_conflicted=n)
        calls = _spy(monkeypatch)
        out = _call(root, params)
        assert out["ok"] is True, out
        counts.append(len(calls))
        monkeypatch.undo()
    assert counts[0] == counts[1]
    assert counts[0] <= 20


def test_one_successful_call_stays_under_the_process_time_bar(tmp_path):
    resource = pytest.importorskip("resource")
    root, _, params = _build(tmp_path, n_conflicted=6)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)

    out = _call(root, params)

    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert out["ok"] is True, out
    child_ms = ((after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)) * 1000
    assert child_ms < 500, child_ms
