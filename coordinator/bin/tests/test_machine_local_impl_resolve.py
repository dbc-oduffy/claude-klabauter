"""test_machine_local_impl_resolve.py — regression net for the settings-home-first
resolution ladder in `coordinator/bin/lib/machine_local_impl_resolve.py`.

Review: code-reviewer (F1, P1) — the diff that introduced this module had zero
test coverage for its own precedence fix (settings-home tried before the
retired `~/.claude/bin` compat mirror). Every resolver below is exercised
across all five states relevant to the ladder: env-override (where
applicable), BOTH candidates present (the case that actually pins
precedence — a naive test that creates only one candidate passes trivially
regardless of ordering), settings-home-only, mirror-only, and neither
present.

Spec backlink: coordinator/bin/lib/machine_local_impl_resolve.py module
docstring (DR-210 Amendment 2026-07-24, "resolves nothing through
~/.claude/bin").
"""
from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import machine_local_impl_resolve as mlir  # noqa: E402


# ---------------------------------------------------------------------------
# claude_home()
# ---------------------------------------------------------------------------

def test_claude_home_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert mlir.claude_home() == str(tmp_path)


def test_claude_home_default_falls_back_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    assert mlir.claude_home() == os.path.join(os.path.expanduser("~"), ".claude")


# ---------------------------------------------------------------------------
# settings_home()
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# machine_local_impl_path() — the precedence-critical resolver.
# ---------------------------------------------------------------------------

def _wire_homes(monkeypatch, tmp_path):
    """Point settings_home()/claude_home() at two distinct, isolated tmp dirs
    via COORDINATOR_SETTINGS_HOME / CLAUDE_HOME so candidate presence can be
    controlled file-by-file without touching the real machine."""
    settings_home = tmp_path / "settings-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
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
    """The precedence-pinning case: BOTH candidates exist on disk —
    settings-home must win. This is the exact case the reviewer flagged as
    untested (a test that creates only one candidate passes trivially
    regardless of resolution order)."""
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
    """Neither candidate exists on disk: returns the (nonexistent) mirror
    path unconditionally — callers are expected to isfile/exists-check the
    result before use."""
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    assert mlir.machine_local_impl_path() == str(claude_home / "bin" / "_machine_local.py")


def test_machine_local_impl_path_custom_env_var_name(monkeypatch, tmp_path):
    """A caller-supplied env_override name (e.g. a registry-local constant)
    is honoured, not just the default MACHINE_LOCAL_IMPL."""
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setenv("CUSTOM_IMPL_ENV", "/custom/override.py")
    assert mlir.machine_local_impl_path(env_override="CUSTOM_IMPL_ENV") == "/custom/override.py"


# ---------------------------------------------------------------------------
# machine_local_bin_candidates()
# ---------------------------------------------------------------------------

def test_machine_local_bin_candidates_posix_order(monkeypatch, tmp_path):
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setattr(mlir.os, "name", "posix")
    assert mlir.machine_local_bin_candidates() == [
        str(settings_home / "bin" / "machine-local"),
        str(claude_home / "bin" / "machine-local"),
    ]


def test_machine_local_bin_candidates_windows_cmd_first_per_base(monkeypatch, tmp_path):
    """Windows: `.cmd`-first for EACH base, settings-home base still tried
    before the mirror base overall. Regression pin for F2 — the shared
    module previously offered only the extensionless form on Windows,
    diverging from resolve-repo-path.py's own (correct) handling."""
    settings_home, claude_home = _wire_homes(monkeypatch, tmp_path)
    monkeypatch.setattr(mlir.os, "name", "nt")
    assert mlir.machine_local_bin_candidates() == [
        str(settings_home / "bin" / "machine-local") + ".cmd",
        str(settings_home / "bin" / "machine-local"),
        str(claude_home / "bin" / "machine-local") + ".cmd",
        str(claude_home / "bin" / "machine-local"),
    ]


# ---------------------------------------------------------------------------
# windows_cmd_first_candidates() — the shared Windows-probing helper itself.
# ---------------------------------------------------------------------------

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
