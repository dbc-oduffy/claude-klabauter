"""Behavior pins for the unimatrix relay `grant` mode."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
RELAY = BIN / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_grant", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def envelope(**over):
    env = {"grantee_session_id": "sess1", "grantee_box_id": "box-b", "note": "suite run",
           "issued_at": "2026-11-05T10:00:00Z", "expires_at": "2026-11-05T11:00:00Z",
           "scope_root": "/repo"}
    env.update(over)
    return env


CMD = "grant box-b:sess1 'suite run' 60 /repo"


def test_parse_accepts_grant_and_maps_fields():
    assert load().parse_mode(CMD) == (
        "grant", {"grantee": "box-b:sess1", "note": "suite run", "ttl": 60, "repo": "/repo"})
    assert load().parse_mode("grant sess1 n 5 -")[1]["repo"] is None


@pytest.mark.parametrize("bad", [
    "grant", "grant a b", "grant a b 60", "grant a b x /r", "grant a b 0 /r",
    "grant a b 60 /r extra", "grant a 'unterminated 60 /r", "granted a b 60 /r"])
def test_parse_refuses_malformed_grant(bad):
    assert load().parse_mode(bad) is None


def test_grant_hands_envelope_to_receive_and_returns_reply():
    relay, seen = load(), []
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope()), receive=lambda e: seen.append(e) or {"ok": True})
    assert reply == {"ok": True} and seen == [envelope()]


@pytest.mark.parametrize("over,reason", [
    ({"grantee_session_id": "other"}, "argv_mismatch:grantee"),
    ({"note": "different"}, "argv_mismatch:note"),
    ({"scope_root": "/elsewhere"}, "argv_mismatch:repo"),
    ({"expires_at": "2026-11-05T12:00:00Z"}, "argv_mismatch:ttl"),
    ({"expires_at": "garbage"}, "bad_ttl"),
])
def test_argv_disagreeing_with_envelope_is_refused_before_the_engine(over, reason):
    relay = load()
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope(**over)), receive=lambda e: pytest.fail("engine reached"))
    assert reply == {"ok": False, "reason": reason}


def test_non_envelope_stdin_is_refused():
    relay = load()
    args = relay.parse_mode(CMD)[1]
    for line in ("", "not json", "[1]"):
        assert relay.grant(args, line, receive=lambda e: pytest.fail("reached")) == {
            "ok": False, "reason": "bad_envelope"}


def test_engine_refusal_passes_through():
    relay = load()
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope()), receive=lambda e: {"ok": False, "reason": "bad_signature"})
    assert reply == {"ok": False, "reason": "bad_signature"}


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
