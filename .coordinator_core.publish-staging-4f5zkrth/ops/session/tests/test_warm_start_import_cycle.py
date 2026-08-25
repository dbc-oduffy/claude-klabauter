"""
coordinator_core.ops.session.tests.test_warm_start_import_cycle

Regression pin: `session.warm_start` must register even when
`coordinator_core.warm.client` is imported FIRST.

Found by dogfooding, not by the suite. `warm/client.py` imports
`ops.ceremony.detached_spawn`, which triggers the `coordinator_core.ops`
package's eager import-all, which reaches `ops/session/warm_start.py`. When
that module imported `SERVER_ENTRY_SCRIPT` from `warm.client` at module
scope, it met a partially-initialized `warm.client` and raised ImportError --
so the op silently failed to register and the engine could never be started
by the SessionStart path.

The import order is what makes this real rather than theoretical: importing
`warm_start` first (what every other test in this directory does) succeeds,
which is why the existing suite was green. Importing `warm.client` first is
the CLIENT PREAMBLE'S OWN ORDER -- the actual production hot path -- so the
failing order was the only one that shipped.

These tests run the import in a SUBPROCESS because import cycles are
order-dependent and a module already resident in the parent's `sys.modules`
cannot reproduce one.

Spawn ratchet C2 disposition: TIER -- a clean interpreter IS the property
under test (an already-imported module cannot reproduce an import-order
cycle), matching this chunk's brief example verbatim.
"""

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="X:/project-makima",
    )


def test_warm_start_registers_when_client_imported_first():
    result = _run(
        "from coordinator_core.warm import client\n"
        "import coordinator_core.ops\n"
        "from coordinator_core.ipc import get_op_handler\n"
        "assert get_op_handler('session.warm_start') is not None, 'op not registered'\n"
        "print('OK')\n"
    )
    assert "OK" in result.stdout, (
        "session.warm_start failed to register when warm.client was imported "
        f"first.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "circular import" not in result.stderr
    assert "FAILED to import" not in result.stderr


def test_warm_start_registers_when_imported_first():
    """The order the rest of this directory's tests use. Green before the fix
    too -- kept so a future change that breaks BOTH orders is distinguishable
    from one that breaks only the production order."""
    result = _run(
        "import coordinator_core.ops.session.warm_start\n"
        "from coordinator_core.ipc import get_op_handler\n"
        "assert get_op_handler('session.warm_start') is not None\n"
        "print('OK')\n"
    )
    assert "OK" in result.stdout, result.stderr
