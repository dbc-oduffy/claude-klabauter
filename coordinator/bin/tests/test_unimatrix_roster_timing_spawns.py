"""unimatrix-roster-timing.py reports per-phase cost of roster() without writing the sessions dir."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

if TYPE_CHECKING:
    from types import ModuleType

BIN = Path(__file__).resolve().parents[1]
SCRIPT = BIN / "unimatrix-roster-timing.py"


def test_cli_prints_json_for_an_empty_sessions_dir(tmp_path: Path) -> None:
    sess = tmp_path / "sessions"
    sess.mkdir()
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")}
    proc = subprocess.run([sys.executable, str(SCRIPT), "--sessions-dir", str(sess)],
                          capture_output=True, text=True, env=env, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["rows"] == 0 and out["entries_candidates"] == 0
    assert {"process_ms", "spawns", "phase_ms"} <= set(out)
    for phase in ("listdir", "json_load", "entry_live", "codex_checks", "print"):
        assert phase in out["phase_ms"], phase
