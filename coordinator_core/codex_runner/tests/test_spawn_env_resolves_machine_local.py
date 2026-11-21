"""A child spawned with the workflow or grok env resolves a machine-local key and keeps the user-profile context."""
import os
import shutil
from pathlib import Path

import pytest

from coordinator_core.codex_runner import grok, review, workflow

_PROFILE_KEYS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH")


@pytest.fixture
def settings_home(tmp_path, monkeypatch):
    template = Path(__file__).resolve().parents[3] / "coordinator" / "templates" / "bin"
    home = tmp_path / "settings"
    (home / "bin").mkdir(parents=True)
    for name in ("_machine_local.py", "claude_machine_local.py"):
        shutil.copy(template / name, home / "bin" / name)
    (home / "machine-local").mkdir()
    (home / "machine-local" / "registry.local.toml").write_text(
        "[repos]\nprobe_repo = '" + str(tmp_path) + "'\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    for key in _PROFILE_KEYS:
        monkeypatch.setenv(key, str(tmp_path / key))
    return home


def _grok_env():
    return grok._build_env()


@pytest.mark.parametrize("build_env", [workflow._environment, _grok_env], ids=["workflow", "grok"])
def test_user_profile_keys_forwarded_when_present_and_only_then(build_env, settings_home, monkeypatch):
    env = build_env()
    for key in (*_PROFILE_KEYS, "COORDINATOR_SETTINGS_HOME"):
        assert env[key] == os.environ[key]
    monkeypatch.delenv("HOMEDRIVE")
    monkeypatch.delenv("HOMEPATH")
    env = build_env()
    assert "HOMEDRIVE" not in env and "HOMEPATH" not in env


def test_one_profile_tuple_names_the_required_keys():
    assert set(review.USER_PROFILE_ENV) >= {
        "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERNAME",
        "COORDINATOR_SETTINGS_HOME",
    }


def test_api_keys_stay_out_of_both_envs(settings_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("XAI_API_KEY", "secret")
    for env in (workflow._environment(), grok._build_env()):
        assert "OPENAI_API_KEY" not in env and "XAI_API_KEY" not in env
