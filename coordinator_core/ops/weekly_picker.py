"""Weekly candidate picker: reads a week of daily logs and emits a bounded, ranked packet.

COMPUTE_ONLY and model-free. Candidates are the plans named in the week's daily lines
(`state/week-changelog/<date>.md`); `weekly_doc_files.plans_completed` supplies sha and sizing.
Each candidate carries its daily line and a plan preview capped at PREVIEW_CAP chars; the
packet reports its own estimated tokens and chars. The model step that writes up what is
important or impactful consumes the packet; nothing here calls one.

CLI: ``python -m coordinator_core.ops.weekly_picker [--week YYYY-MM-DD] [--repo DIR] [--index-path FILE]``
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ops import weekly_doc_files
from coordinator_core.ops.measure_token_envelope import estimate_tokens
from coordinator_core.session import record_homes

DAILY_DIR = record_homes.home_rel("week-changelog")
SIZING_ORDER = ("XL", "L", "M", "S", "XS")
NUDGE = (
    "Skim the previews only, never read a plan in full. Write up only what is important "
    "or impactful this week; omit the rest."
)
_PLAN_RE = re.compile(r"docs/plans/[A-Za-z0-9_./-]+?\.md")


def _sizing_rank(sizing: object) -> int:
    """Lower sorts first; a missing or unknown sizing ranks after XS."""
    text = str(sizing or "").strip().upper()
    return SIZING_ORDER.index(text) if text in SIZING_ORDER else len(SIZING_ORDER)


def default_week(today: Optional[datetime.date] = None) -> datetime.date:
    """The most recent Monday strictly before `today`."""
    today = today or datetime.date.today()
    return today - datetime.timedelta(days=(today.weekday() or 7))


def read_dailies(repo: Path, week: datetime.date) -> List[Dict[str, str]]:
    """One row per (day, plan) named in a daily line: {date, plan, line}; absent days are skipped."""
    rows: List[Dict[str, str]] = []
    for offset in range(7):
        day = (week + datetime.timedelta(days=offset)).isoformat()
        path = repo / DAILY_DIR / f"{day}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            for plan in dict.fromkeys(_PLAN_RE.findall(line)):
                rows.append({"date": day, "plan": plan, "line": line.strip()})
    return rows


def build_packet(
    repo: Path,
    week: datetime.date,
    *,
    index_path: Optional[Path] = None,
) -> Dict[str, object]:
    """The ranked packet for the week starting `week`: week, nudge, candidates, unmatched, tokens, chars."""
    until = week + datetime.timedelta(days=7)
    completed: Dict[str, Dict[str, object]] = {}
    for row in weekly_doc_files.plans_completed(
        week.isoformat(), until.isoformat(), index_path=index_path, repo=repo
    ):
        completed.setdefault(str(row["plan"]), row)

    candidates: Dict[str, Dict[str, object]] = {}
    for row in read_dailies(repo, week):
        plan = row["plan"]
        if plan in candidates:
            continue
        done = completed.get(plan, {})
        candidates[plan] = {
            "plan": plan,
            "sha": done.get("sha"),
            "daily_line": row["line"],
            "sizing": done.get("sizing"),
            "date": row["date"],
        }
    previews = weekly_doc_files.plan_previews(
        [(str(c["plan"]), c["sha"]) for c in candidates.values()],  # type: ignore[misc]
        index_path=index_path,
        repo=repo,
    )
    for cand, preview in zip(candidates.values(), previews):
        cand["preview"] = preview
    ranked = sorted(
        candidates.values(),
        key=lambda c: (_sizing_rank(c["sizing"]), c["date"], c["plan"]),
    )
    packet: Dict[str, object] = {
        "week": week.isoformat(),
        "nudge": NUDGE,
        "candidates": ranked,
        "unmatched": len([p for p in completed if p not in candidates]),
    }
    body = json.dumps(packet, ensure_ascii=False)
    packet["chars"] = len(body)
    packet["tokens"] = estimate_tokens(body)
    return packet


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--week", help="Monday of the week, YYYY-MM-DD (default: last Monday)")
    ap.add_argument("--repo", default=".", help="repo root holding state/week-changelog")
    ap.add_argument("--index-path", default=None, help="doc_files sqlite (default: settings-home key)")
    args = ap.parse_args(argv)
    try:
        week = datetime.date.fromisoformat(args.week) if args.week else default_week()
    except ValueError:
        print(f"weekly_picker: --week must be YYYY-MM-DD, got {args.week!r}", file=sys.stderr)
        return 2
    packet = build_packet(
        Path(args.repo), week, index_path=Path(args.index_path) if args.index_path else None
    )
    print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
