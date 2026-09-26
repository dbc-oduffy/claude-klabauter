from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import machine_local_impl_resolve as mlir  # noqa: E402


def test_claude_home_env_override_wins(monkeypatch, tmp_path):
    """Convention A: CLAUDE_HOME is a $HOME substitute, not the .claude dir
    itself — claude_home() appends the .claude segment."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert mlir.claude_home() == os.path.join(str(tmp_path), ".claude")


def test_claude_home_default_falls_back_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert mlir.claude_home() == os.path.join(os.path.expanduser("~"), ".claude")


def test_claude_home_claude_config_dir_wins_over_claude_home(monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR (the harness's own env var) takes precedence over
    CLAUDE_HOME (this fleet's invention) — mirrors
    coordinator_core._settings_home.claude_config_dir()'s precedence, the
    split-brain this rung exists to close."""
    config_dir = tmp_path / "harness-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "should-be-ignored"))
    assert mlir.claude_home() == str(config_dir)


def test_claude_home_claude_config_dir_absent_falls_back_to_claude_home(monkeypatch, tmp_path):
    """Convention A: with CLAUDE_CONFIG_DIR unset, claude_home() derives
    <CLAUDE_HOME>/.claude, matching check_install_singularity._claude_base_dir()."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert mlir.claude_home() == os.path.join(str(tmp_path), ".claude")


def test_settings_home_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", "/should/be/ignored")
    assert mlir.settings_home() == str(tmp_path)


def test_settings_home_honors_claude_home_when_override_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert mlir.settings_home() == os.path.join(str(tmp_path), ".coordinator-claude-settings")


def test_settings_home_default_falls_back_to_home_not_claude_home(monkeypatch):
    """Negative-spec pin: the fallback base is CLAUDE_HOME-or-HOME directly,
    NEVER claude_home() (which appends /.claude) — using claude_home() here
    would nest settings-home one level too deep."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    expected = os.path.join(os.path.expanduser("~"), ".coordinator-claude-settings")
    assert mlir.settings_home() == expected
    assert ".claude" not in os.path.relpath(mlir.settings_home(), os.path.expanduser("~"))


