"""Pins the idle_compact contract values, path shapes and Injector protocol."""

import json

from coordinator_core.idle_compact import contract as c
from coordinator_core.session import context_usage_sidecar as cus
from coordinator_core.session_hub_path import session_dir_for


def test_thresholds():
    assert (c.IDLE_SECONDS, c.CONTEXT_PCT, c.TICK_SECONDS) == (300, 18.0, 30)


def test_settings_home_matches_sidecar_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert cus.sidecar_path("s1").parents[2] == c.settings_home() == tmp_path
    assert c.injection_log_path() == tmp_path / "state" / "idle-compact" / "injections.jsonl"
    assert c.allowlist_path() == tmp_path / "idle-compact-allowlist.json"
    assert not c.allowlist_path().exists()


def test_default_settings_home(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    assert c.settings_home().name == ".coordinator-claude-settings"


def test_marker_paths_in_hub(tmp_path):
    (tmp_path / ".git").mkdir()
    o = c.optout_marker_path("sid1", str(tmp_path))
    h = c.compact_hold_marker_path("sid1", str(tmp_path))
    assert o.name == "idle-compact-optout" and h.name == "compact-hold"
    assert o.parent == h.parent
    assert o.parent == session_dir_for(tmp_path / ".git", "sid1")


def test_marker_paths_none_outside_repo(tmp_path):
    assert c.optout_marker_path("sid1", str(tmp_path)) is None


def test_injector_protocol_and_log_record():
    class Fake:
        def inject(self, pid):
            return c.InjectResult(True, "ok")

    assert isinstance(Fake(), c.Injector)
    assert not isinstance(object(), c.Injector)
    rec = c.LogRecord("t", "r", "s", 7, "inject", "idle")
    assert json.loads(json.dumps(rec.to_dict())) == {
        "ts": "t", "run_id": "r", "session_id": "s", "pid": 7, "action": "inject", "reason": "idle",
    }
