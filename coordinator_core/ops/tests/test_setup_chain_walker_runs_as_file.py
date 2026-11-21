"""Friction L44: the chain walker runs as a file from any cwd with no PYTHONPATH, rooting its own imports."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import coordinator_core.ops.setup_chain_walker as walker

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_runs_as_a_file_without_pythonpath_and_imports_its_own_root(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-I", walker.__file__, "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert "No module named" not in out
    assert "cannot resolve the chain-walker roots" in out or proc.returncode == 0
