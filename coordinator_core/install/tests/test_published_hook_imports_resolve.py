"""Every module-level third-party import in the published hooks/scripts/*.py
must resolve under the system interpreter (no venv injected).

Cross-repo contract: skips when `repos.coordinator_claude` is unregistered or
its `hooks/scripts` is absent.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import machine_resolver

# `doctrine_surface_tiers` is first-party but lives under DoE's coordinator/lib,
# a layout the published mirror does not carry: a DoE layout mismatch, not a
# package the system interpreter could ever provide.
_NON_THIRD_PARTY_HOOK_IMPORTS = frozenset(
    {"coordinator_core", "coordinator_whoami", "doctrine_surface_tiers"}
)

_PROBE = (
    "import importlib.util, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "names = json.loads(sys.argv[2])\n"
    "missing = []\n"
    "for n in names:\n"
    "    try:\n"
    "        if importlib.util.find_spec(n) is None:\n"
    "            missing.append(n)\n"
    "    except Exception:\n"
    "        missing.append(n)\n"
    "print(json.dumps(missing))\n"
)


def _iter_hook_script_top_level_imports(hooks_scripts_dir: Path):
    """Yield each top-level module name imported at module level (not nested
    in try/def/if) across `*.py` in `hooks_scripts_dir`."""
    for path in sorted(hooks_scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.module.split(".")[0]


@pytest.mark.real_home
@pytest.mark.spawns_process
def test_every_published_hook_import_resolves_under_system_interpreter():
    root = machine_resolver.registry_get("repos.coordinator_claude")
    if not root:
        pytest.skip("repos.coordinator_claude not registered: hook-import contract UNVERIFIED on this machine")
    hooks_scripts_dir = Path(root) / "hooks" / "scripts"
    if not hooks_scripts_dir.is_dir():
        pytest.skip(f"{hooks_scripts_dir} not found")

    third_party = set()
    for name in _iter_hook_script_top_level_imports(hooks_scripts_dir):
        if name in _NON_THIRD_PARTY_HOOK_IMPORTS:
            continue
        if name in sys.stdlib_module_names:
            continue
        if (hooks_scripts_dir / f"{name}.py").is_file():
            continue
        third_party.add(name)

    assert "yaml" in third_party, (
        "expected a published hook script to `import yaml` at module level; "
        "if not, the premise of this check needs re-examination"
    )

    interpreter = shutil.which("python3") or shutil.which("python")
    assert interpreter, "no system python3/python on PATH"
    proc = subprocess.run(
        [interpreter, "-E", "-c", _PROBE, str(hooks_scripts_dir), json.dumps(sorted(third_party))],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    missing = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not missing, (
        f"hook scripts under {hooks_scripts_dir} import {missing} at module "
        f"level, unresolvable under system interpreter {interpreter}"
    )
