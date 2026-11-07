"""Dead-engine non-hook door call: process-time instrument and mechanism spike.

Builds a throwaway hardlink clone of the engine under `scratch/`, builds the door from the chosen source
ref, leaves the warm server dead, runs ONE non-hook op (`session.whoami_live`) through the door, and reports
process time via `process_time.batched_process_time_ms` (never wall clock). Every sample starts from a dead
engine: the previous sample's server is stopped and its service directory removed.

Billing. A door sample bills the door and every process the door waits on (the cold leg it falls through
to). A server the door merely spawns detached is NOT in that figure; it is reported beside it as
`detached_server_boot_cpu_ms` so it is never claimed as free. A sleeping wait burns no CPU, so a candidate
that waits is billed its full wait budget on top of its process time.

Legs (`--door posix|windows`; the windows arm builds the door as `coordinator-invoke.exe` and runs only on Windows):
  baseline       the door at `--door-ref` (default: the pinned base sha) against a dead engine.
  bounded_wait   candidate A: boot the server, wait for it to answer, then take the warm call. Measures the
                 boot-to-ready wall time (a feasibility figure: how long the wait would need to be), the
                 server-tree CPU at ready, and the warm door call's process time.
  slim_cold      candidate B: the same door with its cold entry replaced by a slim entry that skips the
                 `cc_invoke` bootstrap. Door, spawn and cold leg are all billed, and so is any boot wait
                 the cold child sleeps in (`warm_miss.WARM_BOOT_WAIT_SECS`), at its full value.
  cold_no_wait   candidate D: the unchanged cold entry run with the boot wait off
                 (`COORDINATOR_WARM_BOOT_WAIT_SECS=0`), as the door's cold child would receive it.
  fail_fast      candidate C: the same door with a cold entry that exits at once, so it measures the floor
                 of "spawn and return"; the retry that must follow is the warm call from `bounded_wait`.
  floors         interpreter start and the direct cold entries, for decomposition.

The report's `bills.choice` is the cheapest candidate that is under the bar when billed honestly and that
keeps DR-367's contract (a cold fall-through on a warm miss SUCCEEDS). `strict_drain_reading` is the same
verdict when the 500ms publish-drain wait is also charged to any waiting candidate.

Run: `python3 coordinator_core/benchmarks/door_dead_engine_cold_leg.py --door posix [--door-ref REF|worktree]
[--runs K] [--enforce-bar]`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

#: The brightline process-time bar, ms (DR-344).
BAR_MS = 500.0
#: Sha the unchanged doors are pinned at; `--door-ref` defaults to it.
BASE_SHA = "9525213e376453fb0c9cf7b39c234ead8ff7b10f"
WORKTREE = "worktree"
OP = "session.whoami_live"
DEFAULT_RUNS = 5
#: `door_posix.c :: DOOR_DRAIN_WAIT_MS`: waited before RESPAWN ON MISS while a publish marker is fresh.
DRAIN_WAIT_MS = 500.0
#: Granularity a candidate wait budget is rounded up to.
WAIT_STEP_MS = 50.0
BOOT_TIMEOUT_S = 30.0
SOURCE_FILES = {
    "posix": ("door_posix.c", "door_core.c", "door_core.h", "door_env_set.h"),
    "windows": ("door.c", "door_core.c", "door_core.h", "door_env_set.h"),
}
DOOR_DIR_REL = "coordinator_core/warm/door"
ENTRY_REL = Path("coordinator") / "bin" / "coordinator-invoke.py"

#: Candidate B's cold entry: the engine's own `main`, reached without the `cc_invoke` bootstrap ladder.
SLIM_ENTRY = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
    "from coordinator_core.invoke.__main__ import main\n"
    "main()\n"
)
#: Candidate C's cold entry: answers at once, loudly non-success, so only the door's own cost is measured.
FAIL_FAST_ENTRY = (
    "import sys\n"
    "sys.stderr.write('engine booting; retry\\n')\n"
    "raise SystemExit(75)\n"
)
#: A candidate that exits non-success on its first call is rejected by DR-367 unless the door retries.
CONTRACT_OK = {"baseline": True, "bounded_wait": True, "slim_cold": True, "cold_no_wait": True,
               "fail_fast": False}
#: The knob `invoke/warm_miss.py` reads; `0` makes the cold leg run the op at once instead of sleeping.
BOOT_WAIT_ENV = "COORDINATOR_WARM_BOOT_WAIT_SECS"


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile `q` (0-100) of `samples`."""
    if not samples:
        raise ValueError("percentile of no samples")
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: Sequence[float]) -> dict:
    """`{n, min, p50, p90, max, samples}` of `samples`, rounded to 0.1ms."""
    if not samples:
        return {"n": 0, "min": None, "p50": None, "p90": None, "max": None, "samples": []}
    return {
        "n": len(samples),
        "min": round(min(samples), 1),
        "p50": round(percentile(samples, 50), 1),
        "p90": round(percentile(samples, 90), 1),
        "max": round(max(samples), 1),
        "samples": [round(s, 1) for s in samples],
    }


