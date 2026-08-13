"""Tests for coordinator_core.resolution.facade — the Tier-A resolution
facade's two structurally-distinct guard methods.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md § W1-A1 [DEAD-CITATION: plan file never committed to this repo]
Bash-parity fixture reuse: coordinator_core/test_trusted_root_guard.py
"""

from __future__ import annotations

import os
import shutil

import pytest

import coordinator_core.resolution.facade as facade_module
from coordinator_core.resolution.facade import (
    OperatorConfigError,
    guard_plugin_root,
    resolve_operator_config,
)
from coordinator_core.trusted_root_guard import (
    UntrustedRootError,
    coordinator_trusted_root_guard,
)


def _guard_env(**overrides):
    """Mirrors test_trusted_root_guard.py's _env() helper exactly, so the
    parity fixtures below stay byte-identical to that module's corpus."""
    base = {"HOME": "/home/tester", "CLAUDE_HOME": "", "COORDINATOR_PLUGIN_ROOT_TRUSTED": ""}
    base.update(overrides)
    if base["CLAUDE_HOME"] == "":
        base.pop("CLAUDE_HOME")
    return base


# ---------------------------------------------------------------------------
# AC-2 regression test: resolve_operator_config NEVER calls the trust guard;
# guard_plugin_root ALWAYS routes through it.
# ---------------------------------------------------------------------------


def test_resolve_operator_config_never_invokes_trust_guard(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        facade_module,
        "coordinator_trusted_root_guard",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    doe_root = tmp_path / "coordinator-claude"
    doe_root.mkdir()

    env = {
        "COORDINATOR_SETTINGS_HOME": str(settings_home),
        "HOME": str(tmp_path / "home"),
        "MACHINE_LOCAL_REGISTRY_DIR": str(tmp_path / "no-such-registry-dir"),
    }
    (settings_home / "machine-local").mkdir(parents=True, exist_ok=True)
    (settings_home / "machine-local" / ".claude-klabauter-root").write_text(str(claude_klabauter_root) + "\n")
    (settings_home / "machine-local" / ".doe-root").write_text(str(doe_root) + "\n")

    result = resolve_operator_config(env=env)

    assert calls == []
    assert result == {
        "settings_home": str(settings_home),
        "claude_klabauter_bin": str(claude_klabauter_root / "coordinator" / "bin"),
        "claude_klabauter_root": str(claude_klabauter_root),
        "doe_root": str(doe_root),
    }


def test_guard_plugin_root_always_routes_through_trust_guard(monkeypatch):
    calls = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(facade_module, "coordinator_trusted_root_guard", _spy)

    env = _guard_env()
    result = guard_plugin_root("/home/tester/.claude/x", mode="fail-loud", env=env)

    assert result is True
    assert len(calls) == 1
    assert calls[0]["mode"] == "fail-loud"
    assert calls[0]["root"] == "/home/tester/.claude/x"
    assert calls[0]["env"] == env


# ---------------------------------------------------------------------------
# guard_plugin_root <-> coordinator_trusted_root_guard byte-identical
# verdict parity, reusing test_trusted_root_guard.py's fixture corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,root,env",
    [
        ("fail-loud", "/home/tester/.claude/x", _guard_env()),
        ("fail-open", "/home/tester/.claude/x", _guard_env()),
        ("fail-open", "/tmp/evil", _guard_env()),
        ("fail-open", "", _guard_env()),
        ("fail-open", "/does/not/exist/anywhere", _guard_env()),
        ("fail-open", "/home/tester/.claude/../../tmp/evil", _guard_env()),
        (
            "fail-loud",
            "/home/tester/.claude/../../tmp/evil",
            _guard_env(COORDINATOR_PLUGIN_ROOT_TRUSTED="1"),
        ),
        ("fail-open", "/tmp/some/plugin/dir", _guard_env(COORDINATOR_PLUGIN_ROOT_TRUSTED="1")),
    ],
)
def test_guard_plugin_root_matches_delegate_across_fixture_matrix(mode, root, env):
    try:
        expected = coordinator_trusted_root_guard(mode=mode, root=root, env=env)
    except UntrustedRootError:
        with pytest.raises(UntrustedRootError):
            guard_plugin_root(root, mode=mode, env=env)
        return

    assert guard_plugin_root(root, mode=mode, env=env) == expected


