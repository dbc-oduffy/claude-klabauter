"""Run-agent START/END admission lines to the box Group EM: one O_APPEND JSONL line, no spawn.

Channel: ``<settings-home>/state/group-em/box/admission.jsonl`` beside nomination's
``box/holder.json``; the Group EM tails it with ``workflow_watch/mailbox_line.py``. ``post``
fails open: with no verified box holder it writes nothing. Only PIDs live at call time are
reported as live; the rest are listed as ``dead_pids``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import nomination, session_registry

PHASES = ("START", "END")
MAX_PIDS = 64
MAX_TEXT = 512


def admission_path(directory: Optional[Path] = None) -> Path:
    base = Path(directory) if directory is not None else settings_home() / "state" / "group-em"
    return base / "box" / "admission.jsonl"


def post(
    phase: str,
    session_id: str,
    pids: Iterable[int] = (),
    run: str = "",
    note: str = "",
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Append one admission line for the box Group EM; return a structured reply.

    ``posted`` is False with ``reason`` ``bad_phase`` or ``no_box_holder`` when nothing was
    written. A non-live holder still receives the line: it is tailed once the holder is back.
    """
    phase = str(phase).upper()
    if phase not in PHASES:
        return {"posted": False, "reason": "bad_phase"}
    holder = nomination.box_holder(directory, now)
    if holder is None:
        return {"posted": False, "reason": "no_box_holder"}
    live: list[int] = []
    dead: list[int] = []
    for raw in list(pids)[:MAX_PIDS]:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        (live if session_registry.pid_alive(pid) else dead).append(pid)
    line = {
        "ts": time.time() if now is None else now,
        "phase": phase,
        "session_id": session_id,
        "run": run[:MAX_TEXT],
        "pids": live,
        "dead_pids": dead,
        "note": note[:MAX_TEXT],
        "holder_session": holder.get("session_id"),
    }
    path = admission_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return {
        "posted": True,
        "path": str(path),
        "phase": phase,
        "pids": live,
        "dead_pids": dead,
        "holder_live": bool(holder.get("live")),
    }
