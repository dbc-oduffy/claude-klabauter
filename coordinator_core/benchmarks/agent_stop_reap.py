"""Bench: hooks.agent_stop_reaper process time and spawns, plus the recorder half that feeds it.

Reaper half (Windows): three real launches by one agent, in a throwaway settings home seeded with
N unrelated launch records. An orphan tree (parent killed), a live-parent decoy and an orphan tree
whose kill is attempted at a ctime that no longer matches (a recycled pid). `hooks.agent_stop_reaper`
runs once on a SubagentStop payload; the line reports whether the orphan is gone, the decoy lives
and the recycled-pid process survives, with `process_ms` (time.process_time around the op call) and
`spawns` (subprocess.Popen constructions during that call; fixture spawns excluded). Never wall clock.

Recorder half (any sha that has hooks.taskstop_launch_recorder): the same measurement for one
timed-out non-search call, so the number can be taken at a base export that lacks the reaper.

Trap: attribution refuses a launch whose window overlaps another record's, so the three launches
are staggered past the window; the seeded records carry an old mark and mtime so they are not
competitors, and only the op's own work over a large store is timed.

Run: `python coordinator_core/benchmarks/agent_stop_reap.py [--records 900 5] [--recorder-only]`.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import fixture_settings_home

BAR_MS = 500.0
DEFAULT_RECORDS = (900, 5)
# Attribution windows overlap within PRE_SLACK_S + POST_WINDOW_S (2.25s); stay clear of it.
STAGGER_S = 2.6
_S = 10_000_000
_FILETIME_UNIX_EPOCH = 116444736000000000
_OLD_AGE_S = 86400
_SESSION, _AGENT = "bench-session", "bench-agent"
_CHILD_CODE = "import time; time.sleep(120)"
_PARENT_CODE = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(120)\n"
)
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SpawnCounter:
    """Counts `subprocess.Popen` constructions while active."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> "SpawnCounter":
        counter = self
        self._orig = subprocess.Popen.__init__

        def counting(popen_self, *a, **k):
            counter.n += 1
            return counter._orig(popen_self, *a, **k)

        subprocess.Popen.__init__ = counting  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def seed_unrelated(n: int) -> int:
    """Write n launch-record files of other sessions into the active settings home's store.

    Written straight to disk in the pre-change shape (no marker dir), with a day-old mark and
    mtime: stale enough to be no attribution competitor, young enough to survive the TTL prune."""
    from coordinator_core.bash_guards._taskstop_launch_store import _store_dir

    store_dir = _store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    when = time.time() - _OLD_AGE_S
    mark = int(when * _S) + _FILETIME_UNIX_EPOCH
    for i in range(n):
        path = store_dir / f"seed{i:05d}.json"
        path.write_text(
            json.dumps({
                "task_id": f"seed{i:05d}", "session_id": f"other-{i % 7}", "tool_use_id": "tu",
                "command": "pytest -q | grep passed", "mark": mark,
                "agent_id": f"other-agent-{i % 11}", "timed_out": True,
            }),
            encoding="utf-8",
        )
        os.utime(path, (when, when))
    return n


def measure_recorder(n_unrelated: int, reps: int = 20) -> Dict[str, object]:
    """Process time and spawns of one timed-out, non-search call through the recorder op, over a
    store holding n_unrelated records. Mean over reps (process_time ticks at ~15.6ms on Windows, so one
    call reads 0); `wrote` is False if any record is missing."""
    from coordinator_core.bash_guards import _taskstop_launch_store as store
    from coordinator_core.hooks import taskstop_launch_recorder as recorder

    total_ms = 0.0
    spawns = 0
    wrote = True
    for _ in range(reps):
        task_id = "benchrec" + secrets.token_hex(4)
        payload = {"payload": {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q | grep -E 'passed|failed'"},
            "tool_response": {"timedOutAfterMs": 120000, "backgroundTaskId": task_id},
            "session_id": _SESSION, "agent_id": _AGENT, "tool_use_id": "tu",
        }}
        with SpawnCounter() as sc:
            t0 = time.process_time()
            recorder._handler(payload)
            total_ms += (time.process_time() - t0) * 1000.0
        spawns = max(spawns, sc.n)
        wrote = wrote and store.read_record(task_id) is not None
    return {
        "records": n_unrelated, "process_ms": round(total_ms / reps, 3),
        "spawns": spawns, "wrote": wrote, "platform": sys.platform,
    }


def _record_command(cmdline: Optional[str], token: str) -> str:
    """The command a user would have typed for this child: image reduced to its stem, args re-quoted
    (attribution compares the live argv, image stem first, against the record's command stages)."""
    from coordinator_core.bash_guards._taskstop_attribution import _argv

    toks = _argv(cmdline) if cmdline else ["python", "-c", _CHILD_CODE, token]
    return " ".join(f'"{t}"' if any(c in t for c in " ;&|()") else t for t in toks)


