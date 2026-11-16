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
CHECK_SECONDS = 5  # bounds the git spawns a PreToolUse burst can cost; the nag window is separate
_STAMP_FILENAME = "op-dispatch-nag.stamp"
_CHECK_STAMP_FILENAME = "op-dispatch-nag.checked"
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


def _measure(repo_root: str) -> tuple[int, int, bool]:
    """(files, lines, truncated) of the uncommitted edit: the tracked diff against HEAD plus untracked, non-ignored files.

    A binary row is a file with 0 lines. At most two git spawns; the untracked listing runs only
    while the tracked diff is still within the XS bound, and the count stops once the bound is passed;
    `truncated` is true then, and files/lines are lower bounds.
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
        return files, lines, True
    listing = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard", "-z",
         "--", ".", ":(exclude).coordinator-local", ":(exclude)state"],
        capture_output=True, timeout=5, check=True, creationflags=flags,
    )
    rows = [raw for raw in listing.stdout.split(b"\0") if raw]
    for i, raw in enumerate(rows):
        files += 1
        lines += _untracked_lines(Path(repo_root) / raw.decode("utf-8", "surrogateescape"))
        if files > xs_lane_budget.BUDGET_FILES or lines > xs_lane_budget.BUDGET_LINES:
            return files, lines, i < len(rows) - 1
    return files, lines, False


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


def _within(stamp: Path, now: float, window: float) -> bool:
    try:
        return now - float(stamp.read_text(encoding="ascii").strip()) < window
    except (OSError, ValueError):
        return False


def _mark(stamp: Path, now: float) -> None:
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(repr(now), encoding="ascii")


def mark_nag_emitted(repo_root, session_id: Optional[str]) -> None:
    """Open the nag window for this session; the writer calls it once the nag is on the wire. Never raises."""
    try:
        if repo_root is None or not session_id:
            return
        _mark(xs_lane_budget.lane_path(repo_root, session_id).parent / _STAMP_FILENAME, time.time())
    except Exception:
        return


def advisory_for_dispatch(repo_root, session_id: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """The DIY nag text when a non-Claude EM's uncommitted edit exceeds the XS bound, else None.

    Surface-agnostic: needs no prior op call, only repo root, session id and caller env. A hook on
    a surface the EM always crosses (the only cover for an EM that edits and never invokes an op)
    calls this directly; hook wiring is DoE-owned. It never stamps the nag window: the writer that
    put the nag on the wire calls `mark_nag_emitted`.
    """
    try:
        if repo_root is None or not session_id:
            return None
        family = caller_family(env)
        if family is None:
            return None
        hub = xs_lane_budget.lane_path(repo_root, session_id).parent
        stamp, checked = hub / _STAMP_FILENAME, hub / _CHECK_STAMP_FILENAME
        now = time.time()
        # Trap: the nag window is stamped only on emit. Stamping it per check let an under-bound check
        # swallow the nag for the edit that crossed the bound moments later (grok C3, 2026-11-14).
        if _within(stamp, now, THROTTLE_SECONDS) or _within(checked, now, CHECK_SECONDS):
            return None
        _mark(checked, now)
        files, lines, truncated = _measure(str(repo_root))
        if files <= xs_lane_budget.BUDGET_FILES and lines <= xs_lane_budget.BUDGET_LINES:
            return None
        from coordinator_core import warp_fire

        return warp_fire.nag_text(
            session_id, host=family, edit_size=(files, lines), edit_size_truncated=truncated
        )
    except Exception:
        return None
