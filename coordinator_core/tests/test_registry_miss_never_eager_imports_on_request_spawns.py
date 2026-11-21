"""A registry miss is answered without importing every op module on the request thread."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


_ROOT = str(Path(__file__).resolve().parents[2])

_MAP_COVERS_REGISTRY = textwrap.dedent(
    """
    import coordinator_core.ipc as ipc
    import coordinator_core.hooks as hooks_pkg
    import coordinator_core.ops as ops_pkg
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    ops_pkg._eager_import_all()
    hooks_pkg._eager_import_all()
    print("\\n".join(sorted(set(ipc._REGISTRY) - set(OP_MODULE_MAP))))
    """
)


_HOOK_ENTRIES_REGISTER = textwrap.dedent(
    """
    import importlib
    import coordinator_core.ipc as ipc
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    for op, mod in sorted(OP_MODULE_MAP.items()):
        if op.startswith("hooks."):
            importlib.import_module(mod)
            if op not in ipc._REGISTRY:
                print(op)
    """
)


@pytest.mark.spawns_process
def test_the_map_covers_every_op_the_eager_import_registers():
    out = subprocess.run(
        [sys.executable, "-c", _MAP_COVERS_REGISTRY],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_ROOT,
    )
    assert out.returncode == 0, out.stderr
    unmapped = [line for line in out.stdout.splitlines() if line.strip()]
    assert not unmapped, (
        f"ops registered but absent from OP_MODULE_MAP ({unmapped!r}): a registry miss now "
        "answers not-found instead of importing every op module; add them to "
        "coordinator_core/ops/_registry_map.py."
    )


@pytest.mark.spawns_process
def test_every_hooks_map_entry_registers_its_op_from_its_own_module():
    out = subprocess.run(
        [sys.executable, "-c", _HOOK_ENTRIES_REGISTER],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_ROOT,
    )
    assert out.returncode == 0, out.stderr
    stale = [line for line in out.stdout.splitlines() if line.strip()]
    assert not stale, (
        f"hooks ops whose mapped module does not register them ({stale!r}): a registry miss "
        "answers not-found; point each at the module that registers it in "
        "coordinator_core/ops/_registry_map.py."
    )
