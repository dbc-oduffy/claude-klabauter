"""
coordinator_core.benchmarks.concurrency_probe -- Concurrency-dial latency probe.

Purpose: measures how the engine's spawn-per-call latency degrades under
CONCURRENT invocations, at explicit, caller-chosen dial levels -- the
missing evidence identified by
`state/handoffs/2026-08-08-engine-fails-the-load-norm.md`: the only prior
latency evidence (the WSC-tail KPI, and
`coordinator_core/benchmarks/PHASE-0-MEASUREMENTS.md`) was drawn on a quiet
box and gates on `min`, a statistic designed to erase contention effects.
Every number in this repo must hold under the ratified load norm of 50-70
concurrent LLMs, not an idle machine
(`docs/wiki/machine-load-norm.md`) -- this module is the instrument, not the
verdict; it reports a distribution per level, it does not gate or conclude.

Reuse discipline: this module does NOT duplicate the spawn-to-exit
child-process loop. `time_invocation`
(`coordinator_core.benchmarks.timer`) remains the sole primitive that spawns
and times ONE `invoke` child process, including its own AC9 exit/error
guard and its own SUBPROCESS_TIMEOUT_S=60s hard child-process timeout. This
module only calls it concurrently (via a `ThreadPoolExecutor`, one thread
per concurrent dial slot -- `time_invocation`'s `subprocess.run` call blocks
its calling thread, so true OS-process concurrency at level N requires N
threads each blocked inside their own `subprocess.run`).

Ctrl-C / teardown note: individual `invoke` child processes are torn down by
`time_invocation`'s own `SUBPROCESS_TIMEOUT_S` (60s) child-process timeout,
not by an OS-level kill from this module -- `subprocess.run` (used inside
`time_invocation`) does not expose its `Popen` handle to the caller, and
re-implementing spawn-with-a-trackable-handle here would duplicate the
primitive this module is required to reuse. A `KeyboardInterrupt` mid-wave
stops submission of further work immediately (`ThreadPoolExecutor.shutdown
(cancel_futures=True)`) and the run reports whatever waves already
completed; already-in-flight child processes are bounded to the pre-existing
60s ceiling, not instantly killed. This is a documented, deliberate
divergence from a literal "instant teardown" reading -- see the dispatch
sidecar for this chunk.

MUTATING refusal: this module refuses (raises `MutatingOpRefusal`, fails
BEFORE any subprocess spawns) any op whose classification
(`coordinator_core.authz.classification.OP_CLASSIFICATION`) is not exactly
`OpClass.COMPUTE_ONLY` -- including an op absent from the map entirely,
matching the classification module's own fail-closed "ambiguous classifies
MUTATING" discipline. Firing MUTATING ops concurrently on a live shared
worktree would land real, uncoordinated commits (see the
`DISPATCH_TIMEOUT_SECS` comment block in `coordinator_core/ipc.py`: a
client-side timeout does not stop a MUTATING handler thread from
committing after the caller has given up).

Escape hatch (the load-bearing requirement): `read_machine_state()` (default
implementation, psutil-backed -- see below) is called before EVERY wave, and
`evaluate_escape_hatch()` decides abort/proceed against caller-supplied
floors/caps. Fails CLOSED: an unreadable machine state (`MachineState
.readable is False`) is treated as an abort, never as "assume headroom".

Why psutil, not a raw `Get-CIMInstance`/`Get-Process` PowerShell subprocess:
`psutil` is already a required, declared engine dependency
(`pyproject.toml`) and the canonical cross-platform liveness mechanism this
repo already uses in preference to POSIX signal probing on Windows
(`coordinator_core/session/core.py` -- "`kill -0` lies about native Windows
PIDs"). It reads free RAM / CPU / process count in-process, with no extra
subprocess spawn per escape-hatch check (cheaper, and avoids adding PowerShell
cold-start noise to the very box-pressure reading the escape hatch depends
on). `read_machine_state` is injectable so tests never spawn anything, real
or PowerShell.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md,
docs/wiki/machine-load-norm.md.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.timer import BenchmarkSampleInvalid, time_invocation

DEFAULT_LEVELS: List[int] = [1]
"""Lowest dial ONLY -- never default to a higher concurrency level."""

DEFAULT_N_PER_LEVEL = 5
"""Waves per level (post-warm-up N; kept small -- 'brief', per the handoff's
PM-granted conditions). Each wave spawns exactly `level` concurrent
invocations, so total spawns at a level = n_per_level * level."""

DEFAULT_PER_LEVEL_WALL_CAP_S = 60.0
"""Wall-clock cap for one level's whole set of waves."""

