"""Engine per-op overhead contract: what the warm engine charges an op beyond the op's own cost.

Library helper, no `__main__` entry: the driver that calls it declares benchmark origin, and the
spawn helper it goes through (`process_time.batched_process_time_ms`) scopes the origin to its child.
Process time and job process counts only, never wall clock.

An unexplained process is a difference of two job counts on the same op (warm minus in-process),
in which the console host Windows allocates beside each CREATE_NO_WINDOW git child cancels; it is
never `procs - 1`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator, NamedTuple, Optional, Sequence

from coordinator_core.benchmarks.isolated_clone import (
    clone_server_env,
    close_clone_job,
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
    spawn_clone_server,
)
from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.warm import breadcrumb

SEGMENTS: tuple[str, ...] = ("dispatch", "job_object", "json", "extra_process", "residual")

#: Engine per-op overhead bar, process time per call. Question: "does the warm engine charge a tax
#: beyond the op's own cost?" Derived from DR-344's <50ms reach bar. Ratchets DOWN only.
OVERHEAD_BUDGET_MS: float = 50.0

#: Processes per call started in the engine job beyond the op's own. Question: "does any process
#: start in the engine job that the op itself did not start?" Exact (job TotalProcesses is an
#: integer count, not a tick-quantised clock), so the bar is zero.
UNEXPLAINED_PROCS_BUDGET: float = 0.0

#: Boot wait for the isolated engine's breadcrumb pid. Question: "did the engine come up at all?"
_BOOT_WAIT_SECS = 90.0

#: Driver invocations per in-process batch. Question: "how many runs amortise the ~15.6ms job tick?"
_IN_PROCESS_REPS = 3

#: Argument a driver takes for its in-process call count; 0 is its setup-only run.
CALLS_FLAG = "--calls"


class OverheadSample(NamedTuple):
    probe: str
    warm_op_ms: float
    in_process_ms: float
    warm_procs: float
    in_process_procs: float

    @property
    def overhead_ms(self) -> float:
        """Process time the engine path charges beyond the op's own cost."""
        return self.warm_op_ms - self.in_process_ms

    @property
    def unexplained_procs(self) -> float:
        """Processes per call the engine job started that the op in-process does not."""
        return self.warm_procs - self.in_process_procs


class WarmEngine(NamedTuple):
    root: Path
    pid: int
    env: dict


def over_budget(sample: OverheadSample) -> Optional[str]:
    """None when within both bars, else the reason naming the figure and the bar."""
    reasons = []
    if sample.overhead_ms > OVERHEAD_BUDGET_MS:
        reasons.append(
            f"{sample.probe}: overhead_ms {sample.overhead_ms:.3f} exceeds the "
            f"{OVERHEAD_BUDGET_MS}ms overhead bar "
            f"(warm_op_ms {sample.warm_op_ms:.3f} - in_process_ms {sample.in_process_ms:.3f})"
        )
    if sample.unexplained_procs > UNEXPLAINED_PROCS_BUDGET:
        reasons.append(
            f"{sample.probe}: unexplained_procs {sample.unexplained_procs:.3f} exceeds the "
            f"{UNEXPLAINED_PROCS_BUDGET} unexplained-process bar "
            f"(warm_procs {sample.warm_procs:.3f} - in_process_procs {sample.in_process_procs:.3f})"
        )
    return "; ".join(reasons) or None


def _link_tree(src: Path, dst: Path) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        out = dst / Path(root).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, out / name)


def _clone_engine(source_root: Path, root: Path, entrypoints: Sequence[str]) -> None:
    _link_tree(source_root / "coordinator_core", root / "coordinator_core")
    bin_src = source_root / "coordinator" / "bin"
    bin_dst = root / "coordinator" / "bin"
    bin_dst.mkdir(parents=True)
    for name in entrypoints:
        os.link(bin_src / f"{name}.py", bin_dst / f"{name}.py")
    if (bin_src / "lib").is_dir():
        _link_tree(bin_src / "lib", bin_dst / "lib")
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:engine-op-overhead-{time.time_ns()}\n", encoding="utf-8"
    )


def _boot_env(engine_root: Path) -> dict:
    env = dict(os.environ)
    env.update(clone_server_env(engine_root))
    return env


def _terminate_engine(pid: int) -> None:
    try:
        import psutil

        parent = psutil.Process(pid)
        # Workers are signalled while their parent lives: an orphan has left the test's process
        # tree and the suite's foreign-kill tripwire refuses it.
        workers = parent.children(recursive=True)
        for w in workers:
            w.terminate()
        parent.terminate()
        psutil.wait_procs([parent, *workers], timeout=5)
    except Exception:
        pass


@contextlib.contextmanager
def isolated_warm_engine(source_root: Path, *, entrypoints: Sequence[str]) -> Iterator[WarmEngine]:
    """Boot a warm engine from a hardlink clone of `source_root` carrying `entrypoints`' CLI
    scripts, yield WarmEngine(root, pid, env) before any dispatch fires (job membership is
    fixed at process creation), and tear it down with its workers on exit."""
    tmp_parent = mkdtemp_for_clone(source_root, prefix="engine-op-overhead-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    job = None
    try:
        _clone_engine(source_root, root, entrypoints)
        proc, job = spawn_clone_server(root, _boot_env(root))
        deadline = time.time() + _BOOT_WAIT_SECS
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            if proc.poll() is not None:
                raise RuntimeError(f"isolated warm engine exited rc={proc.returncode} before its breadcrumb")
            time.sleep(0.2)
        if not (crumb and crumb.get("pid")):
            raise RuntimeError(f"isolated warm engine did not boot within {_BOOT_WAIT_SECS}s")
        yield WarmEngine(root, int(crumb["pid"]), _boot_env(root))
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            _terminate_engine(int(crumb["pid"]))
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        close_clone_job(job)
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="engine_op_overhead", reaped=reaped)


def in_process_sample_ms(argv: Sequence[str], *, k: int) -> tuple[float, float]:
    """(ms per call, procs per call) of running `argv` (a `python -m` driver that calls the op's
    in-process entry k times) inside a job, minus the same driver's setup-only run.

    The driver takes `--calls N`: N op calls after its setup, so `--calls 0` is the setup-only run
    whose interpreter start and imports cancel in the difference."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    full = batched_process_time_ms([*argv, CALLS_FLAG, str(k)], k=_IN_PROCESS_REPS)
    base = batched_process_time_ms([*argv, CALLS_FLAG, "0"], k=_IN_PROCESS_REPS)
    for label, run in (("measured", full), ("setup-only", base)):
        if run["rc"] != 0:
            raise RuntimeError(f"in-process driver {label} run exited rc={run['rc']}: {list(argv)!r}")
    return (
        (full["process_time_ms"] - base["process_time_ms"]) / k,
        (full["procs_per_call"] - base["procs_per_call"]) / k,
    )