def test_guard_plugin_root_fail_loud_untrusted_raises_parity():
    env = _guard_env()
    with pytest.raises(UntrustedRootError):
        coordinator_trusted_root_guard(mode="fail-loud", root="/tmp/evil", env=env)
    with pytest.raises(UntrustedRootError):
        guard_plugin_root("/tmp/evil", mode="fail-loud", env=env)


def test_guard_plugin_root_mode_empty_raises_value_error_parity():
    env = _guard_env()
    with pytest.raises(ValueError):
        coordinator_trusted_root_guard(mode="", root="/tmp/evil", env=env)
    with pytest.raises(ValueError):
        guard_plugin_root("/tmp/evil", mode="", env=env)


def test_guard_plugin_root_mode_unrecognized_raises_value_error_parity():
    env = _guard_env()
    with pytest.raises(ValueError):
        coordinator_trusted_root_guard(mode="fail-quiet", root="/tmp/evil", env=env)
    with pytest.raises(ValueError):
        guard_plugin_root("/tmp/evil", mode="fail-quiet", env=env)


def test_guard_plugin_root_doe_root_sentinel_anchor_parity(tmp_path):
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".doe-root").write_text(str(tmp_path / "coordinator-claude") + "\n")
    env = {"HOME": str(home)}
    root = str(tmp_path / "coordinator-claude" / "coordinator")

    expected = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert guard_plugin_root(root, mode="fail-open", env=env) == expected
    assert expected is True


def test_guard_plugin_root_registry_example_doctrine_repo_anchor_parity(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    registry_root = tmp_path / "from-registry"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f'"repos.example_doctrine_repo" = "{registry_root}"\n'
    )
    home = tmp_path / "home"
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}
    root = str(registry_root / "coordinator")

    expected = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert guard_plugin_root(root, mode="fail-open", env=env) == expected
    assert expected is True


def test_guard_plugin_root_registry_claude_klabauter_anchor_parity(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = "{claude_klabauter_root}"\n'
    )
    home = tmp_path / "home"
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}
    root = str(claude_klabauter_root / "coordinator")

    expected = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert guard_plugin_root(root, mode="fail-open", env=env) == expected
    assert expected is True


@pytest.mark.skipif(os.name != "nt", reason="Windows path-spelling normalization")
def test_guard_plugin_root_windows_separator_and_case_normalization_parity(tmp_path):
    home = tmp_path
    (home / ".claude").mkdir()
    doe = tmp_path / "coordinator-claude"
    (home / ".claude" / ".doe-root").write_text(str(doe).replace("\\", "/") + "\n")
    env = {"HOME": str(home)}
    root = str(doe / "coordinator")

    expected = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert guard_plugin_root(root, mode="fail-open", env=env) == expected
    assert expected is True


@pytest.mark.skipif(os.name != "nt", reason="Windows path-spelling normalization")
def test_guard_plugin_root_windows_backslash_traversal_rejected_parity(tmp_path):
    env = {"HOME": str(tmp_path)}
    root = str(tmp_path / ".claude") + "\\..\\..\\tmp\\evil"

    expected = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert guard_plugin_root(root, mode="fail-open", env=env) == expected
    assert expected is False


# ---------------------------------------------------------------------------
# resolve_operator_config — corruption checks
# ---------------------------------------------------------------------------


