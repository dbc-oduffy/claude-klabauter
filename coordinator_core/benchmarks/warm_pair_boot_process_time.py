"""Warm-server boot cost in process time: how much CPU a freshly spawned server (and its pool
workers) spends before it answers, and before its pool is ready.

Spawns the real `coordinator_core/warm/server.py` from a tmp engine root through
`client.spawn_detached` (the production spawn seam), then samples CPU time of the server process
and its pool children with psutil. Process time is the verdict; wall time is printed only as
context because it measures the load the box happens to carry.

Milestones, in order:
  listener   the breadcrumb names the server (election won, endpoint connectable, imports done).
  accept     a `warm.request_status` poll is answered: the accept process serves, no pool involved.
             `accept_answer_process_ms` is the tree CPU here.
  first      a `ping` op is answered: the guard-class path, which builds the dispatch pool and
             starts a worker. `server_process_ms_to_first_answer` is the SERVER process's own CPU
             here; `tree_process_ms_to_first_answer` adds the pool children.
  pool_ready the tree CPU has gone quiet (no growth over `SETTLE_WINDOW_SECS`) with pool workers
             alive: worker initializer and the emit-chain prewarm thread are done.
             `pool_ready_process_ms` is the tree CPU here.

Phase attribution is the tree-CPU delta between milestones: boot (interpreter, imports, election)
= spawn..listener; preload (op-registry import, context build) = listener..accept; pool build
(`_ensure_dispatch_pool`, worker start, worker initializer's registry preload) = accept..first;
settle (workers' threaded registry preload finishing) = first..pool_ready; workers no longer prewarm the emit chain.

`--handover` starts the server through `round.start_successor_pair_in_hold` on the copied root
(optionally over a booted old pair with `--old-pair`) and adds `handover` (True iff the successor
answered inside the one attempt's `NEW_PAIR_READY_BUDGET_SECS`) and `handover_wall_ms`.

Usage: python coordinator_core/benchmarks/warm_pair_boot_process_time.py [--work-dir DIR]
[--cold-pyc] [--handover [--old-pair]] [--assert-budget]. Exit 1 only under --assert-budget when
`server_process_ms_to_first_answer` exceeds `FIRST_ANSWER_BUDGET_MS`, or with --handover when the
successor did not answer first time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.warm import breadcrumb, client, skew
from coordinator_core.warm.telemetry import SPAWN_EPOCH_ENV

#: The brightline: a new warm server must reach its first answer within this much process time.
FIRST_ANSWER_BUDGET_MS = 500.0
SAMPLE_INTERVAL_SECS = 0.01
SETTLE_POLL_SECS = 0.25
#: Tree CPU growth below this over `SETTLE_WINDOW_SECS` reads as quiet.
SETTLE_QUIET_MS = 15.0
SETTLE_WINDOW_SECS = 0.5
#: A phase that never completes is a hang, reported as a missing milestone, never a number.
MILESTONE_TIMEOUT_SECS = 180.0
SETTLE_TIMEOUT_SECS = 120.0
_LISTENER_AUTOSTART_ENV = "COORDINATOR_WARM_LISTENER_AUTOSTART"
REQUEST_STATUS_METHOD = "warm.request_status"
PING_METHOD = "ping"

#: Boot never reads these; skipping them keeps the copy cheap. `__pycache__` is kept on purpose:
#: a live engine boots with warm bytecode, and a cold-pyc boot would measure the compiler.
_COPY_IGNORE = shutil.ignore_patterns("tests", "*.md")


@dataclass(frozen=True)
class CpuSnapshot:
    """Cumulative CPU of the server process and of its pool children at one instant."""

    wall_secs: float
    server_ms: float
    pool_ms: float
    pool_workers: int

    @property
    def tree_ms(self) -> float:
        return self.server_ms + self.pool_ms


@dataclass
class BootReport:
    server_process_ms_to_first_answer: Optional[float] = None
    tree_process_ms_to_first_answer: Optional[float] = None
    accept_answer_process_ms: Optional[float] = None
    pool_ready_process_ms: Optional[float] = None
    pool_workers_at_pool_ready: int = 0
    phase_process_ms: dict = field(default_factory=dict)
    wall_ms: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    #: Handover mode only. `handover` is the single `start_successor_pair_in_hold` attempt's result:
    #: True iff the successor answered inside `handover_budget_ms` (so it answered on attempt 1).
    handover: Optional[bool] = None
    handover_wall_ms: Optional[int] = None
    handover_budget_ms: Optional[int] = None
    handover_message: str = ""

    def within_budget(self) -> bool:
        """Plain boot: first answer inside `FIRST_ANSWER_BUDGET_MS`. Handover: the successor
        answered on its one attempt (a cold-bytecode copy measures the compiler, not the 500ms bar)."""
        if self.handover is not None:
            return (
                self.handover
                and not self.missing
                and all(ms <= FIRST_ANSWER_BUDGET_MS for ms in self.phase_process_ms.values())
            )
        v = self.server_process_ms_to_first_answer
        return v is not None and v <= FIRST_ANSWER_BUDGET_MS


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_engine_root(
    dest: Path, source_root: Path, *, cold_pyc: bool = False, fresh_copy: bool = False
) -> Path:
    """A stamped engine root under `dest` holding `source_root`'s `coordinator_core`.

    Files are hard-linked when the volume allows (copied otherwise); the server only reads them.
    `cold_pyc` leaves the bytecode behind: the state of a tree a publish copy just replaced.
    `fresh_copy` writes new files instead of links: a publish copy's files are new on disk, which a
    hard link to an already-read source is not.
    """
    ignore = shutil.ignore_patterns("tests", "*.md", "__pycache__") if cold_pyc else _COPY_IGNORE
    shutil.copytree(
        source_root / "coordinator_core",
        dest / "coordinator_core",
        ignore=ignore,
        copy_function=shutil.copy2 if fresh_copy else _link_or_copy,
    )
    (dest / "coordinator_core" / skew.ENGINE_STAMP_FILENAME).write_text(
        "sha:boot-process-time\n", encoding="utf-8"
    )
    return dest


def snapshot(proc: Any, t0: float) -> CpuSnapshot:
    """Cumulative CPU of `proc` and its live descendants. A child that vanished mid-read counts 0."""
    import psutil

    def _cpu(p: Any) -> float:
        times = p.cpu_times()
        return (times.user + times.system) * 1000.0

    server_ms = _cpu(proc)
    pool_ms = 0.0
    workers = 0
    for child in proc.children(recursive=True):
        try:
            pool_ms += _cpu(child)
            workers += 1
        except psutil.Error:
            continue
    return CpuSnapshot(time.monotonic() - t0, server_ms, pool_ms, workers)


class Probe(threading.Thread):
    """One request/response over the server endpoint, off the sampler's thread."""

    def __init__(self, endpoint: str, method: str, token: str) -> None:
        super().__init__(daemon=True)
        self._endpoint = endpoint
        self._frame = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}, "_engine_token": token})
            + "\n"
        ).encode("utf-8")
        self.done = threading.Event()
        self.response: Optional[dict] = None
        self.error: Optional[str] = None

    def run(self) -> None:
        try:
            fh = client._open_pipe(self._endpoint)
            try:
                fh.write(self._frame)
                fh.flush()
                line = fh.readline()
            finally:
                fh.close()
            self.response = json.loads(line) if line else None
            if self.response is None:
                self.error = "empty reply"
        except Exception as exc:  # noqa: BLE001 -- the report names the failure
            self.error = repr(exc)
        finally:
            self.done.set()


