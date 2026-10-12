"""Wrap-backfill planner: one dispatch entry per missing workday wrap.

Pure parsing of two CLI outputs; runs nothing. The argv spelling (`--for-date <YYYY-MM-DD> --only`)
is taken from `coordinator_core.workday_complete.apply.main`, which forwards both to `apply()`'s
`for_date` / `only_mode`.

`backfill-week-changelog-gaps` (`changelog_ops.backfill_gaps`) reports daily changelog blocks missing
since the week's `Week starting:` date. It does not report workweek-complete wraps, so it yields
no week entries: its dates are folded into day entries (a day it names that the scan did not is
still a day to wrap). A week-level producer would need to list weeks with commits but no
workweek-complete record, as `workday_complete_backfill_scan` does per day.
"""
from __future__ import annotations

import json
import re
from typing import Any

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _scan_days(scan_output: str) -> tuple[dict[str, dict[str, Any]], str]:
    days: dict[str, dict[str, Any]] = {}
    for raw in (scan_output or "").splitlines():
        if not raw.strip():
            continue
        cols = raw.split("\t")
        if len(cols) < 5 or not _DAY_RE.match(cols[0]) or not cols[1].isdigit():
            return {}, f"malformed scan row: {raw[:80]!r}"
        days[cols[0]] = {"commits": int(cols[1]), "baseline_sha": cols[2], "tip_sha": cols[3]}
    return days, ""


def _week_days(week_gap_output: str) -> tuple[list[str], str]:
    text = (week_gap_output or "").strip()
    if not text:
        return [], ""
    try:
        obj = json.loads(text)
    except ValueError:
        return [], "week gap output is not JSON"
    found = obj.get("backfilled") if isinstance(obj, dict) else None
    if not isinstance(found, list) or not all(isinstance(d, str) and _DAY_RE.match(d) for d in found):
        return [], "week gap output has no valid backfilled list"
    return found, ""


def plan_with_reason(scan_output: str, week_gap_output: str) -> tuple[list[dict], str]:
    """Return (entries, reason). Malformed input gives ([], reason); never raises."""
    days, reason = _scan_days(scan_output)
    if reason:
        return [], reason
    week, reason = _week_days(week_gap_output)
    if reason:
        return [], reason
    inputs: dict[str, dict[str, Any]] = {d: {"kind": "day", **v} for d, v in days.items()}
    for d in week:
        inputs.setdefault(d, {"kind": "day"})
    return [
        {"key": f"day:{d}", "brief": None, "argv": ["workday-complete", "--for-date", d, "--only"],
         "inputs": inputs[d]}
        for d in sorted(inputs)
    ], ""


def plan(scan_output: str, week_gap_output: str) -> list[dict]:
    """Digest `backfill` entries (`key`, `brief`, `argv`, `inputs.kind`), oldest day first."""
    return plan_with_reason(scan_output, week_gap_output)[0]
