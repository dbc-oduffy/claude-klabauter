"""
coordinator_core.ops.tests.test_registry_map_sync

Regression coverage for OP_MODULE_MAP (coordinator_core/ops/_registry_map.py),
the op-name -> owning-module lazy-import seam added for F6 / claude-klabauter-windows-
portability section C4 (lazy op registration).

Coverage:
  (a) OP_MODULE_MAP-vs-live-registry parity -- every op key that self-registers
      via coordinator_core.ops's EAGER import path is also present in
      OP_MODULE_MAP (and vice versa). A missing map entry doesn't break
      dispatch_message (the SAFE FALLBACK in ipc._lazy_import_and_lookup
      degrades to a full coordinator_core.ops import), but it silently loses
      the perf win the map exists for, so this is a straight sync check --
      not gated on the fallback's never-worse behavior.
  (b) the C4 never-worse invariant end-to-end, in a subprocess (real process
      isolation is required -- this test file, like ~50 other test files in
      this suite, does a bare `import coordinator_core.ops` at collection
      time via other modules in the same pytest session, so sys.modules /
      coordinator_core.ipc._REGISTRY are already fully populated by the time
      any in-process assertion here would run):
        (b1) with COORDINATOR_CORE_LAZY_OPS=1, dispatching a MAPPED op
             ("ping") imports ONLY that op's owning module
             (coordinator_core.ops.ping) -- not the rest of the ~55-module
             eager-import list (spot-checked via coordinator_core.ops.
             coverage_gate, an unrelated module never touched by the ping
             dispatch path).
        (b2) with COORDINATOR_CORE_LAZY_OPS=1 AND OP_MODULE_MAP missing the
             entry for a real, registerable op ("workflow.scaffold", entry
             deleted in-process to simulate map drift/staleness), dispatching
             that op still succeeds -- proving the SAFE FALLBACK
             (_eager_import_all()) covers a map gap and dispatch is never
             worse than pre-C4 eager-import correctness.

Spec backlink: docs/plans/2026-07-14-claude-klabauter-windows-portability.md section C4
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import coordinator_core.ops  # noqa: F401 -- triggers every op module's register_op(...) side-effect
import coordinator_core.ipc as ipc
from coordinator_core.ops._registry_map import OP_MODULE_MAP


# ---------------------------------------------------------------------------
# (a) static parity -- OP_MODULE_MAP.keys() == live-registry.keys()
# ---------------------------------------------------------------------------


def test_op_module_map_matches_live_registry():
    live_keys = set(ipc._REGISTRY.keys())
    map_keys = set(OP_MODULE_MAP.keys())

    missing_from_map = live_keys - map_keys
    stale_in_map = map_keys - live_keys

    assert not missing_from_map, (
        f"{sorted(missing_from_map)!r} are registered in coordinator_core.ipc._REGISTRY "
        f"(via eager import of coordinator_core.ops) but absent from "
        f"coordinator_core.ops._registry_map.OP_MODULE_MAP. dispatch_message's lazy-miss "
        f"path still works via the SAFE FALLBACK (full coordinator_core.ops import), but "
        f"these ops silently lose the C4 targeted-import perf win -- add them to OP_MODULE_MAP."
    )
    assert not stale_in_map, (
        f"{sorted(stale_in_map)!r} are present in OP_MODULE_MAP but not in the live "
        f"coordinator_core.ipc._REGISTRY -- stale/renamed entries. A stale entry degrades "
        f"gracefully via the SAFE FALLBACK (see ipc._lazy_import_and_lookup docstring), "
        f"but should be cleaned up: either the op was removed/renamed, or its owning "
        f"module no longer registers it under this key."
    )


# ---------------------------------------------------------------------------
# (b) C4 never-worse invariant -- subprocess isolation required (see module
#     docstring: this in-process test session already has every op module
#     imported by the time any test body here runs).
# ---------------------------------------------------------------------------

_MAPPED_OP_SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    os.environ["COORDINATOR_CORE_LAZY_OPS"] = "1"

    import coordinator_core.ops  # noqa: F401 -- lazy mode: SKIPS the eager import list
    import coordinator_core.ipc as ipc

    assert "coordinator_core.ops.ping" not in sys.modules, (
        "coordinator_core.ops.ping must not be imported yet under lazy mode "
        "before dispatch -- otherwise this test cannot prove targeted import."
    )
    assert not ipc._REGISTRY, (
        f"_REGISTRY must start empty under lazy mode; got {sorted(ipc._REGISTRY.keys())!r}"
    )

    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    resp = asyncio.new_event_loop().run_until_complete(ipc.dispatch_message(msg))
    assert "result" in resp, f"ping dispatch must succeed; got {resp!r}"
    assert resp["result"]["ok"] is True

    assert "coordinator_core.ops.ping" in sys.modules, (
        "dispatching the mapped op 'ping' must import its owning module "
        "coordinator_core.ops.ping."
    )
    assert "coordinator_core.ops.coverage_gate" not in sys.modules, (
        "dispatching the mapped op 'ping' must NOT import an unrelated op module "
        "(coordinator_core.ops.coverage_gate) -- a mapped-op dispatch should do a "
        "targeted single-module import, not the whole-package eager-import fallback."
    )
    print("MAPPED_OP_TARGETED_IMPORT_OK")
    """
)