DEFAULT_TOTAL_RUN_CAP_S = 180.0
"""Wall-clock cap for the whole multi-level run."""

DEFAULT_MIN_FREE_RAM_GB = 4.0
"""Conservative free-RAM floor -- a 50-70 concurrent-LLM box (see
docs/wiki/machine-load-norm.md) can consume RAM fast; abort well before
exhaustion rather than at it."""

DEFAULT_MAX_CPU_PERCENT = 85.0
"""CPU-utilization ceiling (percent, 0-100) above which the box is judged
under pressure and the run aborts."""

DEFAULT_MAX_PROCESS_COUNT = 900
"""Conservative process-count ceiling for a box already averaging 50-70
concurrent LLMs, each with its own agent/tool-process tree."""

_MB_PER_WORKER = 150
"""Doctrine constant (docs/wiki/machine-load-norm.md, docs/reference/
test-tiers.md): parallelism cap = min(physical_cores/2, usable_RAM_GB*1024/150MB)."""


class MutatingOpRefusal(RuntimeError):
    """Raised when the target op is not affirmatively COMPUTE_ONLY.

    Fail-closed: an op absent from OP_CLASSIFICATION entirely refuses too --
    this mirrors coordinator_core.authz.classification's own "ambiguous
    classifies MUTATING" discipline, it does not introduce a second one.
    """


class LevelExceedsCapError(RuntimeError):
    """Raised when a requested concurrency level exceeds the computed
    parallelism cap and no override was supplied."""


@dataclass(frozen=True)
class MachineState:
    """One point-in-time reading of machine pressure.

    `readable=False` means the reading itself failed (e.g. psutil raised) --
    callers MUST treat this as "cannot prove headroom", never as "assume
    headroom is fine" (fail-closed escape-hatch contract).
    """

    readable: bool
    free_ram_gb: Optional[float] = None
    cpu_percent: Optional[float] = None
    process_count: Optional[int] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "readable": self.readable,
            "free_ram_gb": self.free_ram_gb,
            "cpu_percent": self.cpu_percent,
            "process_count": self.process_count,
            "error": self.error,
            "timestamp": self.timestamp,
        }


def read_machine_state() -> MachineState:
    """Default machine-state reader -- psutil-backed, cross-platform
    (including native Windows; see module docstring for why psutil over a
    PowerShell subprocess). Never raises: any failure is captured into
    `MachineState(readable=False, error=...)` so the escape hatch can fail
    closed instead of propagating an exception mid-wave.
    """
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover -- psutil is a declared,
        # always-required dependency (pyproject.toml); this branch only
        # fires on a broken/incomplete install, which is exactly a case the
        # escape hatch must fail closed on, not silently proceed past.
        return MachineState(readable=False, error=f"psutil unavailable: {exc!r}")

    try:
        vm = psutil.virtual_memory()
        free_ram_gb = vm.available / (1024 ** 3)
        cpu_percent = psutil.cpu_percent(interval=0.1)
        process_count = len(psutil.pids())
    except Exception as exc:  # noqa: BLE001 -- any read failure fails closed
        return MachineState(readable=False, error=f"{type(exc).__name__}: {exc}")

    return MachineState(
        readable=True,
        free_ram_gb=free_ram_gb,
        cpu_percent=cpu_percent,
        process_count=process_count,
    )


def evaluate_escape_hatch(
    state: MachineState,
    min_free_ram_gb: float,
    max_cpu_percent: float,
    max_process_count: int,
) -> tuple[bool, str]:
    """Returns (ok, reason). ok=False means abort the run.

    Fails CLOSED: `state.readable is False` is always `(False, ...)` --
    an unreadable machine state can never be interpreted as headroom.
    """
    if not state.readable:
        return False, f"machine state unreadable: {state.error}"
    if state.free_ram_gb is not None and state.free_ram_gb < min_free_ram_gb:
        return False, f"free RAM {state.free_ram_gb:.2f}GB < floor {min_free_ram_gb:.2f}GB"
    if state.cpu_percent is not None and state.cpu_percent > max_cpu_percent:
        return False, f"CPU {state.cpu_percent:.1f}% > cap {max_cpu_percent:.1f}%"
    if state.process_count is not None and state.process_count > max_process_count:
        return False, f"process count {state.process_count} > cap {max_process_count}"
    return True, "ok"


