"""coordinator_core.completion_receipts.day — one day's receipt heads and the ones no completion entry covers.

Frontmatter reads only; zero git spawns. `day` is `YYYY-MM-DD`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from coordinator_core.completion_receipts.store import current_heads, read_receipts
from coordinator_core.frontmatter.primitives import split_frontmatter

COMPLETED_DIR = "archive/completed"


def _month_and_next(day: str) -> list[str]:
    year, month = int(day[:4]), int(day[5:7])
    nxt = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"
    return [f"{year}-{month:02d}", nxt]


def _covered_ids(worktree_root: Path, months: list[str]) -> set[str]:
    covered: set[str] = set()
    for month in months:
        base = Path(worktree_root) / COMPLETED_DIR / month
        if not base.is_dir():
            continue
        for path in base.glob("*.md"):
            try:
                split = split_frontmatter(path.read_text(encoding="utf-8"))
                fm = yaml.safe_load(split.fm_text) if split else None
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(fm, dict) or not isinstance(fm.get("receipts"), list):
                continue
            covered.update(
                r["receipt_id"]
                for r in fm["receipts"]
                if isinstance(r, dict) and r.get("receipt_id")
            )
    return covered


def receipts_for_day(worktree_root: Path, day: str) -> dict:
    """`{day, receipts: [{receipt_id, path, baton_id, deliverable_id, verdict}], uncovered: [receipt_id]}`.

    Receipts are the heads whose `concluded_at` date is `day`; uncovered ones are named by no
    `archive/completed` entry of that month or the next.
    """
    date.fromisoformat(day)
    months = _month_and_next(day)
    heads = current_heads(read_receipts(worktree_root, month=day[:7]))
    rows = [
        {
            "receipt_id": r["receipt_id"],
            "path": r["_path"],
            "baton_id": r.get("baton_id"),
            "deliverable_id": r.get("deliverable_id"),
            "verdict": r.get("verdict"),
        }
        for r in heads.values()
        if str(r.get("concluded_at", ""))[:10] == day
    ]
    rows.sort(key=lambda r: r["receipt_id"])
    covered = _covered_ids(worktree_root, months) if rows else set()
    return {
        "day": day,
        "receipts": rows,
        "uncovered": [r["receipt_id"] for r in rows if r["receipt_id"] not in covered],
    }
