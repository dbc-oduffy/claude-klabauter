from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca

_REF_ROWS = [
    ("refs/heads/current", "current", "me@x"),
    ("refs/heads/main", "main", "me@x"),
    ("refs/heads/work/stale", "work/stale", "me@x"),
]


def _fake_git(log_ranges: list[str], main_exists: bool):
    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        if args[:2] == ["config", "user.email"]:
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
        if args[:2] == ["rev-parse", "--verify"]:
            ok = main_exists and args[-1] == "main"
            return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
        if args[0] == "for-each-ref":
            rows = _REF_ROWS if main_exists else [r for r in _REF_ROWS if r[1] != "main"]
            out = "".join(f"{r}\t{s}\t{e}\n" for r, s, e in rows)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[0] == "log" and args[1] == "--oneline":
            log_ranges.append(args[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "worktree":
            return SimpleNamespace(
                returncode=0, stdout="worktree /repo\nHEAD abc\nbranch refs/heads/current\n", stderr=""
            )
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run_git


def test_brief_counts_unique_commits_against_main_not_the_current_branch() -> None:
    ranges: list[str] = []
    ca.brief(repo_root=Path("/repo"), run_git=_fake_git(ranges, main_exists=True))

    assert ranges == ["main..work/stale"]


def test_brief_falls_back_to_current_when_no_main_branch_exists() -> None:
    ranges: list[str] = []
    ca.brief(repo_root=Path("/repo"), run_git=_fake_git(ranges, main_exists=False))

    assert ranges == ["current..work/stale"]
