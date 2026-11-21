"""Behavior pins: the relay (message) key's forced command refuses `launch` and `wake`."""
import importlib.util
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
