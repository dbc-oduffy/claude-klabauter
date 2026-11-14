"""Halt and notify a run that reached stage `staged` and then stopped progressing.

`workflow_watch.stall.scan_for_stall` only sees a run with an unsettled agent and only runs inside a
session hook, so a staged run with no agent in flight is invisible to it. The warm idle tick calls
`on_idle_tick`: it reads the live-run index and each candidate's `run-stage.json`, and for a run that is
`staged`, unhalted, idle past `STAGED_STALL_BOUND_SECS` and without a fresh agent it writes the `halted`
marker (the idempotency key: a halted run is skipped next tick) and sends one notification to the run's
session. Stat reads and bounded journal tails only; no git, no spawn.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from coordinator_core.ops.dispatch_emit import halted_run, live_run_index
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage, record_dir
from coordinator_core.ops.dispatch_emit.halt_resume_record import MISSING, STALLED

HALT_STAGED_STALL = "staged_stall"

#: Idle seconds past which a staged run is halted. Hang bound only: 1198a2 sat silent for an hour, and a
#: healthy stage writes the run dir (admit heartbeat) or an agent transcript far more often than this.
STAGED_STALL_BOUND_SECS = 600

#: A run idle longer than this is history, not a resume that just went quiet; it is never halted here,
#: so the first tick after an upgrade does not mass-halt and mass-notify old staged records.
STAGED_STALL_HORIZON_SECS = 24 * 3600

#: Index entries examined and journals read per tick (the 500ms brightline).
MAX_RUNS_PER_TICK = 64
MAX_JOURNALS_PER_RUN = 16
MAX_RUN_DIR_ENTRIES = 256
INDEX_DEADLINE_MS = 200.0

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StalledStaged:
    run_id: str
    idle_seconds: float
    session_id: Optional[str]


def _newest_mtime(rdir: Path) -> Optional[float]:
    """Newest mtime over the run dir and its top-level entries; None when the dir is absent."""
    try:
        newest = rdir.stat().st_mtime
        with os.scandir(rdir) as it:
            for n, entry in enumerate(it):
                if n >= MAX_RUN_DIR_ENTRIES:
                    break
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return None
    return newest


def agent_in_flight(session_id: Optional[str], cutoff: float) -> bool:
    """True when the session's workflow dirs show progress at or after `cutoff`: a journal written then,
    or an unsettled agent whose transcript was. Such a run is `workflow_watch.stall`'s case."""
    if not session_id:
        return False
    from coordinator_core.usage import run_locate
    from coordinator_core.workflow_watch import stall

    session_dir = run_locate.session_dir_for(session_id)
    if session_dir is None:
        return False
    wf_root = session_dir / "subagents" / "workflows"
    try:
        wfs = [Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()]
    except OSError:
        return False
    stamped = [(stall._mtime(str(wf / "journal.jsonl")), wf) for wf in wfs]
    stamped = [(m, wf) for m, wf in stamped if m is not None]
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    for journal_mtime, wf in stamped[:MAX_JOURNALS_PER_RUN]:
        if journal_mtime >= cutoff:
            return True
        journal = str(wf / "journal.jsonl")
        for agent_id, _label in stall._unsettled(stall._tail_text(journal, stall.JOURNAL_TAIL_BYTES)):
            path = stall._transcript_path(str(wf), agent_id)
            progress = stall._mtime(path) if path else None
            if progress is not None and progress >= cutoff:
                return True
    return False


def scan(
    repo_root: Path,
    now: float,
    *,
    in_flight: Optional[Callable[[Optional[str], float], bool]] = None,
) -> list[StalledStaged]:
    """Staged, unhalted runs of `repo_root` idle between the bound and the horizon with no agent in flight."""
    index = live_run_index.read(repo_root, INDEX_DEADLINE_MS)
    if index is MISSING or index is STALLED:
        return []
    cutoff = now - STAGED_STALL_BOUND_SECS
    found: list[StalledStaged] = []
    for run_id in list(index)[:MAX_RUNS_PER_TICK]:
        stage = read_run_stage(repo_root, run_id)
        if not stage or stage.get("stage") != "staged":
            continue
        try:
            rdir = record_dir(repo_root, run_id)
        except ValueError:
            continue
        if (rdir / halted_run.HALTED_FILE).exists():
            continue
        newest = _newest_mtime(rdir)
        if newest is None or newest >= cutoff or now - newest > STAGED_STALL_HORIZON_SECS:
            continue
        session_id = stage.get("session_id") if isinstance(stage.get("session_id"), str) else None
        if (in_flight or agent_in_flight)(session_id, cutoff):
            continue
        found.append(StalledStaged(run_id, now - newest, session_id))
    return found


def _notify(target: str, text: str) -> str:
    """One bridge message to `target` (skipped for an ended session); the delivery status. Never raises."""
    try:
        from coordinator_core.agent_pipe import bridge
        from coordinator_core.ops.workflow_fire import completion, notify

        if completion.recipient_ended(target):
            return "recipient-ended"
        return bridge.route(bridge.make_message(notify.FROM_IDENTITY, target, text)).status
    except Exception as exc:  # noqa: BLE001 -- the halt marker is the truth; a notice failure is logged
        _log.warning("staged-stall notification failed: %s", exc)
        return "error"


def halt_stalled(
    repo_root: Path,
    now: float,
    *,
    in_flight: Optional[Callable[[Optional[str], float], bool]] = None,
    notify: Optional[Callable[[str, str], str]] = None,
) -> list[str]:
    """Halt and notify every stalled staged run of `repo_root`; the run ids halted. The marker is written
    first, so a run is notified at most once."""
    halted: list[str] = []
    for stalled in scan(repo_root, now, in_flight=in_flight):
        reason = (
            f"staged run idle {int(stalled.idle_seconds)}s (bound {STAGED_STALL_BOUND_SECS}s): "
            "no run-dir write and no agent progress since; resume or re-fire"
        )
        try:
            halted_run.write_halt_marker(
                record_dir(repo_root, stalled.run_id),
                halted=HALT_STAGED_STALL,
                sizing_rel=halted_run.bound_sizing_rel(repo_root, stalled.run_id),
                reason=reason,
            )
        except (OSError, ValueError) as exc:
            _log.warning("staged-stall halt of %s failed: %s", stalled.run_id, exc)
            continue
        halted.append(stalled.run_id)
        if stalled.session_id:
            (notify or _notify)(stalled.session_id, f"run {stalled.run_id} halted: {reason}")
    return halted


def on_idle_tick(repos: Iterable[object], now: Optional[float] = None) -> bool:
    """Run `halt_stalled` over each served repo; True when any run was halted. Never raises."""
    moment = time.time() if now is None else now
    any_halted = False
    for repo in repos:
        try:
            if halt_stalled(Path(str(repo)), moment):
                any_halted = True
        except Exception:  # noqa: BLE001 -- no staged-stall failure may escape the idle tick
            _log.warning("staged-stall scan failed for %s", repo, exc_info=True)
    return any_halted
