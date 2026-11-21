"""setup-chain-walker launcher: argv reaches `setup_chain_walker.main` verbatim, in one process."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1]
_ENTRY = _BIN / "setup-chain-walker.py"
_CMD = _BIN / "setup-chain-walker.cmd"
_INVENTORY = _BIN.parents[1] / "docs" / "install" / "bin-inventory.json"


def test_cli_with_coordinator_root_reaches_walker_main(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "commands").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINATOR_SETUP_")}
    done = subprocess.run(
        [sys.executable, str(_ENTRY), "--coordinator-root", str(root), "--version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    assert "version" in done.stdout