def compute_parallelism_cap(physical_cores: int, usable_ram_gb: float) -> int:
    """min(physical_cores/2, usable_RAM_GB*1024/150MB), floored, minimum 1.

    Doctrine formula -- docs/wiki/machine-load-norm.md /
    docs/reference/test-tiers.md. Recomputed per box, never hardcoded.
    """
    if physical_cores < 1:
        raise ValueError(f"compute_parallelism_cap: physical_cores must be >= 1, got {physical_cores!r}")
    if usable_ram_gb <= 0:
        raise ValueError(f"compute_parallelism_cap: usable_ram_gb must be > 0, got {usable_ram_gb!r}")
    by_cores = physical_cores / 2.0
    by_ram = (usable_ram_gb * 1024.0) / _MB_PER_WORKER
    return max(1, int(min(by_cores, by_ram)))


def default_physical_cores() -> int:
    """Best-effort physical-core count; falls back to os.cpu_count() (logical)
    if psutil is unavailable or returns None (some virtualized/CI hosts)."""
    try:
        import psutil

        cores = psutil.cpu_count(logical=False)
        if cores:
            return cores
    except ImportError:
        pass
    return os.cpu_count() or 1


def default_usable_ram_gb() -> float:
    """Best-effort total system RAM in GB via psutil; fails loud (raises) if
    psutil is unavailable -- there is no safe fallback for this figure and a
    silently wrong cap defeats the whole escape-hatch contract."""
    import psutil

    return psutil.virtual_memory().total / (1024 ** 3)


def validate_levels(levels: List[int], cap: int, override_cap: bool) -> None:
    """Fails loud on: empty list, non-positive/duplicate levels, non-ascending
    order, or any level above `cap` (unless `override_cap`)."""
    if not levels:
        raise ValueError("validate_levels: levels must be non-empty")
    if len(set(levels)) != len(levels):
        raise ValueError(f"validate_levels: duplicate levels in {levels!r}")
    if list(levels) != sorted(levels):
        raise ValueError(f"validate_levels: levels must be ascending, got {levels!r}")
    for level in levels:
        if level < 1:
            raise ValueError(f"validate_levels: level must be >= 1, got {level!r}")
        if level > cap and not override_cap:
            raise LevelExceedsCapError(
                f"validate_levels: level {level} exceeds computed parallelism cap {cap} "
                "-- pass override_cap=True to force it"
            )


def refuse_if_not_compute_only(op: str) -> None:
    """Fails loud (MutatingOpRefusal) BEFORE any subprocess spawns unless
    `op` is affirmatively OpClass.COMPUTE_ONLY."""
    classification = OP_CLASSIFICATION.get(op)
    if classification is not OpClass.COMPUTE_ONLY:
        raise MutatingOpRefusal(
            f"refuse_if_not_compute_only: op={op!r} classification={classification!r} "
            "is not affirmatively COMPUTE_ONLY -- refusing to probe it concurrently"
        )


def _percentile(sorted_samples: List[float], pct: float) -> float:
    """Nearest-rank-with-interpolation percentile -- same convention as
    coordinator_core.benchmarks.harness._percentile (not imported directly to
    avoid a cross-module coupling for a five-line pure function; kept
    byte-for-byte identical in behavior)."""
    if not sorted_samples:
        raise ValueError("_percentile: empty sample list")
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = pct / 100.0 * (len(sorted_samples) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_samples) - 1)
    frac = rank - lower
    return sorted_samples[lower] + (sorted_samples[upper] - sorted_samples[lower]) * frac


