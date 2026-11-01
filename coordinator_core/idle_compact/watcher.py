"""Idle-compaction watcher: one box-wide tick over the live-session registry.

``tick`` reads ``harness_registry.snapshot()`` once, asks ``eligibility.decide`` per session,
injects ``/compact`` through the platform injector on an ``inject`` verdict, and appends one
``LogRecord`` line per session acted on or skipped. A skip is logged when its reason changes for
that session, not every tick, so a never-allowlisted session costs one line per watcher process.
A tick that injects nothing spawns no process.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.idle_compact import contract, eligibility

ACTION_INJECTED = "injected"
ACTION_SKIPPED = "skipped"


def new_run_id() -> str:
    return uuid.uuid4().hex


def make_injector() -> contract.Injector:
    """The injector for this OS; imports only the module the host can run."""
    if sys.platform == "win32":
        from coordinator_core.idle_compact.inject_windows import WindowsInjector

        return WindowsInjector()
    from coordinator_core.idle_compact.inject_posix import PosixInjector

    return PosixInjector()


def append_log(record: contract.LogRecord, path: Optional[Path] = None) -> None:
    """Append one jsonl line; a log fault never stops the tick."""
    target = Path(path) if path is not None else contract.injection_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as fh:
            fh.write((json.dumps(record.to_dict(), ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError as exc:
        print(f"idle-compact-watcher: log append failed: {exc}", file=sys.stderr)


def _now_iso() -> str:
    from coordinator_core.session import core

    return core.now_iso()


def tick(
    run_id: str,
    injector: contract.Injector,
    *,
    armed: Dict[str, dict],
    last_logged: Dict[str, str],
    now: Optional[float] = None,
    log_path: Optional[Path] = None,
    snapshot: Optional[Callable[[], dict]] = None,
) -> List[contract.LogRecord]:
    """Run one pass; returns the records appended. ``armed`` and ``last_logged`` persist across ticks."""
    if snapshot is None:
        from coordinator_core.session import harness_registry

        snapshot = harness_registry.snapshot
    when = time.time() if now is None else now
    out: List[contract.LogRecord] = []
    for sid, record in snapshot().items():
        pid = getattr(record, "pid", None)
        if not isinstance(pid, int):
            continue
        transcript = _transcript_for(sid, getattr(record, "cwd", None))
        verdict = eligibility.decide(sid, record, transcript, when, armed)
        if verdict.action == eligibility.INJECT:
            result = injector.inject(pid)
            size = eligibility.transcript_size(transcript)
            eligibility.arm(armed, sid, size if size is not None else 0)
            action = ACTION_INJECTED if result.ok else ACTION_SKIPPED
            reason = verdict.reason if result.ok else result.reason
        else:
            action, reason = ACTION_SKIPPED, verdict.reason
        key = f"{action}:{reason}"
        if action == ACTION_SKIPPED and last_logged.get(sid) == key:
            continue
        last_logged[sid] = key
        rec = contract.LogRecord(_now_iso(), run_id, sid, pid, action, reason)
        append_log(rec, log_path)
        out.append(rec)
    return out


def _transcript_for(sid: str, cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    from coordinator_core.group_em.read_pass import _transcript_path_for

    return _transcript_path_for(sid, cwd)


def armed_path() -> Path:
    return contract.settings_home() / "state" / "idle-compact" / "armed.json"


def load_armed(path: Optional[Path] = None) -> Dict[str, dict]:
    """The once-per-idle-stretch ledger persisted by ``save_armed``; empty on any fault."""
    try:
        with open(path or armed_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_armed(armed: Dict[str, dict], path: Optional[Path] = None) -> None:
    target = Path(path) if path is not None else armed_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(armed), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        print(f"idle-compact-watcher: armed save failed: {exc}", file=sys.stderr)


def loop(run_id: str, injector: contract.Injector, interval: float = contract.TICK_SECONDS,
         sleep: Callable[[float], None] = time.sleep, max_ticks: Optional[int] = None) -> None:
    """Tick forever (or ``max_ticks`` times); a failed tick is reported on stderr and the loop continues."""
    armed = load_armed()
    last_logged: Dict[str, str] = {}
    n = 0
    while max_ticks is None or n < max_ticks:
        try:
            tick(run_id, injector, armed=armed, last_logged=last_logged)
            save_armed(armed)
        except Exception as exc:  # a watcher that dies on one bad record stops serving the box
            print(f"idle-compact-watcher: tick failed: {exc!r}", file=sys.stderr)
        n += 1
        if max_ticks is None or n < max_ticks:
            sleep(interval)


def run_once(run_id: Optional[str] = None,
             injector: Optional[contract.Injector] = None) -> List[contract.LogRecord]:
    armed = load_armed()
    records = tick(run_id or new_run_id(), injector or make_injector(), armed=armed, last_logged={})
    save_armed(armed)
    return records


def default_watcher_log() -> Path:
    return contract.settings_home() / "state" / "idle-compact" / "watcher.log"


__all__ = ["tick", "loop", "run_once", "make_injector", "new_run_id", "append_log", "default_watcher_log"]
