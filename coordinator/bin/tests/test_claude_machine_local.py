"""test_claude_machine_local — pytest coverage for the shell-out ergonomic wrapper.

Spec backlink: docs/plans/2026-05-20-portable-code-substrate.md §5.1 (Chunk 1 tests)

Coverage (per plan AC7):
  1. repos.project_rag (a genuine working repo declared in registry.toml) returns
     pathlib.Path matching subprocess.run(["machine-local","get","repos.project_rag"]).stdout.strip().
  2. repos.this_key_does_not_exist_12345 raises AttributeError whose message contains
     the dotted key AND a remediation phrase ("Fix:" or "registry.local.toml").
  3. repos._foo raises AttributeError without consulting the registry
     (mock subprocess.run; assert it was NOT called).
  4. Re-import idempotency: from claude_machine_local import repos twice yields a repos
     that resolves correctly (Python caches modules; smoke test).
  5. Memoization: call repos.project_rag twice; verify subprocess.run called
     only once (via mock cache-dict inspection).
  6. Empty-value case: monkeypatch reader via MACHINE_LOCAL_REGISTRY_DIR to a tmp
     directory whose registry.local.toml declares "repos.empty_test" = "" (the
     user-explicit layer that round-trips empty values with rc=0 — see
     _machine_local.py:796-802); assert repos.empty_test raises AttributeError
     with "declared but has no value".
  7. Settings-home resolution ladder (DR-072): COORDINATOR_SETTINGS_HOME override
     wins; CLAUDE_HOME is honored when unset; default falls back to
     ~/.coordinator-claude-settings. Also asserts _reader_invocation composes
     <settings-home>/bin/_machine_local.py under sys.executable — never the
     bare-name `machine-local` wrapper.

All tests that require a live registry key skip gracefully (pytest.skip) when the key
is absent — operator config is not guaranteed in CI or on fresh setups.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# module under test uses (DR-072): COORDINATOR_SETTINGS_HOME override, else
# ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings.
def _default_settings_home() -> str:
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return override
    home = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(home, ".coordinator-claude-settings")


_SETTINGS_HOME_BIN_DIR = os.path.join(_default_settings_home(), "bin")
_TEMPLATES_BIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "templates", "bin")
)
if os.path.isfile(os.path.join(_SETTINGS_HOME_BIN_DIR, "claude_machine_local.py")):
    _BIN_DIR = _SETTINGS_HOME_BIN_DIR
else:
    _BIN_DIR = _TEMPLATES_BIN_DIR
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)


def _fresh_module():
    mod_name = "claude_machine_local"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _cli_get(key: str) -> str | None:
    impl = os.path.join(_BIN_DIR, "_machine_local.py")
    result = subprocess.run(
        [sys.executable, impl, "get", key],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def test_repos_example_retrieval_repo_matches_cli():
    cli_val = _cli_get("repos.project_rag")
    if cli_val is None:
        pytest.skip("repos.project_rag not set on this machine")

    mod = _fresh_module()
    result = mod.repos.project_rag
    assert isinstance(result, Path), f"Expected Path, got {type(result)}"
    assert str(result) == str(Path(cli_val).expanduser()), (
        f"Wrapper returned {result!r}, CLI returned {cli_val!r}"
    )


def test_missing_key_raises_attribute_error_with_message():
    mod = _fresh_module()
    with pytest.raises(AttributeError) as exc_info:
        _ = mod.repos.this_key_does_not_exist_12345
    msg = str(exc_info.value)
    assert "repos.this_key_does_not_exist_12345" in msg, (
        f"Dotted key not in error message: {msg!r}"
    )
    assert ("Fix:" in msg or "registry.local.toml" in msg), (
        f"No remediation phrase in error message: {msg!r}"
    )


def test_underscore_attr_raises_without_subprocess():
    mod = _fresh_module()
    with patch("subprocess.run") as mock_run:
        with pytest.raises(AttributeError) as exc_info:
            _ = mod.repos._foo
        mock_run.assert_not_called()
    assert "_foo" in str(exc_info.value), (
        f"Expected '_foo' in AttributeError message, got: {exc_info.value!r}"
    )


def test_reimport_idempotency():
    mod_name = "claude_machine_local"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_a = importlib.import_module(mod_name)
    mod_b = importlib.import_module(mod_name)
    assert mod_a is mod_b, "Second import returned a different module object"

    cli_val = _cli_get("repos.project_rag")
    if cli_val is None:
        pytest.skip("repos.project_rag not set — skipping resolution smoke")

    result_a = mod_a.repos.project_rag
    result_b = mod_b.repos.project_rag
    assert result_a == result_b, (
        f"Same module, different results: {result_a!r} vs {result_b!r}"
    )


def test_memoization_subprocess_called_once():
    cli_val = _cli_get("repos.project_rag")
    if cli_val is None:
        pytest.skip("repos.project_rag not set on this machine")

    mod = _fresh_module()

    with patch.object(mod, "subprocess") as mock_subprocess_mod:
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = cli_val
        fake_result.stderr = ""
        mock_subprocess_mod.run.return_value = fake_result

        _ = mod.repos.project_rag
        _ = mod.repos.project_rag

        assert mock_subprocess_mod.run.call_count == 1, (
            f"Expected subprocess.run called once, got {mock_subprocess_mod.run.call_count}"
        )


def test_empty_value_raises_attribute_error(tmp_path, monkeypatch):
    registry_toml = tmp_path / "registry.toml"
    registry_toml.write_text(
        textwrap.dedent("""\
            schema = 1
        """),
        encoding="utf-8",
    )

    registry_local_toml = tmp_path / "registry.local.toml"
    registry_local_toml.write_text(
        textwrap.dedent("""\
            "repos.empty_test" = ""
        """),
        encoding="utf-8",
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))

    mod = _fresh_module()
    with pytest.raises(AttributeError) as exc_info:
        _ = mod.repos.empty_test
    msg = str(exc_info.value)
    assert "declared but has no value" in msg, (
        f"Expected 'declared but has no value' in message, got: {msg!r}"
    )
    assert "repos.empty_test" in msg, (
        f"Expected 'repos.empty_test' in message, got: {msg!r}"
    )


def test_settings_home_override_wins(monkeypatch):
    """COORDINATOR_SETTINGS_HOME, when set, outranks CLAUDE_HOME and $HOME."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/tmp/override-settings-home")
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/claude-home-should-be-ignored")
    mod = _fresh_module()
    assert mod._settings_home() == "/tmp/override-settings-home"


def test_settings_home_honors_claude_home(monkeypatch):
    """CLAUDE_HOME is honored when COORDINATOR_SETTINGS_HOME is unset."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/tmp/custom-claude-home")
    mod = _fresh_module()
    assert mod._settings_home() == os.path.join(
        "/tmp/custom-claude-home", ".coordinator-claude-settings"
    )


def test_settings_home_default_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    mod = _fresh_module()
    assert mod._settings_home() == os.path.join(
        os.path.expanduser("~"), ".coordinator-claude-settings"
    )


def test_reader_invocation_composes_settings_home_impl_path(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/tmp/probe-settings-home")
    mod = _fresh_module()
    invocation = mod._reader_invocation()
    assert invocation[0] == sys.executable
    assert invocation[1] == str(
        Path("/tmp/probe-settings-home") / "bin" / "_machine_local.py"
    )
