"""em_id_for_root signs the group-em home as group-em-em and leaves other roots alone."""

from __future__ import annotations

import pytest

from coordinator_core import machine_resolver


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    reg = tmp_path / "registry"
    settings.mkdir()
    reg.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for k in ("MACHINE_LOCAL_COORDINATOR_GROUP_EM_HOME", "MACHINE_LOCAL_COORDINATOR_GENERAL_WORK_HOME"):
        monkeypatch.delenv(k, raising=False)
    return settings, reg


def test_default_home(env):
    settings, _ = env
    home = settings / "group-em"
    home.mkdir()
    assert machine_resolver.em_id_for_root(str(home), {}) == "group-em-em"


def test_configured_home(env, tmp_path):
    _, reg = env
    home = tmp_path / "gem-box"
    home.mkdir()
    (reg / "registry.toml").write_text(
        '[coordinator]\ngroup_em_home = "%s"\n' % str(home).replace("\\", "/"), encoding="utf-8"
    )
    assert machine_resolver.em_id_for_root(str(home), {}) == "group-em-em"


def test_legacy_home(env):
    settings, _ = env
    home = settings / "general-work"
    home.mkdir()
    assert machine_resolver.em_id_for_root(str(home), {}) == "group-em-em"


def test_unregistered_non_home_keeps_basename(env, tmp_path):
    other = tmp_path / "some-repo"
    other.mkdir()
    assert machine_resolver.em_id_for_root(str(other), {}) == "some-repo-em"
