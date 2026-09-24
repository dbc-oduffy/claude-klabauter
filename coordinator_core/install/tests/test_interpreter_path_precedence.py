"""
coordinator_core.install.tests.test_interpreter_path_precedence

Covers C3 leg 1 — the third `_win_user_path_prepend` call site
(`coordinator_core.install.substrate._ensure_interpreter_dir_on_windows_path`),
which puts the resolved interpreter's own directory on the Windows user PATH.

Spec backlink: docs/plans/2026-09-11-the-install-chain-survives-a-genuinely-c.md
(C3), plan-spine row P105-C3
(state/mise-inventory/20260923T192602-acf581c6-hub4.spine.md).

Multi-os-first-class (plan brightline): this leg is Windows-only by the
nature of the defect (a Windows user-PATH registry key has no POSIX analog)
and must SKIP cleanly on POSIX rather than pass vacuously — see
`test_skips_cleanly_on_posix` below, gated by `pytest.mark.skipif` with a
stated reason, and asserted against the production `_is_windows_shell()`
gate the function itself carries (not a test-only shortcut).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from coordinator_core.install import substrate


@pytest.mark.skipif(
    os.name == "nt",
    reason="this test asserts the POSIX skip path; run only where os.name != 'nt'",
)
def test_skips_cleanly_on_posix(monkeypatch):
    """On a non-Windows shell, `_is_windows_shell()` is False regardless of
    any other mocking, so the function must return immediately without
    touching `_resolve_python_bin`, `_cygpath_w`, or the registry-facing
    helpers — a real skip, not a vacuous pass that happens to do nothing."""
    monkeypatch.delenv("OSTYPE", raising=False)
    monkeypatch.delenv("OS", raising=False)
    assert substrate._is_windows_shell() is False

    with mock.patch(
        "coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin"
    ) as resolve_mock:
        substrate._ensure_interpreter_dir_on_windows_path(check_only=False)
    resolve_mock.assert_not_called()


def _fake_win_user_path_entries(already_present=False, target=""):
    entries = [target] if already_present else []
    return lambda: (entries, ";".join(entries), 2)


def _force_windows_shell(monkeypatch):
    monkeypatch.setenv("OS", "Windows_NT")
    assert substrate._is_windows_shell() is True


# Deliberately NOT tmp_path-derived — tmp_path lives under the system temp
# dir, which `_refuse_machine_mutation` blocks as "the signature of a test
# sandbox path" (see test_substrate.py's own `_FAKE_REAL_INSTALL_PATH`
# convention this mirrors). A literal string that never touches disk.
_FAKE_REAL_INTERPRETER_DIR = (
    r"C:\fake-operator-profile\real-interpreter"  # abs-path-ok: fixture, never resolved on disk
    if os.name == "nt"
    else "/fake-operator-profile/real-interpreter"
)


def test_prepends_resolved_interpreter_dir_when_absent(monkeypatch, tmp_path, capsys):
    _force_windows_shell(monkeypatch)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    py_bin = os.path.join(_FAKE_REAL_INTERPRETER_DIR, "python3.exe")
    py_dir_win = _FAKE_REAL_INTERPRETER_DIR

    calls: list = []
    monkeypatch.setattr(
        "coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin",
        lambda: py_bin,
    )
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(
        substrate, "_win_user_path_entries", _fake_win_user_path_entries()
    )
    monkeypatch.setattr(
        substrate,
        "_win_user_path_prepend",
        lambda *a, **k: calls.append(a) or True,
    )

    substrate._ensure_interpreter_dir_on_windows_path(check_only=False)

    assert calls, "expected the third prepend site to fire for an absent, non-temp interpreter dir"
    assert calls[0][0] == py_dir_win
    assert "added" in capsys.readouterr().out


def test_idempotent_on_second_run(monkeypatch, tmp_path, capsys):
    _force_windows_shell(monkeypatch)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    py_bin = os.path.join(_FAKE_REAL_INTERPRETER_DIR, "python3.exe")
    py_dir_win = _FAKE_REAL_INTERPRETER_DIR

    calls: list = []
    monkeypatch.setattr(
        "coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin",
        lambda: py_bin,
    )
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    # Already present this time — mirrors the state after the first run.
    monkeypatch.setattr(
        substrate,
        "_win_user_path_entries",
        _fake_win_user_path_entries(already_present=True, target=py_dir_win),
    )
    monkeypatch.setattr(
        substrate,
        "_win_user_path_prepend",
        lambda *a, **k: calls.append(a) or True,
    )

    substrate._ensure_interpreter_dir_on_windows_path(check_only=False)

    assert not calls, "a second run with the interpreter dir already on PATH must not re-prepend"


def test_check_only_reports_without_mutating(monkeypatch, tmp_path, capsys):
    _force_windows_shell(monkeypatch)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    py_bin = str(tmp_path / "real-interpreter" / "python3.exe")

    calls: list = []
    monkeypatch.setattr(
        "coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin",
        lambda: py_bin,
    )
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(
        substrate, "_win_user_path_entries", _fake_win_user_path_entries()
    )
    monkeypatch.setattr(
        substrate,
        "_win_user_path_prepend",
        lambda *a, **k: calls.append(a) or True,
    )

    substrate._ensure_interpreter_dir_on_windows_path(check_only=True)

    assert not calls, "--check-only must never mutate the registry"
    assert "would: add" in capsys.readouterr().out


def test_no_interpreter_resolved_warns_and_skips(monkeypatch, tmp_path, capsys):
    _force_windows_shell(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin",
        lambda: "",
    )
    calls: list = []
    monkeypatch.setattr(
        substrate,
        "_win_user_path_prepend",
        lambda *a, **k: calls.append(a) or True,
    )

    substrate._ensure_interpreter_dir_on_windows_path(check_only=False)

    assert not calls
    assert "WARNING" in capsys.readouterr().err
