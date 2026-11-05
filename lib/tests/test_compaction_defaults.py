import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compaction_defaults as cd  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(cd.WINDOW_ENV, raising=False)
    monkeypatch.delenv(cd.PCT_ENV, raising=False)


def _write_settings(tmp_path, data):
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def test_default_window_constant():
    assert cd.DEFAULT_AUTOCOMPACT_WINDOW == 400000
    assert cd.WINDOW_KEY == "autoCompactWindow"


def test_settings_reads_dict_and_degrades(tmp_path):
    assert cd._settings() == {}
    _write_settings(tmp_path, {"a": 1})
    assert cd._settings() == {"a": 1}
    (tmp_path / "settings.json").write_text("[1]", encoding="utf-8")
    assert cd._settings() == {}


def test_configured_window_precedence(tmp_path, monkeypatch):
    assert cd.configured_window() is None
    _write_settings(tmp_path, {cd.WINDOW_KEY: 300000, "env": {cd.WINDOW_ENV: "250000"}})
    assert cd.configured_window() == 250000.0
    monkeypatch.setenv(cd.WINDOW_ENV, "200000")
    assert cd.configured_window() == 200000.0
    monkeypatch.delenv(cd.WINDOW_ENV)
    _write_settings(tmp_path, {cd.WINDOW_KEY: 300000})
    assert cd.configured_window() == 300000.0


def test_threshold_uses_top_level_window_clamped_to_model():
    tokens, why = cd.threshold_tokens(1_000_000, {cd.WINDOW_KEY: 400000})
    assert tokens == 400000 - cd.BUFFER_TOKENS
    assert "window 400000" in why
    tokens, _ = cd.threshold_tokens(200000, {cd.WINDOW_KEY: 400000})
    assert tokens == 200000 - cd.BUFFER_TOKENS


def test_threshold_pct_override_lowers_budget():
    tokens, why = cd.threshold_tokens(1_000_000, {cd.WINDOW_KEY: 400000, "env": {cd.PCT_ENV: "40"}})
    assert tokens == 160000
    assert cd.PCT_ENV in why


def test_threshold_unknown_window():
    assert cd.threshold_tokens(None, {}) == (None, "window size unknown")


def test_threshold_accepts_bare_env_dict():
    tokens, _ = cd.threshold_tokens(1_000_000, {cd.WINDOW_ENV: "500000"})
    assert tokens == 500000 - cd.BUFFER_TOKENS


def test_threshold_reads_settings_file_when_none(tmp_path):
    _write_settings(tmp_path, {cd.WINDOW_KEY: 400000})
    tokens, _ = cd.threshold_tokens(1_000_000)
    assert tokens == 400000 - cd.BUFFER_TOKENS


def test_apply_default_sets_when_absent_never_overwrites():
    settings = {}
    assert cd.apply_default(settings) is True
    assert settings == {cd.WINDOW_KEY: 400000}
    operator = {cd.WINDOW_KEY: 123}
    assert cd.apply_default(operator) is False
    assert operator == {cd.WINDOW_KEY: 123}


def test_apply_default_to_file_writes_lf_and_is_idempotent(tmp_path):
    target = tmp_path / "out" / "settings.json"
    assert cd.apply_default_to_file(target) is True
    raw = target.read_bytes()
    assert b"\r" not in raw
    assert json.loads(raw)[cd.WINDOW_KEY] == 400000
    assert cd.apply_default_to_file(target) is False


def test_apply_default_to_file_keeps_other_keys(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")
    assert cd.apply_default_to_file(target) is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"env": {"X": "1"}, cd.WINDOW_KEY: 400000}
