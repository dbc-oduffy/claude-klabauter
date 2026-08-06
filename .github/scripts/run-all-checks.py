#!/usr/bin/env python3
"""Run every validation check in this directory. THE publish gate entrypoint.

CONTRACT — read before changing anything here
---------------------------------------------
percolate's pre-CI gate invokes this exact path in the destination tree:

    <dest>/.github/scripts/run-all-checks.py

and a NON-ZERO EXIT FAILS THE PUBLISH. Three consequences:

  * The path is load-bearing. Renaming or relocating this file silently removes
    the gate — the publisher sees no error, it simply stops checking.
  * The working directory is not ours to assume. The gate may invoke this from
    anywhere, so every check anchors its repo root at ``__file__`` (see
    _repo.py) rather than at cwd. Child processes are still given the repo root
    as cwd for good measure, but no check may depend on it.
  * The exit code is the whole product. A check that cannot run must say so and
    fail, not degrade to a green tick.

Discovery is convention-based: every ``check-*.py`` and ``validate-*.py`` in
this directory runs, excluding this file. Adding a check is adding a file.
Helpers are named with a leading underscore so they are not mistaken for checks.

The test suite is deliberately NOT run from here. Tests are a separate CI job:
the publish gate must stay fast and must not make a publish decision hinge on a
multi-thousand-test run. See run-tests.py.

No shell anywhere in this harness — naked Python, shebang-resolved interpreter,
so it behaves identically on a Windows runner with no bash available.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parents[1]
SELF = pathlib.Path(__file__).resolve().name

PATTERNS = ("check-*.py", "validate-*.py")


def discover_scripts() -> list[pathlib.Path]:
    scripts: set[pathlib.Path] = set()
    for pattern in PATTERNS:
        for path in SCRIPTS_DIR.glob(pattern):
            if path.name == SELF or path.name.startswith("_"):
                continue
            scripts.add(path)
    return sorted(scripts)


def main() -> int:
    scripts = discover_scripts()

    if not scripts:
        # Not a pass. An empty check set means the harness was gutted or the
        # tree is malformed, and the publish gate has verified nothing.
        print("No validation checks found in .github/scripts — refusing to report success.")
        return 1

    print(f"Running {len(scripts)} validation checks against {REPO_ROOT}\n")

    results: list[tuple[str, int, float]] = []

    for script in scripts:
        name = script.stem
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            # Portable console suppression: resolves to CREATE_NO_WINDOW on
            # Windows and to 0 (no-op) everywhere else. The bare constant would
            # raise ValueError off-Windows.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.monotonic() - start
        results.append((name, result.returncode, elapsed))

        status = "PASS" if result.returncode == 0 else "FAIL"
        print(f"  [{status}] {name} ({elapsed:.1f}s)")

        streams = result.stdout if result.returncode == 0 else result.stdout + result.stderr
        for line in streams.strip().splitlines():
            print(f"         {line}")

    passed = sum(1 for _, rc, _ in results if rc == 0)
    failed = len(results) - passed
    total = sum(t for _, _, t in results)

    print()
    print("=" * 56)
    print(f"  {passed} passed, {failed} failed ({total:.1f}s total)")
    print("=" * 56)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