def phase_deltas(marks: dict) -> dict:
    """Tree-CPU ms spent between consecutive milestones present in `marks`."""
    order = (
        ("boot_imports_election", None, "listener"),
        ("registry_preload_context", "listener", "accept"),
        ("pool_build_worker_init", "accept", "first"),
        ("emit_prewarm_settle", "first", "pool_ready"),
    )
    out: dict = {}
    for name, prev, cur in order:
        if cur not in marks or (prev is not None and prev not in marks):
            continue
        base = 0.0 if prev is None else marks[prev].tree_ms
        out[name] = round(marks[cur].tree_ms - base, 1)
    return out


def _wait_for_crumb(engine_root: Path, deadline: float) -> Optional[dict]:
    while time.monotonic() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root)
        if crumb and isinstance(crumb.get("pid"), int) and crumb.get("pipe"):
            return crumb
        time.sleep(SAMPLE_INTERVAL_SECS)
    return None


def _answered(probe: Probe, proc: Any, t0: float, deadline: float) -> Optional[CpuSnapshot]:
    while not probe.done.wait(SAMPLE_INTERVAL_SECS):
        if time.monotonic() > deadline:
            return None
    snap = snapshot(proc, t0)
    return snap if probe.response is not None and "error" not in probe.response else None


def _settle(proc: Any, t0: float) -> Optional[CpuSnapshot]:
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECS
    window_start = snapshot(proc, t0)
    window_began = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(SETTLE_POLL_SECS)
        now = snapshot(proc, t0)
        if abs(now.tree_ms - window_start.tree_ms) > SETTLE_QUIET_MS:
            window_start, window_began = now, time.monotonic()
        elif time.monotonic() - window_began >= SETTLE_WINDOW_SECS and now.pool_workers > 0:
            return now
    return None


