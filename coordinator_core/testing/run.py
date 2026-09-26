
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

_NO_CONSOLE: dict = no_console_creationflags()

_RUNNER_KIND_TO_COMMAND = {
    "node": lambda path: ["node", "--test", str(path)],
    "python3": lambda path: ["python3", str(path)],
}


@dataclass(frozen=True)
class SuiteResult:

    suite: Suite | None
    exit_code: int
    duration: float
    captured_output: str


def _run_subprocess(
    command: Sequence[str], repo_root: Path, timeout: int
) -> tuple[int, float, str]:
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
        duration = time.monotonic() - start
        captured = f"[SPAWN ERROR] {command[0]!r}: {exc}\n"
        return 127, duration, captured


def _build_py_native_command(paths: Sequence[Path]) -> list[str]:
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
    root = Path(repo_root)

    py_native = [s for s in suites if s.runner_kind == "pytest"]
    other = [s for s in suites if s.runner_kind != "pytest"]

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
    return all(r.exit_code == 0 for r in results)
