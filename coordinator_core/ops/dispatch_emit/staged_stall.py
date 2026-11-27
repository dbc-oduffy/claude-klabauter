"""Halt and notify a run that reached stage `staged` and then stopped progressing.

`workflow_watch.stall.scan_for_stall` only sees a run with an unsettled agent and only runs inside a
session hook, so a staged run with no agent in flight is invisible to it. The warm idle tick calls
`on_idle_tick`: it reads the live-run index and each candidate's `run-stage.json`, and for a run that is
`staged`, unhalted, idle past `STAGED_STALL_BOUND_SECS` and without a fresh agent it writes the `halted`
marker (the idempotency key: a halted run is skipped next tick) and sends one notification to the run's
session. Stat reads and bounded journal tails only; no git, no spawn.

A headless fire's liveness is the mtime of its own `journal.jsonl` and `agents/` dirs (`fire_progress_epoch`):
neither the run dir nor a session workflow dir moves while a long wave leg runs, so a fire that is alive and
progressing is never halted, and a `staged_stall` halt written before progress resumed is cleared
(`clear_recovered`) rather than left to invite a resume or re-fire of a run that is still running.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
MAX_DRIVER_SESSIONS = 8
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


MAX_FIRE_RECORDS = 64
MAX_FIRE_AGENT_DIRS = 64


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


#: Parsed fire records keyed by path, valid while the file's (mtime_ns, size) is unchanged: a repeat
#: listing re-reads only a record that moved. A file stamped within `_FIRE_RACY_NS` of the read is not kept.
_FIRE_RECORD_CACHE_MAX = 256
_FIRE_RACY_NS = 2_000_000_000
_FIRE_RECORD_CACHE: dict = {}


def _fire_record(rec_path: str, sig: tuple) -> Optional[dict]:
    """The fire record at `rec_path`, parsed once per stat change; None when unreadable or not an object."""
    held = _FIRE_RECORD_CACHE.get(rec_path)
    if held is not None and held[0] == sig:
        return held[1]
    try:
        record = json.loads(Path(rec_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if time.time_ns() - sig[0] > _FIRE_RACY_NS:
        if len(_FIRE_RECORD_CACHE) >= _FIRE_RECORD_CACHE_MAX:
            _FIRE_RECORD_CACHE.pop(next(iter(_FIRE_RECORD_CACHE)))
        _FIRE_RECORD_CACHE[rec_path] = (sig, record)
    return record


def fire_records_for_run(repo_root: Path, run_id: str) -> list:
    """`(record_path, record)` for each of the newest `MAX_FIRE_RECORDS` fire records naming `run_id`,
    newest first. One listing plus a record read only for a record that changed; `[]` on any failure."""
    from coordinator_core.session_hub_path import hub_for_common

    found: list = []
    try:
        registry = hub_for_common(Path(repo_root) / ".git") / "workflow-fires"
        records = []
        with os.scandir(registry) as it:
            for entry in it:
                if entry.name.endswith(".json") and entry.is_file():
                    stat = entry.stat()
                    records.append((stat.st_mtime, entry.path, (stat.st_mtime_ns, stat.st_size)))
        records.sort(reverse=True)
        for _, rec_path, sig in records[:MAX_FIRE_RECORDS]:
            record = _fire_record(rec_path, sig)
            if record is not None and record.get("run_id") == run_id:
                found.append((Path(rec_path), record))
    except OSError:
        return []
    return found


def fire_progress_epoch(repo_root: Path, run_id: str) -> Optional[float]:
    """Newest mtime of the headless fire that executes `run_id`: its `journal.jsonl`, its `agents/` dir and
    each `agents/N` dir. None when no fire record names the run. One listing plus at most
    `MAX_FIRE_RECORDS` small record reads and `MAX_FIRE_AGENT_DIRS` stats; never raises."""
    try:
        from coordinator_core.session_hub_path import hub_for_common

        registry = hub_for_common(Path(repo_root) / ".git") / "workflow-fires"
        for _, record in fire_records_for_run(repo_root, run_id):
            home = Path(str(record.get("artifact_dir") or registry / "headless" / str(record.get("fire_id"))))
            agents = home / "agents"
            stamps = [_mtime(Path(str(record.get("journal_path") or home / "journal.jsonl"))), _mtime(agents)]
            try:
                with os.scandir(agents) as it:
                    for n, entry in enumerate(it):
                        if n >= MAX_FIRE_AGENT_DIRS:
                            break
                        try:
                            stamps.append(entry.stat().st_mtime)
                        except OSError:
                            continue
            except OSError:
                pass
            live = [m for m in stamps if m is not None]
            if live:
                return max(live)
        return None
    except Exception:  # noqa: BLE001 -- a missing signal means the run-dir verdict stands
        return None


_RUN_ID_RE = re.compile(r"(?:warp|ask)-[A-Za-z0-9._-]*?\d{8}T\d{6}-[0-9a-f]{6}")


def _names_other_run(journal_text: str, run_id: str) -> bool:
    """True when the journal names run ids and none is `run_id`: another run's workflow. A journal that
    names no run id stays attributable to every run of its session."""
    return run_id not in journal_text and _RUN_ID_RE.search(journal_text) is not None


def agent_in_flight(session_id: Optional[str], cutoff: float, run_id: Optional[str] = None) -> bool:
    """True when the session's workflow dirs show progress at or after `cutoff`: a journal written then,
    or an unsettled agent whose transcript was. Such a run is `workflow_watch.stall`'s case.

    Invariant: a session drives many runs, so with `run_id` a journal that names only other runs is not
    this run's progress; without it every journal of the session counts."""
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
        journal = str(wf / "journal.jsonl")
        text = stall._tail_text(journal, stall.JOURNAL_TAIL_BYTES)
        if run_id and _names_other_run(text, run_id):
            continue
        if journal_mtime >= cutoff:
            return True
        for agent_id, _label in stall._unsettled(text):
            path = stall._transcript_path(str(wf), agent_id)
            progress = stall._mtime(path) if path else None
            if progress is not None and progress >= cutoff:
                return True
    return False


