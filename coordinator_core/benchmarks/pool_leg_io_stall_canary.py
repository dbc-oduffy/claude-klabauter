"""Live canary for a sha: an isolated warm pair built from `git archive <sha>`, loaded with guard round trips.

    python -m coordinator_core.benchmarks.pool_leg_io_stall_canary <sha|worktree> <work-dir> [secs] [threads]
        [--think-ms N] [--io-reader] [--reader-threads N] [--reader-mb N] [--reader-mb-per-sec N]
        [--per-process] [--spawn-per-sec N]

Builds `<work-dir>/dest` from the sha (stamped `sha:<sha>`; `worktree` copies the live tree's
non-test `coordinator_core` files, dirt included), runs it under its own runtime base and settings
home, boots the pair and confirms `pair_up`, and only then starts the load: for `secs`, `threads`
concurrent PreToolUse Bash guard round trips plus one `dispatch.emit` through the real pool worker
processes. `--io-reader` starts `warm/tests/io_stall_reader.py` over `<work-dir>/io` after pair_up,
capped at `--reader-mb-per-sec` (default 450, the live a3 bench rate), so the warm pair serves under
a bounded IO load rather than booting under it. Prints `guard_bad` (guard replies that were not a
result), `bad_codes`, `stall_dumps` and `driver_process_ms` (CPU of the driving process; wall clock
carries no verdict). `<work-dir>` is wiped first and must sit under the repo `scratch/` dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import threading  # noqa: E402

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WORKTREE = "worktree"
PAIR_UP_SECS = 90
DEFAULT_READER_MB_PER_SEC = 450.0
DEFAULT_SPAWN_PER_SEC = 5.0

DRIVER = r"""
import concurrent.futures, json, os, subprocess, sys, threading, time
from coordinator_core.warm import client
dest, secs, threads, think = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]) / 1000.0
per_process, spawn_gap = sys.argv[5] == "1", 1.0 / float(sys.argv[6])
pair_up_secs = float(sys.argv[7])
ONESHOT = (
    "import json,sys,time\nfrom coordinator_core.warm import client\nm=json.loads(sys.argv[1])\nt=time.monotonic()\n"
    "try:\n r=client.try_warm_dispatch(m,read_deadline_secs=float(sys.argv[2]),hook_path=True)\n"
    "except Exception as e:\n r={'error':{'code':0,'message':repr(e)}}\n"
    "print(json.dumps({'r':r,'ms':(time.monotonic()-t)*1000}))"
)
spawn_lock = threading.Lock(); spawn_next = [time.monotonic()]
guard = {"jsonrpc": "2.0", "id": 1, "method": "hooks.preuse_bash_dispatch", "params": {
    "hook_event_name": "PreToolUse", "session_id": "canary", "cwd": dest, "tool_name": "Bash",
    "tool_input": {"command": "git status"}}}
emit = {"jsonrpc": "2.0", "id": 2, "method": "dispatch.emit", "params": {
    "ask": "canary fixture ask", "target_root": dest, "session_id": "canary"}}

def call_inproc(msg, deadline):
    t = time.monotonic()
    try:
        r = client.try_warm_dispatch(msg, read_deadline_secs=deadline, hook_path=True)
    except Exception as exc:
        r = {"error": {"code": 0, "message": repr(exc)}}
    return r, (time.monotonic() - t) * 1000

