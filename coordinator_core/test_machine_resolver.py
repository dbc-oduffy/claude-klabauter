"""
test_machine_resolver.py — pytest coverage for coordinator_core.machine_resolver.

Port of: coordinator-daily-branch.sh (DoE 2fbe0e77, 2026-07-19)
Bash-parity fixture backlink: Port of: test-coordinator-daily-branch.sh
  (DoE 2fbe0e77, 2026-07-19) (AC-1 always-lowercase; AC1-AC3 env/registry/hostname precedence; AC9a
  cs_compute_machine_live performs no registry read) — mirrors
  test_daily_branch.py's sanitize_slug bash-parity table pattern.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import machine_resolver as mr

# The subprocess test below spawns a fresh interpreter that imports
# coordinator_core. That child inherits cwd but NOT pytest's rootdir sys.path
# insertion, so it can only resolve the package when cwd is (or is under) the
# repo root -- from any other cwd it dies with ModuleNotFoundError before it
# can write anything to stdout. Pinning cwd to the repo root derived from this
# file's own path makes the subprocess resolvable regardless of the invoking
# shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with a clean slate for every env var this module reads,
    and a clean process-lifetime `git config user.email` cache (added for
    `compute_contributor()`'s fallback rung) — otherwise a prior test's stubbed
    email leaks into a later test via the module-global lru_cache."""
    for key in (
        "COORDINATOR_MACHINE",
        "COORDINATOR_CONTRIBUTOR",
        "COMPUTERNAME",
        "HOSTNAME",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "MACHINE_LOCAL_COORDINATOR_MACHINE_SLUG",
        "MACHINE_LOCAL_COORDINATOR_CONTRIBUTOR_SLUG",
    ):
        monkeypatch.delenv(key, raising=False)
    mr.reset_git_user_email_cache()
    yield
    mr.reset_git_user_email_cache()


def _write_registry(tmp_path, filename, key, value):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / filename
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + f'\n"{key}" = "{value}"\n')
    return reg_dir


# --- AC-1: always lowercase (bash-parity) -------------------------------------


@pytest.mark.parametrize(
    "env_key,raw,expected",
    [
        ("COMPUTERNAME", "MACHINE-A", "machine-a"),
        ("COMPUTERNAME", "Machine-a", "machine-a"),
        ("COORDINATOR_MACHINE", "MyMachine", "mymachine"),
        ("COORDINATOR_MACHINE", "WORKSTATION", "workstation"),
    ],
)
def test_compute_machine_always_lowercase(monkeypatch, tmp_path, env_key, raw, expected):
    # Empty (real, unpopulated) registry dir — isolates from this machine's
    # actual machine-local registry so a COMPUTERNAME-only case isn't shadowed
    # by a real coordinator.machine_slug entry (registry outranks COMPUTERNAME).
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv(env_key, raw)
    assert mr.compute_machine() == expected


@pytest.mark.parametrize(
    "env_key,raw,expected",
    [
        ("COMPUTERNAME", "MACHINE-A", "machine-a"),
        ("COORDINATOR_MACHINE", "MyMachine", "mymachine"),
    ],
)
def test_compute_machine_live_always_lowercase(monkeypatch, env_key, raw, expected):
    monkeypatch.setenv(env_key, raw)
    assert mr.compute_machine_live() == expected


# --- AC1/AC2/AC3: env > registry > hostname precedence ------------------------


def test_ac1_registry_beats_hostname(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "regslug")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    assert mr.compute_machine() == "regslug"


def test_ac2_env_beats_registry(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "regslug")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("COORDINATOR_MACHINE", "envslug")
    assert mr.compute_machine() == "envslug"


def test_ac3a_registry_local_beats_registry_tracked(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "trackedslug")
    _write_registry(tmp_path, "registry.local.toml", "coordinator.machine_slug", "localslug")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    assert mr.compute_machine() == "localslug"


def test_ac3b_registry_absent_falls_to_hostname(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    resolved = mr.compute_machine()
    live = mr.compute_machine_live()
    assert resolved
    assert resolved == live


def test_ac9a_live_performs_no_registry_read(monkeypatch, tmp_path):
    # Registry present with a distinguishable value — live must never see it.
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "regslug")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    assert mr.compute_machine_live() != "regslug"


def test_empty_registry_value_falls_through(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("COMPUTERNAME", "Fallback")
    assert mr.compute_machine() == "fallback"


def test_machine_local_env_override_beats_registry_file(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.machine_slug", "regslug")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_SLUG", "envoverride")
    assert mr.compute_machine() == "envoverride"


def test_all_resolution_rungs_absent_returns_unknown(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mr.socket, "gethostname", lambda: "")
    monkeypatch.setenv("HOSTNAME", "")
    assert mr.compute_machine() == "unknown"
    assert mr.compute_machine_live() == "unknown"


# --- Contributor: env > registry > live(git email) > "unknown" ---------------


def _stub_git_email(monkeypatch, email: str, rc: int = 0):
    def _fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, rc, stdout=email, stderr="")

    monkeypatch.setattr(mr.subprocess, "run", _fake_run)


