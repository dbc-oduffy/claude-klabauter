"""A run held on purpose by the PM: `held.json` beside `halted.json` in the run record dir.

A sibling file, never a field of `halted.json`, so no halt rewrite drops it. Reads cost one open.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.git import call_io_scope

HELD_FILE = "held.json"
#: The `args` key a non-holder sends to proceed against a held run, valued with the run id.
CONTINUE_KEY = "continue_held"

_FIELDS = ("holder_session", "pm_turn_ref", "release_condition", "held_at")


@dataclass(frozen=True)
class HeldRun:
    holder_session: str
    pm_turn_ref: str
    release_condition: str
    held_at: str = ""


def read_hold(record_dir: Path) -> Optional[HeldRun]:
    """The run's hold, or None when absent; a malformed file reads as None plus a logged advisory."""
    path = Path(record_dir) / HELD_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logging.getLogger(__name__).warning("held record %s unreadable: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if not all(isinstance(data.get(k, ""), str) for k in _FIELDS):
            raise ValueError("non-string field")
        hold = HeldRun(*(data.get(k, "") for k in _FIELDS))
        if not hold.holder_session:
            raise ValueError("no holder_session")
        return hold
    except ValueError as exc:
        logging.getLogger(__name__).warning("held record %s malformed: %s", path, exc)
        return None


def write_hold(record_dir: Path, hold: HeldRun) -> Path:
    """Write `hold` atomically to `<record_dir>/held.json` and return the path."""
    target = Path(record_dir) / HELD_FILE
    payload = {k: getattr(hold, k) for k in _FIELDS}
    atomic_write_bytes(target, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    call_io_scope.invalidate(path=target)
    return target


def clear_hold(record_dir: Path) -> bool:
    """Remove the hold; True when a file was removed."""
    target = Path(record_dir) / HELD_FILE
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    call_io_scope.invalidate(path=target)
    return True


def held_reply(run_id: str, hold: HeldRun) -> str:
    """The typed held reply: the one fact, then the one-line continue."""
    return (
        f"Run {run_id} is held by session {hold.holder_session} ({hold.pm_turn_ref}); "
        f"releases when: {hold.release_condition}. "
        f'To proceed, pass args.{CONTINUE_KEY}: "{run_id}".'
    )
