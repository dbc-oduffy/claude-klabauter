"""Per-session minted-ask notices: drain appends, the PostToolUse advisory consumes.

File: <spool>/notices/<session_id>.jsonl, one compact JSON object per line
({"spool_id", "run_id"} for a mint; {"spool_id", "status": "failed", "attempts",
"error"} for a failed drain; {"spool_id", "status": "coalesced", "run_id"}; {"spool_id",
"status": "withdrawn", "reason"}; all written by drain._notify; {"run_id", "status": "halted",
"halted", "reason", "at"} with no spool_id, written by append_halt_notice; {"run_id", "status":
"unstarted", "at"} with no spool_id, written by append_unstarted_notice; {"run_id", "status":
"never_launched", "age_s", "action", "at"} with no spool_id, written by
append_never_launched_notice). The reader makes one
stat, renames the file to
<session_id>.<pid>.reading, then reads and unlinks it, so a drain appending
meanwhile creates a fresh file rather than losing a line to truncation.
"""

from __future__ import annotations

import json
import os
import re
import time
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


def append_halt_notice(repo_root: str, session_id: str, run_id: str, halted: str, reason: str) -> bool:
    """Append one run-halted notice for the submitting session (O_APPEND, one write); False on an unsafe or empty session id."""
    if not session_id or _UNSAFE.search(session_id) is not None:
        return False
    os.makedirs(notices_dir(repo_root), mode=record.DIR_MODE, exist_ok=True)
    line = json.dumps(
        {"run_id": run_id, "status": "halted", "halted": halted, "reason": reason, "at": time.time()},
        separators=(",", ":"),
    ) + "\n"
    fd = os.open(
        notice_path(repo_root, session_id),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        record.FILE_MODE,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def append_unstarted_notice(repo_root: str, session_id: str, run_id: str) -> bool:
    """Append one minted-not-started notice for a run no spool entry minted (O_APPEND, one write); False on an unsafe or empty session id."""
    if not session_id or _UNSAFE.search(session_id) is not None:
        return False
    os.makedirs(notices_dir(repo_root), mode=record.DIR_MODE, exist_ok=True)
    line = json.dumps({"run_id": run_id, "status": "unstarted", "at": time.time()}, separators=(",", ":")) + "\n"
    fd = os.open(
        notice_path(repo_root, session_id),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        record.FILE_MODE,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def append_never_launched_notice(repo_root: str, session_id: str, run_id: str, age_s: float, action: str) -> bool:
    """Append one minted-never-launched notice (O_APPEND, one write); `action` is "refired" or "closed". False on an unsafe or empty session id."""
    if not session_id or _UNSAFE.search(session_id) is not None:
        return False
    os.makedirs(notices_dir(repo_root), mode=record.DIR_MODE, exist_ok=True)
    line = json.dumps(
        {"run_id": run_id, "status": "never_launched", "age_s": age_s, "action": action, "at": time.time()},
        separators=(",", ":"),
    ) + "\n"
    fd = os.open(
        notice_path(repo_root, session_id),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        record.FILE_MODE,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return True


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
                if not isinstance(obj, dict):
                    continue
                if obj.get("status") in ("halted", "unstarted", "never_launched"):
                    if obj.get("run_id"):
                        out.append(obj)
                elif obj.get("spool_id") and (
                    obj.get("run_id") or obj.get("status") in ("failed", "withdrawn")
                ):
                    out.append(obj)
    finally:
        try:
            os.unlink(reading)
        except OSError:
            pass
    return out


def advisory_text(repo_root: Optional[str], session_id: str) -> str:
    """One line per spooled-ask notice (a terminal run reads terminal, never minted), or "" when none."""
    return "\n".join(_line(n, repo_root) for n in consume_notices(repo_root, session_id))


def run_disposition(repo_root: str, run_id: str, spool_id: Optional[str] = None) -> Optional[str]:
    """The run's current stage, with a terminal disposition winning over a stale `minted` (or absent) stage.

    A run-stage file nothing advanced off `minted` still reads minted; the run's `halted.json` or a
    `withdrawn` spool entry for `spool_id` is the terminal record. None on an unsafe run id or no record."""
    if not run_id or _UNSAFE.search(run_id) is not None:
        return None
    from pathlib import Path

    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage, record_dir

    stage = (read_run_stage(Path(repo_root), run_id) or {}).get("stage")
    if stage and stage != "minted":
        return stage
    if spool_id and any(
        os.path.exists(record.entry_path(str(repo_root), spool_id, suffix))
        for suffix in (record.WITHDRAWN_SUFFIX, record.LEGACY_WITHDRAWN_SUFFIX)
    ):
        return "withdrawn"
    try:
        with open(record_dir(Path(repo_root), run_id) / "halted.json", "r", encoding="utf-8") as fh:
            if isinstance(json.load(fh), dict):
                return "halted"
    except (OSError, ValueError):
        pass
    return stage


def _start_call(repo_root: Optional[str], run_id: str) -> Optional[str]:
    """`Workflow({scriptPath: ..., args: ...})` for the run's emitted script, None when the repo root or the script is absent.

    `args` names the `dispatch.minted_run` start whose reply `fire_args` carries `repoRoot` and `runToken`:
    a start without them halts the run at its commit stage ("no run token"). Rendering mints nothing.
    """
    if not repo_root:
        return None
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    script = os.path.join(repo_root, RUN_DIR_ROOT, run_id + ".workflow.mjs")
    if not os.path.isfile(script):
        return None
    start = json.dumps({"run_id": run_id, "action": "start"}, separators=(",", ":"))
    return "Workflow({scriptPath: %s, args: <fire_args of dispatch.minted_run %s>})" % (
        json.dumps(script.replace("\\", "/")),
        start,
    )


def _line(n: dict, repo_root: Optional[str] = None) -> str:
    status = n.get("status")
    if status == "halted":
        return "Your run %s was halted (%s): %s" % (n["run_id"], n.get("halted"), n.get("reason"))
    if status == "never_launched":
        age = n.get("age_s")
        mins = "%d min" % round(age / 60) if isinstance(age, (int, float)) else "over 15 min"
        return "Your ask %s was minted %s ago and never launched; it was %s." % (n["run_id"], mins, n.get("action"))
    if status == "unstarted":
        stage = run_disposition(repo_root, n["run_id"]) if repo_root else None
        if stage and stage != "minted":
            return "Your run %s was minted and not started; it is now %s, nothing to start." % (n["run_id"], stage)
        call = _start_call(repo_root, n["run_id"])
        return "Your run %s is minted, not started.%s" % (n["run_id"], f" Start it: {call}" if call else "")
    if status == "failed":
        return "Your spooled ask %s could not be minted yet (attempt %s): %s" % (n["spool_id"], n.get("attempts"), n.get("error"))
    if status == "withdrawn":
        return "Your spooled ask %s was withdrawn: %s" % (n["spool_id"], n.get("reason"))
    if status == "coalesced":
        return "Your spooled ask %s coalesced into run %s." % (n["spool_id"], n["run_id"])
    # TRAP: a notice can be read long after it was written (pre-r24 backlogs were never consumed); the run
    # may since be halted, landed or superseded, so its current stage decides, never the notice.
    stage = run_disposition(repo_root, n["run_id"], n.get("spool_id")) if repo_root else None
    if stage and stage != "minted":
        return "Your spooled ask %s minted run %s; it is now %s, nothing to start." % (n["spool_id"], n["run_id"], stage)
    # A drain-minted run has no launcher: left unstarted, it is halted abandoned_at_minted in 30 minutes.
    call = _start_call(repo_root, n["run_id"]) if n.get("unstarted") and stage == "minted" else None
    if call:
        return "Your spooled ask %s minted run %s, not started. Start it: %s" % (n["spool_id"], n["run_id"], call)
    return "Your spooled ask %s minted run %s." % (n["spool_id"], n["run_id"])