def stop_engine_root(engine_root: Path) -> None:
    """End every pair process under `engine_root`: the production graceful stop first, a kill only
    for a survivor. Never raises."""
    import psutil

    from coordinator_core.warm import pair_stop

    try:
        pair_stop.stop_warm_pair(engine_root)
    except Exception:  # noqa: BLE001 -- cleanup: fall through to the survivor kill
        pass
    for found in pair_stop.find_pair_processes(engine_root):
        try:
            kill_tree(psutil.Process(found.pid))
        except psutil.Error:
            pass


def kill_tree(proc: Any) -> None:
    import psutil

    try:
        victims = proc.children(recursive=True) + [proc]
    except psutil.Error:
        return
    for victim in victims:
        try:
            victim.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=10)


@contextmanager
def isolated_runtime(runtime_base: Path) -> Iterator[None]:
    """Point the warm runtime at `runtime_base` and keep the http listener from autostarting.

    Both are inherited by a server spawned inside the block, so the tmp engine root's pair never
    touches the box's runtime directory or leaves a foreign listener behind.
    """
    overrides = {breadcrumb.RUNTIME_BASE_ENV: str(runtime_base), _LISTENER_AUTOSTART_ENV: "0"}
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def measure(engine_root: Path, runtime_base: Path) -> BootReport:
    """Spawn the server under `engine_root` and report its boot cost in process time."""
    with isolated_runtime(runtime_base):
        return _measure_in_runtime(engine_root, BootReport())


def _measure_in_runtime(engine_root: Path, report: BootReport) -> BootReport:
    import psutil

    proc = None
    try:
        token = skew.compute_client_token(engine_root)
        t0 = time.monotonic()
        spawned = client.spawn_detached(
            str(engine_root), client.SERVER_ENTRY_SCRIPT, env_extra={SPAWN_EPOCH_ENV: repr(time.time())}
        )
        if not spawned:
            report.missing.append("spawn")
            return report
        crumb = _wait_for_crumb(engine_root, time.monotonic() + MILESTONE_TIMEOUT_SECS)
        if crumb is None:
            report.missing.append("listener")
            return report
        proc = psutil.Process(crumb["pid"])
        _sample_milestones(proc, crumb["pipe"], token, t0, report)
        return report
    finally:
        if proc is not None:
            stop_engine_root(engine_root)


