"""coordinator_core.benchmarks.shim_prototype_inprocess -- THROWAWAY
measurement-only in-process shim prototype for the C7 corrected 2nd
measurement. NOT the C8 production shim; discard when C8 lands its own.

Why this exists: the FIRST stage-2 measurement (`shim_prototype_measure.py`
/ `shim_prototype_forwarder.py` / `shim_prototype_dispatcher.py`) measured a
forwarder that `subprocess.run`s a dispatcher child -- two process starts
chained. But `coordinator/lib/resolve-makima/_resolve_makima.py::exec_cli`
-- the SHIPPED forwarder pattern the plan's C8 body says to follow -- does
NOT spawn a second interpreter on Windows: its own docstring documents
rejecting "spawning a second Python interpreter and subprocess.run-ing the
target" in favor of running the target IN-PROCESS via
`runpy.run_path(..., run_name="__main__")` (`_run_target_in_process`,
called from the `os.name == "nt"` branch of `exec_cli`). This module
mirrors that shape: no child process at all, the target script's body runs
inside the ALREADY-RUNNING benchmark interpreter.

What this shares with `_run_target_in_process`: `runpy.run_path` against
the target's file path with `run_name="__main__"`, `sys.argv` swapped to
`[target_path]` for the duration and restored in `finally`, the target's
`SystemExit` caught and its `.code` normalized to an int return (mirroring
`sys.exit`'s own None/non-int-code contract) exactly as that function
does.

What this does NOT share: no per-target resolution ladder, no
`coordinator/bin/`-relative sentinel probing, no fallback-to-live-tree
logic -- this prototype hardcodes ONE fixed target
(`coordinator/bin/plan-assemble.py`, invoked bare -- the SAME target work
the baseline arm (`shim_decision_rule.build_baseline_primitive`) and the
first prototype's dispatcher (`shim_prototype_dispatcher.py`) both reach),
because C7's job is to measure invocation-path cost, not reproduce C8's
routing table.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7
(corrected 2nd measurement).
"""

from __future__ import annotations

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# coordinator_core/benchmarks/ -> coordinator_core/ -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_TARGET_PATH = os.path.join(_REPO_ROOT, "coordinator", "bin", "plan-assemble.py")


def run_in_process() -> int:
    """Runs `coordinator/bin/plan-assemble.py` (bare, no subcommand token --
    same target work the baseline arm performs) IN-PROCESS via
    `runpy.run_path`, mirroring `exec_cli`'s Windows leg
    (`_run_target_in_process` in `coordinator/lib/resolve-makima/
    _resolve_makima.py`) -- no child process, no second interpreter, the
    process image stays the caller's own throughout.

    `sys.argv` and `sys.path` are swapped for the duration of the call and
    restored in `finally` -- `plan-assemble.py`, like every `coordinator/
    bin/` CLI, reads `sys.argv` directly and expects the repo root on
    `sys.path` for its own absolute `coordinator_core` import, exactly the
    two seams `_run_target_in_process`'s own docstring documents needing to
    bridge for an in-process run.

    Returns the target's intended exit code (0 on a target that falls off
    the end without calling `sys.exit`, matching normal process-exit
    semantics; the target's `SystemExit(code)` otherwise, normalized the
    same way `_run_target_in_process` normalizes it).
    """
    original_argv = sys.argv
    original_path = list(sys.path)
    try:
        sys.argv = [_TARGET_PATH]
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        runpy.run_path(_TARGET_PATH, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        sys.stderr.write(str(code) + "\n")
        return 1
    finally:
        sys.argv = original_argv
        sys.path[:] = original_path
    return 0


def main() -> int:
    return run_in_process()


if __name__ == "__main__":
    sys.exit(main())
