"""
coordinator_core.session.stable_pid_watch — cadence watch over whether ANY
session's ``stable_pid`` capture is missing.

Purpose: K-006 (``state/kill-ledger.md``) deregistered
``hooks.session_heartbeat`` — the sole discharge of the F0 hazard (a session
whose ``stable_pid`` capture misses, running only ``Bash``/``PowerShell`` for
30 minutes, reads DEAD on the Layer-2 recency path in
``coordinator_core/session/liveness.py::session_live`` and becomes
reap-eligible while alive). Deregistration was ruled safe ONLY because
``stable_pid`` capture currently never misses (10/10 live + 354/354 archived
sessions since 2026-08-10). That 0% is load-bearing and was, before this
module, completely unwatched — the ledger's own risk paragraph was unenforced
prose. This module is the artifact that discharges it (CLAUDE.md § north
star: name the artifact, "the operator remembers" is not one).

Threshold — ANY single miss alerts, deliberately NOT a rate/percentage. The
newest known miss is 2026-08-08 (64%, 32/50); the population of interest is
"is this reachable AT ALL", not "how often" — a single occurrence is a regime
change back toward a state K-006's ruling assumed was gone. A percentage
threshold would need a denominator (miss rate over what window, against what
expected baseline) this watch has no principled way to justify.

What counts as a miss — mirrors ``coordinator_core.session.liveness.session_live``'s
own Layer-1 decision exactly, not a re-derivation:
  - ``stable_pid`` empty/absent -> Layer 1 never engages, session_live falls
    straight to Layer 2 recency -> F0-exposed -> MISS.
  - ``stable_pid`` present but BOTH ``stable_pid_lstart`` and
    ``stable_pid_start_epoch`` are empty/absent -> session_live's own comment
    ("Birth-instant witness absent != process dead — fall through to Layer 2
    (A-F1) only when BOTH lstart and start_epoch are missing") means this
    ALSO falls through to Layer 2 -> the identical F0 exposure -> MISS.
  - ``stable_pid`` present with at least one witness -> Layer 1 engages ->
    not F0-exposed -> not a miss (regardless of whether the process is
    actually alive — aliveness is not this watch's question).

Cost constraint (cadence-path, non-negotiable per CLAUDE.md § Load norm): NO
process spawns, NO corpus walk beyond the session dirs themselves. One
``Path.iterdir()`` over ``<git-common-dir>/coordinator-sessions/`` plus one
``meta.json`` read per session dir — the same read primitive
(``core.read_meta_field``) the liveness path already uses, so this watch
never becomes a second parser of that file's shape.

Negative-spec:
  - NEVER raises. Every path — sessions dir absent, unreadable meta.json,
    unexpected exception per-entry — folds into the returned dict; a
    malformed single session's meta.json degrades that ONE entry to
    "unreadable" (still conservatively counted as a miss — an unreadable
    meta.json cannot prove Layer 1 is armed) rather than aborting the scan.
  - Does NOT determine session liveness/aliveness. This watch answers "is
    the capture mechanism intact", not "is this session alive" — orthogonal
    to ``session_live``, which this module never calls.
  - Does NOT mutate any session state, does NOT restore the heartbeat, does
    NOT write ``last_activity``. Read-only probe.

Spec backlink: state/kill-ledger.md § K-006; coordinator_core/session/liveness.py
::session_live Layer 1 comment (the exact fall-through logic mirrored above).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.session import core

STATUS_MISS = "MISS"
STATUS_CLEAN = "CLEAN"
STATUS_EMPTY = "EMPTY"


def scan_stable_pid_misses(
    sessions_dir: Optional[Path | str] = None,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Scan every session dir for a Layer-1-disarming ``stable_pid`` miss.

    Args:
        sessions_dir: explicit ``coordinator-sessions`` root to scan (tests
            pass a ``tmp_path``-rooted directory here). ``None`` ->
            ``core.sessions_dir(cwd)``.
        cwd: forwarded to ``core.sessions_dir`` when ``sessions_dir`` is
            ``None``; unused otherwise.

    Returns a dict, always shaped:
        {
          "status": STATUS_MISS | STATUS_CLEAN | STATUS_EMPTY,
          "checked": int,
          "misses": [{"session": <dirname>, "reason": "empty" | "no_witness" | "unreadable"}],
          "summary": <one-line human string>,
        }

    STATUS_EMPTY (checked == 0, misses == []) covers BOTH "no sessions dir
    yet" and "sessions dir exists but is empty" — distinct from STATUS_CLEAN
    (checked > 0, misses == []) so a caller can tell "nothing to check" apart
    from "checked N sessions, all armed". Neither is treated as MISS.
    """
    root = Path(sessions_dir) if sessions_dir is not None else _resolve_sessions_dir(cwd)

    if root is None or not root.is_dir():
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "No coordinator-sessions directory found — nothing to check.",
        }

    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "coordinator-sessions directory unreadable — nothing to check.",
        }

    checked = 0
    misses: list[dict[str, str]] = []
    for sdir in entries:
        meta_path = sdir / "meta.json"
        if not meta_path.is_file():
            # No meta.json at all is not a session record (e.g. a stray
            # non-session subdirectory under the hub) — not counted.
            continue
        checked += 1
        try:
            stable_pid = core.read_meta_field(str(sdir), "stable_pid")
            if not stable_pid:
                misses.append({"session": sdir.name, "reason": "empty"})
                continue
            lstart = core.read_meta_field(str(sdir), "stable_pid_lstart")
            start_epoch = core.read_meta_field(str(sdir), "stable_pid_start_epoch")
            if not lstart and not start_epoch:
                misses.append({"session": sdir.name, "reason": "no_witness"})
        except Exception:
            # Conservative: an unreadable/unparseable meta.json cannot prove
            # Layer 1 is armed for this session, so it counts as a miss
            # rather than being silently skipped.
            misses.append({"session": sdir.name, "reason": "unreadable"})

    if checked == 0:
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "coordinator-sessions directory has no session records — nothing to check.",
        }

    if misses:
        named = ", ".join(f"{m['session']} ({m['reason']})" for m in misses)
        return {
            "status": STATUS_MISS,
            "checked": checked,
            "misses": misses,
            "summary": (
                f"{len(misses)} of {checked} session(s) missing stable_pid capture — "
                f"F0 hazard (K-006) is live again: {named}."
            ),
        }

    return {
        "status": STATUS_CLEAN,
        "checked": checked,
        "misses": [],
        "summary": f"Checked {checked} session(s); stable_pid capture intact on all.",
    }


def _resolve_sessions_dir(cwd: Optional[str]) -> Optional[Path]:
    try:
        resolved = core.sessions_dir(cwd)
    except Exception:
        return None
    if not resolved:
        return None
    return Path(resolved)
