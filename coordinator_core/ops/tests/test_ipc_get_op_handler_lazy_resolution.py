"""get_op_handler() must self-resolve through the lazy-import + eager-fallback
path on a registry MISS, exactly as coordinator_core.ipc.dispatch_message
already does -- not a bare `_REGISTRY.get(name)`.

Why this matters: `COORDINATOR_CORE_LAZY_OPS=1` (set by the invoke process
itself, coordinator_core/invoke/__main__.py) imports ONLY the directly
dispatched op's owning module. Any op handler that resolves a SIBLING op by
key via `get_op_handler` -- e.g. `cutover.advance` resolving `cutover.gate`
(coordinator_core/ops/cutover_advance.py), or `cutover_gate.py`'s
`_reverify_probe_op_key` resolving an arbitrary caller-supplied probe op key --
got a false registry MISS under lazy ops before this fix, because the sibling
module was never imported. `cutover.advance` was unusable for every record as
a result.

Spec backlink: cross-repo/inbox/2026-07-25-doe-claude-em-posix-bareword-path-provisioning.md

Coverage:
  (a) subprocess isolation (real process isolation required -- see
      ops/tests/test_registry_map_sync.py's module docstring for why an
      in-process assertion here would be meaningless: ~50 other test files
      in this suite already import coordinator_core.ops at collection time):
      in a genuine lazy-ops subprocess where only coordinator_core.ops.
      cutover_advance has ever been imported, get_op_handler("cutover.gate")
      still resolves -- proving the sibling-op lookup now goes through the
      same targeted-import + eager-fallback resolution dispatch_message uses.
  (b) get_op_handler still returns None for a genuinely-unregistered op key
      (no behavior change to the not-found case).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import coordinator_core.ipc as ipc

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


_SIBLING_OP_LAZY_RESOLUTION_SCRIPT = textwrap.dedent(
    """
    import os
    import sys

    os.environ["COORDINATOR_CORE_LAZY_OPS"] = "1"

    import coordinator_core.ops  # noqa: F401 -- lazy mode: SKIPS the eager import list
    import coordinator_core.ipc as ipc

    # Import only cutover_advance's owning module -- mirrors what actually
    # happens on the invoke path when "cutover.advance" is the dispatched op:
    # only ITS module gets imported, never cutover_gate.py.
    import coordinator_core.ops.cutover_advance  # noqa: F401

    assert "coordinator_core.ops.cutover_gate" not in sys.modules, (
        "test fixture assumption broken: cutover_gate.py must not already be "
        "imported -- otherwise this test cannot prove lazy resolution of a "
        "sibling op key."
    )

    handler = ipc.get_op_handler("cutover.gate")
    assert handler is not None, (
        "get_op_handler('cutover.gate') returned None in a lazy-ops subprocess "
        "where only cutover_advance.py was imported -- the sibling-op lazy+"
        "eager-fallback resolution did not fire."
    )
    assert "coordinator_core.ops.cutover_gate" in sys.modules, (
        "get_op_handler() must have imported cutover_gate.py (directly via "
        "OP_MODULE_MAP or via the eager SAFE FALLBACK) to resolve the handler."
    )
    print("SIBLING_OP_LAZY_RESOLUTION_OK")
    """
)


def _run_subprocess_script(script: str) -> subprocess.CompletedProcess:
    # PYTHONPATH must point at the claude-klabauter repo root so the subprocess can
    # import coordinator_core regardless of the parent process's cwd --
    # mirrors ops/tests/test_registry_map_sync.py's _run_subprocess_script.
    import os

    project_root = str(Path(__file__).resolve().parents[3])
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{project_root}{os.pathsep}{existing_pp}" if existing_pp else project_root
    return subprocess.run(  # popup-intentional-last-resort
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=project_root,
        env=env,
    )


def test_get_op_handler_resolves_a_sibling_op_under_lazy_ops():
    result = _run_subprocess_script(_SIBLING_OP_LAZY_RESOLUTION_SCRIPT)
    assert result.returncode == 0, (
        f"subprocess failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "SIBLING_OP_LAZY_RESOLUTION_OK" in result.stdout


def test_get_op_handler_still_returns_none_for_unregistered_key():
    assert ipc.get_op_handler("this.op.does.not.exist.anywhere") is None