def call_fresh_process(msg, deadline):
    with spawn_lock:
        slot = max(spawn_next[0], time.monotonic())
        spawn_next[0] = slot + spawn_gap
    time.sleep(max(slot - time.monotonic(), 0.0))
    try:
        p = subprocess.run([sys.executable, "-c", ONESHOT, json.dumps(msg), str(deadline)], capture_output=True, text=True,
            timeout=deadline + 30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = json.loads(p.stdout.strip().splitlines()[-1])
        return out["r"], out["ms"]
    except Exception as exc:
        return {"error": {"code": 0, "message": "oneshot " + repr(exc)}}, 0.0

def call(msg, deadline):
    return (call_fresh_process if per_process else call_inproc)(msg, deadline)

up = time.monotonic() + pair_up_secs
while time.monotonic() < up:
    r, _ = call_inproc(guard, 8.0)
    if isinstance(r, dict) and "result" in r:
        break
    time.sleep(1)
else:
    print(json.dumps({"pair_up": False, "last": r}), flush=True); sys.exit(2)

print(json.dumps({"pair_up": True}), flush=True)
sys.stdin.readline()

cpu0 = time.process_time()
t0 = time.monotonic(); stop = t0 + secs
def loop(_i):
    ok = bad = 0; worst = 0.0; samples = []; codes = {}
    while time.monotonic() < stop:
        r, ms = call(guard, 8.0)
        worst = max(worst, ms)
        if isinstance(r, dict) and "result" in r:
            ok += 1
        else:
            bad += 1
            err = r.get("error") if isinstance(r, dict) else None
            code = str(err.get("code")) if isinstance(err, dict) else "none"
            codes[code] = codes.get(code, 0) + 1
            if len(samples) < 3: samples.append(f"{ms:.0f}ms " + str(r)[:300])
        if think: time.sleep(think)
    return ok, bad, worst, samples, codes

with concurrent.futures.ThreadPoolExecutor(threads + 1) as ex:
    futs = [ex.submit(loop, i) for i in range(threads)]
    er, ems = call(emit, 60.0)
    res = [f.result() for f in futs]
codes = {}
for r in res:
    for k, v in r[4].items():
        codes[k] = codes.get(k, 0) + v
print(json.dumps({"pair_up": True, "mode": "per-process" if per_process else "shared-client", "guard_ok": sum(r[0] for r in res), "guard_bad": sum(r[1] for r in res),
    "guard_worst_ms": round(max(r[2] for r in res)), "bad_codes": codes,
    "bad_samples": [s for r in res for s in r[3]][:5],
    "emit_ok": isinstance(er, dict) and "result" in er, "emit_ms": round(ems), "emit_reply": str(er)[:300],
    "driver_process_ms": round((time.process_time() - cpu0) * 1000)}))
"""


def _git_out(args: list, **kwargs) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True, creationflags=NO_WINDOW, **kwargs).stdout


def _copy_worktree(dest: Path) -> str:
    names = _git_out(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "coordinator_core"]).decode("utf-8").split("\0")
    for name in names:
        if not name or "/tests/" in name or "/benchmarks/" in name:
            continue
        source = REPO / name
        if source.is_file():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return _git_out(["rev-parse", "HEAD"]).decode().strip() + "-worktree"


_WORK_MARKER = ".canary-work-dir"


def _force_remove(func, path, _exc) -> None:
    """`rmtree` hook: the pack and object files of the previous build's `.git` are read-only on Windows."""
    os.chmod(path, 0o700)
    func(path)


def _rmtree(path: Path) -> None:
    """`rmtree` through the hook keyword the running Python spells (`onexc` from 3.12, `onerror` before)."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def _build(sha: str, work: Path) -> Path:
    """Wipe `work` and build `work/dest` as a committed tree of `sha` (or of the live worktree).

    Refuses a non-empty `work` that a previous build did not mark: the wipe is only for canary output."""
    if work.exists():
        if any(work.iterdir()) and not (work / _WORK_MARKER).exists():
            raise ValueError(f"refusing to wipe {work}: not a canary work dir (no {_WORK_MARKER})")
        _rmtree(work)
    dest = work / "dest"
    dest.mkdir(parents=True)
    (work / _WORK_MARKER).write_text("canary work dir", encoding="utf-8")
    if sha == WORKTREE:
        stamp = _copy_worktree(dest)
    else:
        tar = _git_out(["archive", "--format=tar", sha, "coordinator_core"])
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            archive.extractall(dest, filter="data")
        stamp = sha
    (dest / "coordinator_core" / "_engine_stamp").write_text(f"sha:{stamp}\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "canary"]):
        # core.longpaths: a deep `tmp_path` plus a vendored corpus path exceeds MAX_PATH under a quarantined HOME.
        subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(dest), *args], check=True, capture_output=True, creationflags=NO_WINDOW)
    (work / "settings").mkdir()
    return dest


def run(
    sha: str,
    work: Path,
    secs: float,
    threads: int,
    think_ms: float = 0.0,
    io_reader: bool = False,
    reader_threads: int = 4,
    reader_mb: int = 1024,
    reader_mb_per_sec: float = DEFAULT_READER_MB_PER_SEC,
    per_process: bool = False,
    spawn_per_sec: float = DEFAULT_SPAWN_PER_SEC,
) -> dict:
    """Drive one sha; `per_process` runs each guard call in a fresh client process, paced to `spawn_per_sec` overall. Returns the result dict (`pair_up`, `guard_bad`, `bad_codes`, `stall_dumps`, `passed`, ...)."""
    dest = _build(sha, work)
    env = dict(os.environ)
    env.update(
        COORDINATOR_WARM_RUNTIME_BASE=str(work / "rt"),
        PYTHONPATH=str(dest),
        COORDINATOR_CLONE_ROOT=str(dest),
        COORDINATOR_SETTINGS_HOME=str(work / "settings"),
        COORDINATOR_WARM="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", DRIVER, str(dest), str(secs), str(threads), str(think_ms), "1" if per_process else "0", str(spawn_per_sec), str(PAIR_UP_SECS)],
        cwd=str(dest), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    watchdog = threading.Timer(PAIR_UP_SECS + 60, proc.kill)
    watchdog.start()
    try:
        head = proc.stdout.readline().strip()
    finally:
        watchdog.cancel()
    pair_up = bool(head) and json.loads(head).get("pair_up") is True
    reader = contextlib.nullcontext(None)
    if pair_up and io_reader:
        from coordinator_core.warm.tests.io_stall_reader import io_stall_reader

        reader = io_stall_reader(work / "io", threads=reader_threads, size_mb=reader_mb, seconds=secs + 30.0, mb_per_sec=reader_mb_per_sec)
    reader_report: dict = {}
    with reader as stats:
        try:
            tail, stderr = proc.communicate("go\n", timeout=secs + 300)
        except subprocess.TimeoutExpired:
            proc.kill()
            tail, stderr = proc.communicate()
        if stats is not None:
            reader_report = {"reader_mode": stats.mode, "reader_mb_per_sec": round(stats.mb_per_sec()), "reader_cap_mb_per_sec": reader_mb_per_sec, "reader_loaded": stats.bytes_read > 0, "reader_errors": stats.errors[:3]}
    lines = (tail or "").strip().splitlines()
    result: dict = json.loads(lines[-1]) if lines else (json.loads(head) if head else {"driver_stderr": stderr[-1500:]})
    result.update(reader_report)
    dumps = [p for p in (work / "rt").rglob("*") if p.is_file() and p.parent.name == "stall-dumps" and not p.name.startswith(".")]
    result["stall_dumps"] = len(dumps)
    result["stall_dump_names"] = [p.name for p in dumps][:5]
    subprocess.run(
        [sys.executable, "-c", "import sys\nfrom pathlib import Path\nfrom coordinator_core.warm.pair_stop import stop_warm_pair\nstop_warm_pair(Path(sys.argv[1]))", str(dest)],
        cwd=str(dest), env=env, capture_output=True, timeout=120, creationflags=NO_WINDOW,
    )
    result["passed"] = bool(result.get("pair_up") and result.get("guard_bad") == 0 and result["stall_dumps"] == 0 and result.get("emit_ok"))
    result["stderr_tail"] = stderr[-1500:] if not result.get("pair_up") else ""
    return result


def main(argv: list | None = None) -> int:
    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="a commit-ish, or `worktree` for the live tree")
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("secs", nargs="?", type=float, default=120.0)
    parser.add_argument("threads", nargs="?", type=int, default=12)
    parser.add_argument("--think-ms", type=float, default=0.0)
    parser.add_argument("--io-reader", action="store_true")
    parser.add_argument("--reader-threads", type=int, default=4)
    parser.add_argument("--reader-mb", type=int, default=1024)
    parser.add_argument("--reader-mb-per-sec", type=float, default=DEFAULT_READER_MB_PER_SEC)
    parser.add_argument("--per-process", action="store_true", help="one fresh client process per guard call, as real hooks run")
    parser.add_argument("--spawn-per-sec", type=float, default=DEFAULT_SPAWN_PER_SEC, help="overall client spawn rate cap for --per-process")
    args = parser.parse_args(argv)
    if args.reader_mb_per_sec <= 0:
        parser.error("--reader-mb-per-sec must be > 0: the reader is never uncapped")
    if args.spawn_per_sec <= 0:
        parser.error("--spawn-per-sec must be > 0: per-process mode is spawn-rate bounded")
    work = args.work_dir.resolve()
    if (REPO / "scratch").resolve() not in work.parents:
        parser.error(f"work-dir must sit under {REPO / 'scratch'}")
    result = run(
        args.sha, work, args.secs, args.threads, args.think_ms, args.io_reader, args.reader_threads, args.reader_mb, args.reader_mb_per_sec,
        args.per_process, args.spawn_per_sec,
    )
    print(("CANARY PASS " if result["passed"] else "CANARY FAIL ") + args.sha[:10] + " " + json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
