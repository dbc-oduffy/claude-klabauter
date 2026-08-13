"""
Tests for coordinator_core._settings_home — bootstrap-safe settings-home resolver.

Covers all three precedence rungs (AC1):
  1. COORDINATOR_SETTINGS_HOME override.
  2. CLAUDE_HOME override with COORDINATOR_SETTINGS_HOME unset.
  3. Default ~/.coordinator-claude-settings with both unset.

Spec backlink: pln-repoint-coordinator-core-claud-56d805 § C1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.testing import symlink_capability

from coordinator_core._settings_home import (
    ClaudeConfigDivergenceError,
    SettingsHomeDivergenceError,
    check_claude_config_divergence,
    check_machine_local_divergence,
    claude_config_dir,
    home_dir,
    legacy_machine_local_dir,
    machine_local_dir,
    normalize_native_path,
    settings_home,
)


def test_settings_home_uses_coordinator_settings_home_override(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/tmp/explicit-settings-home")
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/should-be-ignored")
    assert settings_home() == Path("/tmp/explicit-settings-home")


def test_settings_home_uses_claude_home_when_settings_home_unset(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/custom-claude-home")
    assert settings_home() == Path("/tmp/custom-claude-home/.coordinator-claude-settings")


def test_settings_home_defaults_to_home_when_both_unset(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/tmp/fake-home"))
    assert settings_home() == Path("/tmp/fake-home/.coordinator-claude-settings")


def test_settings_home_treats_empty_override_as_unset(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "")
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/tmp/fake-home"))
    assert settings_home() == Path("/tmp/fake-home/.coordinator-claude-settings")


def test_settings_home_absolute_with_no_home_or_claude_home_env(monkeypatch):
    """Native-Windows regression: HOME and CLAUDE_HOME both absent.

    PowerShell/cmd.exe set USERPROFILE, never HOME. A resolver spelled as a
    literal `${CLAUDE_HOME:-$HOME}` returns a bare, cwd-relative
    `.coordinator-claude-settings` there (the bug fixed in
    coordinator/lib/settings_home.py). This module reaches the platform home via
    `Path.home()` instead; pin that it stays rooted with no home env at all.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    resolved = settings_home()

    assert resolved.is_absolute() or resolved.root, resolved
    assert resolved.name == ".coordinator-claude-settings"
    assert str(resolved) != ".coordinator-claude-settings"
    assert str(resolved.parent) not in ("", ".")


def test_home_dir_resolves_via_userprofile_when_home_absent(monkeypatch):
    """Native-Windows condition for the public `home_dir()` resolver: PowerShell
    and cmd.exe set USERPROFILE but never HOME. `home_dir()` must still resolve
    to a rooted path (via `Path.home()`, which itself consults USERPROFILE on
    Windows) rather than degrading to a cwd-relative one -- the exact
    bare_home_or_chain shape this function was extracted to close call sites
    against (home-resolution-lint sweep, 2026-07-29)."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/tmp/fake-userprofile-home"))

    resolved = home_dir()

    assert resolved == Path("/tmp/fake-userprofile-home")
    assert resolved.is_absolute() or resolved.root, resolved


def test_home_dir_prefers_claude_home_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/explicit-claude-home")
    monkeypatch.delenv("HOME", raising=False)

    assert home_dir() == Path("/tmp/explicit-claude-home")


@pytest.mark.parametrize("var", ["COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME"])
def test_settings_home_rejects_relative_override(monkeypatch, var):
    """A relative override silently anchored the settings home at the process
    cwd. Now a fail-loud config error, matching
    coordinator/lib/claude-home/_claude_home.py."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv(var, "relative-dir")

    with pytest.raises(ValueError, match="absolute path"):
        settings_home()