@dataclass
class LevelResult:
    level: int
    samples_ms: List[float]
    invalid_count: int
    state_at_wave_start: List[dict]
    aborted: bool
    abort_reason: Optional[str]

    def summary(self) -> dict:
        if not self.samples_ms:
            return {
                "level": self.level,
                "n": 0,
                "invalid_count": self.invalid_count,
                "aborted": self.aborted,
                "abort_reason": self.abort_reason,
            }
        sorted_samples = sorted(self.samples_ms)
        return {
            "level": self.level,
            "n": len(sorted_samples),
            "min": sorted_samples[0],
            "p50": _percentile(sorted_samples, 50),
            "p90": _percentile(sorted_samples, 90),
            "p95": _percentile(sorted_samples, 95),
            "p99": _percentile(sorted_samples, 99),
            "max": sorted_samples[-1],
            "mean": statistics.mean(sorted_samples),
            "raw_samples_ms": sorted_samples,
            "invalid_count": self.invalid_count,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "machine_state_at_waves": self.state_at_wave_start,
        }


def run_probe(
    op: str,
    params_json: str,
    repo: Optional[str],
    levels: Optional[List[int]] = None,
    n_per_level: int = DEFAULT_N_PER_LEVEL,
    per_level_wall_cap_s: float = DEFAULT_PER_LEVEL_WALL_CAP_S,
    total_run_cap_s: float = DEFAULT_TOTAL_RUN_CAP_S,
    min_free_ram_gb: float = DEFAULT_MIN_FREE_RAM_GB,
    max_cpu_percent: float = DEFAULT_MAX_CPU_PERCENT,
    max_process_count: int = DEFAULT_MAX_PROCESS_COUNT,
    override_cap: bool = False,
    machine_state_reader: Callable[[], MachineState] = read_machine_state,
    physical_cores: Optional[int] = None,
    usable_ram_gb: Optional[float] = None,
) -> dict:
    """Run the concurrency-dial probe over `levels` for one COMPUTE_ONLY op.

    Refuses (raises) before any spawn if `op` is not affirmatively
    COMPUTE_ONLY, or if any requested level exceeds the computed
    parallelism cap without `override_cap`. Checks the escape hatch
    (`machine_state_reader` + `evaluate_escape_hatch`) before every wave AND
    before every level; aborts the WHOLE run on the first failing check and
    returns partial results (levels already completed, plus the aborted
    level's samples so far) -- an aborted run's data is still data.

    A `KeyboardInterrupt` mid-run is caught, stops submitting further work,
    and returns whatever has completed so far (see module docstring's
    Ctrl-C / teardown note for the child-process-lifetime caveat).
    """
    levels = list(DEFAULT_LEVELS) if levels is None else list(levels)

    refuse_if_not_compute_only(op)

    physical_cores = physical_cores if physical_cores is not None else default_physical_cores()
    usable_ram_gb = usable_ram_gb if usable_ram_gb is not None else default_usable_ram_gb()
    cap = compute_parallelism_cap(physical_cores, usable_ram_gb)
    validate_levels(levels, cap, override_cap)

    run_started = time.monotonic()
    level_results: List[LevelResult] = []
    aborted_overall = False
    abort_reason: Optional[str] = None

    try:
        for level in levels:
            if time.monotonic() - run_started > total_run_cap_s:
                aborted_overall = True
                abort_reason = f"total_run_cap_s={total_run_cap_s} exceeded before level {level}"
                break

            state = machine_state_reader()
            ok, reason = evaluate_escape_hatch(state, min_free_ram_gb, max_cpu_percent, max_process_count)
            if not ok:
                aborted_overall = True
                abort_reason = f"escape hatch tripped before level {level}: {reason}"
                break

            level_started = time.monotonic()
            samples: List[float] = []
            invalid_count = 0
            wave_states: List[dict] = [state.to_dict()]
            level_aborted = False
            level_abort_reason: Optional[str] = None

            for _ in range(n_per_level):
                if time.monotonic() - level_started > per_level_wall_cap_s:
                    level_aborted = True
                    level_abort_reason = f"per_level_wall_cap_s={per_level_wall_cap_s} exceeded"
                    break
                if time.monotonic() - run_started > total_run_cap_s:
                    level_aborted = True
                    level_abort_reason = f"total_run_cap_s={total_run_cap_s} exceeded mid-level"
                    break

                wave_state = machine_state_reader()
                wave_states.append(wave_state.to_dict())
                ok, reason = evaluate_escape_hatch(
                    wave_state, min_free_ram_gb, max_cpu_percent, max_process_count
                )
                if not ok:
                    level_aborted = True
                    level_abort_reason = f"escape hatch tripped mid-level {level}: {reason}"
                    break

                with ThreadPoolExecutor(max_workers=level) as executor:
                    futures = [
                        executor.submit(time_invocation, op, params_json, repo) for _ in range(level)
                    ]
                    for fut in futures:
                        try:
                            samples.append(fut.result())
                        except BenchmarkSampleInvalid:
                            invalid_count += 1

            level_results.append(
                LevelResult(
                    level=level,
                    samples_ms=samples,
                    invalid_count=invalid_count,
                    state_at_wave_start=wave_states,
                    aborted=level_aborted,
                    abort_reason=level_abort_reason,
                )
            )
            if level_aborted:
                aborted_overall = True
                abort_reason = level_abort_reason
                break
    except KeyboardInterrupt:
        aborted_overall = True
        abort_reason = "KeyboardInterrupt"

    return {
        "op": op,
        "levels_requested": levels,
        "parallelism_cap": cap,
        "override_cap": override_cap,
        "aborted": aborted_overall,
        "abort_reason": abort_reason,
        "total_wall_s": time.monotonic() - run_started,
        "level_results": [lr.summary() for lr in level_results],
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m coordinator_core.benchmarks.concurrency_probe",
        description=(
            "Bounded, escape-hatch-guarded concurrency-dial latency probe over one "
            "COMPUTE_ONLY op. Defaults to the LOWEST dial only -- pass --levels to "
            "escalate explicitly."
        ),
    )
    parser.add_argument("--op", type=str, default="ping", help="COMPUTE_ONLY op to probe.")
    parser.add_argument(
        "--levels", type=str, default="1", help="Comma-separated ascending concurrency levels, e.g. 1,2,4,8."
    )
    parser.add_argument("--n-per-level", type=int, default=DEFAULT_N_PER_LEVEL)
    parser.add_argument("--per-level-wall-cap-s", type=float, default=DEFAULT_PER_LEVEL_WALL_CAP_S)
    parser.add_argument("--total-run-cap-s", type=float, default=DEFAULT_TOTAL_RUN_CAP_S)
    parser.add_argument("--min-free-ram-gb", type=float, default=DEFAULT_MIN_FREE_RAM_GB)
    parser.add_argument("--max-cpu-percent", type=float, default=DEFAULT_MAX_CPU_PERCENT)
    parser.add_argument("--max-process-count", type=int, default=DEFAULT_MAX_PROCESS_COUNT)
    parser.add_argument(
        "--override-cap",
        action="store_true",
        help="Allow a requested level above the computed parallelism cap.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Path to write the JSON result to (also always printed to stdout).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint. Resolves `--op`'s params/`--repo` via the shared
    op_fixtures registry (same fixture materialization as harness.run()) so
    a worktree-scoped op (e.g. ceremony.session_instructions) gets real
    params, not the bare-op default. Materializes at most one fixture repo
    for the whole CLI invocation and always tears it down, even on abort."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]

    if args.op not in op_fixtures.COMPUTE_ONLY_FIXTURES:
        raise ValueError(
            f"concurrency_probe main: --op {args.op!r} is not in the known "
            f"COMPUTE_ONLY fixture set: {sorted(op_fixtures.COMPUTE_ONLY_FIXTURES)!r}"
        )
    entry = op_fixtures.COMPUTE_ONLY_FIXTURES[args.op]

    worktree_root: Optional[Path] = None
    try:
        if entry["scope"] == "bare":
            params_json = entry["params_json"]
            repo_arg: Optional[str] = None
        else:
            worktree_root = op_fixtures.materialize_fixture_repo()
            params_json = op_fixtures.params_json_for(args.op, worktree_root)
            repo_path = op_fixtures.repo_arg_for(args.op, worktree_root)
            repo_arg = str(repo_path) if repo_path is not None else None

        result = run_probe(
            op=args.op,
            params_json=params_json,
            repo=repo_arg,
            levels=levels,
            n_per_level=args.n_per_level,
            per_level_wall_cap_s=args.per_level_wall_cap_s,
            total_run_cap_s=args.total_run_cap_s,
            min_free_ram_gb=args.min_free_ram_gb,
            max_cpu_percent=args.max_cpu_percent,
            max_process_count=args.max_process_count,
            override_cap=args.override_cap,
        )
    finally:
        if worktree_root is not None:
            import shutil

            shutil.rmtree(worktree_root, ignore_errors=True)

    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out is not None:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")

    print(payload)
    return 0 if not result["aborted"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
