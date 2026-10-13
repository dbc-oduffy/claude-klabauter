"""Rule `changelog-confirm`: every commit of the week has a daily block, lint green, block dates in the week.

Read-only; one `git log` child built from an argv list. `ledger_rows` are mappings (or objects)
with `id`, `status`, `exit_code`. `*-pending-release.md` is not a daily block.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.session.record_homes import home_dir
from coordinator_core.workweek_complete.release.contract import (
    DAILY_BLOCK_RE,
    PENDING_RELEASE_SUFFIX,
    RuleResult,
    last_ledger_row,
    row_field,
)

RULE = "changelog-confirm"
BACKFILL_STEP_IDS = ("backfill", "changelog-backfill", "backfill-week-changelog-gaps")
LINT_STEP_IDS = ("lint", "lint-frontmatter", "changelog-lint")
MAX_LISTED = 20

_close_module = None


def _close():
    """Load `coordinator/bin/workweek-complete-close.py` once, by file path."""
    global _close_module
    if _close_module is None:
        path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "workweek-complete-close.py"
        spec = importlib.util.spec_from_file_location("workweek_complete_close", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _close_module = mod
    return _close_module


def _miss(reason: str, **evidence: object) -> RuleResult:
    return RuleResult(RULE, False, reason, dict(evidence))


def _block_dates(week_dir: Path) -> dict[str, list[str]]:
    """Map `YYYY-MM-DD` to the daily block filenames carrying it; pending-release files excluded."""
    out: dict[str, list[str]] = {}
    for f in sorted(week_dir.glob("*.md")):
        if f.name.endswith(PENDING_RELEASE_SUFFIX):
            continue
        m = DAILY_BLOCK_RE.match(f.name)
        if m:
            out.setdefault(m.group(1), []).append(f.name)
    return out


def _week_commits(root: Path, week_start: str) -> list[tuple[str, set[str]]]:
    """Commits on HEAD since `week_start` (UTC midnight, as `backfill_gaps` walks), newest first.

    Each carries the dates a block may sit under: its UTC date and its local date.
    """
    r = run_git(
        ["-C", str(root), "log", f"--after={week_start}T00:00:00+00:00", "--format=%H %cI", "HEAD"]
    )
    if r.returncode != 0:
        raise RuntimeError(f"git log failed ({r.returncode}): {r.stderr.strip()}")
    commits: list[tuple[str, set[str]]] = []
    for line in r.stdout.splitlines():
        sha, _, iso = line.partition(" ")
        if not sha or not iso:
            continue
        when = datetime.fromisoformat(iso)
        dates = {
            when.astimezone().date().isoformat(),
            when.astimezone(timezone.utc).date().isoformat(),
        }
        commits.append((sha, dates))
    return commits


def evaluate(repo_root: Path | str, *, ledger_rows: Sequence[object]) -> RuleResult:
    root = Path(repo_root)
    week_dir = Path(home_dir(str(root), "week-changelog"))
    close = _close()
    week_start = close.derive_week_start(week_dir / "HEADER.md")
    start = date.fromisoformat(week_start)

    backfill = last_ledger_row(ledger_rows, BACKFILL_STEP_IDS)
    if backfill is None or row_field(backfill, "status") != "ran":
        return _miss("backfill-not-run", week_start=week_start)

    lint = last_ledger_row(ledger_rows, LINT_STEP_IDS)
    if lint is None or row_field(lint, "status") != "ran" or row_field(lint, "exit_code") != 0:
        return _miss("validation-failed", week_start=week_start)

    blocks = _block_dates(week_dir)
    outside = sorted(d for d in blocks if not close._in_week_window(date.fromisoformat(d), start))
    if outside:
        return _miss("block-outside-week", week_start=week_start, dates=outside[:MAX_LISTED])

    commits = _week_commits(root, week_start)
    uncovered = [sha for sha, dates in commits if not (dates & blocks.keys())]
    if uncovered:
        return _miss(
            "uncovered-commits",
            week_start=week_start,
            uncovered_total=len(uncovered),
            uncovered=uncovered[:MAX_LISTED],
        )

    entries = query_records("completion", root, where="status=pending-release", since=week_start)
    entry_paths = sorted(e["path"].replace("\\", "/") for e in entries)
    return RuleResult(
        RULE,
        True,
        evidence={
            "week_start": week_start,
            "commit_count": len(commits),
            "first_sha": commits[-1][0] if commits else "",
            "last_sha": commits[0][0] if commits else "",
            "block_count": sum(len(v) for v in blocks.values()),
            "entries": entry_paths,
        },
    )
