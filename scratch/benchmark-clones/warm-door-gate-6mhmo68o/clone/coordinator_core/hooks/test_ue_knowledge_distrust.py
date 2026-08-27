"""
Tests for coordinator_core.hooks.ue_knowledge_distrust._run_bootstrap --
the C5 native-Python Port of: claude-ue-bootstrap.sh (DoE 4518ca1a,
2026-07-21)'s settings.json write/merge logic (retires the bash subprocess
spawn on this session-hot-path hook; see module docstring).

Spec backlink: pln-claude-klabauter-pure-python-shop-retire-0f8aee id: C5
"""
from __future__ import annotations

import json
import subprocess

from coordinator_core.hooks import ue_knowledge_distrust as mod


def test_fast_path_writes_fresh_settings(tmp_path, monkeypatch):
    """No existing settings.json: writes it fresh with every bootstrap key true."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is True
    assert "wrote UE override" in message
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.is_file()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    for key in mod._BOOTSTRAP_KEYS:
        assert data["enabledPlugins"][key] is True


def test_merge_path_preserves_unrelated_keys(tmp_path, monkeypatch):
    """Existing settings.json with unrelated keys: merges bootstrap keys in,
    leaves everything else untouched."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"someOtherKey": "keep-me", "enabledPlugins": {"foo@bar": True}}),
        encoding="utf-8",
    )

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is True
    assert "merged UE override" in message
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["someOtherKey"] == "keep-me"
    assert data["enabledPlugins"]["foo@bar"] is True
    for key in mod._BOOTSTRAP_KEYS:
        assert data["enabledPlugins"][key] is True


def test_merge_path_noop_when_already_true(tmp_path, monkeypatch):
    """All bootstrap keys already true: no write, no-change message."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"enabledPlugins": {key: True for key in mod._BOOTSTRAP_KEYS}}),
        encoding="utf-8",
    )
    before = settings_path.stat().st_mtime_ns

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is True
    assert "already carries UE override" in message
    assert settings_path.stat().st_mtime_ns == before


def test_malformed_settings_fails_open(tmp_path, monkeypatch):
    """Unparseable settings.json: returns ok=False with a diagnostic message,
    never raises -- matches the bash oracle's fail-open-and-silent contract
    from the calling hook's perspective."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text("{ not valid json", encoding="utf-8")

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is False
    assert "ERROR" in message


def test_mkdir_failure_fails_open(tmp_path, monkeypatch):
    """Review: code-reviewer (F3) -- mkdir() raising OSError must fail open
    (ok=False, ERROR-prefixed message), never raise, matching the malformed-
    JSON test's shape."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    def _raise_mkdir(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(mod.Path, "mkdir", _raise_mkdir)

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is False
    assert message.startswith("ERROR")


def test_write_failure_fails_open(tmp_path, monkeypatch):
    """Review: code-reviewer (F3) -- write_text() raising OSError on the
    fresh-write path must fail open (ok=False, ERROR-prefixed message)."""
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    def _raise_write_text(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod.Path, "write_text", _raise_write_text)

    ok, message = mod._run_bootstrap("unused-plugin-root", str(tmp_path))

    assert ok is False
    assert message.startswith("ERROR")


def _forbid_subprocess(*args, **kwargs):
    """Fail loud if the port ever regresses to spawning a subprocess -- the
    whole point of C5 is that _run_bootstrap no longer shells out at all."""
    raise AssertionError("_run_bootstrap must not spawn a subprocess (C5 native port)")
