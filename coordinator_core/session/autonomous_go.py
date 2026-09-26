"""
coordinator_core.session.autonomous_go — one resolution point for the four
autonomous-discharge signals sizing may skip a human ratification wait for.

WHY THIS EXISTS

Four independent signals can each discharge the post-size ratification
prompt: the `/autonomous` sentinel, `COORDINATOR_JOB_MODE` in {blitz, cron},
a workflow-asserted `backgrounded` flag, and a PM's own verbatim "go". Before
this module each would have been a separate ad-hoc check at the sizing call
site. `active_signals` is the single place all four are read, in a fixed
order, so a new reader inherits every signal for free and a new signal is
added in exactly one place.

COST-INCIDENCE, NOT A FLEET RECORD. Per `mode_resolution`'s own discriminator,
the cost of a wrong autonomous discharge lands on a shared tree with ~50
concurrent peers, so only this session's own local evidence may assert it —
never `fleet_mode.read_fleet_mode()`. `job_mode` is read directly from the
caller's own `env` parameter here, not through `mode_resolution.resolve_mode`,
because that resolver's `environment-wins` precedence would still fall back
to a fleet record when the caller's own env is silent; this helper must not.

CONTENT-AWARE SENTINEL READ. The sentinel is not a presence/absence bit here
— `mise-en-place` is a real, non-autonomous content this file can hold (see
tripwire MISE-AUTONOMOUS-SENTINEL-MODE-IS-SEMANTIC), so this is the first
caller that reads the sentinel's first line and compares it to the literal
`autonomous` rather than merely checking the path exists.

Negative-spec:
    - Do NOT read `env` ambiently (`os.environ`) — it is a parameter, the
      same contract `mode_resolution._job_mode_from_environment` states.
    - Do NOT call `fleet_mode.read_fleet_mode()` from here.
    - Do NOT spawn a process or shell out for any signal.
    - Do NOT treat a missing, unreadable, or non-`autonomous` sentinel as
      anything other than inactive — never raise on it.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from coordinator_core.session import autonomous_sentinel
from coordinator_core.session.mode_resolution import (
    COORDINATOR_JOB_MODE,
    JOB_MODE_VALUES,
)

_ACTIVE_JOB_MODES = frozenset({"blitz", "cron"})


def _autonomous_command_signal(session_id: str) -> Optional[Dict[str, str]]:
    if not session_id:
        return None
    try:
        path = autonomous_sentinel.sentinel_path(session_id)
        with path.open("r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
    except OSError:
        return None
    if first_line != "autonomous":
        return None
    return {"signal": "autonomous-command", "evidence": "autonomous"}


def _job_mode_signal(env: Optional[Mapping[str, str]]) -> Optional[Dict[str, str]]:
    if not env:
        return None
    raw = env.get(COORDINATOR_JOB_MODE)
    if not raw or raw not in JOB_MODE_VALUES or raw not in _ACTIVE_JOB_MODES:
        return None
    return {"signal": "job-mode", "evidence": f"{COORDINATOR_JOB_MODE}={raw}"}


def _backgrounded_signal(backgrounded: bool) -> Optional[Dict[str, str]]:
    if not backgrounded:
        return None
    return {"signal": "backgrounded", "evidence": "backgrounded"}


def _pm_vocalized_signal(pm_go: Optional[str]) -> Optional[Dict[str, str]]:
    if pm_go is None or not pm_go.strip():
        return None
    return {"signal": "pm-vocalized", "evidence": pm_go}


def active_signals(
    session_id: str,
    env: Optional[Mapping[str, str]],
    *,
    backgrounded: bool = False,
    pm_go: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return the autonomous-discharge signals active right now, in fixed
    order: ``autonomous-command``, ``job-mode``, ``backgrounded``,
    ``pm-vocalized``. Each active entry is ``{"signal", "evidence"}``; an
    inactive signal contributes nothing. Never raises, never spawns a
    process, and never reads a fleet-wide record.
    """
    signals: List[Dict[str, str]] = []
    for candidate in (
        _autonomous_command_signal(session_id),
        _job_mode_signal(env),
        _backgrounded_signal(backgrounded),
        _pm_vocalized_signal(pm_go),
    ):
        if candidate is not None:
            signals.append(candidate)
    return signals