def test_contributor_registry_beats_live(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.contributor_slug", "regcontrib")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _stub_git_email(monkeypatch, "someone@example.com\n")
    assert mr.compute_contributor() == "regcontrib"


def test_contributor_env_beats_registry(monkeypatch, tmp_path):
    reg_dir = _write_registry(tmp_path, "registry.toml", "coordinator.contributor_slug", "regcontrib")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("COORDINATOR_CONTRIBUTOR", "EnvContrib")
    assert mr.compute_contributor() == "envcontrib"


def test_contributor_falls_to_live_when_registry_absent(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _stub_git_email(monkeypatch, "Dónal.example-operator@example.com\n")
    assert mr.compute_contributor() == mr.compute_contributor_live()


def test_contributor_live_drops_domain_and_sanitizes(monkeypatch):
    _stub_git_email(monkeypatch, "First.Last+tag@example.com\n")
    assert mr.compute_contributor_live() == "first-last-tag"


def test_contributor_live_no_at_passes_through_sanitized(monkeypatch):
    _stub_git_email(monkeypatch, "not-an-email\n")
    assert mr.compute_contributor_live() == "not-an-email"


def test_contributor_live_no_git_config_returns_unknown(monkeypatch):
    _stub_git_email(monkeypatch, "", rc=1)
    assert mr.compute_contributor_live() == "unknown"


def test_contributor_live_env_override(monkeypatch):
    monkeypatch.setenv("COORDINATOR_CONTRIBUTOR", "EnvLive")
    assert mr.compute_contributor_live() == "envlive"


def test_contributor_live_missing_git_binary(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(mr.subprocess, "run", _raise)
    assert mr.compute_contributor_live() == "unknown"


# --- Process-lifetime cache on the fallback rung -------------------------------


def test_compute_contributor_caches_spawn_across_calls(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    call_count = {"n": 0}

    def _fake_run(args, **kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(args, 0, stdout="cached@example.com\n", stderr="")

    monkeypatch.setattr(mr.subprocess, "run", _fake_run)

    assert mr.compute_contributor() == "cached"
    assert mr.compute_contributor() == "cached"
    assert call_count["n"] == 1


def test_contributor_live_bypasses_cache_and_observes_live_reality(monkeypatch, tmp_path):
    """The stored/cached value and live reality DIFFER — compute_contributor()
    (registry absent) populates the process cache with the FIRST stubbed
    email; the git identity then changes underneath it (simulating a mid-
    session `git config user.email` edit). compute_contributor_live() must
    return the NEW value, never the stale cached one — proving the `_live`
    contract (bypass cached/registry state, observe current reality) holds
    even with the fallback-rung cache in place."""
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    _stub_git_email(monkeypatch, "stale@example.com\n")
    assert mr.compute_contributor() == "stale"  # populates the cache

    _stub_git_email(monkeypatch, "fresh@example.com\n")
    # Cached, non-live resolver still serves the stale cached value...
    assert mr.compute_contributor() == "stale"
    # ...but the _live variant observes the new reality directly.
    assert mr.compute_contributor_live() == "fresh"


def test_reset_git_user_email_cache_clears_stale_value(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    _stub_git_email(monkeypatch, "first@example.com\n")
    assert mr.compute_contributor() == "first"

    _stub_git_email(monkeypatch, "second@example.com\n")
    mr.reset_git_user_email_cache()
    assert mr.compute_contributor() == "second"


def test_git_user_email_cache_does_not_memoize_failure(monkeypatch, tmp_path):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    _stub_git_email(monkeypatch, "", rc=1)
    assert mr.compute_contributor() == "unknown"

    _stub_git_email(monkeypatch, "recovered@example.com\n")
    assert mr.compute_contributor() == "recovered"


# --- Regression: machine_resolver <-> coordinator_core.ops import cycle ------
#
# 2026-07-22: a module-level `from coordinator_core.ops.emit._slug import
# machine_slug` in this module raced coordinator_core.ops's eager op-module
# import (which transitively imports this module's own registry_get, via
# doe_root_pointer). Whichever of {this module, coordinator_core.ops} a
# process imported FIRST left the other partially initialized. This only
# reproduces in a genuinely fresh interpreter — pytest's own collection
# already has both modules in sys.modules by the time any in-process test
# runs — so this test spawns a subprocess importing ONLY this module, the
# exact ordering that triggered the cycle.


def test_fresh_process_import_does_not_trigger_ops_eager_import_cycle():
    result = subprocess.run(
        [sys.executable, "-c", "import coordinator_core.machine_resolver"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "FAILED to import" not in result.stderr
    assert "circular import" not in result.stderr
