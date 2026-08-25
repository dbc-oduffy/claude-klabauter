"""
coordinator_core.benchmarks._import_probe -- fresh-interpreter import-cost probe.

Purpose: run in a SUBPROCESS (never in-process) as `python3 _import_probe.py <entrypoint>` to
measure the `sys.modules` delta and wall-clock elapsed time of importing a single hot-path
entrypoint module, starting from a fresh interpreter with nothing of the target already loaded.
Prints one line of `<module_count_delta> <elapsed_ms> <own_module_count>` to stdout for the
caller (see `import_budget.measure_import_subprocess`) to parse. The third field is the
`coordinator_core.*` share of the delta; the parser tolerates its absence so an older probe
still parses, but this probe always emits it.

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

Spec backlink: pln-windows-hot-path-cost-less-wor-0ec8ea chunk C4
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
    delta = after - before
    module_count = len(delta)
    # Own-vs-stdlib split (2026-08-17): the total is what the ceiling gates, because
    # a stdlib module costs a per-file AV scan on Windows exactly like ours does. But
    # only the own half is ours to shrink, and a breach reached from the OTHER half
    # means the interpreter's stdlib graph moved under a frozen baseline -- which is
    # what actually happened when Python 3.14 routed bz2/lzma through the new
    # `compression` package. Emitting the split here is what stops the next
    # investigator writing a throwaway script to re-derive it, which is the same
    # "every regrowth investigation re-derived it from scratch" complaint this
    # package exists to answer.
    own_module_count = len([m for m in delta if m.split(".", 1)[0] == "coordinator_core"])
    print(f"{module_count} {elapsed_ms:.4f} {own_module_count}")


if __name__ == "__main__":
    main()
