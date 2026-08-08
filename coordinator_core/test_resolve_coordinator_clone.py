"""
Tests for coordinator_core.resolve_coordinator_clone — native CLI-mode peer.

Port of: resolve-coordinator-clone.sh (example-doctrine-repo 290997c7, 2026-07-22)

Covers: the dev-vs-oss mode selector (passthrough / explicit / marker
auto-discovery / ambiguity), the 5-rung clone-root ladder, the 7-rung
content-root ladder, the durable-then-legacy .doe-root pointer read, the
numeric-compare versioned-cache picker, and the CLI wrapper's exit codes.

Spec backlink: docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md § C11/C2
"""

from __future__ import annotations

import os

import pytest

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
    """No pointer file, no registry, no OSS install, no dev marker anywhere --
    the "nothing resolvable" baseline each test builds on top of."""
    _clear_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setattr(rcc, "_registry_example_doctrine_repo", lambda: None)
    monkeypatch.setattr(rcc, "_registry_live_path", lambda: None)
    return home


# --- _resolve_source_mode ---------------------------------------------------


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
    """Falsifying case for the codename-free defect: registry unreachable,
    pointer empty, and the flat `~/.claude/plugins/coordinator-claude`
    layout present WITHOUT a `.claude-plugin/plugin.json` marketplace
    manifest (e.g. a raw git checkout dropped at the flat path) and WITHOUT
    a `.coordinator-dev-repo` dev marker. Previously this fell through every
    rung and raised "no coordinator source found" even though a perfectly
    good coordinator root sat right there. Must now resolve instead of
    raising."""
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    flat.mkdir(parents=True)
    assert rcc._resolve_source_mode("git-ops") == "dev"


def test_clone_root_flat_unmarked_no_manifest_resolves(isolated_home):
    """Same defect, exercised end-to-end through resolve_clone_root(): an
    OSS box with no registry, no pointer, no dev marker, and a flat clone
    with `.git` but no marketplace manifest now resolves the flat clone
    instead of raising "no coordinator source found"."""
    flat = isolated_home / ".claude" / "plugins" / "coordinator-claude"
    (flat / ".git").mkdir(parents=True)
    assert rcc.resolve_clone_root() == str(flat)


def test_mode_registry_ranks_above_pointer_file(isolated_home, monkeypatch, tmp_path):
    """DR-071: registry `repos.example_doctrine_repo` must be tried BEFORE the `.doe-root`
    pointer file in the marker auto-discovery candidate ladder (inverted
    2026-07-22 from the prior pointer-first order). The registry candidate
    here does not exist on disk (unresolvable), while the pointer candidate
    would resolve cleanly with a dev marker IF it were consulted — proving
    the registry rung wins the race, not merely that "a" candidate resolves.
    """
    monkeypatch.setattr(rcc, "_registry_example_doctrine_repo", lambda: str(tmp_path / "registry-nonexistent"))
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


# --- resolve_clone_root ------------------------------------------------------


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


# --- resolve_content_root ----------------------------------------------------


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


# --- _newest_cache_dir numeric-compare ---------------------------------------


def test_newest_cache_numeric_not_lexicographic(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cache_parent = tmp_path / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator"
    (cache_parent / "2.9.0").mkdir(parents=True)
    (cache_parent / "2.10.0").mkdir(parents=True)
    assert rcc._newest_cache_dir() == str(cache_parent / "2.10.0")


def test_newest_cache_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert rcc._newest_cache_dir() is None


# --- _read_doe_root_pointer: durable-first, legacy-fallback ------------------


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


# --- _registry_live_path: no-subprocess common path (2026-07-28 fix) ---------
#
# Spec backlink: hot-path-spawn defect — `_registry_live_path` was CLI-only
# (`machine-local get plugin.mirrors.coordinator-claude.live_path`), so every
# `resolve_content_root()` call on a machine with `machine-local` on PATH
# spawned a subprocess (~80ms warm) on this rung even though it sits on the
# COMMON path (rung 3 of 7 in `resolve_content_root`), not a last resort. See
# `_registry_live_path`'s own docstring for the narrative; these tests assert
# the fix by BEHAVIOUR (spawn count), not timing, since timing is flaky.


def test_registry_live_path_resolves_without_subprocess(monkeypatch):
    """The common case: `registry_get` resolves the key directly (tomllib,
    no subprocess) -- `_machine_local_get`/`subprocess.run` must never be
    reached."""
    monkeypatch.setattr(
        rcc, "registry_get", lambda key: "/from-registry-toml" if key == "plugin.mirrors.coordinator-claude.live_path" else None
    )

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("subprocess.run must not be called when registry_get resolves the key")

    monkeypatch.setattr(rcc.subprocess, "run", _fail_if_called)

    assert rcc._registry_live_path() == "/from-registry-toml"


def test_registry_live_path_falls_back_to_cli_when_registry_get_empty(monkeypatch):
    """Genuine fallback: `registry_get` can't resolve the key (e.g. present
    under `machine-local`'s CLI-managed state but not yet mirrored into the
    TOML files) -- the CLI subprocess rung still fires and its result is
    used."""
    monkeypatch.setattr(rcc, "registry_get", lambda key: None)
    monkeypatch.setattr(rcc, "_machine_local_get", lambda key: "/from-cli-fallback")

    assert rcc._registry_live_path() == "/from-cli-fallback"


def test_resolve_content_root_common_path_spawns_no_subprocess(monkeypatch, tmp_path):
    """End-to-end: a dev-mode `resolve_content_root()` resolution that lands
    on the registry-live-path rung must not spawn any subprocess. Monkeypatch
    the spawn primitive `subprocess.run` itself (not timing) -- this is the
    behavioural assertion the workstream asked for. Deliberately does NOT use
    the `isolated_home` fixture, which stubs `_registry_live_path` to a
    constant `None` -- this test needs the REAL `_registry_live_path` (and
    its now-cheap `registry_get` rung) exercised end to end."""
    _clear_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SOURCE_MODE", "dev")
    monkeypatch.setattr(rcc, "_registry_example_doctrine_repo", lambda: None)

    live = tmp_path / "live-content-root"
    live.mkdir()
    monkeypatch.setattr(rcc, "registry_get", lambda key: str(live) if key == "plugin.mirrors.coordinator-claude.live_path" else None)

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("resolve_content_root must not spawn a subprocess on the common path")

    monkeypatch.setattr(rcc.subprocess, "run", _fail_if_called)

    assert rcc.resolve_content_root() == str(live)


# --- CLI wrapper --------------------------------------------------------------


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
