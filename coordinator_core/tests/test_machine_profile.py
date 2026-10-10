"""Tests for coordinator_core.machine_profile."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core import machine_profile as mp

# The registry reader caches on mtime; each rewrite must advance it by a full
# second or a coarse-resolution filesystem serves the previous contents.
_last_mtime: dict = {}


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
    path = reg / "registry.local.toml"
    prev = _last_mtime.get(path, 0)
    stamp = max(path.stat().st_mtime_ns, prev + 1_000_000_000)
    os.utime(path, ns=(stamp, stamp))
    _last_mtime[path] = stamp


def _deny(event="PreToolUse", reason="Bad thing.\n\nSecond paragraph."):
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def test_non_deny_input_passes_through_unchanged(reg):
    _write(reg, '"coordinator.guard_level" = "off"\n')
    allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    for env in (allow, {}, {"x": 1}):
        assert mp.apply_guard_level("g", env) is env


def test_warn_shapes_follow_event(reg):
    _write(reg, '"coordinator.guard_level" = "warn"\n')
    pre = mp.apply_guard_level("g", _deny())["hookSpecificOutput"]
    assert pre["hookEventName"] == "PreToolUse"
    assert pre["permissionDecision"] == "allow"
    stop = mp.apply_guard_level("g", _deny("Stop"))["hookSpecificOutput"]
    assert stop["hookEventName"] == "Stop"
    assert "permissionDecision" not in stop
    assert "additionalContext" in stop


def test_risk_none_derives_first_paragraph(reg):
    _write(reg, '"coordinator.guard_level" = "warn"\n')
    text = mp.apply_guard_level("g", _deny())["hookSpecificOutput"]["additionalContext"]
    assert "Bad thing." in text
    assert "Second paragraph" not in text


def test_underscore_name_resolves_kebab_key(reg):
    _write(reg, '"coordinator.guard_level.a-b" = "off"\n')
    assert mp.apply_guard_level("a_b", _deny()) is None
    _write(reg, '"coordinator.guard_level" = "warn"\n')
    text = mp.apply_guard_level("a_c", _deny())["hookSpecificOutput"]["additionalContext"]
    assert "a-c" in text and "a_c" not in text


def test_floor_guards_has_the_thirteen_names():
    assert len(mp.FLOOR_GUARDS) == 13
    assert "block-approval-sentinel-creation" in mp.FLOOR_GUARDS
    assert "block-consumed-handoff-edit" in mp.FLOOR_GUARDS
    assert "block-whole-filesystem-scan" in mp.FLOOR_GUARDS


def test_heavy_command_admission_is_report_only_by_default(reg):
    # The morning flip restores strict and FLOOR_GUARDS membership:
    # state/bug-backlog/2026-10-10-heavy-command-admission-guard-is-report-8e3c3b48d41a.yaml.
    assert mp.GUARD_DEFAULT_LEVEL["guard-heavy-command-admission"] == "off"
    assert "guard-heavy-command-admission" not in mp.FLOOR_GUARDS
    assert mp.apply_guard_level("guard-heavy-command-admission", _deny()) is None
    assert mp.apply_guard_level("guard-heavy-command-admission", _deny(), subagent=True) is None


def test_heavy_ue_launch_guard_is_a_subagent_floor():
    assert "guard-subagent-heavy-ue-launch" in mp.SUBAGENT_FLOOR_GUARDS


@pytest.mark.parametrize("name", sorted(mp.FLOOR_GUARDS))
@pytest.mark.parametrize("level", ["warn", "off"])
def test_floor_guard_denies_at_every_level(reg, name, level):
    _write(reg, '"coordinator.guard_level" = "%s"\n' % level)
    deny = _deny()
    assert mp.apply_guard_level(name, deny) is deny
    assert mp.apply_guard_level(name.replace("-", "_"), deny) is deny


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
    _write(
        reg,
        '"repos.content" = "%s"\n"coordinator.guard_level" = "warn"\n' % repo.as_posix(),
    )
    assert mp.guard_level("g") == "warn"


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
    out = mp.apply_guard_level("some-guard", _deny(), risk="Risk.")
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


def test_source_default_rung(reg):
    assert mp.machine_profile_source() == ("consumer", "default", None)


def test_source_sentinel_rung_names_the_repo(reg, tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.coordinator_claude" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile_source() == ("author", "sentinel", repo.as_posix())
    assert mp.machine_profile() == "author"


def test_source_explicit_rung_beats_sentinel(reg, tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(
        reg,
        '"repos.c" = "%s"\n"coordinator.machine_profile" = "consumer"\n' % repo.as_posix(),
    )
    assert mp.machine_profile_source() == ("consumer", "explicit-key", mp.PROFILE_KEY)
    _write(reg, '"coordinator.machine_profile" = "author"\n')
    assert mp.machine_profile_source() == ("author", "explicit-key", mp.PROFILE_KEY)


def test_suite_guard_subagent_deny_survives_off(monkeypatch):
    """A subagent's suite deny is floor: even a per-guard `off` keeps it."""
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_CHECK-TEST-SUITE-INVOCATION", "off")
    mp.reset_cache()
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "BLOCKED: suite"}}
    assert mp.apply_guard_level("check-test-suite-invocation", deny, subagent=True) is deny
    assert mp.apply_guard_level("check-test-suite-invocation", deny) is None
