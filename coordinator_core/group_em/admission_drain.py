"""The managing G-EM's drain of every managed box's admission spool into its own admission channel."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import admission_spool, box_registry, nomination, remote_roster, suite_grant


def _stamp_path(directory: Optional[Path]) -> Path:
    return admission_spool._base(directory) / "box" / "admission-drain" / "last-run"


def _stamp_age_ok(directory: Optional[Path], now: float) -> bool:
    try:
        last = float(_stamp_path(directory).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return now - last >= remote_roster.REFRESH_INTERVAL_S or now < last


def _write_stamp(directory: Optional[Path], now: float) -> None:
    try:
        path = _stamp_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, repr(now).encode("ascii"))
    except OSError:
        pass


def _default_reader(box: dict, offset: int) -> dict:
    from coordinator_core.group_em import box_transport

    res = box_transport.read_admission_spool(box, offset)
    if isinstance(res, dict) and res.get("ok") is True and isinstance(res.get("reply"), dict):
        return res["reply"]
    return res


def _drain_box(box_id: str, directory, home, reader: Callable) -> dict:
    box, why = suite_grant._far_box(box_id, directory, home)
    if box is None:
        return {"ok": False, "reason": why}
    offset = admission_spool._read_cursor(box_id, directory)
    try:
        reply = reader(box, offset)
    except Exception as exc:
        return {"ok": False, "reason": f"read_failed:{type(exc).__name__}:{exc}", "cursor": offset}
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        reason = reply.get("reason") if isinstance(reply, dict) else None
        return {"ok": False, "reason": str(reason or "read_failed"), "cursor": offset}
    drained = admission_spool.drain_into(box_id, reply, directory)
    return {"ok": True, **drained}


def drain_managed(
    directory: Optional[Path] = None,
    reader: Optional[Callable] = None,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> dict:
    """Live holder only, rate-limited: drain each non-local box's spool; ``{box_id: result}``, never raises."""
    when = time.time() if now is None else now
    try:
        if (nomination.box_holder(directory) or {}).get("live") is not True:
            return {}
        if not _stamp_age_ok(directory, when):
            return {}
        _write_stamp(directory, when)
        read = reader if reader is not None else _default_reader
        local = box_registry.local_box_id()
        results: dict = {}
        for row in box_registry.read_boxes(directory):
            box_id = row.get("box_id")
            if not isinstance(box_id, str) or box_id == local:
                continue
            try:
                results[box_id] = _drain_box(box_id, directory, home, read)
            except Exception as exc:
                results[box_id] = {"ok": False, "reason": f"drain_failed:{type(exc).__name__}:{exc}"}
        return results
    except Exception:
        return {}
