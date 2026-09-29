"""Tests for coordinator_core.machine_profile."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import machine_profile as mp


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    d = tmp_path / "reg"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    for k in list(__import__("os").environ):
        if k.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(k)
    mp.reset_cache()
    yield d
    mp.reset_cache()


def _write(reg, text):
    (reg / "registry.local.toml").write_text(text, encoding="utf-8")


def test_empty_registry_is_consumer_warn(reg):
    assert mp.machine_profile() == "consumer"
    assert mp.guard_level("any-guard") == "warn"


def test_registered_dev_repo_sentinel_is_author_strict(reg, tmp_path):
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile() == "author"
    assert mp.guard_level("g") == "strict"


def test_registered_repo_without_sentinel_stays_consumer(reg, tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    _write(reg, '"repos.plain" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile() == "consumer"


def test_explicit_profile_beats_sentinel(reg, tmp_path):
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(
        reg,
        '"repos.content" = "%s"\n"coordinator.machine_profile" = "consumer"\n'
        % repo.as_posix(),
    )
    assert mp.machine_profile() == "consumer"


def test_global_and_per_guard_levels(reg):
    _write(
        reg,
        '"coordinator.guard_level" = "off"\n'
        '"coordinator.guard_level.block-x" = "strict"\n',
    )
    assert mp.guard_level("other") == "off"
    assert mp.guard_level("block-x") == "strict"


def test_invalid_value_falls_through_to_default(reg):
    _write(reg, '"coordinator.guard_level" = "loud"\n')
    assert mp.guard_level("g") == "warn"


def test_env_override_wins(reg, monkeypatch):
    _write(reg, '"coordinator.guard_level" = "off"\n')
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    assert mp.guard_level("g") == "strict"


def test_registry_change_is_seen_without_reset(reg):
    assert mp.guard_level("g") == "warn"
    _write(reg, '"coordinator.guard_level" = "strict"\n')
    assert mp.guard_level("g") == "strict"


def test_zero_subprocesses(reg, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    mp.machine_profile()
    mp.guard_level("g")


def test_apply_guard_level_maps_three_levels(reg):
    deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
    _write(reg, '"coordinator.guard_level" = "strict"\n')
    assert mp.apply_guard_level("g", deny, risk="r.") is deny
    _write(reg, '"coordinator.guard_level" = "off"\n')
    assert mp.apply_guard_level("g", deny, risk="r.") is None
    _write(reg, '"coordinator.guard_level" = "warn"\n')
    out = mp.apply_guard_level("g", deny, risk="Writes leave the repo.")
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "Writes leave the repo." in hso["additionalContext"]
    assert "machine-local set coordinator.guard_level" in hso["additionalContext"]
    assert mp.apply_guard_level("g", None, risk="r.") is None


def test_level_verb_is_a_well_formed_registry_set():
    import shlex

    argv = shlex.split(mp.LEVEL_VERB)
    assert argv[:2] == ["machine-local", "set"]
    assert argv[2] == mp.LEVEL_KEY
    assert argv[3] in mp.LEVELS


def test_warn_advisory_verbs_are_well_formed(reg):
    import re
    import shlex

    _write(reg, '"coordinator.guard_level" = "warn"\n')
    out = mp.apply_guard_level("some-guard", {"x": 1}, risk="Risk.")
    text = out["hookSpecificOutput"]["additionalContext"]
    verbs = re.findall(r"`(machine-local set [^`]+)`", text)
    assert len(verbs) == 2
    for verb in verbs:
        argv = shlex.split(verb)
        assert argv[3] in mp.LEVELS
        assert argv[2].startswith(mp.LEVEL_KEY)


@pytest.mark.parametrize("feature", ["cross_repo_memos", "publishing"])
def test_feature_default_follows_profile(reg, tmp_path, feature):
    assert mp.feature_enabled(feature) is False
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n' % repo.as_posix())
    mp.reset_cache()
    assert mp.feature_enabled(feature) is True


@pytest.mark.parametrize("feature", ["cross_repo_memos", "publishing"])
def test_feature_explicit_overrides_both_ways(reg, tmp_path, feature):
    _write(reg, '"coordinator.feature.%s" = "on"\n' % feature)
    assert mp.feature_enabled(feature) is True
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n"coordinator.feature.%s" = "off"\n' % (repo.as_posix(), feature))
    assert mp.machine_profile() == "author"
    assert mp.feature_enabled(feature) is False


def test_features_are_independent(reg):
    _write(reg, '"coordinator.feature.publishing" = "on"\n')
    assert mp.feature_enabled("publishing") is True
    assert mp.feature_enabled("cross_repo_memos") is False
