"""Port-owner resolution that fails loud: a pid of 0 or NONE is owner-unknown, never "port free".

The owner of a port is read from a pidfile or a supervisor child record, never
from a psutil connection table (unusable for unprivileged callers on Windows).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union


class OwnerUnknown(RuntimeError):
    """The port's owner could not be resolved; the caller must not treat the port as free."""


def parse_owner_pid(raw: Union[int, str, bytes, None], *, source: str = "owner record") -> int:
    """Return a positive pid, or raise OwnerUnknown for None, NONE, empty, 0, negative or non-numeric input."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.upper() in {"NONE", "NULL"}:
            raise OwnerUnknown(f"{source}: owner unknown (empty or NONE)")
        try:
            value = int(text)
        except ValueError:
            raise OwnerUnknown(f"{source}: owner unknown (non-numeric {text!r})") from None
    elif isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    else:
        raise OwnerUnknown(f"{source}: owner unknown ({raw!r})")
    if value <= 0:
        raise OwnerUnknown(f"{source}: owner unknown (pid {value})")
    return value


def owner_from_pidfile(pidfile: Union[str, Path]) -> int:
    """Resolve the owner pid from a pidfile; a missing or unreadable file raises OwnerUnknown."""
    path = Path(pidfile)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnerUnknown(f"{path}: owner unknown ({exc.__class__.__name__})") from exc
    return parse_owner_pid(text, source=str(path))


def owner_from_child_record(record: Optional[dict], *, source: str = "supervisor child record") -> int:
    """Resolve the owner pid from a supervisor child record's `pid` field."""
    if not isinstance(record, dict):
        raise OwnerUnknown(f"{source}: owner unknown (no record)")
    return parse_owner_pid(record.get("pid"), source=source)