def wait_budget_ms(boot_wall_p90_ms: float) -> float:
    """The named wait constant a bounded wait would need: the p90 boot-to-ready time, rounded up."""
    return math.ceil(boot_wall_p90_ms / WAIT_STEP_MS) * WAIT_STEP_MS


def bill_candidate(name: str, process_p90_ms: float, wait_ms: float = 0.0, drain_ms: float = 0.0) -> dict:
    """A candidate's honest bill: p90 process time plus its wait budget at full value plus any drain wait."""
    billed = process_p90_ms + wait_ms + drain_ms
    return {
        "candidate": name,
        "process_p90_ms": round(process_p90_ms, 1),
        "wait_budget_ms": round(wait_ms, 1),
        "drain_wait_ms": round(drain_ms, 1),
        "billed_ms": round(billed, 1),
        "under_bar": billed < BAR_MS,
        "contract_ok": CONTRACT_OK[name],
    }


def choose(bills: Sequence[dict]) -> Optional[str]:
    """Cheapest candidate under the bar that keeps the DR-367 contract; None when none qualifies."""
    ok = [b for b in bills if b["under_bar"] and b["contract_ok"]]
    return min(ok, key=lambda b: b["billed_ms"])["candidate"] if ok else None


def build_bills(report: dict) -> dict:
    """Bills for every candidate from a leg report, plus the strict-drain re-bill and each choice.

    A leg whose cold child sleeps in the Python boot wait is billed that wait at full value."""
    warm_p90 = report["bounded_wait"]["warm_call_ms"]["p90"]
    wait = wait_budget_ms(report["bounded_wait"]["boot_ready_wall_ms"]["p90"])

    def leg_bill(name: str, drain: float = 0.0) -> dict:
        leg = report[name]
        return bill_candidate(name, leg["door_ms"]["p90"], leg["boot_wait_billed_ms"], drain)

    def candidates(drain: float) -> list:
        return [
            bill_candidate("bounded_wait", warm_p90, wait, drain),
            leg_bill("slim_cold"),
            leg_bill("cold_no_wait"),
            bill_candidate("fail_fast", report["fail_fast"]["door_ms"]["p90"] + warm_p90),
        ]

    plain, strict = candidates(0.0), candidates(DRAIN_WAIT_MS)
    return {"baseline": leg_bill("baseline"), "plain": plain, "strict_drain_reading": strict,
            "choice": choose(plain), "choice_strict_drain_reading": choose(strict)}