def _happy_env(tmp_path):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    doe_root = tmp_path / "coordinator-claude"
    doe_root.mkdir()

    (settings_home / "machine-local" / ".claude-klabauter-root").write_text(str(claude_klabauter_root) + "\n")
    (settings_home / "machine-local" / ".doe-root").write_text(str(doe_root) + "\n")

    env = {
        "COORDINATOR_SETTINGS_HOME": str(settings_home),
        "HOME": str(tmp_path / "home"),
    }
    return env, settings_home, claude_klabauter_root, doe_root


def test_resolve_operator_config_happy_path(tmp_path):
    env, settings_home, claude_klabauter_root, doe_root = _happy_env(tmp_path)

    result = resolve_operator_config(env=env)

    assert result == {
        "settings_home": str(settings_home),
        "claude_klabauter_bin": str(claude_klabauter_root / "coordinator" / "bin"),
        "claude_klabauter_root": str(claude_klabauter_root),
        "doe_root": str(doe_root),
    }


def test_resolve_operator_config_missing_claude_klabauter_root_sentinel_is_corrupt(tmp_path):
    env, settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    (settings_home / "machine-local" / ".claude-klabauter-root").unlink()

    with pytest.raises(OperatorConfigError, match="claude_klabauter_root"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_whitespace_only_sentinel_is_corrupt(tmp_path):
    env, settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    (settings_home / "machine-local" / ".doe-root").write_text("   \n")

    with pytest.raises(OperatorConfigError, match="doe_root"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_traversal_segment_is_corrupt(tmp_path):
    env, settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    (settings_home / "machine-local" / ".doe-root").write_text(
        str(tmp_path / "coordinator-claude" / ".." / "evil") + "\n"
    )

    with pytest.raises(OperatorConfigError, match="doe_root"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_not_a_directory_is_corrupt(tmp_path):
    env, settings_home, claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    not_a_dir = tmp_path / "not-a-real-directory"
    (settings_home / "machine-local" / ".claude-klabauter-root").write_text(str(not_a_dir) + "\n")

    with pytest.raises(OperatorConfigError, match="claude_klabauter_root"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_embedded_newline_from_list_registry_value_is_corrupt(
    tmp_path,
):
    env, settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    (settings_home / "machine-local" / "registry.local.toml").write_text(
        '"repos.example_doctrine_repo" = ["line-one", "line-two"]\n'
    )

    with pytest.raises(OperatorConfigError, match="doe_root"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_claude_klabauter_bin_missing_subdir_is_corrupt(tmp_path):
    # Review: code-reviewer -- Finding 5. `claude_klabauter_bin` is DERIVED
    # (`os.path.join(claude_klabauter_root, "coordinator", "bin")`), not read from a
    # sentinel file, so its corruption path is structurally different from
    # the other three fields — a valid `claude_klabauter_root` whose `coordinator/bin`
    # subdirectory simply does not exist on disk.
    env, settings_home, claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    shutil.rmtree(claude_klabauter_root / "coordinator" / "bin")

    with pytest.raises(OperatorConfigError, match="claude_klabauter_bin"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_settings_home_whitespace_only_is_corrupt(tmp_path):
    # Review: code-reviewer -- Finding 5. `COORDINATOR_SETTINGS_HOME` pointed
    # at a whitespace-only path.
    env, _settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    env["COORDINATOR_SETTINGS_HOME"] = "   "

    with pytest.raises(OperatorConfigError, match="settings_home"):
        resolve_operator_config(env=env)


def test_resolve_operator_config_settings_home_nonexistent_is_corrupt(tmp_path):
    # Review: code-reviewer -- Finding 5. `COORDINATOR_SETTINGS_HOME` pointed
    # at a path that does not exist as a directory on disk.
    env, _settings_home, _claude_klabauter_root, _doe_root_dir = _happy_env(tmp_path)
    env["COORDINATOR_SETTINGS_HOME"] = str(tmp_path / "no-such-settings-home")

    with pytest.raises(OperatorConfigError, match="settings_home"):
        resolve_operator_config(env=env)
