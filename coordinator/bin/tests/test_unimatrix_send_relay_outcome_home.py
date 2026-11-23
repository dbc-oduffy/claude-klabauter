"""relay_outcome reads the bridge's deliveries row under the default settings home when the env carries none (ssh)."""
import importlib.util
import json
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def test_unset_settings_home_falls_back_to_the_default_home(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_send_home", BIN / "unimatrix-send.py")
    send = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(send)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rows = tmp_path / ".coordinator-claude-settings" / "unimatrix"
    rows.mkdir(parents=True)
    (rows / "deliveries.jsonl").write_text(
        json.dumps({"target": "mac-peer (pid 5)", "ok": True, "at": 100}) + "\n", encoding="utf-8")
    outcome = send.relay_outcome("mac-peer", 50, wait=0)
    assert outcome and outcome["ok"] is True
