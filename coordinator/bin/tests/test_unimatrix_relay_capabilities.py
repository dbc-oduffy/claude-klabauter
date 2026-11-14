"""Behavior pins for the unimatrix relay `capabilities` verb."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_capabilities", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def test_parse_mode_accepts_bare_capabilities_only():
    assert R.parse_mode("capabilities") == ("capabilities", None)
    assert R.parse_mode("capabilities extra") is None


def test_main_prints_capabilities_and_sha(monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "capabilities")
    monkeypatch.setattr("sys.argv", ["unimatrix-relay.py"])
    R.main()
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is True
    assert reply["relay_sha"] == R.relay_sha()
    assert reply["capabilities"] == R.CAPABILITIES
    assert "capabilities" in reply["capabilities"]


def test_every_listed_capability_is_a_parse_mode_verb():
    probes = {"roster": "roster", "source": "source", "specs": "specs", "capabilities": "capabilities",
              "send": "send 1", "send-batch": "send-batch 1", "inbox-mux": "inbox-mux",
              "inbox": "inbox abc", "reply-inbox": "reply-inbox abc",
              "membership": "membership get", "keypin": "keypin abc", "revoke": "revoke abc"}
    for verb, cmd in probes.items():
        assert verb in R.CAPABILITIES
        parsed = R.parse_mode(cmd)
        assert parsed is not None and parsed[0] == verb, cmd
