"""Box load alarm primitives for the group-em watch: load5, top processes, session attribution.

Every reader is injectable and no function raises. One `ps` spawn per
`top_processes` call (bounded by `_PS_TIMEOUT_S`); no per-process lsof/cwd
spawn -- cwd comes only from the session records.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

_IS_WINDOWS = os.name == "nt"
_PS_TIMEOUT_S = 1
_MAX_HOPS = 64
_UNATTRIBUTED = "?@?"


def read_load5() -> Optional[float]:
    """5-minute load average, or None where the platform has no `getloadavg`."""
    try:
        if not hasattr(os, "getloadavg"):
            return None
        return float(os.getloadavg()[1])
    except (OSError, ValueError, IndexError):
        return None


def core_count() -> int:
    """Logical core count; 1 when undeterminable."""
    return os.cpu_count() or 1


def top_processes(n: int = 8, run: Callable = subprocess.run) -> list[dict]:
    """Top `n` processes by pcpu as `{pid, ppid, pcpu, comm}`; [] on Windows or failure."""
    return _ps_rows(run)[: max(n, 0)]


def _ps_rows(run: Callable) -> list[dict]:
    """All processes from one `ps` spawn, sorted by pcpu descending.

    `comm` is the last field and split off with maxsplit 3: macOS reports a
    full path that may contain spaces.
    """
    if _IS_WINDOWS:
        return []
    try:
        result = run(
            ["ps", "-A", "-o", "pid=,ppid=,pcpu=,comm="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
        )
        if getattr(result, "returncode", 1) != 0:
            return []
        text = result.stdout or ""
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return []
    rows: list[dict] = []
    for line in str(text).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "pcpu": float(parts[2]),
                    "comm": parts[3].strip(),
                }
            )
        except ValueError:
            continue
    rows.sort(key=lambda r: r["pcpu"], reverse=True)
    return rows


def read_sessions(config_dir: Optional[Path] = None) -> dict[int, dict]:
    """`{pid: {sessionId, cwd}}` from `<claude config>/sessions/*.json`; {} when unreadable."""
    try:
        if config_dir is None:
            from coordinator_core.hooks.assert_em_role import _resolve_claude_config_dir

            config_dir = _resolve_claude_config_dir()
        sessions_dir = Path(config_dir) / "sessions" if config_dir else None
        if sessions_dir is None or not sessions_dir.is_dir():
            return {}
        out: dict[int, dict] = {}
        for entry in sessions_dir.glob("*.json"):
            try:
                record = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            pid = record.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool):
                out[pid] = {
                    "sessionId": str(record.get("sessionId") or ""),
                    "cwd": str(record.get("cwd") or ""),
                }
        return out
    except Exception:
        return {}


def repo_of(cwd: str) -> str:
    """Name of the first ancestor of `cwd` holding `.git`; '?' when none."""
    try:
        if not cwd:
            return "?"
        path = Path(cwd)
        for candidate in (path, *path.parents):
            if (candidate / ".git").exists():
                return candidate.name or "?"
    except (OSError, ValueError):
        pass
    return "?"


def attribute(procs: list[dict], ppid_map: dict, sessions: dict) -> list[dict]:
    """Add `session` (first 8 of the id) and `repo` to each proc; unattributed is `?@?`.

    Walks ppid to a pid present in `sessions`; stops at pid 0/1, a revisited
    pid, or `_MAX_HOPS`. Never drops a process.
    """
    out: list[dict] = []
    for proc in procs if isinstance(procs, list) else []:
        row = dict(proc) if isinstance(proc, dict) else {}
        row["session"], row["repo"] = "?", "?"
        try:
            pid = row.get("pid")
            seen: set = set()
            for _ in range(_MAX_HOPS):
                if pid in seen or not isinstance(pid, int) or pid in (0, 1):
                    break
                seen.add(pid)
                record = sessions.get(pid)
                if record is not None:
                    row["session"] = (str(record.get("sessionId") or "")[:8]) or "?"
                    row["repo"] = repo_of(str(record.get("cwd") or ""))
                    break
                pid = ppid_map.get(pid)
        except Exception:
            pass
        out.append(row)
    return out


def next_state(prev, load5: Optional[float], cores: int, n: int = 3) -> tuple[dict, bool]:
    """Advance the over-core streak; fire exactly once when it reaches `n`.

    State is `{"count": int}`. A missing or malformed `prev` is the initial
    state; `load5` None resets without firing; load5 <= cores re-arms.
    """
    count = 0
    if isinstance(prev, dict):
        raw = prev.get("count")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            count = raw
    if load5 is None or not load5 > cores:
        return {"count": 0}, False
    count += 1
    return {"count": count}, count == n


def alarm_line(load5: float, cores: int, ticks: int, attributed: list[dict]) -> str:
    """One `load-alarm:` line naming the top processes with their session@repo."""
    tops = ", ".join(
        f"{p.get('comm', '?')}({p.get('pcpu', 0)}%) "
        f"{p.get('session', '?')}@{p.get('repo', '?')}"
        if p.get("session", "?") != "?" or p.get("repo", "?") != "?"
        else f"{p.get('comm', '?')}({p.get('pcpu', 0)}%) {_UNATTRIBUTED}"
        for p in attributed
    )
    return f"load-alarm: load5={load5:.2f} cores={cores} for {ticks} ticks; top: {tops}"


def top_attributed(
    n: int = 8,
    run: Callable = subprocess.run,
    sessions: Optional[dict] = None,
) -> list[dict]:
    """Top processes with session/repo attribution from one `ps` spawn; [] on failure."""
    try:
        rows = _ps_rows(run)
        if not rows:
            return []
        ppid_map = {p["pid"]: p["ppid"] for p in rows}
        if sessions is None:
            sessions = read_sessions()
        return attribute(rows[: max(n, 0)], ppid_map, sessions)
    except Exception:
        return []