def _candidate_sessions(repo_root: Path, run_id: str, stage_session: Optional[str]) -> list[Optional[str]]:
    """Sessions whose workflow dirs may hold the run's progress: the stage record's, then the submission's
    and the run token's. Invariant: `run-stage.json` can name a session that no longer executes the run (a
    refire under a new session leaves it stale), so it is never the sole liveness source. File reads only."""
    sessions: list[Optional[str]] = [stage_session]
    try:
        record, submission = live_run_index._run_records(Path(repo_root), run_id, None)
    except Exception:  # noqa: BLE001 -- no extra session means the stage session's verdict stands
        return sessions
    for rec in (submission, record):
        sid = rec.get("session_id") if isinstance(rec, dict) else None
        if isinstance(sid, str) and sid and sid not in sessions:
            sessions.append(sid)
    return sessions


def fire_in_flight(repo_root: Path, run_id: str, cutoff: float) -> bool:
    """True when a headless fire record naming `run_id` is attached (pending, or running with a live driver
    pid) or its child stdout log was written at or after `cutoff`. A nested or Codex-hosted run executes
    inside such a fire and leaves no workflow journal under its own session. One registry listing and at
    most `halted_run._MAX_FIRE_RECORDS` record reads; an unreadable registry counts as in flight. The registry
    walk mirrors `halted_run.fire_live`; keep the two in step."""
    from coordinator_core.ops.workflow_fire.fire import _pid_alive
    from coordinator_core.session_hub_path import hub_for_common

    try:
        registry = hub_for_common(Path(repo_root) / ".git") / "workflow-fires"
        with os.scandir(registry) as it:
            entries = sorted(
                ((e.stat().st_mtime, e.path) for e in it if e.name.endswith(".json") and e.is_file()), reverse=True
            )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for _, path in entries[: halted_run._MAX_FIRE_RECORDS]:
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            continue
        if record.get("state") == "pending" or (record.get("state") == "running" and _pid_alive(record.get("pid"))):
            return True
        log_path = record.get("log_path")
        try:
            if isinstance(log_path, str) and log_path and os.stat(log_path).st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def _recorded_sessions(stage: dict) -> list[str]:
    """Sessions that may drive the run: `session_id`, `adopted_from`, `driver_session_ids`; deduped, in that
    order, capped at the newest `MAX_DRIVER_SESSIONS` (a later driver is the likelier live one)."""
    out: list[str] = []
    for value in (stage.get("session_id"), *(stage.get("adopted_from") or []), *(stage.get("driver_session_ids") or [])):
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out[-MAX_DRIVER_SESSIONS:]


