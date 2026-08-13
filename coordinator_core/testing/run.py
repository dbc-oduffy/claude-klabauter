"""
coordinator_core.testing.run — subprocess executor + ThreadPool aggregation.

Purpose: consumes C1's `collect.discover()` `Suite` list and invokes each
suite via the correct interpreter/runner (DEC-3..6), collecting a
`SuiteResult` per suite. py-native suites are batched into a SINGLE pytest
invocation over all collected `test_*.py` paths (DEC-5) rather than one
spawn per file. Dispatch is parallelized via `ThreadPoolExecutor` (DEC-7,
mirroring `coordinator_core/coverage.py`'s `_resolve`/`pool.map` idiom) —
never `ProcessPoolExecutor`/`multiprocessing`.

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: pln-claude-klabauter-python-full-test-runner-f8ca5a § C2 (DEC-3..8, DEC-10, Finding 5)

Negative-spec:
    - Does NOT spawn one subprocess per py-native file — all `test_*.py`
      paths discovered by C1 are passed to a single
      `["python3", "-m", "pytest", *paths]` invocation (DEC-5).
    - Does NOT use `ProcessPoolExecutor`/`multiprocessing` for parallelism —
      house pattern is `ThreadPoolExecutor` dispatching `subprocess.run`
      (DEC-7).
    - Does NOT mask a missing runner (node absent, subprocess rc 127) as a
      skip — it surfaces as a FAILED `SuiteResult`, never silently omitted
      from the aggregate (DEC-10).
    - Does NOT treat a `subprocess.TimeoutExpired` as a skip — it is
      surfaced as a FAILED `SuiteResult` with a synthetic non-zero exit
      code (Finding 5).
"""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from coordinator_core.testing.collect import Suite
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_TIMEOUT: int = 300

# Portable Windows console-suppression flag, mirroring coverage.py:101-105 —
# every dispatched suite spawn (and the batched py-native invocation) must
# not pop a visible console window on Windows.
_NO_CONSOLE: dict = no_console_creationflags()

# DEC-3/4/5/6: per-family invocation command builders. py-native is handled
# separately (batched — see `_build_py_native_command`), never through this
# per-suite table.
_RUNNER_KIND_TO_COMMAND = {
    "node": lambda path: ["node", "--test", str(path)],
    "python3": lambda path: ["python3", str(path)],
}


@dataclass(frozen=True)
class SuiteResult:
    """The outcome of running one dispatched unit (a `Suite`, or the single
    batched py-native invocation).

    `suite` is `None` for the batched py-native result, since that single
    invocation covers N `Suite` objects at once — there is no single `Suite`
    to attribute it to.
    """

    suite: Suite | None
    exit_code: int
    duration: float
    captured_output: str


def _run_subprocess(
    command: Sequence[str], repo_root: Path, timeout: int
) -> tuple[int, float, str]:
    """Invoke `command`, returning (exit_code, duration, captured_output).

    On `TimeoutExpired` the suite is surfaced as FAILED (synthetic exit code
    124, the POSIX timeout-command convention) rather than skipped (Finding 5).
    On a missing runner binary (`FileNotFoundError`, e.g. `node` not on
    PATH) the suite is surfaced as FAILED with the POSIX
    command-not-found convention (rc 127) rather than skipped (DEC-10). Any
    other `OSError` at spawn time (`PermissionError`, `NotADirectoryError`,
    etc.) is likewise surfaced as a single FAILED suite (rc 127) rather than
    propagating and aborting the whole batch.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            **_NO_CONSOLE,
        )
        duration = time.monotonic() - start
        return proc.returncode, duration, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        captured = ""
        if exc.output:
            captured = exc.output if isinstance(exc.output, str) else exc.output.decode(
                "utf-8", errors="replace"
            )
        captured += f"\n[TIMEOUT] suite exceeded timeout={timeout}s\n"
        return 124, duration, captured
    except FileNotFoundError as exc:
        duration = time.monotonic() - start
        captured = f"[RUNNER MISSING] {command[0]!r} not found: {exc}\n"
        return 127, duration, captured
    except OSError as exc:
        # Any other subprocess-spawn-time OS error (PermissionError,
        # NotADirectoryError, a transient OSError from the process table,
        # etc.) degrades to a single FAILED suite rather than propagating
        # uncaught through ThreadPoolExecutor.map and aborting the whole
        # batch (DEC-10 fail-loud-per-suite, not fail-the-run).
        duration = time.monotonic() - start
        captured = f"[SPAWN ERROR] {command[0]!r}: {exc}\n"
        return 127, duration, captured


def _build_py_native_command(paths: Sequence[Path]) -> list[str]:
    """DEC-5: a single batched pytest invocation over ALL py-native paths."""
    return ["python3", "-m", "pytest", *[str(p) for p in paths]]


def _dispatch_one(suite: Suite, repo_root: Path, timeout: int) -> SuiteResult:
    builder = _RUNNER_KIND_TO_COMMAND[suite.runner_kind]
    command = builder(suite.path)
    exit_code, duration, captured = _run_subprocess(command, repo_root, timeout)
    return SuiteResult(
        suite=suite, exit_code=exit_code, duration=duration, captured_output=captured
    )


def _dispatch_py_native_batch(
    suites: Sequence[Suite], repo_root: Path, timeout: int
) -> SuiteResult:
    command = _build_py_native_command([s.path for s in suites])
    exit_code, duration, captured = _run_subprocess(command, repo_root, timeout)
    return SuiteResult(
        suite=None, exit_code=exit_code, duration=duration, captured_output=captured
    )


def run_suites(
    suites: Sequence[Suite],
    repo_root: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
    jobs: int | None = None,
) -> list[SuiteResult]:
    """Run every `Suite`, batching py-native (DEC-5), parallelized via
    `ThreadPoolExecutor` (DEC-7). Returns one `SuiteResult` per dispatched
    unit — all non-py-native suites 1:1, plus at most one batched result for
    all py-native suites combined.
    """
    root = Path(repo_root)

    py_native = [s for s in suites if s.runner_kind == "pytest"]
    other = [s for s in suites if s.runner_kind != "pytest"]

    # Each dispatched unit is a zero-arg callable so both the py-native batch
    # and the per-suite invocations can share one ThreadPoolExecutor.
    jobs_list: list = []
    if py_native:
        jobs_list.append(
            lambda batch=py_native: _dispatch_py_native_batch(batch, root, timeout)
        )
    for suite in other:
        jobs_list.append(
            lambda s=suite: _dispatch_one(s, root, timeout)
        )

    if not jobs_list:
        return []

    max_workers = _resolve_worker_count(len(jobs_list), jobs)
    if max_workers <= 1:
        return [job() for job in jobs_list]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda job: job(), jobs_list))


def _default_max_workers() -> int:
    return max(1, (os.cpu_count() or 1) // 2)


def _resolve_worker_count(unit_count: int, jobs: int | None) -> int:
    cap = jobs if jobs is not None else _default_max_workers()
    cap = max(1, cap)
    return min(unit_count, cap)


def overall_ok(results: Sequence[SuiteResult]) -> bool:
    """DEC-10: aggregate success — every dispatched unit exited 0. A missing
    runner (rc 127) or a timeout (rc 124) is a FAILED result, never masked."""
    return all(r.exit_code == 0 for r in results)
