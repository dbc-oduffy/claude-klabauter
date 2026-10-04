"""Branch-reconcile family of `orient-assemble brief`.

Requirements served (docs/research/2026-10-04-orient-brief-requirements.md):
    REQ-B1  active branch's day span covers today -> `d-branch-span-mismatch`.
    REQ-B2  retired (backing op gravestoned; the old reader emitted nothing).
    REQ-B3  retired by EM ruling 2026-10-04 (dirty-tree read).

Spawn budget: zero. REQ-B1 needs only the current branch name, which the
worktree's HEAD file carries; resolving the gitdir walks the filesystem
(`repo_root.git_dir`, gitlink-aware) and never spawns, so DR-344's "git
justifies itself per use" is answered by not using git. Any future read here
that needs the object graph must name its requirement id in its own docstring.

Trap: the family runs no `status`, `diff-index`, `diff`, `ls-files` or `fetch`
(REQ-B3 retired, REQ-C10 read-only); the test module pins that.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.contract.decision_object.reader_result import (
    ReaderResult,
    build_directive,
)
from coordinator_core.git import repo_root as _repo_root_mod

_REF_PREFIX = "ref: refs/heads/"


def _current_branch(repo_root: Path) -> str:
    """Current branch name, or "" when detached / unreadable. Zero spawns.

    A branch name may contain slashes (`work/<machine>/<date>`), so the full
    suffix after `refs/heads/` is returned.
    """
    git_dir = _repo_root_mod.git_dir(str(repo_root))
    if not git_dir:
        return ""
    try:
        head = (Path(git_dir) / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return head[len(_REF_PREFIX):] if head.startswith(_REF_PREFIX) else ""


def _span_mismatch(branch: str, repo_root: Path) -> dict | None:
    """REQ-B1: the `d-branch-span-mismatch` directive, or None.

    Cheap checks run first: a branch that does not parse as a span, or whose
    span is today's, never pays for the machine-name or day resolution.
    """
    from coordinator_core.daily_branch import format_span_suffix, parse_branch_span
    from coordinator_core.daily_day import local_day

    span = parse_branch_span(branch)
    if span is None:
        return None
    start, end = span
    today = local_day(str(repo_root))
    if end == today:
        return None

    from coordinator_core.machine_resolver import compute_machine

    expected = "work/" + compute_machine() + "/" + format_span_suffix(start, today)
    return build_directive(
        "d-branch-span-mismatch",
        "workday-start-day-branch-resolve",
        ["span-assert"],
        f"Active branch `{branch}` does not cover today ({today}) — end={end}, "
        f"expected rename to `{expected}`. Step 0 Check 4 did not fire. The "
        "library helpers work; the rename was skipped at the command level. "
        "Re-run `/workday-start` Step 0 manually or rename inline.",
    )


def collect(cadence: str, *, repo_root: Path) -> ReaderResult:
    branch = _current_branch(repo_root)
    if not branch:
        return ReaderResult()
    directive = _span_mismatch(branch, repo_root)
    return ReaderResult(directives=[directive] if directive else [])