def _wire_homes(monkeypatch, tmp_path):
    """Point settings_home()/claude_home() at two distinct, isolated tmp dirs
    via COORDINATOR_SETTINGS_HOME / CLAUDE_HOME so candidate presence can be
    controlled file-by-file without touching the real machine.

    Convention A: CLAUDE_HOME is a $HOME substitute, so claude_home() derives
    `<claude_home_env>/.claude` — the returned second element is that derived
    `.claude` directory, which is what every mirror-rung candidate is built
    under."""
    settings_home = tmp_path / "settings-home"
    claude_home_env = tmp_path / "claude-home"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home_env))
    monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
    claude_home = claude_home_env / ".claude"
    return settings_home, claude_home


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_machine_local_impl_path_env_override_wins(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    _touch(settings_home / "bin" / "_machine_local.py")
    _touch(claude_home / "bin" / "_machine_local.py")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", "/override/path.py")
    assert mlir.machine_local_impl_path() == "/override/path.py"


def test_machine_local_impl_path_env_override_none_skips_check(monkeypatch, tmp_path):
    """env_override=None means the caller's pre-existing contract never
    honoured a test-isolation env var — passing None must skip that check
    entirely even when MACHINE_LOCAL_IMPL is set in the environment."""
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    _touch(settings_home / "bin" / "_machine_local.py")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", "/should/be/ignored.py")
    result = mlir.machine_local_impl_path(env_override=None)
    assert result == str(settings_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_both_present_settings_home_wins(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    _touch(settings_home / "bin" / "_machine_local.py")
    _touch(claude_home / "bin" / "_machine_local.py")
    assert mlir.machine_local_impl_path() == str(settings_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_settings_home_only(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    _touch(settings_home / "bin" / "_machine_local.py")
    assert mlir.machine_local_impl_path() == str(settings_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_mirror_only(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    _touch(claude_home / "bin" / "_machine_local.py")
    assert mlir.machine_local_impl_path() == str(claude_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_neither_present_falls_back_to_mirror_path(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    assert mlir.machine_local_impl_path() == str(claude_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_custom_env_var_name(monkeypatch, tmp_path):
    """A caller-supplied env_override name (e.g. a registry-local constant)
    is honoured, not just the default MACHINE_LOCAL_IMPL."""
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setenv("CUSTOM_IMPL_ENV", "/custom/override.py")
    assert mlir.machine_local_impl_path(env_override="CUSTOM_IMPL_ENV") == "/custom/override.py"


def test_machine_local_bin_candidates_posix_order(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setattr(mlir.os, "name", "posix")
    assert mlir.machine_local_bin_candidates() == [
        str(settings_home / "bin" / "machine-local"),
        str(claude_home / "bin" / "machine-local"),
    ]


def test_machine_local_bin_candidates_windows_cmd_first_per_base(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setattr(mlir.os, "name", "nt")
    assert mlir.machine_local_bin_candidates() == [
        str(settings_home / "bin" / "machine-local") + ".cmd",
        str(settings_home / "bin" / "machine-local"),
        str(claude_home / "bin" / "machine-local") + ".cmd",
        str(claude_home / "bin" / "machine-local"),
    ]


def test_windows_cmd_first_candidates_posix_passthrough(monkeypatch):
    monkeypatch.setattr(mlir.os, "name", "posix")
    bases = ["/a/base", "/b/base"]
    assert mlir.windows_cmd_first_candidates(bases) == bases


def test_windows_cmd_first_candidates_nt_expands_each_base(monkeypatch):
    monkeypatch.setattr(mlir.os, "name", "nt")
    bases = ["/a/base", "/b/base"]
    assert mlir.windows_cmd_first_candidates(bases) == [
        "/a/base.cmd", "/a/base",
        "/b/base.cmd", "/b/base",
    ]


def _import_checker_and_seam():
    import importlib

    _COORDINATOR_CORE_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    if _COORDINATOR_CORE_ROOT not in sys.path:
        sys.path.insert(0, _COORDINATOR_CORE_ROOT)
    from coordinator_core import _settings_home
    from coordinator_core.install import check_install_singularity
    from coordinator_core.ops import coordinator_doe_root

    importlib.reload(_settings_home)
    return _settings_home, check_install_singularity, coordinator_doe_root


def _norm(p):
    return str(p).replace("\\", "/") if p else p


def test_agreement_pin_claude_home_only(monkeypatch, tmp_path):
    """CLAUDE_HOME set to a $HOME substitute, CLAUDE_CONFIG_DIR unset: mlir,
    the checker, the canonical seam, and both engine rungs all name
    <CLAUDE_HOME>/.claude (mod separator normalization)."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    settings_home, checker, doe_root_mod = _import_checker_and_seam()

    expected = os.path.join(str(tmp_path), ".claude")
    assert _norm(mlir.claude_home()) == _norm(expected)
    assert _norm(checker._claude_base_dir()) == _norm(expected)
    assert _norm(str(settings_home.claude_config_dir())) == _norm(expected)

    claude_dir = doe_root_mod._cf_claude_config_dir_or_none()
    assert _norm(claude_dir) == _norm(expected)
    assert _norm(os.path.join(claude_dir, "plugins", "coordinator-claude")) == _norm(
        os.path.join(expected, "plugins", "coordinator-claude")
    )
    assert _norm(
        os.path.join(claude_dir, "plugins", "cache", "coordinator-claude", "coordinator")
    ) == _norm(os.path.join(expected, "plugins", "cache", "coordinator-claude", "coordinator"))


def test_agreement_pin_claude_config_dir_set(monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR set: mlir, the canonical seam, and both engine rungs
    agree on CLAUDE_CONFIG_DIR verbatim; the checker (out of scope, Anti-scope)
    still derives <CLAUDE_HOME>/.claude, pinning the known divergence."""
    config_dir = tmp_path / "harness-config"
    home_substitute = tmp_path / "home-substitute"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(home_substitute))
    settings_home, checker, doe_root_mod = _import_checker_and_seam()

    assert _norm(mlir.claude_home()) == _norm(str(config_dir))
    assert _norm(str(settings_home.claude_config_dir())) == _norm(str(config_dir))
    assert _norm(doe_root_mod._cf_claude_config_dir_or_none()) == _norm(str(config_dir))
    assert _norm(checker._claude_base_dir()) == _norm(
        os.path.join(str(home_substitute), ".claude")
    )


def test_agreement_pin_planted_violation_self_check(monkeypatch, tmp_path):
    """Planted-violation self-check: monkeypatch claude_home() back to
    returning CLAUDE_HOME unchanged (the pre-C2 defect) and assert the pin
    reports the divergence, proving this pin goes red on the defect it
    exists to catch."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    settings_home, checker, doe_root_mod = _import_checker_and_seam()

    monkeypatch.setattr(mlir, "claude_home", lambda: os.environ["CLAUDE_HOME"])
    expected = os.path.join(str(tmp_path), ".claude")
    assert _norm(mlir.claude_home()) != _norm(expected)
    assert _norm(mlir.claude_home()) == _norm(str(tmp_path))
