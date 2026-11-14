"""Cheap DIY-nag detector for a non-Claude EM's coordinator-invoke op dispatch.

At most two git spawns per call (tracked diff, untracked listing), throttled per session.
Advisory only: every failure returns None.
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
    """(files, lines) of the uncommitted edit: the tracked diff against HEAD plus untracked, non-ignored files.

    A binary row is a file with 0 lines. At most two git spawns; the untracked listing runs only
    while the tracked diff is still within the XS bound, and the count stops once the bound is passed.
    `.coordinator-local` and `state/` are engine-written records (untracked until committed), never
    a hand edit, so they are not counted.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--numstat", "HEAD"],
        capture_output=True, text=True, timeout=5, check=True, creationflags=flags,
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
    if files > xs_lane_budget.BUDGET_FILES or lines > xs_lane_budget.BUDGET_LINES:
        return files, lines
    listing = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard", "-z",
         "--", ".", ":(exclude).coordinator-local", ":(exclude)state"],
        capture_output=True, timeout=5, check=True, creationflags=flags,
    )
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        files += 1
        lines += _untracked_lines(Path(repo_root) / raw.decode("utf-8", "surrogateescape"))
        if files > xs_lane_budget.BUDGET_FILES or lines > xs_lane_budget.BUDGET_LINES:
            break
    return files, lines


def _untracked_lines(path: Path) -> int:
    """Line count of an untracked file, read up to 1 MiB; binary, unreadable or non-regular is 0."""
    try:
        if not path.is_file():
            return 0
        with open(path, "rb") as fh:
            data = fh.read(1 << 20)
    except OSError:
        return 0
    if b"\0" in data[:8192]:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


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
    """The DIY nag text when a non-Claude EM's uncommitted edit exceeds the XS bound, else None.

    Surface-agnostic: needs no prior op call, only repo root, session id and caller env. A hook on
    a surface the EM always crosses (the only cover for an EM that edits and never invokes an op)
    calls this directly; hook wiring is DoE-owned.
    """
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
