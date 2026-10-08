"""Pre-publish gates: run the spawn-audit, amplification, commit-surface, census and
publish-allowlist gates as one BelowNormal pytest invocation. Exit 0 only when every
gate file passes; each failing file gets one `FAIL <file>: ...` summary line.

The five files are named explicitly (Tier T), so the test-suite invocation guard
classifies the run as scoped and requires no suite mutex.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

GATE_FILES: tuple[str, ...] = (
    "coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py",
    "coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py",
    "coordinator_core/tests/test_commit_surface_budget.py",
    "coordinator_core/ops/generator_census/tests/test_census_oracle.py",
    "coordinator/tests/test_coordinator_bin_publish_allowlist_is_complete.py",
)

# (argv, cwd) -> (returncode, combined output)
Runner = Callable[[Sequence[str], Path], "tuple[int, str]"]

_FAIL_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?\.py)\b")
_SUMMARY_LINE = re.compile(r"^=*\s*(?:\d+ \w+(?:, )?)+.* in [\d.]+s")


def pytest_argv(files: Sequence[str] = GATE_FILES) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider", *files]


def default_runner(argv: Sequence[str], cwd: Path) -> "tuple[int, str]":
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["preexec_fn"] = lambda: os.nice(10)
    proc = subprocess.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_failures(output: str) -> dict[str, int]:
    """Failing gate file -> count of FAILED/ERROR lines naming it."""
    counts: dict[str, int] = {}
    for line in output.splitlines():
        m = _FAIL_LINE.match(line.strip())
        if m:
            name = m.group(1).replace("\\", "/")
            counts[name] = counts.get(name, 0) + 1
    return counts


def pytest_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        if _SUMMARY_LINE.match(line.strip()):
            return line.strip().strip("=").strip()
    return ""


def run_gates(runner: Runner = default_runner, cwd: Path = REPO_ROOT) -> "tuple[int, list[str]]":
    """Return (exit_code, summary_lines); the last line is the pytest tally when present."""
    rc, output = runner(pytest_argv(), cwd)
    failures = parse_failures(output)
    lines = [f"FAIL {path}: {n} failing" for path, n in sorted(failures.items())]
    if rc != 0 and not lines:
        lines.append(f"FAIL pytest: exit {rc}, no per-file failure parsed")
    tally = pytest_summary(output)
    if tally:
        lines.append(tally)
    return (0 if rc == 0 else 1), lines


def main(argv: Sequence[str] | None = None, runner: Runner = default_runner) -> int:
    parser = argparse.ArgumentParser(prog="pre-publish-gates.py", description=__doc__)
    parser.add_argument("--skip-gates", action="store_true", help="Skip the gates (emergency).")
    args = parser.parse_args(argv)
    if args.skip_gates:
        print("pre-publish-gates: SKIPPED via --skip-gates", file=sys.stderr)
        return 0
    rc, lines = run_gates(runner)
    for line in lines:
        print(line)
    print("pre-publish-gates: " + ("PASS" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
