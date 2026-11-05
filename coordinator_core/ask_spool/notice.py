"""Per-session minted-ask notices: drain appends, the PostToolUse advisory consumes.

File: <spool>/notices/<session_id>.jsonl, one compact JSON object per line
({"spool_id", "run_id"} for a mint; {"spool_id", "status": "failed", "attempts",
"error"} for a failed drain, written by drain._notify). The reader makes one stat, renames the file to
<session_id>.<pid>.reading, then reads and unlinks it, so a drain appending
meanwhile creates a fresh file rather than losing a line to truncation.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from coordinator_core.ask_spool import record

NOTICES_DIRNAME = "notices"
NOTICE_SUFFIX = ".jsonl"
READING_SUFFIX = ".reading"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def notices_dir(repo_root: str) -> str:
    return os.path.join(record.spool_dir(str(repo_root)), NOTICES_DIRNAME)


def _safe(session_id: str) -> str:
    return _UNSAFE.sub("_", session_id)


def notice_path(repo_root: str, session_id: str) -> str:
    return os.path.join(notices_dir(repo_root), _safe(session_id) + NOTICE_SUFFIX)


def append_notice(repo_root: str, session_id: str, spool_id: str, run_id: str) -> None:
    """Append one minted notice (O_APPEND, one write). Test seam only: the production writer is drain._notify."""
    os.makedirs(notices_dir(repo_root), mode=record.DIR_MODE, exist_ok=True)
    line = json.dumps({"spool_id": spool_id, "run_id": run_id}, separators=(",", ":")) + "\n"
    fd = os.open(
        notice_path(repo_root, session_id),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        record.FILE_MODE,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def consume_notices(repo_root: Optional[str], session_id: str) -> list[dict]:
    """Return and delete the session's pending notices; [] when there are none."""
    if not repo_root or not session_id:
        return []
    path = notice_path(str(repo_root), session_id)
    try:
        os.stat(path)
    except OSError:
        return []
    reading = "%s.%d%s" % (path[: -len(NOTICE_SUFFIX)], os.getpid(), READING_SUFFIX)
    try:
        os.rename(path, reading)
    except OSError:
        return []
    out: list[dict] = []
    try:
        with open(reading, "r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("spool_id") and (
                    obj.get("run_id") or obj.get("status") == "failed"
                ):
                    out.append(obj)
    finally:
        try:
            os.unlink(reading)
        except OSError:
            pass
    return out


def advisory_text(repo_root: Optional[str], session_id: str) -> str:
    """One line per minted spooled ask, or "" when none."""
    return "\n".join(
        "Your spooled ask %s could not be minted yet (attempt %s): %s" % (n["spool_id"], n.get("attempts"), n.get("error"))
        if n.get("status") == "failed"
        else "Your spooled ask %s minted run %s." % (n["spool_id"], n["run_id"])
        for n in consume_notices(repo_root, session_id)
    )
