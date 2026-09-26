
from __future__ import annotations

import os

import pytest

from coordinator_core import _claude_klabauter_root as _claude_klabauter_root_mod
from coordinator_core import resolve_coordinator_clone as rcc


def _clear_env(monkeypatch):
    for var in (
        "COORDINATOR_CLONE",
        "COORDINATOR_ROOT",
        "CLAUDE_PLUGIN_ROOT",
        "COORDINATOR_SOURCE_MODE",
        "COORDINATOR_SETTINGS_HOME",
        "CLAUDE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setattr(rcc, "_registry_doe_claude", lambda: None)
    monkeypatch.setattr(rcc, "_registry_live_path", lambda: None)
    return home


def test_mode_passthrough_git_ops_on_coordinator_clone(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_CLONE", "/anything")
    assert rcc._resolve_source_mode("git-ops") == "passthrough"


def test_mode_passthrough_content_on_plugin_root_or_coordinator_root(isolated_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/anything")
    assert rcc._resolve_source_mode("content") == "passthrough"
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    monkeypatch.setenv("COORDINATOR_ROOT", "/anything")
    assert rcc._resolve_source_mode("content") == "passthrough"


def test_mode_explicit_dev_or_oss(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    assert rcc._resolve_source_mode("git-ops") == "dev"
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "oss")
    assert rcc._resolve_source_mode("git-ops") == "oss"


def test_mode_explicit_invalid_value_fails_loud(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "bogus")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="bogus"):
        rcc._resolve_source_mode("git-ops")


def test_mode_no_source_found_fails_loud(isolated_home):
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no coordinator source found"):
        rcc._resolve_source_mode("git-ops")


def test_mode_no_source_found_carries_no_source_found_flag(isolated_home):
    with pytest.raises(rcc.ResolveCoordinatorCloneError) as exc_info:
        rcc._resolve_source_mode("git-ops")
    assert exc_info.value.no_source_found is True


def test_mode_explicit_invalid_value_does_not_carry_no_source_found_flag(isolated_home, monkeypatch):
    """A bad `COORDINATOR_SOURCE_MODE` value is an actionable misconfiguration,
    not "nothing to report" -- must NOT be classified alongside the silent
    no-source-anywhere case."""
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "bogus")
    with pytest.raises(rcc.ResolveCoordinatorCloneError) as exc_info:
        rcc._resolve_source_mode("git-ops")
    assert exc_info.value.no_source_found is False


def test_mode_oss_present_alone_resolves_oss(isolated_home):
    plugin_json = isolated_home / ".claude" / "plugins" / "coordinator-claude" / ".claude-plugin"
    plugin_json.mkdir(parents=True)
    (plugin_json / "plugin.json").write_text("{}")
    assert rcc._resolve_source_mode("git-ops") == "oss"


def test_mode_unmarked_sole_candidate_resolves_dev(isolated_home, monkeypatch):
    candidate = isolated_home.parent / "candidate-clone"
    candidate.mkdir()
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(candidate))
    assert rcc._resolve_source_mode("git-ops") == "dev"


def test_mode_dev_marker_present_resolves_dev_even_with_oss(isolated_home, monkeypatch):
    candidate = isolated_home.parent / "candidate-clone"
    candidate.mkdir()
    (candidate / ".coordinator-dev-repo").write_text("")
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(candidate))
    plugin_json = isolated_home / ".claude" / "plugins" / "coordinator-claude" / ".claude-plugin"
    plugin_json.mkdir(parents=True)
    (plugin_json / "plugin.json").write_text("{}")
    assert rcc._resolve_source_mode("git-ops") == "dev"


