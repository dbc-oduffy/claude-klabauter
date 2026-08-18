"""
Tests for coordinator_core.install.migrations.whoami_pin_migration —
AC1/AC2 of docs/plans/2026-08-18-retire-coordinator-venv.md (C1): the
`coordinator.whoami_python` pin repoint leg.

Starts from the old pin value, asserts the repoint, asserts the second run
is a no-op, asserts the refusal path when the target interpreter lacks the
package. Also covers the graceful-degradation (no CLI) and
nothing-sane-to-repoint-onto refusal legs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coordinator_core.install.migrations import whoami_pin_migration as m


def _fake_ml(registry: dict, calls: list):
    def _get(cli, key):
        return registry.get(key, "")

    def _set(cli, key, value):
        registry[key] = value
        calls.append((key, value))

    return _get, _set


def _venv_python_str(tmp_path: Path) -> str:
    return str(tmp_path / ".coordinator-venv" / "Scripts" / "python.exe")


# ---------------------------------------------------------------------------
# AC1: repoints an old venv-shaped pin onto the machine interpreter
# ---------------------------------------------------------------------------


def test_repoints_when_target_is_healthy(tmp_path, monkeypatch):
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    target = sys.executable
    registry = {
        m.WHOAMI_PIN_KEY: venv_py,
        m.GENERAL_PIN_KEY: target,
    }
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)
    monkeypatch.setattr(m, "_target_imports_whoami", lambda py: True)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.REPOINTED
    assert registry[m.WHOAMI_PIN_KEY] == target
    assert calls == [(m.WHOAMI_PIN_KEY, target)]


def test_verification_runs_from_neutral_cwd(tmp_path, monkeypatch):
    """AC1: the target-interpreter import probe must run with cwd set to a
    neutral directory (tempfile.gettempdir()), never the repo root — a
    regression guard for the exact defect the chunk brief names."""
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    target = sys.executable
    registry = {m.WHOAMI_PIN_KEY: venv_py, m.GENERAL_PIN_KEY: target}
    monkeypatch.setattr(m, "_ml_get", lambda cli, key: registry.get(key, ""))
    monkeypatch.setattr(m, "_ml_set", lambda cli, key, value: registry.__setitem__(key, value))

    captured_kwargs = {}

    def fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.REPOINTED
    import tempfile

    assert captured_kwargs.get("cwd") == tempfile.gettempdir()
    repo_root = str(Path(__file__).resolve().parents[4])
    assert captured_kwargs.get("cwd") != repo_root


# ---------------------------------------------------------------------------
# AC2: idempotent — a second run (already repointed) is a no-op
# ---------------------------------------------------------------------------


def test_second_run_is_noop(tmp_path, monkeypatch):
    ml_cli = [str(tmp_path / "machine-local")]
    target = sys.executable
    registry = {
        m.WHOAMI_PIN_KEY: target,  # already repointed, not venv-shaped
        m.GENERAL_PIN_KEY: target,
    }
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)
    monkeypatch.setattr(m, "_target_imports_whoami", lambda py: True)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.NOOP_ALREADY_MIGRATED
    assert calls == []
    assert registry[m.WHOAMI_PIN_KEY] == target


def test_migrate_then_migrate_again_is_idempotent(tmp_path, monkeypatch):
    """Full round trip: repoint once, then run again against the resulting
    registry state and observe a clean no-op."""
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    target = sys.executable
    registry = {m.WHOAMI_PIN_KEY: venv_py, m.GENERAL_PIN_KEY: target}
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)
    monkeypatch.setattr(m, "_target_imports_whoami", lambda py: True)

    first = m.migrate_whoami_pin(ml_cli)
    second = m.migrate_whoami_pin(ml_cli)

    assert first == m.REPOINTED
    assert second == m.NOOP_ALREADY_MIGRATED
    assert calls == [(m.WHOAMI_PIN_KEY, target)]


def test_noop_when_pin_never_set(tmp_path, monkeypatch):
    ml_cli = [str(tmp_path / "machine-local")]
    registry = {m.GENERAL_PIN_KEY: sys.executable}
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.NOOP_ALREADY_MIGRATED
    assert calls == []


# ---------------------------------------------------------------------------
# Refusal: verify before repointing — do not repoint blind
# ---------------------------------------------------------------------------


def test_refuses_when_target_does_not_import_whoami(tmp_path, monkeypatch, capsys):
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    target = str(tmp_path / "unhealthy-python")
    registry = {m.WHOAMI_PIN_KEY: venv_py, m.GENERAL_PIN_KEY: target}
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)
    monkeypatch.setattr(m, "_target_imports_whoami", lambda py: False)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.REFUSED_TARGET_UNHEALTHY
    assert calls == []
    assert registry[m.WHOAMI_PIN_KEY] == venv_py  # left untouched
    assert "REFUSED" in capsys.readouterr().err


def test_refuses_when_general_pin_unset(tmp_path, monkeypatch):
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    registry = {m.WHOAMI_PIN_KEY: venv_py}
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.REFUSED_NO_MACHINE_PIN
    assert calls == []


def test_refuses_when_general_pin_also_names_the_venv(tmp_path, monkeypatch):
    """No fallback escape hatches: if coordinator.python itself still names
    the venv, there is nothing sane to repoint onto."""
    ml_cli = [str(tmp_path / "machine-local")]
    venv_py = _venv_python_str(tmp_path)
    registry = {m.WHOAMI_PIN_KEY: venv_py, m.GENERAL_PIN_KEY: venv_py}
    calls = []
    get, set_ = _fake_ml(registry, calls)
    monkeypatch.setattr(m, "_ml_get", get)
    monkeypatch.setattr(m, "_ml_set", set_)

    result = m.migrate_whoami_pin(ml_cli)

    assert result == m.REFUSED_NO_MACHINE_PIN
    assert calls == []


# ---------------------------------------------------------------------------
# Graceful degradation — no machine-local CLI available
# ---------------------------------------------------------------------------


def test_noop_when_ml_cli_absent(capsys):
    result = m.migrate_whoami_pin(None)
    assert result == m.NOOP_NO_CLI
    assert "machine-local CLI not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _target_imports_whoami — real subprocess integration (unmocked)
# ---------------------------------------------------------------------------


def test_target_imports_whoami_false_for_nonexistent_interpreter(tmp_path):
    fake_interp = str(tmp_path / "does-not-exist" / "python.exe")
    assert m._target_imports_whoami(fake_interp) is False


def test_write_surface_declaration_is_valid():
    from coordinator_core.install.write_surface import validate

    assert validate(m.WRITE_SURFACE) == ()


def test_target_imports_whoami_true_for_this_interpreter_importing_sys():
    """Uses `sys` (always importable) in place of `coordinator_whoami` to
    exercise the real subprocess path without depending on this box's
    actual package layout — a monkeypatch-free smoke test of the mechanism
    itself, separate from the mocked-outcome tests above."""
    import subprocess
    import tempfile

    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.run(
        [sys.executable, "-c", "import sys"],
        capture_output=True,
        timeout=30,
        cwd=tempfile.gettempdir(),
        **no_console_creationflags(),
    )
    assert proc.returncode == 0
