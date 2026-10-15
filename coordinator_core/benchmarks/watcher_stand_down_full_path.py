"""Driver for the watcher stand-down op, measured as a process tree.

Each invocation builds, under a caller-given directory, a settings home holding N TaskStop launch
records for the closing session and N for a peer, a Group EM nomination held by the closing
session, and a session-registry directory with both sessions, then calls the op's handler
in-process against the real process table. `--setup-only` does everything except that call, so
the cost of the call is `full - setup-only`.

The fixture spawns one real orphaned child (a sleeping script named group-em-watch.py under the
given directory) and writes the closing session's launch record for it, so the measured call
attributes, kills and re-verifies it. The kill seam terminates only processes whose command line
names that script and refuses everything else, so no peer's watcher is ever touched. Launch
records are spaced beyond the attribution window so none is ambiguous.

    python -m coordinator_core.benchmarks.watcher_stand_down_full_path <dir> [--setup-only]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin

SESSION_ID = "bench-closing-session"
PEER_SESSION_ID = "bench-peer-session"
LAUNCHES_PER_SESSION = 4
_COMMANDS = (
    "python coordinator/bin/group-em-watch.py --until-event",
    "python coordinator/bin/group-em-memory-governor.py",
    "python coordinator/bin/group-em-watch-until-event.py",
    "python -c pass",
)
_SPACING_TICKS = 100_000_000  # 10s, beyond the attribution window
_CHILD_SCRIPT = "group-em-watch.py"
_CHILD_BODY = "import time\ntime.sleep(120)\n"
_SPAWN_ORPHAN = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, sys.argv[1]], stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=int(sys.argv[2]))\n"
    "print(p.pid)\n"
)
_PEER_PID = 4194301  # above any live pid on a default box; never signalled
_FILETIME_UNIX_EPOCH = 116444736000000000


def _filetime_now() -> int:
    return int(time.time() * 1e7) + _FILETIME_UNIX_EPOCH


def _child_procs(script: Path) -> list:
    """Live (pid, ctime) rows whose command line names the fixture script."""
    from coordinator_core.bash_guards import _host_probe as hp
    from coordinator_core.bash_guards import _process_kill

    return [
        r for r in (hp.snapshot() or ())
        if r.pid != os.getpid() and str(script) in (_process_kill.command_line(r.pid, r.ctime) or "")
    ]


def _spawn_orphan(script: Path) -> int:
    """Start the sleeping watcher stand-in as an orphan (its launcher exits); returns its ctime."""
    script.write_text(_CHILD_BODY, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [sys.executable, "-c", _SPAWN_ORPHAN, str(script), str(flags)],
        capture_output=True, text=True, check=True, creationflags=flags,
    )
    pid = int(out.stdout.strip())
    for r in _child_procs(script):
        if r.pid == pid:
            return r.ctime
    raise RuntimeError("fixture child not found in the process table")


def build_fixture(workdir: Path) -> tuple[str, Path]:
    """Points the settings home and registry at `workdir`; returns (repo_root, registry_dir)."""
    settings = workdir / "settings"
    config = workdir / "claude-config"
    repo = workdir / "repo"
    for d in (settings, config / "sessions", repo):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["COORDINATOR_SETTINGS_HOME"] = str(settings)
    os.environ["CLAUDE_CONFIG_DIR"] = str(config)

    from coordinator_core.bash_guards import _taskstop_launch_store as store
    from coordinator_core.bash_guards._taskstop_contract import LaunchRecord
    from coordinator_core.group_em import nomination

    script = workdir / _CHILD_SCRIPT
    child_ctime = _spawn_orphan(script)
    spread = 0
    for sid in (SESSION_ID, PEER_SESSION_ID):
        for n in range(LAUNCHES_PER_SESSION):
            if sid == SESSION_ID and n == 0:
                command, mark = f"python {script}", child_ctime
            else:
                spread += 1
                command, mark = _COMMANDS[n % len(_COMMANDS)], child_ctime + spread * _SPACING_TICKS
            ok = store.write_record(LaunchRecord(
                task_id=f"{sid[6:10]}{n}", session_id=sid, tool_use_id=f"tu{n}",
                command=command, mark=mark,
            ), now_s=(mark - _FILETIME_UNIX_EPOCH) / 1e7)
            if not ok:
                raise RuntimeError("launch record refused")
    for pid, sid, name in ((os.getpid(), SESSION_ID, "closer"), (_PEER_PID, PEER_SESSION_ID, "peer")):
        (config / "sessions" / f"{sid}.json").write_text(
            json.dumps({"sessionId": sid, "name": name, "pid": pid, "cwd": str(repo), "status": "idle"}),
            encoding="utf-8",
        )
    record = nomination._build_record(str(repo), SESSION_ID, "closer", None)
    nomination._write_json_atomic(nomination._record_path(str(repo)), record)
    return str(repo), config / "sessions"


def _fixture_kill_only(script: Path) -> None:
    from coordinator_core.bash_guards import _process_kill
    from coordinator_core.workstream_complete import watcher_stand_down as impl

    class _NoKill:
        command_line = staticmethod(_process_kill.command_line)

        @staticmethod
        def terminate_verified(pid: int, ctime: int) -> bool:
            if str(script) not in (_process_kill.command_line(pid, ctime) or ""):
                return False
            return _process_kill.terminate_verified(pid, ctime)

    impl._kill = _NoKill


def _reap_child(script: Path) -> None:
    from coordinator_core.bash_guards import _process_kill

    for r in _child_procs(script):
        _process_kill.terminate_verified(r.pid, r.ctime)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: watcher_stand_down_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    workdir = Path(args[0]).resolve()
    repo, _ = build_fixture(workdir)
    script = workdir / _CHILD_SCRIPT
    from coordinator_core.ops import watcher_stand_down as op

    _fixture_kill_only(script)
    if "--setup-only" in argv:
        _reap_child(script)
        return 0
    out = op._stand_down_watchers({"session_id": SESSION_ID, "repo_root": repo})
    stand_down = out.get("stand_down") or {}
    from coordinator_core.bash_guards import _taskstop_launch_store as store

    peer_kept = sum(1 for r in store.all_records() if r.session_id == PEER_SESSION_ID)
    leftover = _child_procs(script)
    _reap_child(script)
    stopped_child = any(
        row["watcher_id"] == "group-em-watch" and row["killed"] for row in out.get("stopped", ())
    )
    complete = (
        "error" not in out and stand_down.get("ok") is True and stopped_child and not leftover
        and not out.get("survivors") and peer_kept == LAUNCHES_PER_SESSION
    )
    if not complete:
        print(f"stand-down did not complete: {out!r} peer_kept={peer_kept}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
