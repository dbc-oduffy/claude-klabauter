"""A fault raised inside the bridge process is a local send-leg fault, never ssh exit 255; the send leg resolves the settings home unset."""
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
LINE = b'{"from": "uds:/s.sock", "message": "hi"}\n'


def load_bridge():
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_local_fault", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def __init__(self) -> None:
        self.chunks = [LINE, b""]

    def read(self):
        return self.chunks.pop(0)

    def write(self, data) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def unset_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bridge whose process carries no COORDINATOR_SETTINGS_HOME and whose account home is under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    bridge = load_bridge()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "name": "far", "messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock",
         "unimatrixRemote": "lan", "unimatrixFarName": "far", "sessionId": "sid-far"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun: lan)
    bridge.notices = []
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: bridge.notices.append(error))
    mux = bridge._box_mux()
    mux._FAR_PLATFORM.clear()
    monkeypatch.setattr(mux, "IS_WIN", False)
    far = home / ".coordinator-claude-settings" / "unimatrix" / "control-far"
    far.mkdir(parents=True)
    (far / "lan.json").write_text(json.dumps({"platform": "linux"}))
    yield bridge, home / ".coordinator-claude-settings" / "unimatrix" / "deliveries.jsonl"
    bridge.log.close()
    mux._FAR_PLATFORM.clear()


def test_an_in_process_exception_is_a_local_send_leg_fault_not_ssh_255(unset_home, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, _ = unset_home
    mux = bridge._box_mux()

    def boom(*a, **k):
        raise RuntimeError("broker spawn exploded")

    monkeypatch.setattr(mux, "for_remote", boom)
    r = bridge._ssh_relay("send-session 9 x", LINE)
    assert r.returncode == bridge.LOCAL_FAULT_EXIT != 255
    assert r.stderr.decode().startswith(f"{bridge.LOCAL_FAULT}: RuntimeError: broker spawn exploded")
    assert r.transport == "none"


def test_a_local_fault_row_names_the_fault_and_never_claims_ssh_exit_255(unset_home, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = unset_home
    monkeypatch.setattr(bridge._box_mux(), "for_remote", lambda *a, **k: (_ for _ in ()).throw(ValueError("no channel")))
    bridge.serve_one(Conn())
    [row] = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert row["ok"] is False and "local send-leg fault: ValueError: no channel" in row["error"]
    assert "255" not in row["error"]
    assert bridge.notices and "255" not in bridge.notices[0]
    assert "relay send failed 255" not in Path(bridge.log.name).read_text(encoding="utf-8")


def test_a_refused_route_is_still_the_255_result(unset_home, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, _ = unset_home

    def refuse(remote):
        raise bridge.RouteRefused("no route")

    monkeypatch.setattr(bridge, "route_target", refuse)
    assert bridge._ssh_relay("send-session 9 x", LINE).returncode == 255


def test_a_bridge_send_with_the_settings_home_unset_reaches_the_far_relay(unset_home, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = unset_home
    assert "COORDINATOR_SETTINGS_HOME" not in os.environ
    argvs = []

    def run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    bridge.serve_one(Conn())
    assert argvs and argvs[0][-1].startswith("send-session 9 ")
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert rows and all("MissingSettingsError" not in json.dumps(r) for r in rows)
