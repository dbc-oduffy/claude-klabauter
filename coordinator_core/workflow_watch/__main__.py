"""`python3 -m coordinator_core.workflow_watch` entry point.

`-m` on a PACKAGE executes `__main__.py`, never `__init__.py`. Without this
module the command the PostToolUse advisory tells an EM to paste
(`postuse_advisory_dispatch :: _check_workflow_monitor_arm_sync`) dies with
"cannot be directly executed" before it polls once — and no import-level test
notices, because every other test in this package calls `main`/`_watch`
directly. `tests/test_render_and_cap.py :: test_module_is_executable_via_dash_m`
is the guard that this stays runnable.

Negative-spec: this file holds no logic. Argv parsing, the poll loop and the
exit-code contract live in `__init__.py`; a second copy here would be a second
source of truth for the exit codes a Monitor consumer reads.
"""

from __future__ import annotations

import sys

from coordinator_core.workflow_watch import main

if __name__ == "__main__":
    sys.exit(main())
