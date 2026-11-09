"""A registered worktree whose directory is gone is reported prunable, never probed."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.consolidate_assemble import brief, default_run_git, worktree_is_dirty


def test_missing_path_is_prunable_without_spawn(tmp_path):
    gone = tmp_path / "gone-worktree"

    def _cwd_missing(argv, cwd, **kw):
        raise FileNotFoundError(str(cwd))

    result = worktree_is_dirty(_cwd_missing, str(gone))
    assert result["degraded"] is True
    assert result["prunable"] is True
    assert "prunable/stale" in result["evidence"]


def test_oserror_on_existing_path_still_raises(tmp_path):
    def _boom(argv, cwd, **kw):
        raise FileNotFoundError("git")

    try:
        worktree_is_dirty(_boom, str(tmp_path))
    except FileNotFoundError:
        return
    raise AssertionError("an existing worktree path must not be reported prunable")


def test_missing_path_with_real_default_run_git_does_not_raise(tmp_path):
    result = worktree_is_dirty(default_run_git, str(tmp_path / "gone"))
    assert result["degraded"] is True and result["prunable"] is True


def test_existing_path_still_probes(tmp_path):
    seen: list[list[str]] = []

    def _fake(argv, cwd, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert worktree_is_dirty(_fake, str(tmp_path)) == {"degraded": False, "value": False}
    assert seen == [["--no-optional-locks", "status", "--porcelain"]]


def test_brief_reports_missing_worktree_as_prunable_not_dirty(tmp_path):
    gone = str(tmp_path / "gone-worktree")
    root = "stub-repo-root"
    porcelain = (
        f"worktree {root}\nHEAD {'a' * 40}\nbranch refs/heads/work\n\n"
        f"worktree {gone}\nHEAD {'b' * 40}\nbranch refs/heads/stale-branch\n"
    )
    me = "me@example.com"

    def _fake(argv, cwd, **kw):
        out = ""
        if argv[:2] == ["rev-parse", "--abbrev-ref"]:
            out = "work\n"
        elif argv[:2] == ["rev-parse", "--verify"]:
            out = "main\n" if argv[-1] == "main" else ""
        elif argv[0] == "branch":
            out = "  work\n  main\n  stale-branch\n"
        elif argv[0] == "for-each-ref":
            out = "".join(f"refs/heads/{n}\t{n}\t{me}\n" for n in ("work", "main", "stale-branch"))
        elif argv[0] == "worktree":
            out = porcelain
        elif argv[0] == "log":
            out = f"{me}\n"
        elif argv[:2] == ["--no-optional-locks", "status"]:
            raise FileNotFoundError(str(cwd))
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    decision = brief(repo_root=Path(root), my_email=me, run_git=_fake)
    entry = next(w for w in decision["gates"]["worktrees"] if w["path"] == gone)
    assert entry["prunable"] is True
    assert entry["dirty"] is False
    assert entry["dirty_probe_degraded"] is True
