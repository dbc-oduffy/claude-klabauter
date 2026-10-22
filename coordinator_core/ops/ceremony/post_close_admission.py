"""Memory admission for the close job's detached post-close launchers.

``admit`` answers one question from one kernel-available reading: may this launcher start, given
its declared peak plus the fixed reserve and what this leg already admitted. ``notice`` posts one
START line to the box Group EM for an admitted launch.

Spawns nothing and never raises. An untrusted or missing reading defers (fail-closed): a
deferred index refresh costs staleness until the next close, a wrong admit costs the box.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

RESERVE_MB = 8192
DECLARED_PEAK_MB: Mapping[str, int] = {"structural-index": 2048, "scip-rebuild": 3072}


@dataclass(frozen=True)
class Verdict:
    """``reason`` is a short path-free phrase safe for a close-leg detail."""

    label: str
    admitted: bool
    avail_mb: Optional[int]
    reason: str


def read_available() -> tuple[Optional[int], bool]:
    """One kernel-available reading as ``(avail_mb, trusted)``; ``(None, False)`` on any failure."""
    try:
        from coordinator_core.bash_guards import _host_probe

        reading = _host_probe.read_available_mb()
        return reading.avail_mb, bool(reading.trusted)
    except Exception:
        return None, False


def admit(
    label: str,
    avail_mb: Optional[int],
    trusted: bool,
    already_admitted_mb: int = 0,
) -> Verdict:
    """Admit iff ``DECLARED_PEAK_MB[label] + RESERVE_MB <= avail_mb - already_admitted_mb``."""
    peak = DECLARED_PEAK_MB.get(label)
    if peak is None:
        return Verdict(label, False, avail_mb, "no declared peak")
    need = peak + RESERVE_MB
    if not trusted or avail_mb is None:
        return Verdict(label, False, avail_mb, "memory unreadable")
    remaining = avail_mb - already_admitted_mb
    if need <= remaining:
        return Verdict(label, True, avail_mb, f"need {need}MB <= avail {remaining}MB")
    return Verdict(label, False, avail_mb, f"need {need}MB > avail {remaining}MB")


def notice(verdict: Verdict, run_id: str, pids: Optional[Sequence[int]] = None) -> bool:
    """Post one START line for an admitted verdict; False when deferred, undelivered, or on error.

    An admitted launch whose line did not land writes one stderr line with the reason.

    ``pids`` names the launched child processes and is forwarded to the post only when given.

    The line is appended whether or not a box Group EM holder exists; no END line follows a
    launch."""
    if not verdict.admitted:
        return False
    try:
        from coordinator_core.group_em import admission_post

        peak = DECLARED_PEAK_MB[verdict.label]
        note = (
            f"post-close {verdict.label}: peak {peak}MB + {RESERVE_MB}MB reserve "
            f"<= avail {verdict.avail_mb}MB"
        )
        extra = {"pids": list(pids)} if pids else {}
        reply = admission_post.post(
            "START",
            session_id=f"close:{run_id}",
            run=run_id,
            note=note,
            require_holder=False,
            **extra,
        )
        if reply.get("delivered", reply.get("posted")):
            return True
        _report_failure(verdict.label, run_id, str(reply.get("reason") or "not delivered"))
        return False
    except Exception as exc:
        _report_failure(verdict.label, run_id, f"post raised {type(exc).__name__}")
        return False


def _report_failure(label: str, run_id: str, reason: str) -> None:
    """One stderr line naming an admitted launch whose START line did not land; never raises."""
    try:
        print(
            f"post_close_admission: START for {label} run {run_id} not delivered: {reason}",
            file=sys.stderr,
        )
    except Exception:
        pass
