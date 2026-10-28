"""Tests for coordinator_core.machine_profile."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core import deny_registry
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


def _nudges(env):
    """True for a one-line advisory that is not a deny."""
    assert env is not None and not mp.is_deny_envelope(env)
    text = (env.get("hookSpecificOutput") or {}).get("additionalContext") or env.get("systemMessage")
    return text.startswith(mp.ADVISORY_PREFIX) and "\n" not in text


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


@pytest.mark.parametrize("event", ["PreToolUse", "Stop"])
def test_unregistered_guard_nudges_once_and_never_denies_at_every_level(reg, event):
    for level in ("strict", "warn", "off"):
        _write(reg, '"coordinator.guard_level" = "%s"\n' % level)
        assert _nudges(mp.apply_guard_level("g", _deny(event)))


def test_unregistered_guard_logs_under_the_author_profile(reg, tmp_path):
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile() == "author"
    assert mp.guard_level("g") == "warn"
    assert mp.resolved_level("g") == "log"
    assert _nudges(mp.apply_guard_level("g", _deny()))


def test_per_guard_strict_key_cannot_lift_an_unregistered_guard(reg):
    _write(reg, '"coordinator.guard_level.g" = "strict"\n')
    assert mp.guard_level("g") == "strict"
    assert mp.resolved_level("g") == "log"
    assert _nudges(mp.apply_guard_level("g", _deny()))


def test_underscore_name_resolves_kebab_registration(reg):
    assert mp.resolved_level("destructive_rm") == "floor"
    assert mp.resolved_level("a_b") == "log"


def test_floor_guards_are_the_registry_and_name_the_degrading_set():
    assert mp.FLOOR_GUARDS == frozenset(deny_registry.GUARD_DENY_CLASS)
    assert "block-whole-filesystem-scan" in mp.FLOOR_GUARDS
    assert "guard-heavy-command-admission" in mp.FLOOR_GUARDS
    assert "block-external-workspace-vcs-write" in mp.FLOOR_GUARDS
    for demoted in (
        "block-em-post-fire-product-write",
        "block-approval-sentinel-creation",
        "block-consumed-handoff-edit",
        "block-topic-branch",
        "block-subagent-destructive-action",
        "block-unreal-engine-resave",
    ):
        assert demoted not in mp.FLOOR_GUARDS


def test_heavy_command_admission_blocks_at_every_level(reg):
    assert mp.apply_guard_level("guard-heavy-command-admission", _deny()) is not None
    assert mp.apply_guard_level("guard-heavy-command-admission", _deny(), subagent=True) is not None


def test_memory_store_cap_no_longer_denies_under_any_level(reg):
    for level in ("strict", "warn", "off"):
        _write(reg, '"coordinator.guard_level" = "%s"\n' % level)
        assert mp.resolved_level("guard-memory-store-cap") == "log"
        assert _nudges(mp.apply_guard_level("guard-memory-store-cap", _deny()))


def test_heavy_ue_launch_guard_is_a_subagent_floor():
    assert "guard-subagent-heavy-ue-launch" in mp.SUBAGENT_FLOOR_GUARDS
    assert _nudges(mp.apply_guard_level("guard-subagent-heavy-ue-launch", _deny()))
    deny = _deny()
    assert mp.apply_guard_level("guard-subagent-heavy-ue-launch", deny, subagent=True) is deny


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


def test_author_box_guard_level_fallback_is_warn(reg, tmp_path):
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile() == "author"
    assert mp.guard_level("g") == "warn"
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


def test_apply_guard_level_keeps_a_registered_deny_and_logs_the_rest(reg):
    deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
    for level in ("strict", "off", "warn"):
        _write(reg, '"coordinator.guard_level" = "%s"\n' % level)
        assert mp.apply_guard_level("destructive-rm", deny, risk="r.") is deny
        assert _nudges(mp.apply_guard_level("g", deny, risk="r."))
    assert mp.apply_guard_level("g", None, risk="r.") is None


def test_level_verb_is_a_well_formed_registry_set():
    import shlex

    argv = shlex.split(mp.LEVEL_VERB)
    assert argv[:2] == ["machine-local", "set"]
    assert argv[2] == mp.LEVEL_KEY
    assert argv[3] in mp.LEVELS


def test_advisory_tail_verbs_are_well_formed():
    import re
    import shlex

    text = mp.advisory_tail("some-guard")
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


def _author_repo(reg, tmp_path, extra=""):
    repo = tmp_path / "authoring"
    repo.mkdir(exist_ok=True)
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n%s' % (repo.as_posix(), extra))


def test_gem_cross_repo_commit_unset_is_on_for_consumer_and_author(reg, tmp_path):
    assert mp.machine_profile() == "consumer"
    assert mp.feature_enabled("gem_cross_repo_commit") is True
    _author_repo(reg, tmp_path)
    mp.reset_cache()
    assert mp.machine_profile() == "author"
    assert mp.feature_enabled("gem_cross_repo_commit") is True


def test_gem_cross_repo_commit_explicit_wins_both_ways(reg, tmp_path):
    _write(reg, '"coordinator.feature.gem_cross_repo_commit" = "off"\n')
    assert mp.feature_enabled("gem_cross_repo_commit") is False
    _write(reg, '"coordinator.feature.gem_cross_repo_commit" = "on"\n')
    assert mp.feature_enabled("gem_cross_repo_commit") is True
    _author_repo(reg, tmp_path, '"coordinator.feature.gem_cross_repo_commit" = "off"\n')
    assert mp.machine_profile() == "author"
    assert mp.feature_enabled("gem_cross_repo_commit") is False


def test_gem_cross_repo_commit_env_override_wins(reg, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_GEM_CROSS_REPO_COMMIT", "off")
    mp.reset_cache()
    assert mp.feature_enabled("gem_cross_repo_commit") is False


def test_gem_cross_repo_commit_leaves_other_defaults(reg):
    assert mp.feature_enabled("gem_cross_repo_commit") is True
    assert mp.feature_enabled("cross_repo_memos") is False
    assert mp.feature_enabled("publishing") is False
    assert mp.feature_enabled("doctrine_edit_gate") is True


_OOR = "out_of_repo_write_block"


def test_out_of_repo_write_block_registered():
    assert len(mp.FEATURES) == 5
    assert _OOR in mp.FEATURES
    assert not hasattr(mp, "OUT_OF_REPO_WRITE_GUARDS")


def test_out_of_repo_write_block_unset_follows_profile(reg, tmp_path):
    assert mp.out_of_repo_write_blocked() is False
    _author_repo(reg, tmp_path)
    mp.reset_cache()
    assert mp.machine_profile() == "author"
    assert mp.out_of_repo_write_blocked() is True


def test_out_of_repo_write_block_explicit_wins_on_both_profiles(reg, tmp_path):
    _write(reg, '"coordinator.feature.%s" = "on"\n' % _OOR)
    assert mp.out_of_repo_write_blocked() is True
    _author_repo(reg, tmp_path, '"coordinator.feature.%s" = "off"\n' % _OOR)
    assert mp.machine_profile() == "author"
    assert mp.out_of_repo_write_blocked() is False


@pytest.mark.parametrize(
    "name",
    [
        "bump-foreign-repo-write",
        "bump-outside-repo-write",
        "bump-out-of-repo-tool-write",
        "block-drive-root-new-dir-bash-write",
        "block-home-root-bash-write",
    ],
)
@pytest.mark.parametrize("block", ["on", "off"])
def test_out_of_repo_write_guards_never_deny_whatever_the_feature(reg, name, block):
    _write(reg, '"coordinator.feature.%s" = "%s"\n' % (_OOR, block))
    assert _nudges(mp.apply_guard_level(name, _deny()))


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


def test_suite_guard_keeps_its_block_for_subagent_and_em(monkeypatch):
    """An unscheduled suite run is box-degrading: the registered deny holds for both callers."""
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_CHECK-TEST-SUITE-INVOCATION", "strict")
    mp.reset_cache()
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "BLOCKED: suite"}}
    assert mp.is_deny_envelope(mp.apply_guard_level("check-test-suite-invocation", deny, subagent=True))
    assert mp.is_deny_envelope(mp.apply_guard_level("check-test-suite-invocation", deny))


def test_ladder_arm_env_pin_resolves_strict_on_consumer_box(reg, monkeypatch):
    """The arm pins strict by env var on a consumer-profile box with an empty registry."""
    assert mp.machine_profile() == "consumer"
    assert mp.guard_level("xs-lane-shell-write") == "warn"
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    assert mp.guard_level("xs-lane-shell-write") == "strict"
    assert mp.guard_level("nudge-em-code-dispatch") == "strict"


def test_ladder_arm_registry_pin_resolves_strict_and_is_never_stale(reg):
    assert mp.guard_level("nudge-em-code-dispatch") == "warn"
    _write(reg, '"coordinator.guard_level" = "strict"\n')
    assert mp.guard_level("nudge-em-code-dispatch") == "strict"
    _write(reg, '"coordinator.guard_level" = "warn"\n')
    assert mp.guard_level("nudge-em-code-dispatch") == "warn"


def test_resolved_level_is_floor_or_log_whatever_the_key(reg, monkeypatch):
    for level in ("warn", "strict", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        assert mp.resolved_level("nudge_em_code_dispatch") == "log"
        assert mp.resolved_level("destructive-rm") == "floor"


def test_envelope_records_floor_and_unregistered_returns_a_nudge(reg, monkeypatch):
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        assert _nudges(mp.apply_guard_level("nudge-em-code-dispatch", _deny()))
        out = mp.apply_guard_level("destructive-rm", _deny())
        assert out["guardLevel"] == "floor"
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


_OFF_LIST_OUT_OF_REPO = (
    "bump-foreign-repo-write",
    "bump-outside-repo-write",
    "bump-out-of-repo-tool-write",
    "block-drive-root-new-dir-bash-write",
    "block-home-root-bash-write",
)


@pytest.mark.parametrize("name", _OFF_LIST_OUT_OF_REPO)
def test_fresh_author_box_denies_no_recoverable_out_of_repo_write(reg, tmp_path, name):
    repo = tmp_path / "authoring"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    _write(reg, '"repos.content" = "%s"\n' % repo.as_posix())
    assert mp.machine_profile() == "author" and mp.out_of_repo_write_blocked() is True
    assert _nudges(mp.apply_guard_level(name, _deny()))
    assert mp.guard_level(name) == "warn"


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_one_event_yields_one_nudge_in_process(reg, tmp_path):
    repo = _git_repo(tmp_path)
    first = mp.apply_guard_level(
        "g1", _deny(), session_id="sess", cwd=str(repo), event_id="toolu_1"
    )
    second = mp.apply_guard_level(
        "g2", _deny(), session_id="sess", cwd=str(repo), event_id="toolu_1"
    )
    other = mp.apply_guard_level(
        "g1", _deny(), session_id="sess", cwd=str(repo), event_id="toolu_2"
    )
    assert _nudges(first) and second is None and _nudges(other)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_one_event_yields_one_nudge_across_two_hook_processes(reg, tmp_path):
    import json
    import subprocess
    import sys

    repo = _git_repo(tmp_path)
    code = (
        "import json,sys;from coordinator_core import machine_profile as mp;"
        "d={'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny',"
        "'permissionDecisionReason':'Bad.'}};"
        "o=mp.apply_guard_level(sys.argv[1],d,session_id='sess',cwd=sys.argv[2],event_id='toolu_x');"
        "print(json.dumps(o))"
    )
    env = dict(os.environ, MACHINE_LOCAL_REGISTRY_DIR=str(reg))
    outs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", code, g, str(repo)],
                capture_output=True, text=True, env=env, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        )
        for g in ("g1", "g2")
    ]
    assert sum(o is not None for o in outs) == 1


def test_dispatch_two_off_list_denies_on_one_event_yield_one_advisory(reg, tmp_path, monkeypatch):
    import json

    from coordinator_core.bash_guards import dispatch

    def boom():
        raise RuntimeError("x")

    def chain(*_a, **_k):
        return [
            dispatch.GuardEntry("off-list-a", boom, True, dispatch.GuardBand.CONFINEMENT_DENY),
            dispatch.GuardEntry("off-list-b", boom, True, dispatch.GuardBand.CONFINEMENT_DENY),
        ]

    monkeypatch.setattr(dispatch, "_build_guard_chain", chain)
    raw = json.dumps({
        "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "sess",
        "tool_use_id": "toolu_d", "cwd": str(_git_repo(tmp_path)),
    })
    out = dispatch.evaluate_payload_json(raw, collect_advisories=True)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["hookSpecificOutput"]["additionalContext"].startswith(mp.ADVISORY_PREFIX)
