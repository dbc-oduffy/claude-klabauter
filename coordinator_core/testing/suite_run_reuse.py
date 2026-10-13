"""Reuse of a fresh suite run: find it, announce it, and the tee/record helpers a run needs.

``find_reusable`` reads only the ``current`` slot of ``testing.suite_runs`` (0 spawns when no
record exists, else the op's 2). ``reused_line``/``ran_line`` go to stderr: callers' stdout is
machine-read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.testing import suite_run_record
from coordinator_core.win_portability import no_console_creationflags

TEE_JOIN_SECS = 5.0
GIT_STATUS_TIMEOUT_SECS = 10.0
_REUSABLE_OUTCOMES = ("pass", "fail")


@dataclass(frozen=True)
class Reuse:
    collection: str
    head_sha: str
    exit_code: int
    outcome: str
    failed_ids: Optional[list]
    log_path: str


def command_key(argv: List[str]) -> List[str]:
    """argv with its executable resolved the way with-suite-mutex records it."""
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def _suite_runs(params: dict) -> dict:
    from coordinator_core.ops import suite_runs_op

    return suite_runs_op._suite_runs(params)


def find_reusable(repo_root, collection: str, command: List[str]) -> Optional[Reuse]:
    """The fresh, same-command, verdict-bearing ``current`` record, else None."""
    try:
        root = Path(repo_root)
        result = _suite_runs({"repo_root": str(root), "collection": collection})
        rec = ((result.get("collections") or {}).get(collection) or {}).get("current")
        if not rec or not (rec.get("freshness") or {}).get("fresh"):
            return None
        if rec.get("command") != command_key(command):
            return None
        if rec.get("outcome") not in _REUSABLE_OUTCOMES or rec.get("log_truncated"):
            return None
        log = suite_run_record.record_dir(root, collection) / str(rec.get("log") or "")
        if not rec.get("log") or not log.is_file():
            return None
        return Reuse(
            collection=collection, head_sha=str(rec.get("head_sha") or ""),
            exit_code=int(rec["exit_code"]), outcome=rec["outcome"],
            failed_ids=rec.get("failed_ids"), log_path=str(log),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def reused_line(r: Reuse) -> str:
    return f"suite-run: reused {r.collection}@{r.head_sha} outcome={r.outcome} log={r.log_path}"


def ran_line(collection: str, head_sha: Optional[str], outcome: str, log_path) -> str:
    return f"suite-run: ran {collection}@{head_sha} outcome={outcome} log={log_path}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_head_and_dirty(repo_root: str) -> Tuple[Optional[str], List[str]]:
    """``(head_sha, tracked paths modified)`` from one ``git status --porcelain=v2
    --branch --untracked-files=no`` spawn; ``(None, [])`` when git fails."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain=v2", "--branch",
             "--untracked-files=no"],
            capture_output=True, text=True, timeout=GIT_STATUS_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None, []
    if result.returncode != 0:
        return None, []
    head: Optional[str] = None
    dirty: List[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            head = oid if len(oid) == 40 else None
        elif line.startswith("1 "):
            dirty.append(line.split(" ", 8)[8])
        elif line.startswith("2 "):
            dirty.append(line.split(" ", 9)[9].split("\t")[0])
        elif line.startswith("u "):
            dirty.append(line.split(" ", 10)[10])
    return head, dirty


def begin_run(repo_root, collection: str) -> Tuple[Optional[str], List[str], Optional[str]]:
    """``(head_sha, dirty_paths, log_tmp_path)``; the log path is None when the record
    directory cannot be prepared."""
    head_sha, dirty_paths = _git_head_and_dirty(str(repo_root))
    try:
        rec_dir = suite_run_record.record_dir(repo_root, collection)
        rec_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(rec_dir / ("run-%d.log.tmp" % os.getpid()))
        open(log_path, "wb").close()
    except (OSError, ValueError) as exc:
        print(f"suite-run record disabled: {exc}", file=sys.stderr)
        return head_sha, dirty_paths, None
    return head_sha, dirty_paths, log_path


class Tee:
    """Forwards a child's stdout/stderr pipes to this process's streams, byte for
    byte and flushed per chunk, while appending every chunk to one log under a lock."""

    def __init__(self, process: "subprocess.Popen[bytes]", log_path: Optional[str]):
        self._lock = threading.Lock()
        self._log = open(log_path, "ab") if log_path else None
        self._threads = []
        for pipe, out in ((process.stdout, sys.stdout), (process.stderr, sys.stderr)):
            if pipe is None:
                continue
            thread = threading.Thread(target=self._pump, args=(pipe, out), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _pump(self, pipe, out) -> None:
        sink = getattr(out, "buffer", None)
        while True:
            chunk = pipe.read1(65536)
            if not chunk:
                return
            try:
                if sink is not None:
                    sink.write(chunk)
                    sink.flush()
                else:
                    out.write(chunk.decode("utf-8", errors="replace"))
                    out.flush()
            except (OSError, ValueError):
                pass
            with self._lock:
                if self._log is not None:
                    try:
                        self._log.write(chunk)
                        self._log.flush()
                    except (OSError, ValueError):
                        self._log = None  # keep draining so the child never blocks on a full pipe

    def finish(self) -> bool:
        """Join the pumps within ``TEE_JOIN_SECS``; True when all hit EOF. The
        log is closed under the lock, so a straggler cannot write into it after."""
        deadline = time.monotonic() + TEE_JOIN_SECS
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        complete = not any(t.is_alive() for t in self._threads)
        with self._lock:
            if self._log is not None:
                self._log.close()
                self._log = None
        return complete


def record_run(
    repo_root: str, collection: str, command: List[str], head_sha, dirty_paths,
    started_at: str, started_mono: float, exit_code: int, outcome: str,
    serialized: bool, log_path: Optional[str], log_truncated: bool,
) -> None:
    """Best-effort: a record failure never changes the wrapped command's exit code."""
    try:
        suite_run_record.write_run(
            suite_run_record.record_dir(repo_root, collection),
            {
                "repo_root": repo_root, "collection": collection, "command": command,
                "head_sha": head_sha, "started_at": started_at, "ended_at": _now_iso(),
                "dirty_paths": dirty_paths,
                "duration_s": round(time.monotonic() - started_mono, 3),
                "exit_code": exit_code, "outcome": outcome, "serialized": serialized,
                "log_truncated": log_truncated,
            },
            log_path,
        )
    except Exception as exc:  # noqa: BLE001 - record is secondary to the run
        print(f"suite-run record not written: {exc}", file=sys.stderr)
