"""A restarted far peer keeps its socket; its relay pid still needs refreshing."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "restart_roster", Path(__file__).resolve().parents[1] / "unimatrix-roster.py")
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)


@pytest.mark.parametrize("name", ["codex-repo-12", None])
def test_existing_bridge_refreshes_far_pid_without_changing_local_identity(tmp_path, monkeypatch, name):
    sock = "/tmp/cc-socks/stable.sock"
    local = {"pid": 31337, "procStart": "local-start", "pidDomain": "win32:box",
             "name": "codex-repo-12", "farPid": 62725, "messagingSocketPath": "local-pipe",
             "macSocketPath": sock}
    path = tmp_path / "31337.json"
    path.write_text(json.dumps(local))
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "live_sessions", lambda: [])
    monkeypatch.setattr(roster, "mirror", lambda *a: pytest.fail("restarted live bridge"))
    bridge = SimpleNamespace(pid=31337, poll=lambda: None)
    bridges = {sock: bridge}
    entry = {"pid": 80954, "procStart": "far-start", "messagingSocketPath": sock}
    if name is not None:
        entry["name"] = name
    row = {"entry": entry, "key": "rotated-far-key"}
    cfg = SimpleNamespace(remote="macmini")
    roster.sync(bridges, {sock: row}, cfg, lambda *a: None)
    updated = json.loads(path.read_text())
    expected = {**local, "farPid": 80954, "box": "macmini", "vendor": "unknown", "unimatrixRemote": "macmini",
                **roster.supervisor_fields()}
    if name is not None:
        expected["unimatrixFarName"] = name
    assert updated == expected
    assert bridges[sock] is bridge
    monkeypatch.setattr(roster, "write_json_atomic", lambda *a: pytest.fail("rewrote unchanged mirror"))
    roster.sync(bridges, {sock: row}, cfg, lambda *a: None)