@contextlib.contextmanager
def patched_environ(updates: dict) -> Iterator[None]:
    """`os.environ` with `updates` applied, restored on exit."""
    saved = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def export_door_sources(repo_root: Path, ref: str, door: str, dest: Path) -> Path:
    """The door's C sources at `ref` (`worktree` = the live tree) copied into `dest`; returns `dest`.

    Read with `git show`, so the pinned base is exactly the committed bytes whatever the tree holds."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_FILES[door]:
        target = dest / name
        if ref == WORKTREE:
            shutil.copyfile(repo_root / DOOR_DIR_REL / name, target)
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ref}:{DOOR_DIR_REL}/{name}"],
            capture_output=True, check=True, **no_console_creationflags(),
        )
        target.write_bytes(proc.stdout)
    return dest


def build_door(door: str, sources: Path, engine_root: Path, out_dir: Path) -> Path:
    """Build the door from the exported `sources` through the repo's own builder (its source constants
    are pointed at `sources` for the call and restored); returns the binary path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if door == "posix":
        from coordinator_core.warm.door import build_posix as builder

        patch = {"_SOURCES": (sources / "door_posix.c", sources / "door_core.c"),
                 "_HEADER": sources / "door_core.h", "_ENV_SET_HEADER": sources / "door_env_set.h"}
        output = out_dir / "coordinator-invoke"
    else:
        from coordinator_core.warm.door import build as builder

        patch = {"_SOURCE": sources / "door.c", "_CORE_SOURCE": sources / "door_core.c",
                 "_CORE_HEADER": sources / "door_core.h", "_ENV_SET_HEADER": sources / "door_env_set.h",
                 "SOURCES": tuple(sources / n for n in SOURCE_FILES["windows"])}
        # The door resolves its CLI from its own image name; a bare `door` image has no door.py and
        # refuses. Name it as installed (door_install.DOOR_INSTALLED_NAME on Windows).
        output = out_dir / "coordinator-invoke.exe"
    saved = {k: getattr(builder, k) for k in patch}
    try:
        for k, v in patch.items():
            setattr(builder, k, v)
        builder.build(engine_root, output=output)
    finally:
        for k, v in saved.items():
            setattr(builder, k, v)
    return output


def server_processes(root: Path) -> list:
    """Live processes whose command line names `root`, excluding this one."""
    import psutil

    needle = str(root)
    found = []
    for proc in psutil.process_iter(["pid"]):
        if proc.info["pid"] == os.getpid():
            continue
        # psutil raises SystemError, not psutil.Error, for some macOS processes it cannot read.
        with contextlib.suppress(psutil.Error, SystemError):
            if any(needle in part for part in proc.cmdline()):
                found.append(proc)
    return found


def stop_engine_processes(root: Path, timeout_s: float = 5.0) -> None:
    """Terminate every process rooted at `root` with its descendants, children first."""
    import psutil

    victims = {}
    for proc in server_processes(root):
        victims[proc.pid] = proc
        with contextlib.suppress(psutil.Error):
            for child in proc.children(recursive=True):
                victims[child.pid] = child
    ordered = sorted(victims.values(), key=lambda p: -p.pid)
    for proc in ordered:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, alive = psutil.wait_procs(ordered, timeout=timeout_s)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(alive, timeout=timeout_s)


def tree_cpu_ms(root: Path) -> float:
    """User+system CPU of every live process rooted at `root` plus their descendants, ms."""
    import psutil

    seen = {}
    for proc in server_processes(root):
        seen[proc.pid] = proc
        with contextlib.suppress(psutil.Error):
            for child in proc.children(recursive=True):
                seen[child.pid] = child
    total = 0.0
    for proc in seen.values():
        with contextlib.suppress(psutil.Error):
            times = proc.cpu_times()
            total += times.user + times.system
    return total * 1000.0


