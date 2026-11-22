"""The relay and file-relay forced commands start with COORDINATOR_SETTINGS_HOME unset and use the default
settings home; an ordinary roster caller still refuses."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_unset", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bare_env(tmp_path, command):
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_SETTINGS_HOME"}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), SSH_ORIGINAL_COMMAND=command)
    return env


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_relay_default_invocation_starts_without_the_variable(tmp_path):
    done = subprocess.run([sys.executable, str(BIN / "unimatrix-relay.py"), "--peer", "box"], input=b"",
                          env=bare_env(tmp_path, "capabilities"), capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert b"MissingSettingsError" not in done.stderr


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_relay_roster_mode_starts_without_the_variable(tmp_path):
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    done = subprocess.run([sys.executable, str(BIN / "unimatrix-relay.py"), "--peer", "box"], input=b"",
                          env=bare_env(tmp_path, "roster"), capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert b"MissingSettingsError" not in done.stderr


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("command", ["send-session 1 d n s", "send-named 1 d n", "send-to 1 d", "send 1"])
def test_relay_send_verbs_start_without_the_variable(tmp_path, command):
    # Mini/Machine-b -> Machine-a sends died here (ssh 255, MissingSettingsError) on 2026-11-22.
    done = subprocess.run([sys.executable, str(BIN / "unimatrix-relay.py"), "--peer", "box"], input=b"",
                          env=bare_env(tmp_path, command), capture_output=True, timeout=60)
    assert b"MissingSettingsError" not in done.stderr
    assert b"missing-settings" not in done.stderr + done.stdout


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_file_relay_put_lands_under_the_default_home(tmp_path):
    done = subprocess.run([sys.executable, str(BIN / "unimatrix-file-relay.py"), "--peer", "box"], input=b"",
                          env=bare_env(tmp_path, "put thing"), capture_output=True, timeout=60)
    assert b"MissingSettingsError" not in done.stderr
    assert (tmp_path / ".coordinator-claude-settings" / "unimatrix" / "drop" / "box").is_dir()


def test_relay_loaded_roster_resolves_the_default_home(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("UNIMATRIX_TEST_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    helpers = load("unimatrix-relay").load_roster()
    assert helpers.settings_home() == tmp_path / ".coordinator-claude-settings"
    assert helpers.default_key().startswith(str(tmp_path / ".coordinator-claude-settings"))


def test_an_ordinary_roster_caller_still_refuses(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    roster = load("unimatrix-roster")
    with pytest.raises(roster.MissingSettingsError):
        roster.settings_home()
    relay_copy = load("unimatrix-relay").load_roster()
    assert relay_copy.settings_home() != Path()  # the relay's own copy defaults
    with pytest.raises(roster.MissingSettingsError):
        roster.settings_home()


def unset_home(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path / ".coordinator-claude-settings"


def test_delivery_helpers_resolve_the_default_home(tmp_path, monkeypatch):
    home = unset_home(tmp_path, monkeypatch)
    roster = load("unimatrix-roster")
    assert roster.delivery_settings_home() == home
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "  ")
    assert roster.delivery_settings_home() == home
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "set"))
    assert roster.delivery_settings_home() == tmp_path / "set"
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    router = load("unimatrix-peer-router")
    assert router.default_peers_file() == home / "unimatrix" / "peers.json"
    assert router.default_legs_dir() == home / "unimatrix" / "agy-legs"
    assert router.default_state_dir() == home / "unimatrix" / "router"
    codex = load("unimatrix-peer-codex")
    assert codex.recorded_binary_file() == home / "host-sessions" / "codex" / codex.RECORDED_BINARY
    with pytest.raises(roster.MissingSettingsError):
        roster.settings_home()


def test_box_mux_resolves_the_default_home(tmp_path, monkeypatch):
    home = unset_home(tmp_path, monkeypatch)
    mux = load("lib/unimatrix_box_mux")
    chan = mux.channel("farbox", key="k", host="h", win=True)
    assert Path(chan.home) == home
    (home / "unimatrix" / "control-far").mkdir(parents=True)
    (home / "unimatrix" / "control-far" / "farbox.json").write_text('{"platform": "linux"}', encoding="utf-8")
    assert mux._recorded_far_platform("farbox") == "posix"


def bridge_module(tmp_path, monkeypatch):
    import types
    unset_home(tmp_path, monkeypatch)
    bridge = load("unimatrix-bridge")
    monkeypatch.setattr(bridge._box_mux(), "IS_WIN", False)
    bridge.cfg = types.SimpleNamespace(endpoint="e", key="k", remote="farbox", port=None)
    return bridge


def test_bridge_send_with_the_variable_unset_reaches_the_far_relay(tmp_path, monkeypatch):
    bridge = bridge_module(tmp_path, monkeypatch)
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'{"ok": true}\n', b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = bridge._ssh_relay("capabilities", b"")
    assert r.returncode == 0 and seen and seen[0][0] == "ssh" and "farbox" in seen[0]


def test_a_local_exception_is_a_local_fault_never_ssh_255(tmp_path, monkeypatch):
    bridge = bridge_module(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(bridge._box_mux(), "for_remote", boom)
    r = bridge._ssh_relay("capabilities", b"")
    assert r.returncode == bridge.LOCAL_FAULT_EXIT != 255
    assert r.stderr.decode().startswith(bridge.LOCAL_FAULT) and "settings exploded" in r.stderr.decode()
    assert r.transport == "none"


def run_append_program(tmp_path, monkeypatch, capsys, *, groups, existing=None):
    """Run the far-box key-append program as if on win32 with `groups` as `whoami /groups`; returns (status, icacls argv list)."""
    install = load("unimatrix-install")
    monkeypatch.setattr(sys, "platform", "win32")
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("ProgramData", str(programdata))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    if existing is not None:
        (programdata / "ssh").mkdir(parents=True)
        (programdata / "ssh" / "administrators_authorized_keys").write_text(existing, encoding="utf-8")
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, groups if argv[0] == "whoami" else "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    program = install._APPEND_PROGRAM.format(body="KEYBODY", line="restrict,command=\"x\" ssh-ed25519 KEYBODY c")
    try:
        exec(compile(program, "append", "exec"), {"__name__": "append"})
    except SystemExit:
        pass
    return capsys.readouterr().out.strip(), [c for c in calls if c[0] == "icacls"]






def test_non_admin_windows_user_keeps_the_user_authorized_keys(tmp_path, monkeypatch, capsys):
    status, acl = run_append_program(tmp_path, monkeypatch, capsys, groups='"Users","S-1-5-32-545"')
    assert status == "added" and acl == []
    assert "KEYBODY" in (tmp_path / "home" / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    assert not (tmp_path / "ProgramData" / "ssh").exists()


def test_whitespace_variable_is_unset_for_both_default_resolvers(tmp_path, monkeypatch):
    home = unset_home(tmp_path, monkeypatch)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "   ")
    assert load("unimatrix-relay").settings_home() == home
    assert load("unimatrix-roster").delivery_settings_home() == home


FAR_SID = "5a1f0c2e-1111-4222-8333-444455556666"


def sending_bridge(tmp_path, monkeypatch):
    import json
    import types
    bridge = bridge_module(tmp_path, monkeypatch)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": "/tmp/m.sock", "macSocketPath": "/far/4242.sock", "name": "far-session",
         "unimatrixFarName": "far-session", "sessionId": FAR_SID}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    bridge.cfg = types.SimpleNamespace(endpoint="/tmp/m.sock", key="k", remote="farbox", port=None)
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    bridge.written = []
    monkeypatch.setattr(bridge, "write_local", lambda ep, data: bridge.written.append((ep, data)))
    return bridge


class OneLine:
    def __init__(self):
        import json
        self.chunks = [json.dumps({"from": "uds:/tmp/s.sock", "message": "hi"}).encode() + b"\n", b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


@pytest.mark.parametrize("code, out, err", [
    (255, b"", b"MissingSettingsError: COORDINATOR_SETTINGS_HOME is not set"),
    (0, b"", b""),
])
def test_a_far_side_failure_or_unconfirmed_write_fails_the_send(tmp_path, monkeypatch, code, out, err):
    import json
    bridge = sending_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, code, out, err))
    bridge.relay_send(OneLine())
    status = bridge.delivery_status()
    assert len(status) == 1 and status[0].startswith("FAILED")
    notice = json.loads(bridge.written[0][1])["message"]
    assert "not delivered" in notice.lower() or "unconfirmed" in notice.lower()
    if err:
        assert "MissingSettingsError" in notice


def test_supervisor_paths_resolve_the_default_home_with_the_variable_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    roster = load("unimatrix-roster")
    base = tmp_path / ".coordinator-claude-settings" / "unimatrix"
    assert roster.stop_request_path(123) == base / "stop-123.json"
    assert roster.delivery_settings_home() == tmp_path / ".coordinator-claude-settings"


def test_supervisor_paths_honor_the_variable_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "h"))
    roster = load("unimatrix-roster")
    assert roster.stop_request_path(7) == tmp_path / "h" / "unimatrix" / "stop-7.json"