def _is_halted(rdir: Path) -> bool:
    """True when `halted.json` exists, except a marker a retraction resumed (`status` resumed carrying
    `retracted_at`): that run is stall-watched again."""
    if not (rdir / halted_run.HALTED_FILE).exists():
        return False
    marker = halted_run._read(rdir)
    return not (
        isinstance(marker, dict) and marker.get("status") == halted_run.STATUS_RESUMED and marker.get("retracted_at")
    )


def scan(
    repo_root: Path,
    now: float,
    *,
    in_flight: Optional[Callable[[Optional[str], float], bool]] = None,
) -> list[StalledStaged]:
    """Staged, unhalted runs of `repo_root` idle between the bound and the horizon with no agent in flight
    and no attached or freshly written fire."""
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
        if _is_halted(rdir):
            continue
        newest = _newest_mtime(rdir)
        if newest is None or newest >= cutoff or now - newest > STAGED_STALL_HORIZON_SECS:
            continue
        session_id = stage.get("session_id") if isinstance(stage.get("session_id"), str) else None
        fire_seen = fire_progress_epoch(repo_root, run_id)
        if fire_seen is not None and fire_seen >= cutoff:
            continue
        probe = in_flight or (lambda sid, at, _run=run_id: agent_in_flight(sid, at, run_id=_run))
        sessions = dict.fromkeys([*_candidate_sessions(repo_root, run_id, session_id), *_recorded_sessions(stage)])
        if any(probe(sid, cutoff) for sid in sessions):
            continue
        if fire_in_flight(repo_root, run_id, cutoff):
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
            "worker gone or stalled: no run-dir write, fire journal or run-attributed agent progress since; "
            "confirm the fire is not live before resuming or re-firing"
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


def _stall_open(rdir: Path) -> bool:
    """True when the run's marker is a `halted` / `staged_stall` one: the only state `clear_recovered`
    can clear, so a run in any other state never costs a fire-registry read."""
    marker = halted_run._read(rdir)
    return (
        isinstance(marker, dict)
        and marker.get("status") == halted_run.STATUS_HALTED
        and marker.get("halted") == HALT_STAGED_STALL
    )


def clear_recovered(
    repo_root: Path,
    now: float,
    *,
    notify: Optional[Callable[[str, str], str]] = None,
) -> list[str]:
    """Clear every `staged_stall` halt of `repo_root` whose fire has progressed since the marker was
    written; the run ids cleared. The marker becomes `resumed` (live again, neither resumable nor terminal)
    and the stage returns to `staged`, so no refire or resume duplicates a run that is still running. One
    notice to the run's session withdraws the stall notice."""
    index = live_run_index.read(repo_root, INDEX_DEADLINE_MS)
    if index is MISSING or index is STALLED:
        return []
    cleared: list[str] = []
    for run_id in list(index)[:MAX_RUNS_PER_TICK]:
        try:
            rdir = record_dir(repo_root, run_id)
        except ValueError:
            continue
        marker_at = _mtime(rdir / halted_run.HALTED_FILE)
        if marker_at is None or not _stall_open(rdir):
            continue
        seen = fire_progress_epoch(repo_root, run_id)
        if seen is None or seen <= marker_at:
            continue
        evidence = {"fire_progress_at": seen, "halted_at": marker_at, "cleared_at": now}
        if not halted_run.mark_stall_cleared(rdir, evidence):
            continue
        cleared.append(run_id)
        session_id = (read_run_stage(repo_root, run_id) or {}).get("session_id")
        if isinstance(session_id, str) and session_id:
            (notify or _notify)(
                session_id,
                f"run {run_id} staged_stall cleared: its fire is progressing. Do not resume or re-fire it.",
            )
    return cleared


def on_idle_tick(repos: Iterable[object], now: Optional[float] = None) -> bool:
    """Run `clear_recovered` then `halt_stalled` over each served repo; True when any run was halted or
    cleared. Never raises."""
    moment = time.time() if now is None else now
    any_halted = False
    for repo in repos:
        try:
            if clear_recovered(Path(str(repo)), moment):
                any_halted = True
            if halt_stalled(Path(str(repo)), moment):
                any_halted = True
        except Exception:  # noqa: BLE001 -- no staged-stall failure may escape the idle tick
            _log.warning("staged-stall scan failed for %s", repo, exc_info=True)
    return any_halted
