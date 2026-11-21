"""Backstop: guard set pinned to the registry, deny/allow on push remotes, no warm/ops import."""
from __future__ import annotations

import subprocess
import sys

import pytest

from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "scratch").mkdir()
    return tmp_path


def test_degrade_new_classifiers_load_no_guard_module(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "untracked.py").write_text("x\n", encoding="utf-8")
    code = (
        "import sys\n"
        "from coordinator_core.host_hooks import backstop\n"
        "assert backstop._recursive_rm('rm -rf src', %r) and backstop._stash_destruction('git stash clear')\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('coordinator_core.bash_guards.block_subagent',"
        "'coordinator_core.warm','coordinator_core.ops')))\n"
        "print('LOADED', bad)\n"
        "sys.exit(1 if bad else 0)\n"
    ) % str(repo)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_backstop_loads_no_warm_or_ops_module() -> None:
    code = (
        "import sys\n"
        "from coordinator_core.host_hooks import backstop\n"
        "from coordinator_core.host_hooks.contract import HostEvent\n"
        "backstop.match(HostEvent('codex','PreToolUse','s',tool_name='Bash',"
        "tool_input={'command':'git push origin main'},cwd='/nonexistent-backstop-cwd'))\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('coordinator_core.warm','coordinator_core.ops')))\n"
        "print('LOADED', bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stdout + proc.stderr
