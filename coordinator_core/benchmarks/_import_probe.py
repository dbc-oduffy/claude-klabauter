"""
coordinator_core.benchmarks._import_probe -- fresh-interpreter import-cost probe.

Purpose: run in a SUBPROCESS (never in-process) as `python3 _import_probe.py <entrypoint>` to
measure the `sys.modules` delta and wall-clock elapsed time of importing a single hot-path
entrypoint module, starting from a fresh interpreter with nothing of the target already loaded.
Prints one line of `<module_count_delta> <elapsed_ms>` to stdout for the caller (see
`import_budget.measure_import_subprocess`) to parse.

Isolation is load-bearing: measuring in-process after the caller (or a sibling import) has
already pulled the target (or its dependencies) into `sys.modules` silently undercounts the
delta. A fresh subprocess is the only way to get a trustworthy `len(sys.modules)` count for a
single entrypoint.

AC11 mechanism deviation: AC11 names `-X importtime` as the measurement command. This probe
instead diffs `set(sys.modules)` around `__import__(entrypoint)` -- a different mechanism,
chosen because `-X importtime`'s own timing/count includes interpreter-startup noise this
sys.modules-delta approach does not; the ceiling this module gates on is a module count, and
the delta is the more precise way to obtain one. Mechanism only -- not a scope reduction from
AC11's intent.

Spec backlink: docs/plans/2026-08-06-windows-hot-path-less-work-per-interpreter.md chunk C4
(AC9, AC11).
"""

from __future__ import annotations

import sys
import time


def main() -> None:
    entrypoint = sys.argv[1]
    before = set(sys.modules)
    t0 = time.perf_counter()
    __import__(entrypoint)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    after = set(sys.modules)
    module_count = len(after - before)
    print(f"{module_count} {elapsed_ms:.4f}")


if __name__ == "__main__":
    main()
