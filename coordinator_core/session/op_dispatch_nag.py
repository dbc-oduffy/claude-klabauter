"""Cheap DIY-nag detector for a non-Claude EM's coordinator-invoke op dispatch.

One `git diff --numstat HEAD` per call, throttled per session. Advisory only: every failure
returns None.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.session import xs_lane_budget

THROTTLE_SECONDS = 30
_STAMP_FILENAME = "op-dispatch-nag.stamp"
_SUBAGENT_ROLES = ("worker", "reviewer")
_FAMILIES = ("codex", "grok", "agy")


def caller_family(env: Mapping[str, str]) -> Optional[str]:
    """codex|grok|agy for a non-Claude EM caller, None for Claude, a subagent or an unmarked caller."""
    if env.get("COORDINATOR_AGENT_ROLE") in _SUBAGENT_ROLES:
        return None
    for key in ("COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR"):
        val = (env.get(key) or "").strip().lower()
        if val:
            return val if val in _FAMILIES else None
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if env.get("ANTIGRAVITY_CONVERSATION_ID") or env.get("AGY_SESSION_ID"):
        return "agy"
    return None


def _measure(repo_root: str) -> tuple[int, int]:
    """(files, lines) of the tracked uncommitted diff against HEAD; a binary row is a file with 0 lines.

    Untracked new files are not counted: `git diff` does not list them, so a hand edit made only of
    new files stays under the bound.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--numstat", "HEAD"],
        capture_output=True, text=True, timeout=5, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    files = lines = 0
    for row in proc.stdout.splitlines():
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        for n in parts[:2]:
            if n.isdigit():
                lines += int(n)
    return files, lines


def _throttled(stamp: Path, now: float) -> bool:
    """True inside the window; otherwise records `now` and returns False."""
    try:
        if now - float(stamp.read_text(encoding="ascii").strip()) < THROTTLE_SECONDS:
            return True
    except (OSError, ValueError):
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(repr(now), encoding="ascii")
    return False


def advisory_for_dispatch(repo_root, session_id: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """The DIY nag text when a non-Claude EM's uncommitted edit exceeds the XS bound, else None."""
    try:
        if repo_root is None or not session_id:
            return None
        family = caller_family(env)
        if family is None:
            return None
        stamp = xs_lane_budget.lane_path(repo_root, session_id).parent / _STAMP_FILENAME
        if _throttled(stamp, time.time()):
            return None
        files, lines = _measure(str(repo_root))
        if files <= xs_lane_budget.BUDGET_FILES and lines <= xs_lane_budget.BUDGET_LINES:
            return None
        from coordinator_core import warp_fire

        return warp_fire.nag_text(session_id, host=family)
    except Exception:
        return None
