"""Run-agent START/END admission lines to the box Group EM: one O_APPEND JSONL line, no spawn.

Channel: ``<settings-home>/state/group-em/box/admission.jsonl`` beside nomination's
``box/holder.json``; the Group EM tails it with ``workflow_watch/mailbox_line.py``. ``post``
fails open: with no verified box holder it writes nothing unless ``require_holder=False``.
Only PIDs live at call time are reported as live; the rest are listed as ``dead_pids``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import nomination, session_registry

PHASES = ("START", "END", "REQUEST")
MAX_PIDS = 64
MAX_TEXT = 512
MAX_VERIFY_SPAN = 65536
MAX_PEAK_MB = 1048576.0


def admission_path(directory: Optional[Path] = None) -> Path:
    base = Path(directory) if directory is not None else settings_home() / "state" / "group-em"
    return base / "box" / "admission.jsonl"


def valid_peak_mb(value: object) -> Optional[float]:
    """Return ``value`` as a finite non-negative float of MB no larger than ``MAX_PEAK_MB``, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    peak = float(value)
    if not (0.0 <= peak <= MAX_PEAK_MB):
        return None
    return peak


def _append_verified(path: Path, data: bytes) -> Optional[str]:
    """Append ``data`` and confirm it is present whole in the span appended at ``path``.

    Delivery means present at ``path`` as resolved here, not receipt by the tailer.
    Returns None on verified delivery, else a reason: ``write_failed:<exc>``, ``short_write``
    or ``not_found_at_path``. Reads at most ``MAX_VERIFY_SPAN`` bytes past the pre-write size.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o644)
        try:
            before = os.fstat(fd).st_size
            written = os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as exc:
        return f"write_failed:{type(exc).__name__}:{exc}"
    if written != len(data):
        return "short_write"
    try:
        with open(path, "rb") as fh:
            fh.seek(before)
            span = fh.read(len(data) + MAX_VERIFY_SPAN)
    except OSError:
        return "not_found_at_path"
    return None if data in span else "not_found_at_path"


def post(
    phase: str,
    session_id: str,
    pids: Iterable[int] = (),
    run: str = "",
    note: str = "",
    directory: Optional[Path] = None,
    now: Optional[float] = None,
    require_holder: bool = True,
    declared_peak_mb: Optional[float] = None,
) -> dict:
    """Append one admission line for the box Group EM; return a structured reply.

    ``posted`` is False with ``reason`` ``bad_phase``, ``bad_peak`` (REQUEST without a valid
    ``declared_peak_mb``) or ``no_box_holder`` when nothing was
    written. ``delivered`` is True only when this call's line was verified present at
    ``path``; otherwise ``posted`` and ``delivered`` are False, ``reason`` names the failure
    (``write_failed:*``, ``short_write``, ``not_found_at_path``) and a stderr line is printed.
    ``declared_peak_mb`` (MB the run expects its process tree to reach) is written on START
    and REQUEST lines only, as ``declared_peak_mb``; an absent or invalid value omits the field.
    A non-live holder still receives the line: it is tailed once the holder is back.
    With ``require_holder=False`` a holderless box is still written (``holder_session`` null,
    ``holder_live`` False) so a governor reading the file sees the line.
    """
    phase = str(phase).upper()
    if phase not in PHASES:
        return {"posted": False, "reason": "bad_phase"}
    peak = valid_peak_mb(declared_peak_mb)
    if phase == "REQUEST" and peak is None:
        return {"posted": False, "reason": "bad_peak"}
    holder = nomination.box_holder(directory, now)
    if holder is None:
        if require_holder:
            return {"posted": False, "reason": "no_box_holder"}
        holder = {}
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
    if phase in ("START", "REQUEST") and peak is not None:
        line["declared_peak_mb"] = peak
    path = admission_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
    reason = _append_verified(path, data)
    if reason:
        print(f"admission_post: {phase} NOT delivered to {path}: {reason}", file=sys.stderr)
    return {
        "posted": reason is None,
        "delivered": reason is None,
        **({"reason": reason} if reason else {}),
        "path": str(path),
        "phase": phase,
        "pids": live,
        "dead_pids": dead,
        "holder_live": bool(holder.get("live")),
    }
