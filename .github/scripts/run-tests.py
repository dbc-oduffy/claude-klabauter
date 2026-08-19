#!/usr/bin/env python3
"""Run the engine's fast test tier with a BOUNDED worker count.

Deliberately not named ``check-*``/``validate-*``: run-all-checks.py discovers
those by convention and is the fast publish gate. The suite is a separate CI
job — a publish decision must not hinge on a multi-thousand-test run, and the
suite must not be skipped just because the gate needs to be quick.

TIER SELECTION IS READ OFF pyproject.toml, NEVER OFF PROSE
----------------------------------------------------------
Both ``testpaths`` and the marker set drift on every edit to that config, and
prose describing them has gone stale repeatedly in this project's history —
once badly enough that a triage pass wrote off real failures as an unwired
corpus. So this script parses ``[tool.pytest.ini_options]`` at runtime and
builds ``-m`` from the tier-excluding markers that are ACTUALLY DECLARED there.
Add or remove a marker in pyproject and this follows it with no edit here.

WORKER COUNT IS A DERIVATION. DO NOT "SIMPLIFY" IT BACK TO `-n auto`
---------------------------------------------------------------------
`-n auto` takes the host's physical core count with NO CEILING. It is unbounded
and has killed a session in this project — this is a recorded incident, not a
precaution. The cap is:

    min(physical_cores / 2, usable_RAM_MB / 150MB)

The halving is not a fudge factor. This tier spawns subprocesses and averages
several live processes per xdist worker, so one worker per core oversubscribes
the CPU several-fold. That is the suite's own behaviour and therefore true on
every platform, including whatever runner size GitHub hands us today. The RAM
leg is the second binding constraint at ~150MB resident per worker.

Both inputs are measured at runtime rather than hardcoded, so the derivation
stays correct when the runner size changes under us. On a small runner the
formula legitimately yields 1, in which case xdist is skipped entirely — `-n 1`
pays the distribution overhead for none of the benefit.

EMPTY-SUITE BEHAVIOUR (mid-bootstrap)
--------------------------------------
The engine tree publishes after the harness does. Until it lands there is
nothing to run. That is reported as an explicit SKIP with a reason and exits 0 —
not a hard error (which would red-flag a correct intermediate state) and not a
silent green (which would let a genuinely empty suite masquerade as a passing
one). Pass ``--require-tests`` to make an empty suite a FAILURE instead; the
workflow should switch that on once the engine has landed, so "no tests" can
never again be mistaken for "tests passed".

EXIT CONTRACT
  0 — the tier passed, or the suite is empty and --require-tests was not given
  1 — empty suite under --require-tests, or a configuration problem
  N — pytest's own exit code otherwise
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import subprocess
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Markers whose presence in pyproject means "exclude from the fast tier".
# Membership is a policy statement; whether each one applies is read from
# pyproject, so a marker retired there drops out of the expression here.
TIER_EXCLUDED_MARKERS = ("cadence", "pending_fix", "designed_red")

MB_PER_WORKER = 150

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_pytest_config() -> dict:
    if not PYPROJECT.exists():
        print(f"ERROR: {PYPROJECT} not found — cannot determine the test tier.")
        sys.exit(1)
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("tool", {}).get("pytest", {}).get("ini_options", {})


def declared_markers(config: dict) -> set[str]:
    """Marker NAMES declared in pyproject (entries are 'name: description')."""
    return {entry.split(":", 1)[0].strip() for entry in config.get("markers", [])}


def marker_expression(config: dict) -> str:
    declared = declared_markers(config)
    excluded = [m for m in TIER_EXCLUDED_MARKERS if m in declared]
    return " and ".join(f"not {m}" for m in excluded)


def physical_cores() -> int:
    try:
        import psutil
        count = psutil.cpu_count(logical=False)
        if count:
            return count
    except Exception:
        pass
    # No psutil: os.cpu_count() is LOGICAL. Assume SMT and halve rather than
    # over-report, because over-reporting is the failure mode that hurts.
    logical = os.cpu_count() or 2
    return max(1, logical // 2)


def usable_ram_mb() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        pass
    meminfo = pathlib.Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    # Unknown memory: assume the smallest runner we would plausibly be on
    # rather than an optimistic figure.
    return 2048


def worker_count() -> tuple[int, str]:
    cores = physical_cores()
    ram_mb = usable_ram_mb()
    cpu_leg = cores // 2
    ram_leg = ram_mb // MB_PER_WORKER
    workers = max(1, min(cpu_leg, ram_leg))
    reason = (
        f"{cores} physical cores -> cpu leg {cpu_leg}; "
        f"{ram_mb}MB usable RAM / {MB_PER_WORKER}MB -> ram leg {ram_leg}; "
        f"min -> {workers}"
    )
    return workers, reason


def test_roots(config: dict) -> list[pathlib.Path]:
    paths = config.get("testpaths") or ["."]
    return [REPO_ROOT / p for p in paths]


def count_test_files(roots: list[pathlib.Path], config: dict) -> int:
    patterns = config.get("python_files") or ["test_*.py"]
    total = 0
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            total += sum(1 for _ in root.rglob(pattern))
    return total


def emit_summary(text: str) -> None:
    """Surface a line in the GitHub Actions run summary, when running there."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fast test tier with bounded workers.")
    parser.add_argument(
        "--require-tests",
        action="store_true",
        help="Treat an empty suite as a failure. Switch this on once the engine tree has landed.",
    )
    parser.add_argument(
        "pytest_args", nargs="*",
        help="Extra arguments passed through to pytest.",
    )
    # parse_known_args, not parse_args: pytest passthrough arguments are
    # overwhelmingly flag-shaped (-v, -x, --lf), and parse_args would reject
    # them as unrecognized options before they ever reached pytest.
    args, passthrough = parser.parse_known_args()
    extra_args = [*args.pytest_args, *passthrough]

    config = load_pytest_config()
    roots = test_roots(config)
    declared = sorted(declared_markers(config))
    expression = marker_expression(config)

    print(f"testpaths (from pyproject): {[str(r.relative_to(REPO_ROOT)) for r in roots]}")
    print(f"markers declared in pyproject: {declared or '(none)'}")
    print(f"fast-tier marker expression: {expression or '(no tier exclusions declared)'}")

    found = count_test_files(roots, config)
    if found == 0:
        message = (
            "SKIPPED: no test files under the configured testpaths. "
            "The engine tree has not been published to this mirror yet."
        )
        print()
        print(message)
        emit_summary(f"**Test suite {message}**")
        if args.require_tests:
            print("--require-tests was given: an empty suite is a failure.")
            return 1
        return 0

    workers, reason = worker_count()
    print(f"test files discovered: {found}")
    print(f"bounded worker derivation: {reason}")

    command = [sys.executable, "-m", "pytest"]
    if expression:
        command += ["-m", expression]
    if workers > 1:
        command += ["-n", str(workers)]
        print(f"running with {workers} xdist workers")
    else:
        # -n 1 pays xdist's distribution overhead for none of its benefit.
        print("running serially: the bounded derivation yielded a single worker")
    command += ["-q", *extra_args]

    print()
    # shlex.join, not ' '.join: the marker expression contains spaces, and an
    # unquoted echo of it is not the command that actually ran.
    print(f"$ {shlex.join(command)}")
    print()

    result = subprocess.run(command, cwd=str(REPO_ROOT), creationflags=NO_WINDOW)
    emit_summary(
        f"Test tier: `{expression or 'all'}`, {found} test files, "
        f"{workers} worker(s) — exit {result.returncode}"
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