def test_mode_flat_layout_unmarked_no_manifest_resolves_dev(isolated_home):
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    (flat / "schemas").mkdir(parents=True)
    (flat / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    assert rcc._resolve_source_mode("git-ops") == "dev"


def test_mode_flat_layout_bare_empty_dir_fails_loud(isolated_home):
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    flat.mkdir(parents=True)
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no coordinator source found"):
        rcc._resolve_source_mode("git-ops")


def test_clone_root_flat_unmarked_no_manifest_resolves(isolated_home):
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    (flat / ".git").mkdir(parents=True)
    assert rcc.resolve_clone_root() == str(flat)


def test_mode_registry_ranks_above_pointer_file(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setattr(rcc, "_registry_doe_claude", lambda: str(tmp_path / "registry-nonexistent"))
    monkeypatch.setattr(rcc, "_registry_live_path", lambda: None)
    pointer_candidate = tmp_path / "pointer-candidate"
    pointer_candidate.mkdir()
    (pointer_candidate / ".coordinator-dev-repo").write_text("")
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(pointer_candidate))

    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no coordinator source found"):
        rcc._resolve_source_mode("git-ops")


def test_mode_unmarked_candidate_plus_oss_is_ambiguous(isolated_home, monkeypatch):
    candidate = isolated_home.parent / "candidate-clone"
    candidate.mkdir()
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(candidate))
    plugin_json = isolated_home / ".claude" / "plugins" / "coordinator-claude" / ".claude-plugin"
    plugin_json.mkdir(parents=True)
    (plugin_json / "plugin.json").write_text("{}")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="ambiguous"):
        rcc._resolve_source_mode("git-ops")


def test_mode_ambiguous_does_not_carry_no_source_found_flag(isolated_home, monkeypatch):
    """The ambiguous-source failure is actionable (the message tells the
    operator to set `COORDINATOR_SOURCE_MODE`) -- it must not be silenced
    by a caller that only suppresses the truly-empty case."""
    candidate = isolated_home.parent / "candidate-clone"
    candidate.mkdir()
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(candidate))
    plugin_json = isolated_home / ".claude" / "plugins" / "coordinator-claude" / ".claude-plugin"
    plugin_json.mkdir(parents=True)
    (plugin_json / "plugin.json").write_text("{}")
    with pytest.raises(rcc.ResolveCoordinatorCloneError) as exc_info:
        rcc._resolve_source_mode("git-ops")
    assert exc_info.value.no_source_found is False


