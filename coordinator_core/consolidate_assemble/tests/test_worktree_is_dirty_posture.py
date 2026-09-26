from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.consolidate_assemble import brief, worktree_is_dirty


def _run_git(returncode: int, stdout: str, stderr: str = ""):
    def _fake(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return _fake


_STUB_WORKTREE = "stub-worktree"


def test_clean_tree_is_computed_not_dirty():
    result = worktree_is_dirty(_run_git(0, ""), _STUB_WORKTREE)
    assert result == {"degraded": False, "value": False}


def test_dirty_tree_is_computed_dirty():
    result = worktree_is_dirty(_run_git(0, " M some/file.py\n"), _STUB_WORKTREE)
    assert result == {"degraded": False, "value": True}


def test_failed_probe_is_degraded_not_clean():
    result = worktree_is_dirty(_run_git(128, "", "fatal: not a git repository"), _STUB_WORKTREE)
    assert result["degraded"] is True
    assert "value" not in result
    assert "fatal: not a git repository" in result["evidence"]
    assert result != {"degraded": False, "value": False}


_MY_EMAIL = "me@example.com"
_STALE_WORKTREE = "stub-worktree-other"

_WORKTREE_PORCELAIN = (
    "worktree stub-repo-root\n"
    "HEAD abc1230000000000000000000000000000000000\n"
    "branch refs/heads/work\n"
    "\n"
    "worktree stub-worktree-other\n"
    "HEAD def4560000000000000000000000000000000000\n"
    "branch refs/heads/stale-branch\n"
)


def _fake_run_git_degraded_dirty_probe(argv: list[str], cwd: Path):
    if argv[:2] == ["rev-parse", "--abbrev-ref"]:
        stdout = "work\n"
    elif argv[:2] == ["rev-parse", "--verify"]:
        stdout = "main\n" if argv[-1] == "main" else ""
    elif argv[0] == "branch":
        stdout = "  work\n  main\n  stale-branch\n"
    elif argv[0] == "for-each-ref":
        stdout = "".join(
            f"refs/heads/{n}\t{n}\t{_MY_EMAIL}\n" for n in ("work", "main", "stale-branch")
        )
    elif argv[0] == "worktree":
        stdout = _WORKTREE_PORCELAIN
    elif argv[0] == "log":
        stdout = f"{_MY_EMAIL}\n"
    elif argv[0] == "merge-base":
        stdout = ""
    elif argv[:2] == ["--no-optional-locks", "status"]:
        return subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="fatal: not a git repository"
        )
    else:
        stdout = ""
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_degraded_dirty_probe_call_site_routes_through_judgment_point():
    decision_object = brief(
        repo_root=Path("stub-repo-root"),
        my_email=_MY_EMAIL,
        run_git=_fake_run_git_degraded_dirty_probe,
    )

    worktrees_report = decision_object["gates"]["worktrees"]
    stale_entry = next(w for w in worktrees_report if w["path"] == _STALE_WORKTREE)
    assert stale_entry["dirty_probe_degraded"] is True
    assert stale_entry["dirty"] is True

    remove_directives = [
        d
        for d in decision_object["directives"]
        if d["cli"] == "worktree-remove" and d["args"] == [_STALE_WORKTREE]
    ]
    assert len(remove_directives) == 1
    remove_directive = remove_directives[0]
    assert remove_directive["depends_on"] is not None

    jp_id = remove_directive["depends_on"]
    matching_jps = [jp for jp in decision_object["judgment_points"] if jp["id"] == jp_id]
    assert len(matching_jps) == 1
    assert "cannot confirm clean" in matching_jps[0]["question"]
    assert matching_jps[0]["evidence"]["probe_degraded"] is True
