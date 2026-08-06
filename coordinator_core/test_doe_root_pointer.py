"""
Tests for coordinator_core.doe_root_pointer — example-doctrine-repo root pointer-file reader.

Covers all resolution paths (mirrors the bash oracle, Port of:
read-doe-root-pointer.sh (example-doctrine-repo 6fb5fb37, 2026-07-22), updated for DR-071
registry-first precedence):
  1. CLAUDE_HOME set, pointer file present -> content returned (trailing
     newlines stripped).
  2. HOME fallback when CLAUDE_HOME unset.
  3. Pointer file absent -> "".
  4. No home resolvable (CLAUDE_HOME/HOME/USERPROFILE all unset) -> "".
  5. Multi-line / whitespace content -> only trailing newlines stripped,
     mirroring bash `$(cat ...)` semantics.
  6. Registry `repos.example_doctrine_repo` ranks above BOTH pointer-file rungs
     (DR-071 canonical anchor).

Test hygiene (mandatory — see cross-repo/archive/2026-07-20-claude-central-em-
doe-root-pointer-test-clobbers-real-home.md and
2026-07-20-*-test-corrupts-live-machine-config.md): every test here isolates
HOME, CLAUDE_HOME, and COORDINATOR_SETTINGS_HOME to tmp_path via monkeypatch,
and never touches the real machine's settings-home or ~/.claude.

Spec backlink: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md
DR-071: docs/decisions/DR-071-durable-coordinator-root-anchor-settings-home-registry-doe-root-demoted-to-cache.md (example-doctrine-repo)
"""

from __future__ import annotations

import pytest

from coordinator_core import doe_root_pointer as drp


@pytest.fixture(autouse=True)
def _isolate_registry_env(monkeypatch):
    """Every test in this file starts with the DR-071 registry rung's own env
    vars cleared, so a registry read never accidentally sees a real ambient
    ``COORDINATOR_SETTINGS_HOME``/``MACHINE_LOCAL_REGISTRY_DIR`` and leaks a
    real machine's ``repos.example_doctrine_repo`` value into an isolation test. Tests
    that want to exercise the registry rung set these explicitly."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)


def _write_registry(settings_home_dir, filename, key, value):
    """Write a flat quoted-dotted-key TOML entry under
    ``<settings_home_dir>/machine-local/<filename>`` — mirrors
    test_machine_resolver.py's helper of the same shape."""
    reg_dir = settings_home_dir / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / filename
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + f'\n"{key}" = "{value}"\n')
    return reg_dir


def test_reads_pointer_file_under_claude_home(monkeypatch, tmp_path):
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text("/tmp/doe-clone\n")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    assert drp.read_doe_root_pointer() == "/tmp/doe-clone"


def test_falls_back_to_home_when_claude_home_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir(parents=True)
    (tmp_path / ".claude" / ".doe-root").write_text("/tmp/from-home\n")
    assert drp.read_doe_root_pointer() == "/tmp/from-home"


def test_returns_empty_when_pointer_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert drp.read_doe_root_pointer() == ""


def test_returns_empty_when_no_home_resolvable(monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    # _isolate_registry_env (autouse) already clears COORDINATOR_SETTINGS_HOME
    # and MACHINE_LOCAL_REGISTRY_DIR — the DR-071 registry rung's guard (see
    # read_doe_root_pointer) is therefore also exercising the "no home
    # resolvable" contract this test asserts, not just the file rungs.
    assert drp.read_doe_root_pointer() == ""


def test_strips_trailing_newlines_only(monkeypatch, tmp_path):
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text("/tmp/doe-clone\n\n\n")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    assert drp.read_doe_root_pointer() == "/tmp/doe-clone"


def test_claude_home_takes_precedence_over_home(monkeypatch, tmp_path):
    claude_home = tmp_path / "claude-home"
    home = tmp_path / "home"
    (claude_home / ".claude").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text("/tmp/from-claude-home\n")
    (home / ".claude" / ".doe-root").write_text("/tmp/from-home\n")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("HOME", str(home))
    assert drp.read_doe_root_pointer() == "/tmp/from-claude-home"


# --- DR-071: registry `repos.example_doctrine_repo` ranks above both file rungs --------


def test_registry_wins_when_both_pointer_files_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home_dir = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-unused"))
    expected = tmp_path / "from-registry"
    _write_registry(settings_home_dir, "registry.local.toml", "repos.example_doctrine_repo", str(expected))
    assert drp.read_doe_root_pointer() == str(expected)


def test_registry_wins_over_durable_and_legacy_with_different_values(monkeypatch, tmp_path):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home_dir = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    expected = tmp_path / "from-registry"
    _write_registry(settings_home_dir, "registry.local.toml", "repos.example_doctrine_repo", str(expected))
    (settings_home_dir / "machine-local" / ".doe-root").write_text(str(tmp_path / "from-durable") + "\n")

    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text(str(tmp_path / "from-legacy") + "\n")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    assert drp.read_doe_root_pointer() == str(expected)


def test_registry_absent_falls_to_durable_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home_dir = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    expected = tmp_path / "from-durable"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    (settings_home_dir / "machine-local" / ".doe-root").write_text(str(expected) + "\n")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-unused"))
    assert drp.read_doe_root_pointer() == str(expected)


def test_registry_and_durable_absent_falls_to_legacy_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home_dir = tmp_path / "settings-home-empty"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    claude_home = tmp_path / "claude-home"
    expected = tmp_path / "from-legacy"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text(str(expected) + "\n")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    assert drp.read_doe_root_pointer() == str(expected)


def test_registry_and_both_pointer_files_absent_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-empty"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-empty"))
    assert drp.read_doe_root_pointer() == ""
