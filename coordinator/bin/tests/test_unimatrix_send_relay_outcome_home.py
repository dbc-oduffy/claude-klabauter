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


def _send_with_rows(tmp_path, monkeypatch, rows):
    spec = importlib.util.spec_from_file_location("unimatrix_send_pushed", BIN / "unimatrix-send.py")
    send = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(send)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    (tmp_path / "unimatrix").mkdir(parents=True)
    (tmp_path / "unimatrix" / "deliveries.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return send


def test_a_pushed_mirror_forward_counts_as_the_outcome(tmp_path, monkeypatch):
    # Mac-mini 2026-11-23: a send to claude-klabauter-0e.machine-a landed but printed "no far ingest ack":
    # a relay-pushed mirror records `mirror-out` under its bare name, never a bridge `target` row.
    send = _send_with_rows(tmp_path, monkeypatch, [
        {"side": "mirror-out", "ok": True, "written": True, "reached": "claude-klabauter-0e", "at": 100}])
    outcome = send.relay_outcome("claude-klabauter-0e.machine-a", 50, wait=0)
    assert outcome and outcome["ok"] is True and outcome["transport"] == "push"


def test_a_stale_or_foreign_pushed_forward_is_not_the_outcome(tmp_path, monkeypatch):
    send = _send_with_rows(tmp_path, monkeypatch, [
        {"side": "mirror-out", "ok": True, "reached": "claude-klabauter-0e", "at": 10},
        {"side": "mirror-out", "ok": True, "reached": "other-session", "at": 100}])
    assert send.relay_outcome("claude-klabauter-0e.machine-a", 50, wait=0) is None


def test_a_failed_pushed_forward_wins(tmp_path, monkeypatch):
    send = _send_with_rows(tmp_path, monkeypatch, [
        {"side": "mirror-out", "ok": True, "reached": "x", "at": 100},
        {"side": "mirror-out", "ok": False, "reached": "x", "error": "broken pipe", "at": 101}])
    assert send.relay_outcome("x.machine-a", 50, wait=0)["ok"] is False