def test_clone_root_dev_env_var_must_have_git(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    clone = tmp_path / "clone-no-git"
    clone.mkdir()
    monkeypatch.setenv("COORDINATOR_CLONE", str(clone))
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no .git directory"):
        rcc.resolve_clone_root()


def test_clone_root_dev_env_var_with_git_wins(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    clone = tmp_path / "clone-with-git"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_CLONE", str(clone))
    assert rcc.resolve_clone_root() == str(clone)


def test_clone_root_dev_falls_through_registry_to_pointer_to_flat(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    doe_root = tmp_path / "doe-root"
    (doe_root / ".git").mkdir(parents=True)
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(doe_root))
    assert rcc.resolve_clone_root() == str(doe_root)


def test_clone_root_oss_mode_requires_flat_git(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "oss")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="OSS mode selected"):
        rcc.resolve_clone_root()
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    (flat / ".git").mkdir(parents=True)
    assert rcc.resolve_clone_root() == str(flat)


def test_clone_root_fail_loud_when_nothing_resolves(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: "")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no git-backed coordinator clone found"):
        rcc.resolve_clone_root()


def test_content_root_passthrough_plugin_root(isolated_home, monkeypatch, tmp_path):
    root = tmp_path / "plugin-root"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert rcc.resolve_content_root() == str(root)


def test_content_root_plugin_root_must_exist(isolated_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/does/not/exist")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="does not exist"):
        rcc.resolve_content_root()


def test_content_root_dev_registry_live_path_wins_over_cache(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    live = tmp_path / "live-clone"
    live.mkdir()
    monkeypatch.setattr(rcc, "_registry_live_path", lambda: str(live))
    monkeypatch.setattr(rcc, "_newest_cache_dir", lambda: "/should/not/be/used")
    assert rcc.resolve_content_root() == str(live)


def test_content_root_dev_pointer_appends_coordinator_subdir(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    doe_root = tmp_path / "doe-root"
    (doe_root / "coordinator").mkdir(parents=True)
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: str(doe_root))
    assert rcc.resolve_content_root() == str(doe_root / "coordinator")


def test_content_root_oss_mode_uses_newest_cache(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "oss")
    cache_parent = isolated_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator"
    (cache_parent / "2.9.0").mkdir(parents=True)
    (cache_parent / "2.10.0").mkdir(parents=True)
    assert rcc.resolve_content_root() == str(cache_parent / "2.10.0")


def test_content_root_fail_loud_when_nothing_resolves(isolated_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: "")
    with pytest.raises(rcc.ResolveCoordinatorCloneError, match="no readable coordinator content root found"):
        rcc.resolve_content_root()


def test_newest_cache_numeric_not_lexicographic(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cache_parent = tmp_path / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator"
    (cache_parent / "2.9.0").mkdir(parents=True)
    (cache_parent / "2.10.0").mkdir(parents=True)
    assert rcc._newest_cache_dir() == str(cache_parent / "2.10.0")


def test_newest_cache_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert rcc._newest_cache_dir() is None


def test_pointer_durable_wins_over_legacy(monkeypatch, tmp_path):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    (settings_home / "machine-local" / ".doe-root").write_text("/from-durable\n")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    legacy_home = tmp_path / "legacy-home"
    (legacy_home / ".claude").mkdir(parents=True)
    (legacy_home / ".claude" / ".doe-root").write_text("/from-legacy\n")
    monkeypatch.setenv("CLAUDE_HOME", str(legacy_home))

    assert rcc._read_doe_root_pointer() == "/from-durable"


def test_pointer_falls_back_to_legacy_when_durable_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-empty"))
    legacy_home = tmp_path / "legacy-home"
    (legacy_home / ".claude").mkdir(parents=True)
    (legacy_home / ".claude" / ".doe-root").write_text("/from-legacy\n")
    monkeypatch.setenv("CLAUDE_HOME", str(legacy_home))

    assert rcc._read_doe_root_pointer() == "/from-legacy"


def test_pointer_empty_when_neither_present(isolated_home):
    assert rcc._read_doe_root_pointer() == ""


# the fix by BEHAVIOUR (spawn count), not timing, since timing is flaky.


def test_registry_live_path_resolves_without_subprocess(monkeypatch):
    monkeypatch.setattr(
        rcc, "registry_get", lambda key: "/from-registry-toml" if key == "plugin.mirrors.coordinator-claude.live_path" else None
    )

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("subprocess.run must not be called when registry_get resolves the key")

    monkeypatch.setattr(_claude_klabauter_root_mod.subprocess, "run", _fail_if_called)

    assert rcc._registry_live_path() == "/from-registry-toml"


def test_registry_live_path_falls_back_to_cli_when_registry_get_empty(monkeypatch):
    monkeypatch.setattr(rcc, "registry_get", lambda key: None)
    monkeypatch.setattr(rcc, "_machine_local_get", lambda key: "/from-cli-fallback")

    assert rcc._registry_live_path() == "/from-cli-fallback"


def test_resolve_content_root_common_path_spawns_no_subprocess(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    monkeypatch.setattr(rcc, "_registry_doe_claude", lambda: None)

    live = tmp_path / "live-content-root"
    live.mkdir()
    monkeypatch.setattr(rcc, "registry_get", lambda key: str(live) if key == "plugin.mirrors.coordinator-claude.live_path" else None)

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("resolve_content_root must not spawn a subprocess on the common path")

    monkeypatch.setattr(_claude_klabauter_root_mod.subprocess, "run", _fail_if_called)

    assert rcc.resolve_content_root() == str(live)


def test_main_usage_error_wrong_arg_count(capsys):
    assert rcc.main([]) == 2
    assert rcc.main(["--clone-root", "extra"]) == 2


def test_main_usage_error_unknown_flag(capsys):
    assert rcc.main(["--bogus"]) == 2


def test_main_resolution_failure_returns_1(isolated_home, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    monkeypatch.setattr(rcc, "_read_doe_root_pointer", lambda: "")
    assert rcc.main(["--clone-root"]) == 1
    assert "no git-backed coordinator clone found" in capsys.readouterr().err


def test_main_success_prints_path_no_trailing_newline(isolated_home, monkeypatch, capsys, tmp_path):
    root = tmp_path / "plugin-root"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert rcc.main(["--content-root"]) == 0
    out = capsys.readouterr().out
    assert out == str(root)


def test_main_accepts_legacy_aliases(isolated_home, monkeypatch, capsys, tmp_path):
    root = tmp_path / "plugin-root"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert rcc.main(["--for-content"]) == 0