def _sample_milestones(proc: Any, pipe: str, token: str, t0: float, report: BootReport) -> None:
    """Walk listener/accept/first/pool_ready on the server `proc` and fill `report`."""
    marks = {"listener": snapshot(proc, t0)}

    deadline = time.monotonic() + MILESTONE_TIMEOUT_SECS
    while True:
        accept_probe = Probe(pipe, REQUEST_STATUS_METHOD, token)
        accept_probe.start()
        accept = _answered(accept_probe, proc, t0, deadline)
        if accept is not None or accept_probe.error is None or time.monotonic() > deadline:
            break
        time.sleep(SAMPLE_INTERVAL_SECS)
    if accept is None:
        report.missing.append(f"accept ({accept_probe.error})")
    else:
        marks["accept"] = accept

    ping_probe = Probe(pipe, PING_METHOD, token)
    ping_probe.start()
    first = _answered(ping_probe, proc, t0, deadline)
    if first is None:
        report.missing.append(f"first ({ping_probe.error})")
    else:
        marks["first"] = first
        ready = _settle(proc, t0)
        if ready is None:
            report.missing.append("pool_ready")
        else:
            marks["pool_ready"] = ready

    if "accept" in marks:
        report.accept_answer_process_ms = round(marks["accept"].tree_ms, 1)
    if "first" in marks:
        report.server_process_ms_to_first_answer = round(marks["first"].server_ms, 1)
        report.tree_process_ms_to_first_answer = round(marks["first"].tree_ms, 1)
    if "pool_ready" in marks:
        report.pool_ready_process_ms = round(marks["pool_ready"].tree_ms, 1)
        report.pool_workers_at_pool_ready = marks["pool_ready"].pool_workers
    report.phase_process_ms = phase_deltas(marks)
    report.wall_ms = {k: round(v.wall_secs * 1000.0) for k, v in marks.items()}


def measure_handover(engine_root: Path, runtime_base: Path, *, old_pair: bool = False) -> BootReport:
    """Run the publish handover on `engine_root` and report the successor's boot cost.

    The successor is started by `round.start_successor_pair_in_hold` (one attempt, the production
    budget `NEW_PAIR_READY_BUDGET_SECS`) while the publish marker is held, as a klabauter publish
    does after its copy. With `old_pair` an old generation is booted first and the stamp flipped to
    a new one, so the handover pins, spawns, waits and stops the old pair; without it the root has
    no pair and the successor is the only one. Phases are sampled on the successor's server process.
    """
    with isolated_runtime(runtime_base):
        return _measure_handover_in_runtime(engine_root, BootReport(), old_pair)


def _boot_old_pair(engine_root: Path, deadline: float) -> Optional[dict]:
    token = skew.compute_client_token(engine_root)
    if not client.spawn_detached(
        str(engine_root), client.SERVER_ENTRY_SCRIPT, env_extra={SPAWN_EPOCH_ENV: repr(time.time())}
    ):
        return None
    crumb = _wait_for_crumb(engine_root, deadline)
    while crumb is not None and time.monotonic() < deadline:
        probe = Probe(crumb["pipe"], REQUEST_STATUS_METHOD, token)
        probe.start()
        if probe.done.wait(max(0.0, deadline - time.monotonic())) and probe.response is not None:
            return crumb
        time.sleep(SAMPLE_INTERVAL_SECS)
        crumb = breadcrumb.read_breadcrumb(engine_root)
    return None


