"""A roster supervisor, a bridge send and box_mux started with COORDINATOR_SETTINGS_HOME unset resolve the default
settings home (HOME under tmp_path); an in-process send-leg fault is never labelled ssh exit 255; the ordinary roster
refusal stays."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
MIRROR = "/tmp/cc-socks/mirror.sock"
SENDER = "/tmp/cc-socks/sender.sock"
FAR_SID = "5a1f0c2e-1111-4222-8333-444455556666"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_sh_unset", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bare_home(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("UNIMATRIX_TEST_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path / ".coordinator-claude-settings"


def test_supervisor_paths_resolve_the_default_home(bare_home):
    roster = load("unimatrix-roster")
    (bare_home / "unimatrix").mkdir(parents=True)
    roster.stamp_supervisor("farbox", "bridge")
    pid = os.getpid()
    assert (bare_home / "unimatrix" / f"supervisor-{pid}.json").is_file()
    assert roster.stop_request_path(pid) == bare_home / "unimatrix" / f"stop-{pid}.json"
    roster.clear_stop(pid)
    assert not (bare_home / "unimatrix" / f"supervisor-{pid}.json").exists()
    assert roster.control_path("farbox").parent == bare_home / "unimatrix" / "ctl"
    assert roster.link_state_path("farbox").parent == bare_home / "unimatrix" / "link-state"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_supervisor_exports_the_resolved_home_to_its_children(bare_home, monkeypatch):
    roster = load("unimatrix-roster")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "")  # registers the undo: the export below must not leak past this test
    roster.export_settings_home()
    assert os.environ["COORDINATOR_SETTINGS_HOME"] == str(bare_home)
    out = subprocess.run([sys.executable, "-c", "import os;print(os.environ['COORDINATOR_SETTINGS_HOME'])"],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == str(bare_home)


def test_the_ordinary_settings_home_still_refuses_and_default_key_resolves(bare_home):
    roster = load("unimatrix-roster")
    with pytest.raises(roster.MissingSettingsError):
        roster.settings_home()
    assert roster.default_key().startswith(str(roster.delivery_settings_home()))


def test_box_mux_resolves_the_default_home(bare_home):
    bridge = load("unimatrix-bridge")
    mux = bridge._box_mux()
    far = bare_home / "unimatrix" / "control-far"
    far.mkdir(parents=True)
    (far / "farbox.json").write_text(json.dumps({"platform": "win32"}))
    assert mux._recorded_far_platform("farbox") is not None
    reader = bare_home / "bin"
    reader.mkdir(parents=True)
    assert mux.machine_local_value("x") == ""  # no reader file under the default home: empty, not a raise


class FakeConn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


@pytest.fixture
def bridge(bare_home, tmp_path, monkeypatch):
    mod = load("unimatrix-bridge")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": MIRROR, "macSocketPath": "/far/4242.sock", "name": "far-session",
         "unimatrixFarName": "far-session", "sessionId": FAR_SID}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setattr(mod._box_mux(), "IS_WIN", False)
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    return mod


def send(bridge):
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    bridge.relay_send(FakeConn(line + b"\n"))


def test_a_bridge_send_with_the_variable_unset_reaches_ssh(bridge, bare_home, monkeypatch):
    dialled = []

    def fake_run(*a, **k):
        dialled.append(a)
        replies = "".join(json.dumps({"ok": True, "digest": hashlib.sha256(l).hexdigest()}) + "\n"
                          for l in k.get("input", b"").splitlines())
        return subprocess.CompletedProcess(a, 0, replies.encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    send(bridge)
    assert dialled, "the send never reached ssh"
    rows = [json.loads(l) for l in (bare_home / "unimatrix" / "deliveries.jsonl").read_text().splitlines()]
    assert rows and not any("MissingSettingsError" in json.dumps(r) for r in rows)


@pytest.mark.parametrize("stdout", [b"", b'{"ok": true}\n'])
def test_a_far_side_failure_fails_the_send_with_the_far_error(bridge, bare_home, monkeypatch, stdout):
    far_error = b"MissingSettingsError: COORDINATOR_SETTINGS_HOME is not set"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout, far_error))
    send(bridge)
    rows = [json.loads(l) for l in (bare_home / "unimatrix" / "deliveries.jsonl").read_text().splitlines()]
    assert rows and all(r["ok"] is False for r in rows)
    assert all("MissingSettingsError" in r["error"] for r in rows) or all("unconfirmed" in r["error"] for r in rows)


def test_an_in_process_fault_is_a_local_fault_not_ssh_255(bridge, bare_home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("channel construction failed")

    monkeypatch.setattr(bridge._box_mux(), "channel", boom)
    r = bridge._ssh_relay("send-batch", b"x\n")
    assert r.returncode != 255 and r.returncode == bridge.LOCAL_FAULT_EXIT
    assert r.stderr.startswith(bridge.LOCAL_FAULT.encode()) and b"RuntimeError" in r.stderr
    assert r.transport == "none"
    send(bridge)
    rows = [json.loads(l) for l in (bare_home / "unimatrix" / "deliveries.jsonl").read_text().splitlines()]
    assert rows and all("255" not in json.dumps(r) for r in rows)
    assert any(bridge.LOCAL_FAULT in json.dumps(r) for r in rows)
