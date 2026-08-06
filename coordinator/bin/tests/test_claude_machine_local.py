"""test_claude_machine_local — pytest coverage for the shell-out ergonomic wrapper.

Spec backlink: docs/plans/2026-05-20-portable-code-substrate.md §5.1 (Chunk 1 tests)

Coverage (per plan AC7):
  1. repos.example_retrieval_repo (a genuine working repo declared in registry.toml) returns
     pathlib.Path matching subprocess.run(["machine-local","get","repos.example_retrieval_repo"]).stdout.strip().
  2. repos.this_key_does_not_exist_12345 raises AttributeError whose message contains
     the dotted key AND a remediation phrase ("Fix:" or "registry.local.toml").
  3. repos._foo raises AttributeError without consulting the registry
     (mock subprocess.run; assert it was NOT called).
  4. Re-import idempotency: from claude_machine_local import repos twice yields a repos
     that resolves correctly (Python caches modules; smoke test).
  5. Memoization: call repos.example_retrieval_repo twice; verify subprocess.run called
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

_NO_CONSOLE_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

# ---------------------------------------------------------------------------
# sys.path bootstrap — ensure <settings-home>/bin is importable without
# installation. Settings-home is resolved by the same two-rung ladder the
# module under test uses (DR-072): COORDINATOR_SETTINGS_HOME override, else
# ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings.
# ---------------------------------------------------------------------------
def _default_settings_home() -> str:
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return override
    home = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~")
    return os.path.join(home, ".coordinator-claude-settings")


# Prefer the installed settings-home copy when present (exercises the live
# install surface); fall back to this repo's own templates/bin (the
# source-of-truth this test suite lives beside) when the settings-home copy
# has not been (re)installed yet — this repo's test suite must exercise the
# file it is co-located with, not silently ModuleNotFoundError on a stale
# install (self-resolves from __file__, never cwd, per the repo's
# "scripts self-resolve their own root" convention).
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
    """Return a freshly-imported claude_machine_local module (bypasses cache).

    Used for tests that need a pristine _Namespace instance without cached
    attribute state from a prior test in the same process.
    """
    mod_name = "claude_machine_local"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _cli_get(key: str) -> str | None:
    """Return CLI output for key, or None if unset.

    Uses the settings-home _machine_local.py reader directly as the oracle
    (the contract the module under test now composes and shells out to),
    so the test validates the Python wrapper against the documented CLI
    contract without going through the forbidden bare-name wrapper.
    """
    impl = os.path.join(_BIN_DIR, "_machine_local.py")
    result = subprocess.run(
        [sys.executable, impl, "get", key],
        capture_output=True, text=True,
        **_NO_CONSOLE_WINDOW,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


# ---------------------------------------------------------------------------
# Test 1 — known key returns Path matching CLI output
# ---------------------------------------------------------------------------

def test_repos_example_retrieval_repo_matches_cli():
    """repos.example_retrieval_repo returns a Path whose str() matches CLI output.

    Uses repos.example_retrieval_repo as the sample working-repo key — a genuine working repo
    declared in registry.toml that survives the publish-vs-working-repo migration
    (docs/plans/2026-06-30-registry-publish-vs-working-targets.md § C10).
    repos.coordinator_claude was previously used here but is a publish-mirror key
    removed from repos.* in that migration, which caused this test to silently skip.
    """
    cli_val = _cli_get("repos.example_retrieval_repo")
    if cli_val is None:
        pytest.skip("repos.example_retrieval_repo not set on this machine")

    mod = _fresh_module()
    result = mod.repos.example_retrieval_repo
    assert isinstance(result, Path), f"Expected Path, got {type(result)}"
    assert str(result) == str(Path(cli_val).expanduser()), (
        f"Wrapper returned {result!r}, CLI returned {cli_val!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — missing key raises AttributeError with dotted key + remediation
# ---------------------------------------------------------------------------

def test_missing_key_raises_attribute_error_with_message():
    """repos.this_key_does_not_exist_12345 raises AttributeError with key + remediation."""
    mod = _fresh_module()
    with pytest.raises(AttributeError) as exc_info:
        _ = mod.repos.this_key_does_not_exist_12345
    msg = str(exc_info.value)
    assert "repos.this_key_does_not_exist_12345" in msg, (
        f"Dotted key not in error message: {msg!r}"
    )
    # Remediation phrase — plan AC7 says "Fix:" or "registry.local.toml"
    assert ("Fix:" in msg or "registry.local.toml" in msg), (
        f"No remediation phrase in error message: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — underscore-attribute guard raises without consulting the registry
# ---------------------------------------------------------------------------

def test_underscore_attr_raises_without_subprocess():
    """repos._foo raises AttributeError without invoking subprocess.run."""
    mod = _fresh_module()
    with patch("subprocess.run") as mock_run:
        with pytest.raises(AttributeError) as exc_info:
            _ = mod.repos._foo
        # subprocess.run must NOT have been called — guard fires before lookup.
        # This is the load-bearing assertion; the name-preservation check below
        # is cosmetic (Python's AttributeError(name) preserves it by definition).
        mock_run.assert_not_called()
    # Attribute name is preserved in the AttributeError (Python protocol).
    assert "_foo" in str(exc_info.value), (
        f"Expected '_foo' in AttributeError message, got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — re-import idempotency (smoke test — Python module cache)
# ---------------------------------------------------------------------------

def test_reimport_idempotency():
    """Importing claude_machine_local twice yields the same module object."""
    mod_name = "claude_machine_local"
    # Ensure module is in cache from a prior import.
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_a = importlib.import_module(mod_name)
    mod_b = importlib.import_module(mod_name)
    assert mod_a is mod_b, "Second import returned a different module object"

    cli_val = _cli_get("repos.example_retrieval_repo")
    if cli_val is None:
        pytest.skip("repos.example_retrieval_repo not set — skipping resolution smoke")

    # Both access through the same cached module resolve correctly.
    result_a = mod_a.repos.example_retrieval_repo
    result_b = mod_b.repos.example_retrieval_repo
    assert result_a == result_b, (
        f"Same module, different results: {result_a!r} vs {result_b!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — memoization: subprocess.run called only once for repeated access
# ---------------------------------------------------------------------------

def test_memoization_subprocess_called_once():
    """Accessing repos.example_retrieval_repo twice calls subprocess.run exactly once."""
    cli_val = _cli_get("repos.example_retrieval_repo")
    if cli_val is None:
        pytest.skip("repos.example_retrieval_repo not set on this machine")

    mod = _fresh_module()

    # Patch subprocess.run on the claude_machine_local module's own reference
    # so we capture calls from within the module's __getattr__.
    with patch.object(mod, "subprocess") as mock_subprocess_mod:
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = cli_val
        fake_result.stderr = ""
        mock_subprocess_mod.run.return_value = fake_result

        _ = mod.repos.example_retrieval_repo  # first access — subprocess called
        _ = mod.repos.example_retrieval_repo  # second access — cache hit, no call

        assert mock_subprocess_mod.run.call_count == 1, (
            f"Expected subprocess.run called once, got {mock_subprocess_mod.run.call_count}"
        )


# ---------------------------------------------------------------------------
# Test 6 — empty-value case raises AttributeError with "declared but has no value"
# ---------------------------------------------------------------------------

def test_empty_value_raises_attribute_error(tmp_path, monkeypatch):
    """repos.empty_test raises AttributeError when key is declared with empty string.

    Per _machine_local.py:796-802, resolve_sibling_repo maps empty->None for BOTH
    "absent key" and "key explicitly set to empty string" (rung 4), specifically
    to prevent registry.toml tracked-baseline sentinels (`repos.x = ""`) from
    resolving as hits. The round-trip-rc=0/empty-stdout contract is honored only
    when the key is explicitly declared in registry.local.toml — declaring it in
    registry.toml instead exercises the "not found" branch, not this one.
    """
    # Write a minimal registry.toml (tracked-baseline schema only, no declaration).
    registry_toml = tmp_path / "registry.toml"
    registry_toml.write_text(
        textwrap.dedent("""\
            schema = 1
        """),
        encoding="utf-8",
    )

    # Declare repos.empty_test = "" in registry.local.toml — the user-explicit
    # layer that must round-trip empty values with rc=0 (see docstring above).
    registry_local_toml = tmp_path / "registry.local.toml"
    registry_local_toml.write_text(
        textwrap.dedent("""\
            "repos.empty_test" = ""
        """),
        encoding="utf-8",
    )

    # Point the machine-local reader at our tmp registry.
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


# ---------------------------------------------------------------------------
# Test 7 — settings-home resolution ladder (DR-072)
# ---------------------------------------------------------------------------

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
    """With no overrides, settings home is ~/.coordinator-claude-settings."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    mod = _fresh_module()
    assert mod._settings_home() == os.path.join(
        os.path.expanduser("~"), ".coordinator-claude-settings"
    )


def test_reader_invocation_composes_settings_home_impl_path(monkeypatch):
    """_reader_invocation composes <settings-home>/bin/_machine_local.py under
    sys.executable — never the bare-name `machine-local` wrapper."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/tmp/probe-settings-home")
    mod = _fresh_module()
    invocation = mod._reader_invocation()
    assert invocation[0] == sys.executable
    assert invocation[1] == os.path.join(
        "/tmp/probe-settings-home", "bin", "_machine_local.py"
    )
