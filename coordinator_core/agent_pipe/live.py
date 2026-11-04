"""agent_pipe.live -- map a live agy or codex TUI (by pid or cwd) to its conversation id.

One `ps` spawn plus one `lsof` spawn per call; no scan of conversation or rollout files.
agy: the TUI holds `.../brain/<conversation-id>` open (pid-exact). codex: the TUI holds no
thread file; the managed app-server daemon holds `thread-writer-locks/<id>.lock` and the
rollout, so pid -> TUI cwd -> threads whose rollout `payload.cwd` matches (not pid-exact when
several threads share a cwd; such rows carry `ambiguous: true`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_TIMEOUT_S = 5
_BRAIN = re.compile(r"/brain/([0-9A-Za-z][0-9A-Za-z-]{7,})(?:/|$)")
_LOCK = re.compile(r"/thread-writer-locks/([^/]+)\.lock$")
_ROLLOUT = re.compile(r"/rollout-[^/]*?-([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$")


def _unsupported(reason: str) -> dict[str, Any]:
    return {"sessions": [], "unsupported": reason}


def _classify(args: str) -> Optional[str]:
    """'agy', 'codex' (TUI) or 'daemon' (managed app-server) for a ps args string, else None."""
    parts = args.split()
    if not parts:
        return None
    name = os.path.basename(parts[0])
    if name == "agy":
        return "agy"
    if name == "codex":
        if "app-server" in parts:
            return "daemon" if "--managed-daemon" in parts else None
        return "codex"
    return None


def _parse_ps(text: str) -> list[tuple[int, str, str]]:
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        kind = _classify(fields[2])
        if kind is None:
            continue
        if kind != "daemon" and fields[1] in ("??", "?", "-"):
            continue
        rows.append((int(fields[0]), kind, fields[1]))
    return rows


def _parse_lsof(text: str) -> dict[int, dict[str, Any]]:
    """{pid: {"cwd": str | None, "names": [path, ...]}} from `lsof -Fpfn` output."""
    out: dict[int, dict[str, Any]] = {}
    pid: Optional[int] = None
    fd = ""
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p" and value.isdigit():
            pid = int(value)
            out[pid] = {"cwd": None, "names": []}
        elif tag == "f":
            fd = value
        elif tag == "n" and pid is not None:
            if fd == "cwd":
                out[pid]["cwd"] = value
            else:
                out[pid]["names"].append(value)
    return out


def _agy_conversation(names: list[str], home: Path) -> Optional[str]:
    ids: list[str] = []
    for name in names:
        m = _BRAIN.search(name)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    brain = home / ".gemini" / "antigravity-cli" / "brain"

    def mtime(conv: str) -> float:
        try:
            return (brain / conv).stat().st_mtime
        except OSError:
            return 0.0

    return max(ids, key=mtime)


def _codex_home(home: Path) -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else home / ".codex"


def _rollout_cwd(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
        payload = json.loads(first).get("payload")
    except (OSError, ValueError, AttributeError):
        return None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) else None


def _codex_threads(daemon_names: list[str], codex_home: Path) -> dict[str, Optional[str]]:
    """{thread_id: cwd} for live threads: daemon-held locks, else the lock directory listing."""
    threads: dict[str, Optional[str]] = {}
    rollouts: dict[str, Path] = {}
    for name in daemon_names:
        m = _LOCK.search(name)
        if m and m.group(1).startswith("."):
            continue
        if m:
            threads.setdefault(m.group(1), None)
            continue
        r = _ROLLOUT.search(name)
        if r:
            rollouts[r.group(1)] = Path(name)
    if not threads:
        lock_dir = codex_home / "thread-writer-locks"
        try:
            for entry in lock_dir.iterdir():
                if entry.suffix == ".lock" and not entry.name.startswith("."):
                    threads.setdefault(entry.stem, None)
        except OSError:
            pass
    for tid in threads:
        path = rollouts.get(tid)
        if path is None:
            matches = list((codex_home / "sessions").glob(f"*/*/*/rollout-*-{tid}.jsonl"))
            path = matches[0] if matches else None
        if path is not None:
            threads[tid] = _rollout_cwd(path)
    return threads


def find_live(
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    """Live agy/codex TUIs matching `pid` and/or `cwd` (both None -> all), as
    `{"sessions": [{agent, pid, cwd, conversation}], "unsupported"?: reason}`.
    Never raises on a missing/failed `ps` or `lsof`: returns `unsupported` instead."""
    if sys.platform == "win32":
        return _unsupported("no lsof on Windows")
    home = Path(home) if home is not None else Path.home()
    try:
        ps = run(["ps", "-axo", "pid=,tty=,args="], capture_output=True, text=True, timeout=_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported(f"ps unavailable: {exc}")
    if ps.returncode != 0:
        return _unsupported(f"ps exited {ps.returncode}")
    rows = _parse_ps(ps.stdout or "")
    wanted = [(p, k) for p, k, _ in rows if k != "daemon" and (pid is None or p == pid)]
    if not wanted:
        return {"sessions": []}
    daemon_pids = [p for p, k, _ in rows if k == "daemon"]
    probe = [p for p, _ in wanted]
    if any(k == "codex" for _, k in wanted):
        probe += daemon_pids
    try:
        lsof = run(
            ["lsof", "-p", ",".join(str(p) for p in probe), "-Fpfn"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported(f"lsof unavailable: {exc}")
    files = _parse_lsof(lsof.stdout or "")
    if not files and lsof.returncode not in (0, 1):
        return _unsupported(f"lsof exited {lsof.returncode}")

    sessions: list[dict[str, Any]] = []
    codex_tuis = [(p, files.get(p, {}).get("cwd")) for p, k in wanted if k == "codex"]
    if codex_tuis:
        daemon_names = [n for d in daemon_pids for n in files.get(d, {}).get("names", [])]
        threads = _codex_threads(daemon_names, _codex_home(home))
        for tpid, tcwd in codex_tuis:
            same = [t for t, c in threads.items() if tcwd is not None and c == tcwd]
            peers = sum(1 for _, c in codex_tuis if c == tcwd)
            for tid in same:
                row = {"agent": "codex", "pid": tpid, "cwd": tcwd, "conversation": tid}
                if peers != len(same):
                    row["ambiguous"] = True
                sessions.append(row)
    for apid, kind in wanted:
        if kind != "agy":
            continue
        info = files.get(apid, {"cwd": None, "names": []})
        conv = _agy_conversation(info["names"], home)
        if conv is not None:
            sessions.append({"agent": "antigravity", "pid": apid, "cwd": info["cwd"], "conversation": conv})
    if cwd is not None:
        sessions = [s for s in sessions if s["cwd"] == cwd]
    return {"sessions": sorted(sessions, key=lambda s: (s["pid"], s["conversation"]))}
