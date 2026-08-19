"""
coordinator_core.session.tests.test_work_state_imports

Purpose: pins AC13 of docs/plans/2026-08-19-fleet-work-state-who-holds-
which-baton.md, chunk C1a — `coordinator_core.session.work_state` must
import STANDALONE, with `coordinator_core.ops` absent from `sys.modules`.

THE REAL CYCLE RUNS THROUGH `coordinator_core.ops`, NOT `pickup_assemble`
(see the chunk's own cycle trace). Once `session/holder_evidence.py` is
imported by `session/work_state.py`, a module-level `coordinator_core.ops.*`
import anywhere in that chain executes `ops/__init__.py`, which by default
runs `_eager_import_all()` over `_EAGER_OP_MODULES`. A future C3 adding
`ops/session_work_state.py` to that list (importing `session.work_state`)
closes the loop:

    session.work_state -> session.holder_evidence -> (a module-level
    ops.* import) -> ops.__init__._eager_import_all() ->
    ops.session_work_state -> session.work_state (partially initialized)
    -> ImportError

...caught per-module inside `_eager_import_all()` and re-raised at
dispatch time, in a caller-order-dependent way, with a misleading error
(state/lessons/2026-08-14-pre-existing-and-unrelated-answers-prove-
8a05492b0e34.yaml describes the identical shape for `session_ledger.
aggregate_chain_loe`). "The suite passes" does not prove this: the suite
imports things in an order that hides the defect — the assertion below
does not rely on suite import order at all, since it runs in a fresh
subprocess.

This test runs in a SUBPROCESS (same discipline as `coordinator_core/ops/
session/tests/test_warm_start_import_cycle.py`) because an import cycle is
order-dependent and a module already resident in the parent process's
`sys.modules` cannot reproduce one.

Behaviour tests for `work_state.py`'s helpers belong to C1b, in a
different file — this file stays runnable without importing any of that
chunk's fixtures, which is the import order that would hide the defect
this test exists to catch.
"""

import subprocess
import sys

from coordinator_core.win_portability import no_console_creationflags


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="X:/claude-klabauter",
        **no_console_creationflags(),
    )


def test_session_work_state_imports_standalone_without_ops():
    result = _run(
        "import sys\n"
        "assert 'coordinator_core.ops' not in sys.modules\n"
        "import coordinator_core.session.work_state\n"
        "assert 'coordinator_core.session.work_state' in sys.modules\n"
        "print('OK')\n"
    )
    assert "OK" in result.stdout, (
        "coordinator_core.session.work_state failed to import standalone "
        f"(coordinator_core.ops absent from sys.modules).\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "circular import" not in result.stderr
    assert "ImportError" not in result.stderr


def test_session_holder_evidence_imports_standalone_without_ops():
    """The same assertion at the module `session.work_state` re-exports
    through — `holder_evidence.py`'s relocation into `session/` is what
    introduces the module-level-`ops`-import risk in the first place (its
    `_resolve_transcript` import must stay function-local; see that
    module's `holder_evidence()` docstring)."""
    result = _run(
        "import sys\n"
        "assert 'coordinator_core.ops' not in sys.modules\n"
        "import coordinator_core.session.holder_evidence\n"
        "assert 'coordinator_core.session.holder_evidence' in sys.modules\n"
        "print('OK')\n"
    )
    assert "OK" in result.stdout, (
        "coordinator_core.session.holder_evidence failed to import standalone "
        f"(coordinator_core.ops absent from sys.modules).\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "circular import" not in result.stderr
    assert "ImportError" not in result.stderr
