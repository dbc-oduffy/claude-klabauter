"""apply_settings_env ensures the settings parent dir before spawning the checker."""

from __future__ import annotations

import pytest

from coordinator_core.install import settings_env

_FAKE_CHECKER = (
    "import json, sys\n"
    "path = sys.argv[sys.argv.index('--settings') + 1]\n"
    "open(path + '.tmp1', 'w').write('{}')\n"
    "print(json.dumps({'applied': ['X'], 'findings': []}))\n"
)


def test_missing_settings_parent_dir_is_created_and_a_verdict_returned(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "check-settings-env.py").write_text(_FAKE_CHECKER, encoding="utf-8")
    monkeypatch.setattr(settings_env, "_journal_resolution", lambda *a, **k: None)
    settings = tmp_path / "home" / ".claude" / "settings.json"
    assert not settings.parent.exists()

    record = settings_env.apply_settings_env(plugin, settings)

    assert settings.parent.is_dir()
    assert record["applied"] == ["X"]


def test_missing_checker_still_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        settings_env.apply_settings_env(tmp_path / "nope", tmp_path / "h" / "settings.json")
    assert not (tmp_path / "h").exists()
