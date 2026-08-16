"""coordinator_core.benchmarks.shim_prototype_forwarder -- THROWAWAY
measurement-only "forwarder" half of the C7 shim prototype. NOT the C8
production forwarder; discard when C8 lands its own.

Purpose: reproduces the SHAPE the plan's shim arm is expected to have --
a forwarder process that spawns a dispatcher process, which performs the
real work -- so `shim_decision_rule`'s shim arm can be measured at all
before C8 exists. See `shim_prototype_dispatcher.py`'s module docstring
for the paired "what this shares / does not share with C8" statement;
the same applies here, one level up: this forwarder does nothing but
spawn `shim_prototype_dispatcher.py` as a child process and propagate its
exit code -- no routing table, no name resolution, no shim registry. A
real C8 forwarder additionally resolves WHICH dispatcher subcommand a
given old entry-point name maps to; this prototype hardcodes that
resolution to a single fixed target because there is nothing yet to
route between.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7.
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DISPATCHER_PATH = os.path.join(_HERE, "shim_prototype_dispatcher.py")


def main() -> int:
    completed = subprocess.run(
        [sys.executable, _DISPATCHER_PATH],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
