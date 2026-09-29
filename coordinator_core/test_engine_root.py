"""
Tests for coordinator_core.engine_root — CLAUDE_KLABAUTER_ROOT resolver primitive.

Covers all resolution rungs (mirrors the bash oracle's own resolution
chain, coordinator/lib/coordinator-claude-klabauter-root.sh):
  1. COORDINATOR_ENGINE_ROOT env var already set. (CLAUDE_KLABAUTER_ROOT was Rung 1 until
     C14 closed the dual-read window; it now answers nothing and reaching for it
     here would test the retirement, not the rung. The retirement itself is
     covered by tests/test_engine_root_env_accessor.py.)
  2. `machine-local get repos.claude_klabauter` CLI.
  3. Hard failure (RuntimeError) with remediation text.

The `.claude-klabauter-live-root` pointer-file fallback rung was deleted per PM ruling
(no fallback carries a false sense of security when claude-klabauter is absent) —
absence now fails loudly through Rung 2/3 like every other rung.

Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § C1 / AC1
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import engine_root as mr

_REAL_SELF_LOCATED = mr._self_located_root


@pytest.fixture(autouse=True)
def _clear_root_memo(monkeypatch):
    """`coordinator_engine_root`'s Rung 2 answer is memoized process-scope on
    `_registry_mtime_pair`, and every test here points COORDINATOR_SETTINGS_HOME at
    a tmp_path with no registry files — so they all share the one memo key that
    tuple resolves to. Without this reset the first test to reach Rung 2 answers
    for every later one. Order-dependence, not a resolver defect: the module
    ships `_reset_root_memo` as exactly this seam."""
    mr._reset_root_memo()
    monkeypatch.setattr(mr, "_self_located_root", lambda: None)
    yield
    mr._reset_root_memo()


def test_rung1_env_var_short_circuits(monkeypatch):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/tmp/from-env")
    assert mr.coordinator_engine_root() == "/tmp/from-env"


def test_falls_through_to_rung2_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home-empty"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError) as exc:
        mr.coordinator_engine_root()
    assert "repos.claude_klabauter" in str(exc.value)


def test_rung2_machine_local_cli_used_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: "/usr/bin/machine-local")

    def fake_run(cmd, capture_output, text, check, timeout=None, **kwargs):
        assert cmd == ["/usr/bin/machine-local", "get", "repos.claude_klabauter"]
        return subprocess.CompletedProcess(cmd, 0, stdout="/tmp/from-registry\n", stderr="")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    assert mr.coordinator_engine_root() == "/tmp/from-registry"


def test_rung2_empty_registry_value_falls_to_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: "/usr/bin/machine-local")

    def fake_run(cmd, capture_output, text, check, timeout=None, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        mr.coordinator_engine_root()


def test_rung2_nonzero_exit_falls_to_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: "/usr/bin/machine-local")

    def fake_run(cmd, capture_output, text, check, timeout=None, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such key")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        mr.coordinator_engine_root()


def test_rung3_hard_error_when_machine_local_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError) as exc:
        mr.coordinator_engine_root()
    assert "cannot resolve CLAUDE_KLABAUTER_ROOT" in str(exc.value)
    assert "machine-local set repos.claude_klabauter" in str(exc.value)


def test_rung2_timeout_falls_to_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: "/usr/bin/machine-local")

    def fake_run(cmd, capture_output, text, check, timeout=None, **kwargs):
        assert timeout is not None, "Rung 2 must bound the subprocess"
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        mr.coordinator_engine_root()
    assert "cannot resolve CLAUDE_KLABAUTER_ROOT" in str(exc.value)


def test_env_var_wins_over_rung2(monkeypatch, tmp_path):
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/tmp/from-env-wins")
    monkeypatch.setattr(mr.shutil, "which", lambda _name: None)
    assert mr.coordinator_engine_root() == "/tmp/from-env-wins"


def test_unset_env_no_registry_derives_engine_root_from_own_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(mr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mr, "_self_located_root", _REAL_SELF_LOCATED)
    assert mr.coordinator_engine_root() == str(Path(mr.__file__).resolve().parent.parent)
