"""Compose a missing ``archive/daily-summaries/<day>-<machine>.md`` from the commit log.

Pure composition: reads git (one ``git log --all`` for the day window, one
``git remote get-url`` for the repo slug) and returns the target path and file
text. It never writes. The day's commits come from every ref, so work that
lives only on a side branch is covered. Content is only what the commit log
supports: subjects under ``## Work Completed``, no narrative.

The tip is resolved by the caller-supplied ``resolve_tip`` so the CLI that owns
the descendant-tip walk stays its single implementation.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.git.run import GitResult, run_git

_SEP = "\x1f"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GapRefusal(ValueError):
    """The day cannot be composed (bad date, file exists, no commits, diverged tips)."""


def _git(worktree: Path, *args: str) -> GitResult:
    return run_git(["-C", str(worktree), *args])


def existing_summary(worktree: Path, date: str) -> Optional[Path]:
    """The first ``<date>-*.md`` or ``<date>.md`` already under archive/daily-summaries, else None."""
    ds_dir = Path(worktree) / "archive" / "daily-summaries"
    if not ds_dir.is_dir():
        return None
    for cand in sorted(ds_dir.glob(f"{date}-*.md")) + [ds_dir / f"{date}.md"]:
        if cand.is_file():
            return cand
    return None


def _repo_slug(worktree: Path) -> str:
    """``owner/name`` from origin's URL; the worktree directory name when there is no origin."""
    proc = _git(worktree, "remote", "get-url", "origin")
    url = proc.stdout.strip() if proc.returncode == 0 else ""
    m = re.search(r"([^/:]+/[^/:]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else Path(worktree).resolve().name


def _day_commits(worktree: Path, date: str) -> "list[tuple[str, list[str], str]]":
    proc = _git(
        worktree, "log", "--all", "--no-merges",
        f"--since={date} 00:00:00", f"--until={date} 23:59:59",
        f"--format=%H{_SEP}%P{_SEP}%s",
    )
    if proc.returncode != 0:
        raise GapRefusal(f"git log failed for {date}: {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split(_SEP, 2)
        if len(parts) == 3 and parts[0]:
            rows.append((parts[0], parts[1].split(), parts[2]))
    return rows


def compose_daily_summary_gap(
    worktree: "Path | str",
    date: str,
    machine: str,
    resolve_tip: Callable[[str, "list[str]"], Optional[str]],
    today: Optional[str] = None,
) -> "tuple[Path, str]":
    """Return ``(path, text)`` for the missing summary of ``date``.

    ``resolve_tip(root, shas)`` returns the one sha descending every other, or
    None when the day's branches diverge. Raises GapRefusal when ``date`` is
    malformed, a summary for the day already exists, the day has no commits on
    any ref, or the tips diverge.
    """
    worktree = Path(worktree)
    if not _ISO_DATE_RE.match(date or ""):
        raise GapRefusal(f"DATE must be YYYY-MM-DD (got {date!r})")
    try:
        datetime.date.fromisoformat(date)
    except ValueError as exc:
        raise GapRefusal(f"DATE is not a real day: {date!r}") from exc
    if not (machine or "").strip():
        raise GapRefusal("machine must be non-empty")

    found = existing_summary(worktree, date)
    if found is not None:
        raise GapRefusal(f"summary already exists: {found}")

    commits = _day_commits(worktree, date)
    if not commits:
        raise GapRefusal(f"no commits on any ref for {date}")

    in_day = {sha for sha, _, _ in commits}
    parents_in_day = {p for _, parents, _ in commits for p in parents if p in in_day}
    leaves = [sha for sha, _, _ in commits if sha not in parents_in_day]
    tip = resolve_tip(str(worktree), leaves)
    if not tip:
        raise GapRefusal(f"no single descendant tip for {date} (diverged branches)")

    today = today or datetime.date.today().strftime("%Y-%m-%d")
    path = worktree / "archive" / "daily-summaries" / f"{date}-{machine}.md"
    bullets = "\n".join(f"- {subject} ({sha[:10]})" for sha, _, subject in commits)
    text = (
        "---\n"
        "rollup_schema: 1\n"
        "grain: day\n"
        f"period: {date}\n"
        f"repo: {_repo_slug(worktree)}\n"
        f"machine: {machine}\n"
        f"commits: {len(commits)}\n"
        f"covered_tip_sha: {tip}\n"
        f"covered_machine: {machine}\n"
        "backfilled: true\n"
        "---\n"
        f"# Daily Summary — {date}\n\n"
        f"> Generated: {today} by workday-complete-backfill-anchor write-gap "
        "(mechanical, from the day's commit log across all refs)\n"
        f"> Commits: {len(commits)} | Files changed: n/a\n"
        f"covered_tip_sha: {tip}\n"
        f"covered_machine: {machine}\n\n"
        "## Work Completed\n"
        f"{bullets}\n"
    )
    return path, text
