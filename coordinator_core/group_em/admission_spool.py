"""Local spool of admission posts for a box with no box holder, and the managing G-EM's cursor drain of it.

Spool: ``<group-em>/box/admission-spool.jsonl``, one O_APPEND JSONL line per post. The managing
G-EM reads it by byte offset (``read_since``, whole lines only) and ``drain_into`` appends the lines
to its own ``admission.jsonl``, then advances a per-box cursor. File I/O only, no spawn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import admission_post
from coordinator_core.group_em.box_registry import fold_box_id

_SKIP_CHUNK = 65536


def _base(directory: Optional[Path]) -> Path:
    return Path(directory) if directory is not None else settings_home() / "state" / "group-em"


def spool_path(directory: Optional[Path] = None) -> Path:
    """``<group-em>/box/admission-spool.jsonl``."""
    return _base(directory) / "box" / "admission-spool.jsonl"


def cursor_path(box_id: str, directory: Optional[Path] = None) -> Path:
    """``<group-em>/box/admission-drain/<folded box_id>.cursor``."""
    return _base(directory) / "box" / "admission-drain" / f"{fold_box_id(box_id)}.cursor"


def spool_line(line: dict, directory: Optional[Path] = None) -> Optional[str]:
    """Append ``line`` as one JSONL line to the spool; None on verified append, else a reason. Never raises."""
    try:
        data = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
        path = spool_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        return admission_post._append_verified(path, data)
    except (OSError, TypeError, ValueError) as exc:
        return f"write_failed:{type(exc).__name__}:{exc}"


def _skip_oversize_line(fh, start: int, size: int) -> int:
    """Offset just past the newline ending the line that begins at ``start``; ``start`` when none yet."""
    pos = start
    while pos < size:
        fh.seek(pos)
        chunk = fh.read(_SKIP_CHUNK)
        if not chunk:
            break
        idx = chunk.find(b"\n")
        if idx >= 0:
            return pos + idx + 1
        pos += len(chunk)
    return start


def read_since(offset: int, directory: Optional[Path] = None, max_bytes: int = 65536) -> dict:
    """Whole spool lines from byte ``offset``: ``{"ok", "lines", "next_offset"}``.

    An offset past EOF (spool replaced) restarts at 0 and the reply carries ``restarted: true``. A torn trailing line is left for the next read;
    a line longer than ``max_bytes`` is skipped whole. Never raises.
    """
    try:
        start = int(offset)
        if start < 0:
            start = 0
        path = spool_path(directory)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return {"ok": True, "lines": [], "next_offset": 0}
        restarted = start > size
        if restarted:
            start = 0
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(max(1, int(max_bytes)))
            cut = chunk.rfind(b"\n")
            if cut < 0:
                if len(chunk) >= max(1, int(max_bytes)) and start + len(chunk) < size:
                    nxt = _skip_oversize_line(fh, start, size)
                    return {"ok": True, "lines": [], "next_offset": nxt, **({"restarted": True} if restarted else {})}
                return {"ok": True, "lines": [], "next_offset": start, **({"restarted": True} if restarted else {})}
            body = chunk[: cut + 1]
        lines = [raw.decode("utf-8", "replace") for raw in body.split(b"\n")[:-1]]
        return {"ok": True, "lines": lines, "next_offset": start + len(body), **({"restarted": True} if restarted else {})}
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "lines": [], "next_offset": 0, "reason": f"{type(exc).__name__}:{exc}"}


def _read_cursor(box_id: str, directory: Optional[Path]) -> int:
    try:
        return max(0, int(cursor_path(box_id, directory).read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def drain_into(box_id: str, reply: dict, directory: Optional[Path] = None) -> dict:
    """Append a ``read_since`` ``reply`` from ``box_id`` to the local admission channel; advance the cursor.

    Each line gains ``drained_from``. A reply that does not start at the cursor is a repeat (or stale)
    and appends nothing, unless it carries ``restarted`` from a replaced spool. Returns ``{"appended", "cursor"}``;
    an append failure stops the drain with the cursor at the last line delivered. Never raises.
    """
    cursor = _read_cursor(box_id, directory)
    try:
        lines = [x for x in (reply.get("lines") or []) if isinstance(x, str)]
        end = int(reply["next_offset"])
        span = sum(len(x.encode("utf-8")) + 1 for x in lines)
        start = end - span
        skipped_ahead = not lines and end > cursor
        replaced = reply.get("restarted") is True and start == 0
        if start != cursor and not replaced and not skipped_ahead:
            return {"appended": 0, "cursor": cursor}
        target = admission_post.admission_path(directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        appended = 0
        pos = start
        for raw in lines:
            step = len(raw.encode("utf-8")) + 1
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            if isinstance(record, dict):
                record["drained_from"] = box_id
                data = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
                if admission_post._append_verified(target, data) is not None:
                    break
                appended += 1
            pos += step
        else:
            pos = end
        path = cursor_path(box_id, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, str(pos).encode("ascii"))
        return {"appended": appended, "cursor": pos}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"appended": 0, "cursor": cursor}
