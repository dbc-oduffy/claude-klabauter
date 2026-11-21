"""Behavior pins for the unimatrix relay `grant` mode."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
RELAY = BIN / "unimatrix-relay.py"


def envelope(**over):
    env = {"grantee_session_id": "sess1", "grantee_box_id": "box-b", "note": "suite run",
           "issued_at": "2026-11-05T10:00:00Z", "expires_at": "2026-11-05T11:00:00Z",
           "scope_root": "/repo"}
    env.update(over)
    return env


CMD = "grant box-b:sess1 'suite run' 60 /repo"


def run_relay(cmd, cwd, stdin=""):
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": cmd,
           "COORDINATOR_SETTINGS_HOME": str(cwd / "home")}
    return subprocess.run([sys.executable, str(RELAY)], cwd=cwd, env=env, input=stdin,
                          capture_output=True, text=True, timeout=30)


def test_grant_mode_accepted_from_foreign_cwd_and_refuses_unsigned(tmp_path):
    # An envelope with no pinned issuer key must be refused by the engine, not by `usage:`.
    p = run_relay(CMD, tmp_path, json.dumps(envelope()) + "\n")
    assert "usage:" not in p.stderr
    assert p.returncode == 1
    assert json.loads(p.stdout)["ok"] is False


def test_malformed_grant_exits_with_usage(tmp_path):
    p = run_relay("grant only-one", tmp_path)
    assert p.returncode != 0 and "usage:" in p.stderr and "grant" in p.stderr


def run_isolated_relay(tmp_path, door_body):
    """Run a lone copy of the relay under `-I` with no engine, no lib/, and a stub door."""
    shutil.copy(RELAY, tmp_path / "unimatrix-relay.py")
    home = tmp_path / "home"
    if door_body is not None:
        door = home / "bin" / "coordinator-invoke"
        door.parent.mkdir(parents=True)
        door.write_text(door_body)
        door.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "COORDINATOR_ENGINE_ROOT")}
    env.update(SSH_ORIGINAL_COMMAND=CMD, COORDINATOR_SETTINGS_HOME=str(home))
    return subprocess.run([sys.executable, "-I", str(tmp_path / "unimatrix-relay.py")], cwd=tmp_path,
                          env=env, input=json.dumps(envelope()) + "\n", capture_output=True,
                          text=True, timeout=30)


@pytest.mark.skipif(os.name == "nt", reason="stub door is a posix shell script")
@pytest.mark.parametrize("door,ok,reason", [
    ("#!/bin/sh\necho '{\"result\": {\"ok\": true}}'\n", True, None),
    ("#!/bin/sh\necho '{\"result\": {\"ok\": false, \"reason\": \"bad_signature\"}}'\nexit 1\n",
     False, "bad_signature"),
    (None, False, "door_unavailable:FileNotFoundError"),
    ("#!/bin/sh\necho 'not json'\n", False, "door_bad_reply"),
    ("#!/bin/sh\necho '{\"jsonrpc\": \"2.0\", \"error\": {}}'\n", False, "door_bad_reply"),
])
def test_relay_without_engine_replies_through_the_door(tmp_path, door, ok, reason):
    p = run_isolated_relay(tmp_path, door)
    assert "Traceback" not in p.stderr
    reply = json.loads(p.stdout)
    assert reply["ok"] is ok
    if reason:
        assert reply["reason"] == reason


def test_main_runs_keypin_under_the_forced_command_peer(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
    env.update(SSH_ORIGINAL_COMMAND="keypin box-a", COORDINATOR_SETTINGS_HOME=str(tmp_path))
    line = json.dumps({"box_id": "box-a", "key_b64": "a2V5"}) + "\n"
    no_peer = subprocess.run([sys.executable, str(RELAY)], env=env, input=line, capture_output=True, text=True, timeout=60)
    assert json.loads(no_peer.stdout)["reason"] == "no_peer" and no_peer.returncode == 1
    wrong = subprocess.run([sys.executable, str(RELAY), "--peer", "box-b"], env=env, input=line,
                           capture_output=True, text=True, timeout=60)
    assert json.loads(wrong.stdout)["reason"] == "issuer_not_peer" and wrong.returncode == 1
