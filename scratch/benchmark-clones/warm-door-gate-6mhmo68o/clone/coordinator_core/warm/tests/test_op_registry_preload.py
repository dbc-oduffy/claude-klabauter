"""The warm server's op-registry preload must actually populate the registry.

Spec backlink: state/dispatch-briefs/2026-08-22-the-import-path-costs-nothing/C17.md.
Found by the AST side-effect-import sweep, 2026-08-22.

WHAT THIS PROTECTS. `warm/server.py::_preload_op_registry` used to do a bare
`import coordinator_core.ops` inside a try/except that prints to stderr on
failure. Under the lazy-only op package (`ops/__init__.py` no longer
eager-imports anything at package-init time), that bare import registers
NOTHING and raises nothing -- the except never fires, nothing is printed, and
the preload silently becomes a no-op while the server reports healthy. Every
dispatch then takes `ipc.get_op_handler`'s SAFE FALLBACK on its first registry
miss, paying the ~700ms eager-import cost on the first real caller instead of
on boot -- exactly the cost the preload exists to absorb.

The fix calls `coordinator_core.ops._eager_import_all()` explicitly. This test
asserts the registry is actually populated after preload runs, in a fresh
interpreter, so a silent no-op is caught here rather than surfacing later as
unattributed first-dispatch latency.

NEGATIVE-SPEC:
  - Does NOT touch the warm CLIENT. `test_client_does_not_import_op_registry.py`
    asserts the client stays lazy; this test only exercises the server's
    `_preload_op_registry` function, called directly, not the full election/
    serve_forever path.
  - Does NOT assert timing. Wall-clock is load-dominated on this box; registry
    population count is the stable proxy for the same fact.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_COUNT_AFTER_PRELOAD = (
    "import coordinator_core.warm.server as server;"
    "server._preload_op_registry();"
    "from coordinator_core.ipc import _REGISTRY;"
    "print(len(_REGISTRY))"
)


def _registry_size_after(code: str) -> int:
    """Run in a FRESH interpreter, never this one: pytest has already
    imported much of the tree, so an in-process registry check would read
    the suite's own imports and pass unconditionally."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_preload_populates_the_op_registry():
    registered = _registry_size_after(_COUNT_AFTER_PRELOAD)
    assert registered > 0, (
        "warm.server._preload_op_registry() left coordinator_core.ipc._REGISTRY "
        "empty. Under the lazy-only op package, a bare "
        "`import coordinator_core.ops` registers nothing and raises nothing -- "
        "the preload must call `coordinator_core.ops._eager_import_all()` "
        "explicitly to force full registration."
    )
