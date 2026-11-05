"""
Every hooks.* key in OP_MODULE_MAP maps to the coordinator_core.hooks.<module> whose
register_op(...) names it, and each key resolves in a fresh interpreter through
ipc._lazy_import_and_lookup step 1 without the hooks-wide _eager_import_all().
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import coordinator_core.hooks as hooks_pkg
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HOOKS_DIR = Path(hooks_pkg.__file__).parent
_LITERAL = re.compile(r"""register_op\(\s*["'](hooks\.[A-Za-z0-9_]+)["']""")
_CONST = re.compile(r"""^OP_NAME\s*=\s*["'](hooks\.[A-Za-z0-9_]+)["']""", re.MULTILINE)
_CONST_USE = re.compile(r"register_op\(\s*OP_NAME\b")


def _scan_owning_modules() -> dict[str, str]:
    owners: dict[str, str] = {}
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        keys = set(_LITERAL.findall(text))
        if _CONST_USE.search(text):
            keys.update(_CONST.findall(text))
        for key in keys:
            assert key not in owners, f"{key} registered by both {owners[key]} and {path.stem}"
            owners[key] = f"coordinator_core.hooks.{path.stem}"
    return owners


def test_every_hooks_key_maps_to_its_owning_module():
    owners = _scan_owning_modules()
    mapped = {k: v for k, v in OP_MODULE_MAP.items() if k.startswith("hooks.")}
    # A hooks.* key owned by an ops module (hooks.sessionend_pm_directive_check) is not
    # registered under hooks/; every other key must match the scan.
    foreign = {k for k, v in mapped.items() if not v.startswith("coordinator_core.hooks.")}
    assert owners, "precondition: the scan found hooks.* registrations"
    assert {k: v for k, v in mapped.items() if k not in foreign} == owners
    assert foreign.isdisjoint(owners)


_PROBE = """
import json, sys
import coordinator_core.hooks as hooks_pkg
from coordinator_core import ipc
from coordinator_core.ops._registry_map import OP_MODULE_MAP

def _boom():
    raise AssertionError("hooks _eager_import_all called")

hooks_pkg._eager_import_all = _boom
failed = []
for key in sorted(k for k in OP_MODULE_MAP if k.startswith("hooks.")):
    try:
        if ipc._lazy_import_and_lookup(key) is None:
            failed.append(key)
    except AssertionError:
        failed.append(key)
print(json.dumps(failed))
"""


def test_every_hooks_key_resolves_in_a_fresh_process_without_eager_import():
    repo_root = Path(hooks_pkg.__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
