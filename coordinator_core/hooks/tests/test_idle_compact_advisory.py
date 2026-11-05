"""Tests for hooks.idle_compact_advisory: advisory only for a fresh watcher-injected /compact."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.hooks import idle_compact_advisory as adv
from coordinator_core.idle_compact.contract import injection_log_path

SID = "sess-adv-1"


def _log(tmp_path, monkeypatch, *records):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    path = injection_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    lines = []
    for sid, age_s, action, run_id in records:
        lines.append(json.dumps({"ts": (now - timedelta(seconds=age_s)).isoformat(),
                                 "run_id": run_id, "session_id": sid, "pid": 1,
                                 "action": action, "reason": "r"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _call(prompt, sid=SID):
    return adv._handler({"prompt": prompt, "session_id": sid})


def test_names_run_id_for_fresh_injected_compact(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch, (SID, 10, "injected", "run-abc"))
    out = _call("/compact")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "run-abc" in ctx and "idle-compaction watcher" in ctx
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_uses_newest_injected_record_for_session(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch, (SID, 200, "injected", "run-old"), (SID, 10, "injected", "run-new"))
    assert "run-new" in _call("/compact focus")["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("prompt", ["hello", "please /compact", "/compaction", ""])
def test_non_compact_prompt_gets_no_advisory(tmp_path, monkeypatch, prompt):
    _log(tmp_path, monkeypatch, (SID, 10, "injected", "run-abc"))
    assert _call(prompt) == {}


@pytest.mark.parametrize("records", [
    [(SID, 3600, "injected", "run-abc")],
    [(SID, 10, "skipped", "run-abc")],
    [("other", 10, "injected", "run-abc")],
])
def test_compact_without_fresh_injection_gets_no_advisory(tmp_path, monkeypatch, records):
    _log(tmp_path, monkeypatch, *records)
    assert _call("/compact") == {}


def test_missing_log_and_garbage_lines_never_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "none"))
    assert _call("/compact") == {}
    path = injection_log_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json\n[1]\n", encoding="utf-8")
    assert _call("/compact") == {}


def test_op_is_registered_and_scoped():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP["hooks.idle_compact_advisory"] == "coordinator_core.hooks.idle_compact_advisory"
    assert _OP_KEY_SCOPE["hooks.idle_compact_advisory"] == "none"
    from coordinator_core import hooks

    assert "coordinator_core.hooks.idle_compact_advisory" in hooks._EAGER_HOOK_MODULES
