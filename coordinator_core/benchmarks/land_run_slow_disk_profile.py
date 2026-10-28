"""Slow-disk falsifier for `dispatch.land_run` and the warm status surface.

Reports, per axis and per condition (`baseline`, `slow-disk`), this process's `time.process_time()`
plus descendants (a fresh `commit_leg_profile._ChildMeter` per bracket), the `subprocess.Popen`
count, and `injected_wait_ms`: the virtual disk wait the op's own call path took. Wall clock is never
recorded. A slow disk costs an op waiting, not CPU, so process time alone cannot see a counter that
blocks on it; `injected_wait_ms` is the deterministic wait the injection seam charged, and an axis
passes only when BOTH it and the process time are under `BAR_MS`.

Axes:
  land_run.own_checkpoint     `dispatch.land_run` where the only since-base change is the run's own
                              checkpoint (same Checkpoint-Plan, Checkpoint-Not-Landed, an off-chain
                              stamped base)
  land_run.refusal            `dispatch.land_run` where a foreign commit's line is dropped
  land_run.landing            `dispatch.land_run` of a clean landing that commits
  warm.status.request_status  the `warm.request_status` poll handler (the only registered status op)
  warm.status.counter.<w>     each of the nine best-effort `warm.telemetry` writers, called as its
                              hot path calls it
  warm.status.read            `telemetry.warm_rate()`, the reader behind the orientation warm line

There is no `warm.status` op; "the warm status surface" is the last three rows together.

Slow-disk injection is in-process and portable: `locked_write.held_lock` charges the delay before
delegating (a delay past the caller's `timeout` charges the timeout and raises `LockTimeout`, as a
held lock would), and an append-mode `Path.open` of a `.jsonl` charges the delay once. Nothing sleeps.

Fixture scale: `--tracked` files in the seed tree and `--chain` foreign commits between the run's
checkpoint and HEAD; both are what the since-base walk and the dirty scan read.

    python coordinator_core/benchmarks/land_run_slow_disk_profile.py [--delay-s S] [--tracked N]
        [--chain N] [--json] [--attribute]
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core import locked_write  # noqa: E402
from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402

BAR_MS = 500.0
DEFAULT_DELAY_S = 12.0
DEFAULT_OP_LATENCY_MS = 50.0
DEFAULT_TRACKED = 2000
DEFAULT_CHAIN = 300
SELF = "5e1f0000-0000-4000-8000-0000000000c0"
RUN_ID = "plan-20261028T100000-c0c0c0"
SCRIPT = "run-c0.mjs"
PLAN = "docs/plans/own-plan.md"
PATH_P = "src/p.py"
OFF_CHAIN_BASE = "e51c5e50cc" + "0" * 30
OWN_LINES = "".join(f"v{i} = {i}\n" for i in range(6))
EPOCH = 1_700_000_000


@dataclass
class Row:
    """One measured axis under one condition."""

    axis: str
    condition: str
    process_time_ms: float = 0.0
    spawns: int = 0
    injected_wait_ms: float = 0.0
    sync_points: dict = field(default_factory=dict)
    outcome: str = ""
    spawn_sites: dict = field(default_factory=dict)
    sync_sites: dict = field(default_factory=dict)
    top_functions: list = field(default_factory=list)

    @property
    def under_bar(self) -> bool:
        return self.process_time_ms < BAR_MS and self.injected_wait_ms < BAR_MS


class Injector:
    """Virtual slow disk; accumulates the wait the op's own call path took and sleeps for none of it.

    Two faults, both charged on the calling path: a peer that holds every advisory lock for
    `delay_s` (a lock acquire charges `min(delay_s, timeout)` and raises `LockTimeout` when the
    caller's `timeout` is shorter), and `op_latency_s` per file operation (a read- or write-mode
    open, `os.fsync`, `os.replace`/`os.rename`). Operations are counted by kind either way; stat,
    scandir and mmap page faults are not seen, so a count is a floor.
    """

    def __init__(self, delay_s: float, op_latency_s: float, attribute: bool = False) -> None:
        self.delay_s = delay_s
        self.op_latency_s = op_latency_s
        self.attribute = attribute
        self.lock_wait_s = 0.0
        self.counts: dict = {}
        self.sites: dict = {}

    @property
    def waited_s(self) -> float:
        return self.lock_wait_s + self.op_latency_s * sum(
            n for kind, n in self.counts.items() if kind != "lock"
        )

    def _count(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.attribute:
            key = f"{kind} @ {_site_of(traceback.extract_stack(limit=40))}"
            self.sites[key] = self.sites.get(key, 0) + 1

    @contextmanager
    def active(self):
        import builtins
        import io

        real = (locked_write._acquire_flock, builtins.open, io.open, os.fsync, os.replace, os.rename)
        real_acquire, real_open, _io_open, real_fsync, real_replace, real_rename = real
        me = self

        def acquire(fd, timeout):
            me._count("lock")
            if me.delay_s > timeout:
                me.lock_wait_s += timeout
                raise locked_write.LockTimeout(f"injected: lock held {me.delay_s}s, caller waited {timeout}s")
            me.lock_wait_s += me.delay_s
            return real_acquire(fd, timeout)

        def opened(file, mode="r", *a, **k):
            writing = isinstance(mode, str) and any(c in mode for c in "wax+")
            me._count("open-write" if writing else "open-read")
            return real_open(file, mode, *a, **k)

        def fsync(fd):
            me._count("fsync")
            return real_fsync(fd)

        def replace(src, dst, *a, **k):
            me._count("replace")
            return real_replace(src, dst, *a, **k)

        def rename(src, dst, *a, **k):
            me._count("replace")
            return real_rename(src, dst, *a, **k)

        locked_write._acquire_flock = acquire
        builtins.open = io.open = opened
        os.fsync, os.replace, os.rename = fsync, replace, rename
        try:
            yield
        finally:
            locked_write._acquire_flock = real_acquire
            builtins.open, io.open = real_open, _io_open
            os.fsync, os.replace, os.rename = real_fsync, real_replace, real_rename


def _git(args, cwd: Path, stdin: bytes | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, input=stdin, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("utf-8", "replace")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _bind_session() -> None:
    os.environ["COORDINATOR_SESSION_ID"] = SELF
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def _stream(tracked: int, chain: int, own: bool) -> bytes:
    """A `git fast-import` stream: seed (tracked tree + P), the checkpoint or foreign commit that
    adds OWN_LINES to P, then `chain` foreign commits each touching one other file."""
    out: list = []
    clock = [EPOCH]

    def commit(message: str, files: dict) -> None:
        clock[0] += 60
        body = message.encode()
        out.append(f"commit refs/heads/main\ncommitter t <t@example.com> {clock[0]} +0000\ndata {len(body)}\n".encode())
        out.append(body + b"\n")
        for path, data in files.items():
            out.append(f"M 644 inline {path}\ndata {len(data)}\n".encode() + data + b"\n")

    seed = {"README.md": b"seed\n", PLAN: b"# plan\n", PATH_P: b"head = 0\n"}
    for i in range(tracked):
        seed[f"tracked/d{i % 50:02d}/s{i % 7}/f{i:06d}.txt"] = f"{i}\n".encode()
    commit("seed", seed)
    message = (
        f"checkpoint(wave 0): 1 row - C1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {OFF_CHAIN_BASE}\n"
        f"Checkpoint-Not-Landed: run-open\nSession-Id: {SELF}\n" if own else "peer commit\n"
    )
    commit(message, {PATH_P: b"head = 0\n" + OWN_LINES.encode()})
    for i in range(chain):
        commit(f"peer {i}", {f"peer/f{i % 40:02d}.txt": f"{i}\n".encode()})
    return b"".join(out)


LAND_AXES = {
    "own": "land_run.own_checkpoint",
    "foreign": "land_run.refusal",
    "clean": "land_run.landing",
}


def build_land_fixture(parent: Path, *, mode: str, tracked: int, chain: int) -> tuple:
    """A repo whose run base (the seed) is followed by one commit adding OWN_LINES to PATH_P, `chain`
    foreign commits, then a worktree edit of PATH_P. `own` stamps the first commit as the run's own
    checkpoint (plan, Not-Landed, an off-chain base) and the worktree drops its lines; `foreign`
    leaves it unstamped and drops them; `clean` stamps it and keeps its lines (a landing that
    commits). Returns (root, base)."""
    own = mode in ("own", "clean")
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    root = parent / mode
    root.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        _git(["config", key, value], root)
    _git(["fast-import", "--quiet"], root, stdin=_stream(tracked, chain, own))
    _git(["checkout", "-q", "-f", "main"], root)
    base = _git(["rev-parse", f"main~{chain + 1}"], root).strip()

    session_core.init(SELF, cwd=str(root))
    _write(root, PATH_P, "head = 0\n" + OWN_LINES + "tail = 1\n" if mode == "clean" else "head = 1\n")
    session_scope.touch(SELF, PATH_P, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=PATH_P,
        content_hash=touch_record.compute_content_hash(root / PATH_P), kind="w",
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN_ID}/run.json", json.dumps({"run_id": RUN_ID}))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(PATH_P,)),), plan_path=PLAN)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return root, base


def _call_land(root: Path, base: str) -> dict:
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    land_run._launch_close_job = lambda *a, **k: True
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF, "run_id": RUN_ID, "run_base_sha": base},
        repo_root=root / ".git",
    )


def _site_of(stack) -> str:
    """The innermost coordinator_core frame that is not the spawn plumbing."""
    for frame in reversed(stack):
        name = frame.filename.replace("\\", "/")
        if "coordinator_core/" in name and not name.endswith(("git/run.py", "benchmarks/land_run_slow_disk_profile.py")):
            return f"{name.split('coordinator_core/', 1)[1]}:{frame.name}"
    return "?"


@contextmanager
def _null():
    yield


def measure(axis: str, condition: str, call, injector: Injector | None, *, attribute: bool) -> Row:
    """Run `call()` once inside one process-time + descendants bracket; spawns are Popen constructions."""
    row = Row(axis=axis, condition=condition)
    real_init = subprocess.Popen.__init__
    sites: dict = {}

    def counting(self, *a, **k):
        row.spawns += 1
        if attribute:
            argv = a[0] if a else k.get("args")
            sub = ""
            if isinstance(argv, (list, tuple)) and len(argv) > 1 and str(argv[0]).lower().startswith("git"):
                sub = " git " + str(argv[1])
            key = _site_of(traceback.extract_stack(limit=40)) + sub
            sites[key] = sites.get(key, 0) + 1
        return real_init(self, *a, **k)

    child = _ChildMeter()
    prof = cProfile.Profile(time.process_time) if attribute else None
    subprocess.Popen.__init__ = counting
    child.start()
    cpu0 = time.process_time()
    result = None
    try:
        with (injector.active() if injector else _null()):
            if prof:
                prof.enable()
            try:
                result = call()
            finally:
                if prof:
                    prof.disable()
    finally:
        row.process_time_ms = round((time.process_time() - cpu0) * 1000.0 + child.stop_ms(), 3)
        subprocess.Popen.__init__ = real_init
    if injector:
        row.injected_wait_ms = round(injector.waited_s * 1000.0, 3)
        row.sync_points = dict(injector.counts)
        row.sync_sites = dict(sorted(injector.sites.items(), key=lambda kv: -kv[1]))
    row.spawn_sites = dict(sorted(sites.items(), key=lambda kv: -kv[1]))
    row.outcome = _outcome(result)
    if prof:
        rows = []
        for (fname, _line, name), (_cc, nc, tt, ct, _c) in pstats.Stats(prof).stats.items():
            f = fname.replace("\\", "/")
            if "coordinator_core/" in f and "benchmarks/" not in f:
                rows.append((ct, f"{f.split('coordinator_core/', 1)[1]}:{name}", nc, tt))
        rows.sort(reverse=True)
        row.top_functions = [
            {"cum_ms": round(ct * 1000, 1), "fn": fn, "calls": nc, "self_ms": round(tt * 1000, 1)}
            for ct, fn, nc, tt in rows[:25]
        ]
    return row


def _outcome(result) -> str:
    if isinstance(result, dict):
        if result.get("committed") is True:
            return "committed"
        if result.get("refused"):
            return f"refused:{result['refused']}"
        return "returned:" + ",".join(sorted(result)[:6])
    return "ok" if result is None else str(result)[:60]


def _status_request_status():
    from coordinator_core.ops import warm_request_status

    return lambda: warm_request_status._warm_request_status({"key": "k"}, None)


def _counter_writers(engine_root: Path) -> dict:
    """Every best-effort `warm.telemetry` writer, called with the arguments its hot path passes."""
    from coordinator_core.warm import telemetry as t

    kw = {"engine_root": engine_root}

    def flush():
        tel = t.ServerTelemetry()
        tel.record_invocation(warm=True)
        tel.flush(**kw)

    return {
        "client_cold": lambda: t.record_client_cold_fallback(op="x", pid=1, reason="r", **kw),
        "server_boot": lambda: t.record_server_boot(listener_secs=0.1, ready_secs=0.2, pid=1, **kw),
        "election_lost": lambda: t.record_election_lost(endpoint="e", pid=1, **kw),
        "worker_pool_depth": lambda: t.record_worker_pool_depth(depth=1, pid=1, **kw),
        "push_sweep": lambda: t.record_sweep([], [], [], **kw),
        "boot_wait": lambda: t.record_client_boot_wait(waited_secs=0.1, served=True, deadline_secs=1.0, **kw),
        "degrade": lambda: t.record_degrade(kind=t.KIND_COLD_RUN, cause="c", **kw),
        "publish_warm": lambda: t.record_publish_warm_attempt(stamped=True, **kw),
        "flush": flush,
    }


def _status_read(engine_root: Path):
    from coordinator_core.warm import telemetry

    return lambda: telemetry.warm_rate(engine_root)


def run_axes(
    workdir: Path, delay_s: float, op_latency_s: float, tracked: int, chain: int, attribute: bool
) -> list:
    declare_benchmark_origin()
    _bind_session()
    rows: list = []

    def fresh(condition: str) -> Injector:
        """Baseline still counts sync points; only the slow-disk condition charges them."""
        slow = condition == "slow-disk"
        return Injector(delay_s if slow else 0.0, op_latency_s if slow else 0.0, attribute)

    for mode, axis in LAND_AXES.items():
        for condition in ("baseline", "slow-disk"):
            root, base = build_land_fixture(workdir / condition, mode=mode, tracked=tracked, chain=chain)
            rows.append(measure(axis, condition, lambda: _call_land(root, base), fresh(condition), attribute=attribute))
    engine_root = workdir / "engine"
    engine_root.mkdir(parents=True, exist_ok=True)
    for condition in ("baseline", "slow-disk"):
        rows.append(measure("warm.status.request_status", condition, _status_request_status(), fresh(condition), attribute=attribute))
        for name, fn in _counter_writers(engine_root).items():
            rows.append(measure(f"warm.status.counter.{name}", condition, fn, fresh(condition), attribute=attribute))
        rows.append(measure("warm.status.read", condition, _status_read(engine_root), fresh(condition), attribute=attribute))
    return rows


def render(rows: list) -> str:
    out = [f"{'axis':38} {'condition':10} {'proc_ms':>9} {'spawns':>6} {'wait_ms':>9} {'bar':>5}  sync-points  outcome"]
    for r in rows:
        points = ",".join(f"{k}={v}" for k, v in sorted(r.sync_points.items())) or "-"
        out.append(
            f"{r.axis:38} {r.condition:10} {r.process_time_ms:9.1f} {r.spawns:6d} "
            f"{r.injected_wait_ms:9.1f} {'ok' if r.under_bar else 'OVER':>5}  {points}  {r.outcome}"
        )
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--delay-s", type=float, default=DEFAULT_DELAY_S, help="seconds a peer holds every lock")
    ap.add_argument("--op-latency-ms", type=float, default=DEFAULT_OP_LATENCY_MS, help="per file open, fsync, replace")
    ap.add_argument("--tracked", type=int, default=DEFAULT_TRACKED)
    ap.add_argument("--chain", type=int, default=DEFAULT_CHAIN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--attribute", action="store_true", help="per-leg cProfile and spawn call sites")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="lrsd-", dir=args.dir))
    try:
        rows = run_axes(
            workdir, args.delay_s, args.op_latency_ms / 1000.0, args.tracked, args.chain, args.attribute
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=1))
    else:
        print(render(rows))
        if args.attribute:
            for r in rows:
                if r.axis.startswith("land_run") and r.condition == "baseline":
                    print(f"\n== {r.axis} [{r.condition}] spawn sites: {json.dumps(r.spawn_sites)}")
                    for site, n in list(r.sync_sites.items())[:25]:
                        print(f"   sync {n:4d}x {site}")
                    for f in r.top_functions[:20]:
                        print(f"   {f['cum_ms']:8.1f} cum {f['self_ms']:8.1f} self {f['calls']:6d}x {f['fn']}")
    return 0 if all(r.under_bar for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
