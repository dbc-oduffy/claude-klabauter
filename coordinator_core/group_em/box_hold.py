"""Box-wide Group EM hold on heavy commands: one capped-TTL record, read without a spawn.

Record: ``<settings-home>/state/group-em/box/hold.json`` beside nomination's ``box/holder.json``.
Leaf module -- never import ``nomination`` (its closure pulls the session registry onto the
admission guard's hot path). ``read_hold`` fails open: an absent, expired or malformed record is
no hold, so a corrupt file cannot wedge the box and the TTL cap bounds a forgotten one.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes

MAX_HOLD_TTL_S = 1800


@dataclass(frozen=True)
class Hold:
    set_by_session: str
    reason: str
    set_at: float
    expires_at: float


def _hold_path(directory: Optional[Path] = None) -> Path:
    base = Path(directory) if directory is not None else settings_home() / "state" / "group-em"
    return base / "box" / "hold.json"


def _parse(raw: str) -> Optional[Hold]:
    try:
        data = json.loads(raw)
        sess = data["set_by_session"]
        reason = data["reason"]
        set_at = data["set_at"]
        expires = data["expires_at"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(sess, str) or not sess or not isinstance(reason, str):
        return None
    for n in (set_at, expires):
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return None
    return Hold(sess, reason, float(set_at), float(expires))


def read_hold(now: Optional[float] = None, directory: Optional[Path] = None) -> Optional[Hold]:
    """Return the live hold, or None when absent, expired or malformed. One open+read."""
    try:
        with open(_hold_path(directory), "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    hold = _parse(raw)
    if hold is None:
        return None
    deadline = min(hold.expires_at, hold.set_at + MAX_HOLD_TTL_S)
    if (time.time() if now is None else now) >= deadline:
        return None
    return hold


def set_hold(
    session_id: str,
    reason: str,
    ttl_s: float,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> Hold:
    """Write the hold atomically; ttl_s is clamped to (0, MAX_HOLD_TTL_S]."""
    ttl = max(1.0, min(float(ttl_s), float(MAX_HOLD_TTL_S)))
    t = time.time() if now is None else now
    hold = Hold(session_id, reason, t, t + ttl)
    atomic_write_bytes(
        _hold_path(directory), json.dumps(asdict(hold), indent=2).encode("utf-8")
    )
    return hold


def clear_hold(session_id: Optional[str] = None, directory: Optional[Path] = None) -> bool:
    """Remove the hold; True when a record was removed.

    A live hold set by another session is refused (False) unless session_id is None, the
    operator path. An expired or malformed record is cleared by anyone.
    """
    path = _hold_path(directory)
    if session_id is not None:
        live = read_hold(directory=directory)
        if live is not None and live.set_by_session != session_id:
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
