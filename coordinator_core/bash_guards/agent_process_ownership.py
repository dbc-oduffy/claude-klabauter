"""Prove a PID belongs to a subagent: session ledger of its Bash commands plus a process-table read.

`record_agent_command` appends one row per agent Bash command; `pid_owned_by_agent`
allows a PID only when it is provable from the ledger and one psutil process-table
read. Anything unprovable is denied (returns False).

Traps:
- A backgrounded child is reparented to pid 1, so a pure parent-chain walk cannot prove
  ownership on POSIX; the proof is the live agent shell's session/process group.
- Windows has no session id: the proof is the ancestor walk to the agent shell.
- A ledger row is written before later guards may deny the command, so a row for a
  command that never ran is inert: no live shell carries its text.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core._settings_home import settings_home

_LEDGER_DIR_RELPATH = ("state", "agent-process-ledger")
_CLOCK_SLACK_S = 1.0
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _psutil() -> Any:
    import psutil

    return psutil


def _has_session_ids() -> bool:
    return hasattr(os, "getsid") and hasattr(os, "getpgid")


def ledger_path(session_id: str) -> Path:
    """Return the session-scoped ledger path under settings_home()."""
    name = _SAFE_NAME_RE.sub("_", session_id or "") or "unknown-session"
    return settings_home() / Path(*_LEDGER_DIR_RELPATH) / f"{name}.jsonl"


def record_agent_command(session_id: str, agent_id: str, command: str) -> None:
    """Append one newline-terminated row for an agent's Bash command; never raises."""
    if not agent_id or not command:
        return
    row = json.dumps(
        {"agent_id": agent_id, "t": time.time(), "cmd": command}, ensure_ascii=False
    )
    try:
        path = ledger_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (row + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return


def _read_rows(session_id: str, agent_id: str) -> List[dict]:
    """Return this agent's well-formed ledger rows; a missing file or torn row yields none."""
    try:
        text = ledger_path(session_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: List[dict] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and row.get("agent_id") == agent_id
            and isinstance(row.get("cmd"), str)
            and isinstance(row.get("t"), (int, float))
            and row["cmd"].strip()
        ):
            rows.append(row)
    return rows


def _joined(cmdline: Optional[List[str]]) -> str:
    return " ".join(cmdline or [])


def _carries_command_text(cmdline: Optional[List[str]], command: str) -> bool:
    """True when the process line contains the command, or every argument after argv0 appears in it."""
    if not cmdline:
        return False
    if command.strip() in _joined(cmdline):
        return True
    args = cmdline[1:]
    return bool(args) and all(a in command for a in args)


def _live_shells(ps: Any, command: str, recorded_at: float, exclude_pid: int) -> List[Any]:
    """Live non-target processes whose command line carries the ledgered command and started after it was recorded."""
    me = os.getpid()
    cmd = command.strip()
    shells = []
    for proc in ps.process_iter(["pid", "cmdline", "create_time"]):
        info = proc.info
        if info.get("pid") in (me, exclude_pid):
            continue
        line = _joined(info.get("cmdline"))
        created = info.get("create_time")
        if cmd in line and created is not None and created >= recorded_at - _CLOCK_SLACK_S:
            shells.append(proc)
    return shells


def _chain(ps: Any, proc: Any) -> List[Any]:
    """The process followed by its live ancestors; each link must satisfy parent.create_time <= child.create_time."""
    chain = [proc]
    seen = {proc.pid}
    cur = proc
    while True:
        try:
            parent = cur.parent()
        except (ps.Error, OSError):
            break
        if parent is None or parent.pid <= 1 or parent.pid in seen:
            break
        try:
            if parent.create_time() > cur.create_time():
                break
        except (ps.Error, OSError):
            break
        chain.append(parent)
        seen.add(parent.pid)
        cur = parent
    return chain


def _same_session(pid: int, shell_pid: int) -> bool:
    try:
        return os.getsid(pid) == os.getsid(shell_pid) or os.getpgid(pid) == os.getpgid(shell_pid)
    except (OSError, ProcessLookupError):
        return False


def _owned_via_row(ps: Any, target: Any, row: dict) -> bool:
    command = row["cmd"]
    shells = _live_shells(ps, command, float(row["t"]), target.pid)
    if len(shells) != 1:
        return False
    shell = shells[0]
    try:
        shell_created = shell.create_time()
        target_created = target.create_time()
    except (ps.Error, OSError):
        return False
    if target_created < shell_created:
        return False
    try:
        chain = _chain(ps, target)
    except (ps.Error, OSError):
        return False
    if not _has_session_ids():
        return any(p.pid == shell.pid for p in chain)
    if not _same_session(target.pid, shell.pid):
        return False
    for p in chain:
        try:
            if _carries_command_text(p.cmdline(), command):
                return True
        except (ps.Error, OSError):
            continue
    return False


def _path_tokens(command: str) -> List[str]:
    """Scratch-path tokens of a ledgered command.

    Only `scratch/` paths: a repo-root token would match every orphan working in the repo,
    including detached engine children such as the post-commit index refresh.
    """
    tokens = command.replace('"', " ").replace("'", " ").replace("\\", "/").split()
    return [t for t in tokens if "scratch/" in t and len(t.split("scratch/", 1)[1]) >= 3]


def _is_orphan(proc: Any, ps: Any) -> bool:
    try:
        parent = proc.parent()
    except (ps.Error, OSError):
        return True
    return parent is None or parent.pid <= 1


def _orphan_owned_via_row(ps: Any, target: Any, row: dict) -> bool:
    """An orphan (parent dead or pid 1) created after the row whose argv or cwd carries the row's command text or a path from it."""
    if not _is_orphan(target, ps):
        return False
    if _live_shells(ps, row["cmd"], float(row["t"]), target.pid):
        return False
    try:
        if target.create_time() < float(row["t"]) - _CLOCK_SLACK_S:
            return False
        cmdline = target.cmdline()
    except (ps.Error, OSError):
        return False
    if _carries_command_text(cmdline, row["cmd"]):
        return True
    haystack = _joined(cmdline)
    try:
        haystack += " " + str(target.cwd()).replace("\\", "/")
    except Exception:
        pass
    return any(tok in haystack for tok in _path_tokens(row["cmd"]))


def pid_owned_by_agent(pid: int, agent_id: str, session_id: str) -> bool:
    """True only when `pid` is provably a descendant of a Bash command this agent launched."""
    if not isinstance(pid, int) or pid <= 1 or not agent_id:
        return False
    claude_pid = os.environ.get("CLAUDE_PID", "")
    if claude_pid.isdigit() and int(claude_pid) == pid:
        return False
    rows = _read_rows(session_id, agent_id)
    if not rows:
        return False
    try:
        ps = _psutil()
        target = ps.Process(pid)
        target.create_time()
    except Exception:
        return False
    seen = set()
    for row in rows:
        key = (row["cmd"].strip(), row["t"])
        if key in seen:
            continue
        seen.add(key)
        try:
            if _owned_via_row(ps, target, row) or _orphan_owned_via_row(ps, target, row):
                return True
        except Exception:
            continue
    return False
