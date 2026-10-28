"""Run-side slot admission: post a REQUEST line, then poll for the Group EM's slot grant.

A step at or under ``SLOT_THRESHOLD_MB`` is admitted with no line written. Above it the run
posts REQUEST (holder required) and polls ``slot_grant.check_slot_grant`` (one path read, no
spawn) until granted or the deadline. ``request_and_wait`` never raises and fails closed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.group_em import admission_post, slot_grant
from coordinator_core.group_em.suite_grant import _safe_sid

SLOT_THRESHOLD_MB = 2048
SLOT_WAIT_TIMEOUT_S = 900.0
SLOT_POLL_S = 2.0


def request_and_wait(
    session_id: str,
    run: str,
    declared_peak_mb: float,
    timeout_s: float = SLOT_WAIT_TIMEOUT_S,
    poll_s: float = SLOT_POLL_S,
    directory: Optional[Path] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Return ``{"admitted": bool, "reason": str}``: may the step declared at ``declared_peak_mb`` start?

    Reasons: ``under_threshold``, ``granted``, ``timeout``, ``no_box_holder``, ``bad_peak``,
    ``bad_run`` (session or run label a grant path cannot carry: refused before posting),
    ``post_failed:<reason>``, ``error:<exc>``. Only ``admitted`` True permits the step. The
    wait bound is not a memory budget; start any step cap timer after this returns.
    No engine step calls this yet: the only caller is the ``slot-admit`` CLI.
    """
    try:
        peak = admission_post.valid_peak_mb(declared_peak_mb)
        if peak is None:
            return {"admitted": False, "reason": "bad_peak"}
        if peak <= SLOT_THRESHOLD_MB:
            return {"admitted": True, "reason": "under_threshold"}
        if not (_safe_sid(session_id) and _safe_sid(run)):
            return {"admitted": False, "reason": "bad_run"}
        posted = admission_post.post(
            "REQUEST", session_id, run=run, directory=directory, declared_peak_mb=peak
        )
        if not posted.get("posted"):
            reason = str(posted.get("reason") or "unknown")
            return {"admitted": False, "reason": reason if reason == "no_box_holder" else f"post_failed:{reason}"}
        deadline = clock() + max(0.0, float(timeout_s))
        while True:
            granted, _record = slot_grant.check_slot_grant(session_id, run, directory, peak_mb=peak)
            if granted:
                return {"admitted": True, "reason": "granted"}
            if clock() >= deadline:
                return {"admitted": False, "reason": "timeout"}
            sleep(max(0.0, float(poll_s)))
    except Exception as exc:
        return {"admitted": False, "reason": f"error:{type(exc).__name__}"}
