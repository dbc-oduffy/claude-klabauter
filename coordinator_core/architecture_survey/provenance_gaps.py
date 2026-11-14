"""Targeted atlas read-set and daily-summary provenance gaps; no model call.

`read_set` is exactly the mechanical report's `pages_changed` (11a reads only these pages).
`provenance_gaps` is each commit date in the window with no `archive/daily-summaries/<date>.md` or `<date>-<suffix>.md`,
one `git log` spawn and one directory listing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from coordinator_core.git.run import run_git

DAILY_DIR = Path("archive") / "daily-summaries"
DEFAULT_WINDOW = "--since=14.days"
_DAILY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-[^/]+)?\.md$")
REPORT_DIR = Path("scratch") / "atlas-mechanical"


def commit_dates(repo_root: Path, window: str) -> list[str]:
    """Distinct commit dates (YYYY-MM-DD) in `window`; one `git log` spawn.

    `window` reaches git's argv from the CLI, so a leading-dash value other than a
    `--since=`/`--after=` bound (`--output=<file>` writes a file) is refused.
    """
    if window.startswith("-") and not window.startswith(("--since=", "--after=")):
        raise ValueError(f"unsupported git-log window: {window!r}")
    proc = run_git(["-C", str(repo_root), "log", "--format=%cs", window])
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, "git log", proc.stdout, proc.stderr)
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def present_daily_dates(repo_root: Path) -> set[str]:
    """Dates that have a daily-summary file; one `os.listdir`."""
    try:
        names = os.listdir(repo_root / DAILY_DIR)
    except FileNotFoundError:
        return set()
    return {m.group(1) for n in names if (m := _DAILY_NAME.match(n))}


def provenance_gaps(repo_root: Path, window: str = DEFAULT_WINDOW) -> list[dict[str, str]]:
    """Backfill items: commit dates in `window` lacking a daily summary."""
    have = present_daily_dates(repo_root)
    return [{"date": d, "missing": f"{DAILY_DIR.as_posix()}/{d}.md", "action": "backfill daily summary"}
            for d in commit_dates(repo_root, window) if d not in have]


def read_set(report: dict[str, Any]) -> list[str]:
    """The pages 11a may read: the report's `pages_changed`, sorted, nothing else."""
    return sorted(report.get("pages_changed", []))


def build(repo_root: Path | str, report: dict[str, Any], window: str = DEFAULT_WINDOW) -> dict[str, Any]:
    """The JSON payload: `read_set` from `report`, `provenance_gaps` from git and the daily dir."""
    root = Path(repo_root)
    return {"read_set": read_set(report), "provenance_gaps": provenance_gaps(root, window), "window": window}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--report", default=None, help="mechanical report JSON (default: today's under scratch/atlas-mechanical)")
    parser.add_argument("--window", default=DEFAULT_WINDOW, help="a single git-log window argument, e.g. --since=14.days or A..B")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    path = Path(args.report) if args.report else root / REPORT_DIR / f"{date.today().isoformat()}-report.json"
    if not path.is_file():
        print(f"no mechanical report at {path}; run coordinator_core.architecture_survey.mechanical_report first", file=sys.stderr)
        return 2
    report = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(build(root, report, args.window), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