_UNMAPPED_OP_SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    os.environ["COORDINATOR_CORE_LAZY_OPS"] = "1"

    import coordinator_core.ops  # noqa: F401 -- lazy mode: SKIPS the eager import list
    import coordinator_core.ipc as ipc
    from coordinator_core.ops import _registry_map

    assert "workflow.scaffold" in _registry_map.OP_MODULE_MAP, (
        "test fixture assumption broken: 'workflow.scaffold' must be a real "
        "OP_MODULE_MAP entry for this deliberate-removal test to be meaningful."
    )
    del _registry_map.OP_MODULE_MAP["workflow.scaffold"]

    assert not ipc._REGISTRY, (
        f"_REGISTRY must start empty under lazy mode; got {sorted(ipc._REGISTRY.keys())!r}"
    )

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "workflow.scaffold",
        "params": {"name": "demo-workflow", "description": "a demo workflow"},
    }
    resp = asyncio.new_event_loop().run_until_complete(ipc.dispatch_message(msg))
    assert "result" in resp, (
        f"workflow.scaffold dispatch must still succeed via the SAFE FALLBACK "
        f"(whole-package import) even with its OP_MODULE_MAP entry deliberately "
        f"removed (never-worse invariant); got {resp!r}"
    )
    assert "script" in resp["result"]
    print("UNMAPPED_OP_FALLBACK_OK")
    """
)


def _run_subprocess_script(script: str) -> subprocess.CompletedProcess:
    # PYTHONPATH must point at the claude-klabauter repo root so the subprocess can
    # import coordinator_core regardless of the parent process's cwd (this
    # test file may be collected as part of a larger pytest run rooted
    # elsewhere) -- mirrors coordinator_core/tests/test_invoke_main.py's
    # _make_env() PYTHONPATH-injection pattern.
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


def test_mapped_op_dispatch_imports_only_its_module():
    result = _run_subprocess_script(_MAPPED_OP_SUBPROCESS_SCRIPT)
    assert result.returncode == 0, (
        f"subprocess failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "MAPPED_OP_TARGETED_IMPORT_OK" in result.stdout


def test_unmapped_op_dispatch_falls_back_to_whole_package_import():
    result = _run_subprocess_script(_UNMAPPED_OP_SUBPROCESS_SCRIPT)
    assert result.returncode == 0, (
        f"subprocess failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "UNMAPPED_OP_FALLBACK_OK" in result.stdout
