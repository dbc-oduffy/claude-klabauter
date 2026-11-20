"""Friction L16: a forwarder whose co-located resolver is missing exits 127 with one line, not a traceback."""
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install import substrate

pytestmark = pytest.mark.spawns_process


def test_missing_resolver_is_a_one_line_exit_127(tmp_path: Path) -> None:
    fwd = tmp_path / "hook-run"
    fwd.write_text(substrate._agent_forwarder_content("hook-run", "hook-run.py", "_resolve_absent"), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(fwd)], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=tmp_path
    )
    assert proc.returncode == 127
    assert "Traceback" not in proc.stderr and "/coordinator:install" in proc.stderr
    assert len(proc.stderr.strip().splitlines()) == 1