def test_settings_home_rejects_windows_drive_relative_override(monkeypatch):
    """`C:foo` names a path relative to the cwd ON drive C:, not an absolute
    path — the Windows form `Path.is_absolute()` correctly rejects."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "C:foo")

    with pytest.raises(ValueError, match="absolute path"):
        settings_home()


def test_legacy_machine_local_dir_rejects_relative_claude_home(monkeypatch):
    """The legacy-home resolver shares `_home_dir()`, so it validates too."""
    monkeypatch.setenv("CLAUDE_HOME", "relative-dir")

    with pytest.raises(ValueError, match="absolute path"):
        legacy_machine_local_dir()


def test_machine_local_dir_is_settings_home_slash_machine_local(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/tmp/explicit-settings-home")
    assert machine_local_dir() == Path("/tmp/explicit-settings-home/machine-local")


def test_normalize_native_path_converts_msys_mount_form(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("/x/coordinator-claude") == Path("X:/coordinator-claude")


def test_normalize_native_path_converts_cygdrive_mount_form(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("/cygdrive/x/coordinator-claude") == Path("X:/coordinator-claude")


def test_normalize_native_path_leaves_native_forward_slash_form_unchanged(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("X:/coordinator-claude") == Path("X:/coordinator-claude")


def test_normalize_native_path_leaves_native_backslash_form_unchanged(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path(r"X:\coordinator-claude") == Path(r"X:\coordinator-claude")


def test_normalize_native_path_handles_empty_string(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("") == Path("")


def test_normalize_native_path_leaves_relative_path_unchanged(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("relative/sub/dir") == Path("relative/sub/dir")


def test_normalize_native_path_is_noop_on_posix(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "posix")
    assert normalize_native_path("/x/coordinator-claude") == Path("/x/coordinator-claude")


def test_normalize_native_path_drive_root_no_trailing_path(monkeypatch):
    monkeypatch.setattr("coordinator_core._settings_home.os.name", "nt")
    assert normalize_native_path("/x") == Path("X:/")


def test_legacy_machine_local_dir_is_claude_home_slash_machine_local(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/fake-claude-home")
    assert legacy_machine_local_dir() == Path("/tmp/fake-claude-home/.claude/machine-local")


def _populated_dir(path: Path) -> Path:
    """Create `path` holding one file — a machine-local home with actual state.

    Seeding content matters: an empty directory is an absent-or-husk home to
    `check_machine_local_divergence`, so a test that seeds nothing short-circuits
    before reaching the behaviour it names.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / "registry.toml").write_text("# seeded by test\n")
    return path


def test_divergence_noop_when_legacy_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-claude-home"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    _populated_dir(tmp_path / "settings-home" / "machine-local")
    check_machine_local_divergence()  # must not raise


def test_divergence_noop_when_legacy_is_empty_husk(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude" / "machine-local").mkdir(parents=True)  # husk: exists, empty
    settings_home_dir = tmp_path / "settings-home"
    _populated_dir(settings_home_dir / "machine-local")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    check_machine_local_divergence()  # completed migration — must not raise


def test_divergence_noop_when_new_is_empty_husk(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    _populated_dir(claude_home / ".claude" / "machine-local")
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)  # husk: exists, empty

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    check_machine_local_divergence()  # pre-migration scaffold — must not raise


def test_divergence_noop_when_new_absent(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    _populated_dir(claude_home / ".claude" / "machine-local")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-absent"))
    check_machine_local_divergence()  # must not raise


def test_divergence_noop_when_both_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-absent"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-absent"))
    check_machine_local_divergence()  # must not raise


@symlink_capability.requires_symlink_capability
def test_divergence_noop_when_compat_symlink_resolves_equal(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    settings_home_dir = tmp_path / "settings-home"
    new_dir = _populated_dir(settings_home_dir / "machine-local")
    legacy_dir = claude_home / ".claude"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "machine-local").symlink_to(new_dir, target_is_directory=True)

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    check_machine_local_divergence()  # symlink resolves equal — must not raise


def test_divergence_raises_when_both_exist_and_resolve_unequal(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    _populated_dir(claude_home / ".claude" / "machine-local")
    settings_home_dir = tmp_path / "settings-home"
    _populated_dir(settings_home_dir / "machine-local")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

    with pytest.raises(SettingsHomeDivergenceError, match="DIVERGENT MACHINE-LOCAL HOMES"):
        check_machine_local_divergence()


def test_claude_config_dir_uses_override_when_set(tmp_path, monkeypatch):
    override = tmp_path / "explicit-config-dir"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    assert claude_config_dir() == override


def test_claude_config_dir_rejects_relative_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-config-dir")
    with pytest.raises(ValueError, match="absolute path"):
        claude_config_dir()


def test_claude_config_dir_falls_back_to_home_dir_slash_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/fake-claude-home")
    assert claude_config_dir() == Path("/tmp/fake-claude-home/.claude")


def test_claude_config_dir_unset_matches_todays_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/tmp/fake-home"))
    assert claude_config_dir() == Path("/tmp/fake-home/.claude")


def test_claude_config_divergence_noop_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    check_claude_config_divergence()  # must not raise


def test_claude_config_divergence_noop_when_configured_absent(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    _populated_dir(claude_home / ".claude")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config-dir-absent"))
    check_claude_config_divergence()  # must not raise


def test_claude_config_divergence_noop_when_default_is_empty_husk(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)  # husk: exists, empty
    configured = _populated_dir(tmp_path / "config-dir")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    check_claude_config_divergence()  # must not raise


@symlink_capability.requires_symlink_capability
def test_claude_config_divergence_noop_when_symlink_resolves_equal(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    real = _populated_dir(tmp_path / "real-config-dir")
    configured = claude_home / ".claude"
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    check_claude_config_divergence()  # symlink resolves equal — must not raise


def test_claude_config_divergence_raises_on_genuine_divergence(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    _populated_dir(claude_home / ".claude")
    configured = _populated_dir(tmp_path / "config-dir")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))

    with pytest.raises(ClaudeConfigDivergenceError, match="DIVERGENT CLAUDE CONFIG DIRS"):
        check_claude_config_divergence()