def _measure_handover_in_runtime(engine_root: Path, report: BootReport, old_pair: bool) -> BootReport:
    import io

    import psutil

    from coordinator_core.percolate import round as percolate_round

    proc = None
    try:
        old_pid: Optional[int] = None
        old_stamp: Optional[bytes] = None
        if old_pair:
            old_crumb = _boot_old_pair(engine_root, time.monotonic() + MILESTONE_TIMEOUT_SECS)
            if old_crumb is None:
                report.missing.append("old_pair")
                return report
            old_pid = old_crumb["pid"]
            old_stamp = skew.read_engine_stamp_bytes(engine_root)
            skew.replace_engine_stamp_bytes(engine_root, b"sha:boot-process-time-successor\n")
        token = skew.compute_client_token(engine_root)
        outcome: dict = {}
        err = io.StringIO()

        def _handover() -> None:
            began = time.monotonic()
            try:
                with breadcrumb.hold_publish_marker(engine_root, holder="warm-pair-boot-benchmark"):
                    outcome["ok"] = percolate_round.start_successor_pair_in_hold(
                        engine_root, err=err, old_stamp=old_stamp
                    )
            except Exception as exc:  # noqa: BLE001 -- the report names the failure
                outcome["ok"] = False
                err.write(repr(exc))
            outcome["wall_ms"] = round((time.monotonic() - began) * 1000.0)

        t0 = time.monotonic()
        runner = threading.Thread(target=_handover, daemon=True)
        runner.start()
        deadline = time.monotonic() + MILESTONE_TIMEOUT_SECS
        crumb = None
        while time.monotonic() < deadline and crumb is None:
            found = _wait_for_crumb(engine_root, time.monotonic() + SETTLE_POLL_SECS)
            if found is not None and found["pid"] != old_pid:
                crumb = found
        if crumb is None:
            report.missing.append("listener")
        else:
            proc = psutil.Process(crumb["pid"])
            try:
                _sample_milestones(proc, crumb["pipe"], token, t0, report)
            except psutil.Error as exc:
                report.missing.append(f"milestones ({exc!r})")
        runner.join(timeout=percolate_round.NEW_PAIR_READY_BUDGET_SECS + SETTLE_TIMEOUT_SECS)
        report.handover = bool(outcome.get("ok"))
        report.handover_wall_ms = outcome.get("wall_ms")
        report.handover_budget_ms = round(percolate_round.NEW_PAIR_READY_BUDGET_SECS * 1000.0)
        report.handover_message = err.getvalue().strip()
        return report
    finally:
        stop_engine_root(engine_root)


def run(
    work_dir: Optional[Path] = None,
    source_root: Optional[Path] = None,
    *,
    cold_pyc: bool = False,
    handover: bool = False,
    old_pair: bool = False,
) -> BootReport:
    """Build a tmp engine root from `source_root` (default: this checkout) and measure one boot,
    or with `handover` the successor's boot under the publish handover over a freshly copied tree."""
    source = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    base = work_dir if work_dir is not None else source / "scratch"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="warm-boot-process-time-", dir=base))
    try:
        root = build_engine_root(tmp / "engine", source, cold_pyc=cold_pyc, fresh_copy=handover)
        if handover:
            return measure_handover(root, tmp / "runtime", old_pair=old_pair)
        return measure(root, tmp / "runtime")
    finally:
        rmtree_or_raise(tmp, label="warm-pair-boot")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--cold-pyc", action="store_true", help="boot from a tree with no bytecode cache")
    parser.add_argument("--assert-budget", action="store_true")
    parser.add_argument(
        "--handover", action="store_true", help="start the successor through the publish handover"
    )
    parser.add_argument(
        "--old-pair", action="store_true", help="with --handover: boot an old pair first and swap it"
    )
    args = parser.parse_args(argv)
    if args.old_pair and not args.handover:
        parser.error("--old-pair needs --handover")
    report = run(args.work_dir, cold_pyc=args.cold_pyc, handover=args.handover, old_pair=args.old_pair)
    if args.handover:
        print(f"handover={report.handover}")
        print(f"handover_wall_ms={report.handover_wall_ms} budget_ms={report.handover_budget_ms}")
        print(f"phase_process_ms={json.dumps(report.phase_process_ms, sort_keys=True)}")
    print(f"server_process_ms_to_first_answer={report.server_process_ms_to_first_answer}")
    print(f"tree_process_ms_to_first_answer={report.tree_process_ms_to_first_answer}")
    print(f"pool_ready_process_ms={report.pool_ready_process_ms}")
    print(f"accept_answer_process_ms={report.accept_answer_process_ms}")
    print(json.dumps(asdict(report), sort_keys=True))
    if args.assert_budget and not report.within_budget():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
