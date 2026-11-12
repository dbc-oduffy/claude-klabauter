"""Behavior pins: the relay (message) key's forced command refuses `launch` and `wake`."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_launch", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("cmd", [
    "launch claude_klabauter", "wake group-em", "launch", "wake", "launch a b", "launch ../x",
])
def test_parse_refuses_launch_and_wake(cmd):
    assert load().parse_mode(cmd) is None


def test_relay_carries_no_spawn_surface():
    relay = load()
    for name in ("launch", "wake", "spawn_launch", "resolve_repo_key", "LAUNCH_FAMILIES"):
        assert not hasattr(relay, name)


@pytest.mark.parametrize("cmd", ["launch claude_klabauter", "wake group-em"])
def test_relay_main_exits_usage_and_spawns_nothing(cmd, tmp_path):
    r = subprocess.run([sys.executable, str(RELAY)], capture_output=True, text=True, timeout=30,
                       env={"SSH_ORIGINAL_COMMAND": cmd, "COORDINATOR_SETTINGS_HOME": str(tmp_path),
                            "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr
    assert not (tmp_path / "unimatrix").exists()