class DeadEngine:
    """A stamped hardlink clone of the engine, a built door, and a private runtime base.

    `reset()` returns the clone to "no server, no service directory". Use as a context manager."""

    def __init__(self, repo_root: Path, door: str, door_ref: str) -> None:
        from coordinator_core.benchmarks import isolated_clone

        self.repo_root = repo_root
        self.door = door
        self.door_ref = door_ref
        self._parent = isolated_clone.mkdtemp_for_clone(repo_root, prefix="dec-")
        self.root = self._parent / "clone"
        isolated_clone.clone_engine(repo_root, self.root, entrypoint="coordinator-invoke")
        # `coordinator_core.engine_root` loads this shim by path relative to itself.
        shim = Path("coordinator") / "lib" / "resolve-claude-klabauter"
        isolated_clone._link_tree(repo_root / shim, self.root / shim)
        self.root = self.root.resolve()
        self.settings = self._parent / "settings"
        self.settings.mkdir()
        # sun_path is 104 bytes on macOS: the socket's runtime base must be short.
        self.runtime = Path(tempfile.mkdtemp(prefix="dec", dir="/tmp" if os.name != "nt" else None))
        self.entry_original = (self.root / ENTRY_REL).read_text(encoding="utf-8")
        sources = export_door_sources(repo_root, door_ref, door, self._parent / "door-src")
        self.door_path = build_door(door, sources, self.root, self._parent / "door-bin")

    def env(self) -> dict:
        from coordinator_core.benchmarks.isolated_clone import engine_env

        env = engine_env(self.root)
        env.update({
            "COORDINATOR_WARM_RUNTIME_BASE": str(self.runtime),
            "COORDINATOR_SETTINGS_HOME": str(self.settings),
            "COORDINATOR_ENGINE_ROOT": str(self.root),
        })
        env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
        return env

    def set_cold_entry(self, text: Optional[str]) -> None:
        """Replace the clone's cold entry with `text` (None restores the original). The clone's file is a
        hardlink of the repo's, so it is unlinked before writing: the repo's copy is never touched."""
        path = self.root / ENTRY_REL
        path.unlink()
        path.write_text(self.entry_original if text is None else text, encoding="utf-8", newline="\n")

    def svc_dir(self) -> Path:
        from coordinator_core.warm import breadcrumb

        with patched_environ({breadcrumb.RUNTIME_BASE_ENV: str(self.runtime)}):
            return breadcrumb.svc_dir(engine_root=self.root)

    def reset(self) -> None:
        stop_engine_processes(self.root)
        shutil.rmtree(self.svc_dir(), ignore_errors=True)

    def server_ready(self) -> bool:
        """True once the server ANSWERS the op as the door would ask it (POSIX), or its breadcrumb names a pid.

        A socket that merely accepts a connection is not ready: the client's call still goes cold."""
        if os.name == "nt":
            from coordinator_core.warm import breadcrumb

            with patched_environ({breadcrumb.RUNTIME_BASE_ENV: str(self.runtime)}):
                crumb = breadcrumb.read_breadcrumb(engine_root=self.root)
            return bool(crumb and crumb.get("pid"))
        from coordinator_core.warm import breadcrumb, election, skew

        token = skew.compute_client_token(self.root)
        with patched_environ({breadcrumb.RUNTIME_BASE_ENV: str(self.runtime)}):
            sock_path = election.socket_path(token, engine_clone=self.root)
        request = {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv",
                   "params": {"argv": [OP], "cwd": str(self.root)}, "_engine_token": token}
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(2.0)
            probe.connect(str(sock_path))
            probe.sendall(json.dumps(request).encode("utf-8") + b"\n")
            return b"session_id" in probe.recv(65536)
        except OSError:
            return False
        finally:
            probe.close()

    def prime_bytecode(self, timeout_s: float = 300.0) -> None:
        """Boot the server once and stop it, so the clone carries compiled bytecode like an installed engine
        and no measured boot pays for compiling the engine's sources."""
        server = self.spawn_server_like_door()
        try:
            if self.await_server(timeout_s) is None:
                raise RuntimeError(f"priming boot did not answer within {timeout_s}s")
        finally:
            server.terminate()
            self.reset()

    def await_server(self, timeout_s: float = BOOT_TIMEOUT_S) -> Optional[float]:
        """Server-tree CPU ms once the server answers, or None when it never does within the timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.server_ready():
                return tree_cpu_ms(self.root)
            time.sleep(0.01)
        return None

    def spawn_server_like_door(self) -> subprocess.Popen:
        """The server exactly as the door's `door_maybe_spawn_server` starts it (PYTHONPATH = engine root)."""
        env = self.env()
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [sys.executable, str(self.root / "coordinator_core" / "warm" / "server.py")],
            env=env, cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, **no_console_creationflags(),
        )

    def door_call(self, env_extra: Optional[dict] = None, accept_rc: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
        """One unmeasured door call. Raises unless it exited with an accepted rc and, on rc 0, answered the op."""
        env = self.env()
        env.update(env_extra or {})
        proc = subprocess.run(
            [str(self.door_path), OP], capture_output=True, text=True, env=env, cwd=str(self.root),
            timeout=120, **no_console_creationflags(),
        )
        if proc.returncode not in accept_rc or (proc.returncode == 0 and "session_id" not in proc.stdout):
            raise RuntimeError(f"door call did not succeed: rc={proc.returncode} stdout={proc.stdout[:200]!r} "
                               f"stderr={proc.stderr[:300]!r}")
        return proc

    def boot_wait_billed_ms(self, env_extra: Optional[dict] = None, accept_rc: Sequence[int] = (0,)) -> float:
        """The sleeping boot wait the cold child announces on a dead engine, in ms (0 when it announces none).

        Read from the child's own `waiting up to Ns` line, so a wait that sleeps ~0 CPU is still billed."""
        self.reset()
        stderr = self.door_call(env_extra, accept_rc).stderr
        self.reset()
        match = re.search(r"waiting up to ([0-9.]+)s", stderr)
        return float(match.group(1)) * 1000.0 if match else 0.0

    def is_warm_answer(self, proc: subprocess.CompletedProcess) -> bool:
        """True when the server answered: a cold fall-through announces itself on stderr."""
        return proc.returncode == 0 and "[warm-client]" not in proc.stderr

    def close(self) -> None:
        from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

        stop_engine_processes(self.root)
        shutil.rmtree(self.runtime, ignore_errors=True)
        rmtree_or_raise(self._parent, label="door_dead_engine_cold_leg")

    def __enter__(self) -> "DeadEngine":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def measure_process(cmd: Sequence[str], env: dict, cwd: str, accept_rc: Sequence[int] = (0,)) -> dict:
    """One `batched_process_time_ms` sample (k=1) of `cmd`; raises when its rc is not accepted."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    result = batched_process_time_ms(list(cmd), k=1, env=env, cwd=cwd)
    if result["rc"] not in accept_rc:
        raise RuntimeError(f"measured command exited rc={result['rc']}: {list(cmd)!r}")
    return result


def dead_door_leg(engine: DeadEngine, runs: int, accept_rc: Sequence[int] = (0,),
                  env_extra: Optional[dict] = None) -> dict:
    """`runs` samples of one non-hook door call on a dead engine, each from a freshly reset engine.

    `door_ms` bills the door and the processes it waited on; `boot_wait_billed_ms` is the sleeping wait the
    cold child announces, billed at its full named value; `detached_server_boot_cpu_ms` is the CPU of the
    server the door spawned detached, read once it answers, reported beside them."""
    door_ms, procs, boot_cpu = [], [], []
    env = engine.env()
    env.update(env_extra or {})
    wait_ms = engine.boot_wait_billed_ms(env_extra, accept_rc)
    for _ in range(runs):
        engine.reset()
        result = measure_process([str(engine.door_path), OP], env, str(engine.root), accept_rc)
        door_ms.append(result["process_time_ms"])
        procs.append(result["procs_per_call"])
        cpu = engine.await_server()
        if cpu is not None:
            boot_cpu.append(cpu)
    engine.reset()
    return {"door_ms": summarize(door_ms), "boot_wait_billed_ms": wait_ms,
            "procs_per_call": summarize(procs),
            "detached_server_boot_cpu_ms": summarize(boot_cpu), "runs": runs}


def bounded_wait_leg(engine: DeadEngine, runs: int) -> dict:
    """Candidate A components: boot-to-ready wall time, server CPU at ready, and the warm door call."""
    wall, cpu, warm = [], [], []
    for _ in range(runs):
        engine.reset()
        started = time.monotonic()
        server = engine.spawn_server_like_door()
        try:
            ready_cpu = engine.await_server()
            if ready_cpu is None:
                raise RuntimeError(f"server did not answer within {BOOT_TIMEOUT_S}s")
            wall.append((time.monotonic() - started) * 1000.0)
            cpu.append(ready_cpu)
            if not engine.is_warm_answer(engine.door_call()):
                raise RuntimeError("a door call after the server answered still fell through cold")
            warm.append(measure_process([str(engine.door_path), OP], engine.env(), str(engine.root))
                        ["process_time_ms"])
        finally:
            server.terminate()
            engine.reset()
    return {"boot_ready_wall_ms": summarize(wall), "boot_ready_cpu_ms": summarize(cpu),
            "warm_call_ms": summarize(warm), "runs": runs}


def floors_leg(engine: DeadEngine, runs: int) -> dict:
    """Interpreter start and the direct cold entries (warm path off), k=`runs` batches."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    env = engine.env()
    env["COORDINATOR_WARM"] = "0"
    entry = str(engine.root / ENTRY_REL)
    out = {}
    for name, cmd in (("interpreter", [sys.executable, "-c", "pass"]),
                      ("cold_entry_direct", [sys.executable, entry, OP])):
        res = batched_process_time_ms(cmd, k=runs, env=env, cwd=str(engine.root))
        if res["rc"] != 0:
            raise RuntimeError(f"floor {name} exited rc={res['rc']}")
        out[name] = {"process_time_ms": res["process_time_ms"], "procs_per_call": res["procs_per_call"], "k": runs}
    engine.set_cold_entry(SLIM_ENTRY)
    try:
        res = batched_process_time_ms([sys.executable, entry, OP], k=runs, env=env, cwd=str(engine.root))
    finally:
        engine.set_cold_entry(None)
    if res["rc"] != 0:
        raise RuntimeError(f"floor slim_entry_direct exited rc={res['rc']}")
    out["slim_entry_direct"] = {"process_time_ms": res["process_time_ms"],
                                "procs_per_call": res["procs_per_call"], "k": runs}
    return out


def run(repo_root: Path, door: str, door_ref: str, runs: int) -> dict:
    """The full report for one door at one source ref."""
    if door == "windows" and os.name != "nt":
        raise SystemExit("door_dead_engine_cold_leg: --door windows measures door.exe and runs only on Windows")
    if door == "posix" and os.name == "nt":
        raise SystemExit("door_dead_engine_cold_leg: --door posix runs only on macOS/Linux")
    declare_benchmark_origin()
    with DeadEngine(repo_root, door, door_ref) as engine:
        engine.door_call()
        engine.prime_bytecode()
        report: dict = {
            "op": OP, "door": door, "door_ref": door_ref, "bar_ms": BAR_MS, "runs": runs,
            "platform": sys.platform, "baseline": dead_door_leg(engine, runs),
        }
        report["floors"] = floors_leg(engine, runs)
        report["bounded_wait"] = bounded_wait_leg(engine, runs)
        engine.set_cold_entry(SLIM_ENTRY)
        try:
            engine.door_call()
            engine.reset()
            report["slim_cold"] = dead_door_leg(engine, runs)
        finally:
            engine.set_cold_entry(None)
        report["cold_no_wait"] = dead_door_leg(engine, runs, env_extra={BOOT_WAIT_ENV: "0"})
        engine.set_cold_entry(FAIL_FAST_ENTRY)
        try:
            report["fail_fast"] = dead_door_leg(engine, runs, accept_rc=(75,))
        finally:
            engine.set_cold_entry(None)
        report["bills"] = build_bills(report)
        report["baseline_under_bar"] = report["bills"]["baseline"]["under_bar"]
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--door", choices=("posix", "windows"), default="posix")
    ap.add_argument("--door-ref", default=BASE_SHA,
                    help=f"git ref the door sources are built from; '{WORKTREE}' = the live tree (default: base sha)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true",
                    help=f"exit 2 unless the dead-engine door p90 process time is under {BAR_MS:.0f}ms")
    args = ap.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    # The measured door inherits this process's stdout: park it so op output cannot interleave the report.
    sys.stdout.flush()
    report_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    try:
        report = run(repo_root, args.door, args.door_ref, args.runs)
    finally:
        os.dup2(report_fd, 1)
        os.close(report_fd)
    print(json.dumps(report, indent=2))
    return 2 if args.enforce_bar and not report["baseline_under_bar"] else 0


if __name__ == "__main__":
    sys.exit(main())
