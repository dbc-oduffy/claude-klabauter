"""Behavior pins: the relay (message) key's forced command refuses `launch` and `wake`."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


@pytest.mark.parametrize("cmd", ["launch claude_klabauter", "wake group-em"])
def test_relay_main_exits_usage_and_spawns_nothing(cmd, tmp_path):
    r = subprocess.run([sys.executable, str(RELAY)], capture_output=True, text=True, timeout=30,
                       env={"SSH_ORIGINAL_COMMAND": cmd, "COORDINATOR_SETTINGS_HOME": str(tmp_path),
                            "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr
    assert not (tmp_path / "unimatrix").exists()