class _Tree:
    """One launched parent -> child pair; `child` is the (pid, ctime) the record attributes to."""

    def __init__(self, name: str, parent_alive: bool) -> None:
        from coordinator_core.bash_guards import _host_probe as hp
        from coordinator_core.bash_guards import _process_kill as pk

        self.task_id = f"bench-{name}"
        token = f"agentstop.{secrets.randbelow(10**9):09d}"
        self.parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_CODE, _CHILD_CODE, token],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_NOWIN,
        )
        child_pid = int(self.parent.stdout.readline().strip())
        deadline = time.monotonic() + 15
        self.child: Optional[tuple] = None
        while time.monotonic() < deadline and self.child is None:
            for r in hp.snapshot() or []:
                if r.pid == child_pid:
                    self.child = (r.pid, r.ctime)
                    break
            else:
                time.sleep(0.05)
        if self.child is None:
            raise RuntimeError(f"child {child_pid} of {name} never appeared")
        self.command = _record_command(pk.command_line(*self.child), token)
        if not parent_alive:
            self.parent.kill()
            self.parent.wait(timeout=10)

    def alive(self) -> bool:
        from coordinator_core.bash_guards import _host_probe as hp

        return hp.alive(*self.child)

    def cleanup(self) -> None:
        from coordinator_core.bash_guards import _process_kill as pk

        if self.parent.poll() is None:
            self.parent.kill()
        self.parent.wait(timeout=10)
        self.parent.stdout.close()
        pk.terminate_verified(*self.child)


class _RecycledKill:
    """Real kill primitives, except a terminate for the given pids is attempted at a ctime one tick
    off: what a recycled pid looks like to the verified kill."""

    def __init__(self, real, recycled_pids: Sequence[int]) -> None:
        self._real = real
        self._recycled = set(recycled_pids)

    def terminate_verified(self, pid: int, ctime: int) -> bool:
        return self._real.terminate_verified(pid, ctime + 1 if pid in self._recycled else ctime)

    def command_line(self, pid: int, ctime: int):
        return self._real.command_line(pid, ctime)


def measure_reaper(n_unrelated: int, stagger_s: float = STAGGER_S) -> Dict[str, object]:
    """Fire hooks.agent_stop_reaper once over the default fixture; the settings home must be pinned."""
    from coordinator_core.bash_guards import _process_kill as pk
    from coordinator_core.bash_guards import _taskstop_launch_store as store
    from coordinator_core.bash_guards._taskstop_contract import LaunchRecord
    from coordinator_core.hooks import agent_stop_reaper as reaper

    seed_unrelated(n_unrelated)
    trees: List[_Tree] = []
    real_kill = reaper._kill
    try:
        orphan = _Tree("a-orphan", parent_alive=False)
        trees.append(orphan)
        time.sleep(stagger_s)
        live = _Tree("b-live", parent_alive=True)
        trees.append(live)
        time.sleep(stagger_s)
        recycled = _Tree("c-recycled", parent_alive=False)
        trees.append(recycled)
        for t in trees:
            store.write_record(LaunchRecord(
                t.task_id, _SESSION, "tu", t.command, t.child[1] + _S,
                agent_id=_AGENT, timed_out=True,
            ))
        reaper._kill = _RecycledKill(pk, [recycled.child[0]])
        payload = {"payload": {"session_id": _SESSION, "agent_id": _AGENT}}
        with SpawnCounter() as sc:
            t0 = time.process_time()
            reaper._handler(payload)
            process_ms = (time.process_time() - t0) * 1000.0
        log = store._log_path()
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []
        return {
            "records": n_unrelated,
            "orphan_alive": int(orphan.alive()),
            "live_alive": int(live.alive()),
            "recycled_killed": int(not recycled.alive()),
            "process_ms": round(process_ms, 3),
            "spawns": sc.n,
            "outcomes": ",".join(r["outcome"] for r in rows),
        }
    finally:
        reaper._kill = real_kill
        for t in trees:
            t.cleanup()


def verdict(row: Dict[str, object]) -> bool:
    """True when the reaper line meets the expected-when-true pins."""
    return (
        row.get("orphan_alive") == 0 and row.get("live_alive") == 1 and row.get("recycled_killed") == 0
        and float(row["process_ms"]) <= BAR_MS and row.get("spawns") == 0
    )


def format_reaper(row: Dict[str, object]) -> str:
    return (
        f"agent_stop_reap records={row['records']} orphan_alive={row['orphan_alive']} "
        f"live_alive={row['live_alive']} recycled_killed={row['recycled_killed']} "
        f"process_ms={row['process_ms']} spawns={row['spawns']} outcomes={row['outcomes']}"
    )


def format_recorder(row: Dict[str, object]) -> str:
    return (
        f"recorder records={row['records']} process_ms={row['process_ms']} spawns={row['spawns']} "
        f"wrote={int(bool(row['wrote']))} platform={row['platform']}"
    )


def run(records: Sequence[int], recorder_only: bool, stagger_s: float, scratch: Optional[Path] = None) -> int:
    """Print one recorder line and (unless recorder_only, Windows only) one reaper line per size."""
    base = scratch or Path(__file__).resolve().parents[2] / "scratch"
    base.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    for n in records:
        root = Path(tempfile.mkdtemp(prefix="agent-stop-reap-", dir=str(base)))
        try:
            with fixture_settings_home(root):
                seed_unrelated(n)
                print(format_recorder(measure_recorder(n)))
            if recorder_only:
                continue
            if sys.platform != "win32":
                print(f"agent_stop_reap records={n} skipped=not-win32")
                continue
            with fixture_settings_home(root / "reaper"):
                row = measure_reaper(n, stagger_s)
            print(format_reaper(row))
            if n == records[0] and not verdict(row):
                exit_code = 1
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(prog="agent_stop_reap")
    ap.add_argument("--records", type=int, nargs="+", default=list(DEFAULT_RECORDS))
    ap.add_argument("--recorder-only", action="store_true")
    ap.add_argument("--stagger-s", type=float, default=STAGGER_S)
    args = ap.parse_args(argv)
    return run(args.records, args.recorder_only, args.stagger_s)


if __name__ == "__main__":
    sys.exit(main())
