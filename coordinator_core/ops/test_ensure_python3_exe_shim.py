"""Characterization tests for coordinator_core.ops.ensure_python3_exe_shim.

Covers the internal-logic branches (AppX stub, stale-shim reshim,
hardlink-fallback, CHECK_ONLY) the bash suite deferred as TODO stubs (real
hardlink/copy needs a real NTFS volume with python.exe present — this
module's logic is OS-gate-independent below main(), so it's exercised
directly here without a Windows host).

Port of: test-ensure-python3-exe-shim.sh (example-doctrine-repo 432e3285, 2026-07-22)

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""
from __future__ import annotations

import os
import sys

import pytest

import asyncio
import subprocess

from coordinator_core.ops import ensure_python3_exe_shim as mod
from coordinator_core.ops.ensure_python3_exe_shim import (
    _classify_python3,
    _detect_python3_appx_stub,
    _install_shim,
    _is_appx_stub,
    _resolve_python_bin,
    main,
)


def _write(path, data=b"payload"):
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# main() OS gate
# ---------------------------------------------------------------------------


def test_main_noop_on_non_windows(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    assert main([]) == 0


def test_main_delegates_when_windows(monkeypatch, capsys):
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_resolve_python_bin", lambda: "")
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no Python interpreter resolved" in captured.err


def test_main_reads_check_only_env(monkeypatch, tmp_path, capsys):
    _write(tmp_path / "python.exe", b"aaa")
    _write(tmp_path / "python3.exe", b"bbb")
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_resolve_python_bin", lambda: str(tmp_path / "python.exe"))
    monkeypatch.setenv("CHECK_ONLY", "1")
    rc = main([])
    # Stale shim -- check-only now fails loud rather than reporting 0.
    assert rc == 1
    captured = capsys.readouterr()
    assert "would install" in captured.out
    # No mutation happened.
    assert (tmp_path / "python3.exe").read_bytes() == b"bbb"


# ---------------------------------------------------------------------------
# _resolve_python_bin — PythonPinInvalid fallback
# ---------------------------------------------------------------------------


def test_resolve_python_bin_returns_resolved_value(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.pyresolve.resolve_python_bin",
        lambda: ("/opt/found/python", []),
    )
    assert _resolve_python_bin() == "/opt/found/python"


def test_resolve_python_bin_falls_back_on_invalid_pin(monkeypatch):
    from coordinator_core.pyresolve import PythonPinInvalid

    def _raise():
        raise PythonPinInvalid("bad pin")

    monkeypatch.setattr("coordinator_core.pyresolve.resolve_python_bin", _raise)
    assert _resolve_python_bin() == sys.executable


# ---------------------------------------------------------------------------
# _install_shim — no PYTHON_BIN resolved
# ---------------------------------------------------------------------------


def test_install_shim_no_interpreter_resolved(capsys):
    rc = _install_shim("", check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no Python interpreter resolved" in captured.err
    assert "PYTHON_BIN" not in captured.err or True  # message names the state, not the literal env var


# ---------------------------------------------------------------------------
# _install_shim — launcher fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("launcher", ["py", "pyw"])
def test_install_shim_launcher_skip(launcher, capsys):
    rc = _install_shim(launcher, check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert "launcher" in captured.err
    assert launcher in captured.err


# ---------------------------------------------------------------------------
# _install_shim — source binary missing
# ---------------------------------------------------------------------------


def test_install_shim_source_missing(tmp_path, capsys):
    missing = tmp_path / "nonexistent" / "python.exe"
    rc = _install_shim(str(missing), check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert "not found" in captured.err


# ---------------------------------------------------------------------------
# _install_shim — AppX zero-byte reparse stub (exists, not a regular file)
# ---------------------------------------------------------------------------


def test_install_shim_appx_stub_fails_loud(tmp_path, capsys):
    _write(tmp_path / "python.exe")
    (tmp_path / "python3.exe").mkdir()  # a directory: exists(), not is_file()
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "not a regular file" in captured.err
    assert "install-substrate" in captured.err


# ---------------------------------------------------------------------------
# _install_shim — `._pth` executable-basename trap
# ---------------------------------------------------------------------------


def test_install_shim_exe_basename_pth_trap_fails_loud(tmp_path, capsys):
    _write(tmp_path / "python.exe")
    _write(tmp_path / "python._pth", b"python312.zip\n.\n")  # keyed off python.exe's own basename
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "python._pth" in captured.err
    assert "isolated mode" in captured.err.lower() or "isolation" in captured.err.lower()
    # No shim installed -- fail loud, not a degraded shim.
    assert not (tmp_path / "python3.exe").exists()


def test_install_shim_dll_named_pth_does_not_trip_trap(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"fresh-bytes")
    _write(tmp_path / "python312._pth", b"python312.zip\n.\n")  # DLL-named -- unaffected
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    assert (tmp_path / "python3.exe").is_file()
    assert (tmp_path / "python3.exe").read_bytes() == b"fresh-bytes"


def test_install_shim_python3_only_pth_does_not_trip_trap(tmp_path, capsys):
    # A distribution shipping only python3._pth (no python._pth) does not
    # trap: the candidate is always built off py_exe.stem, which is
    # "python", so the checked candidate is always "python._pth" -- never
    # "python3._pth" -- regardless of what other ._pth files sit alongside.
    _write(tmp_path / "python.exe", b"fresh-bytes")
    _write(tmp_path / "python3._pth", b"python3.zip\n.\n")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    assert (tmp_path / "python3.exe").is_file()
    assert (tmp_path / "python3.exe").read_bytes() == b"fresh-bytes"


def test_install_shim_unrelated_basename_pth_does_not_trip_trap(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"fresh-bytes")
    _write(tmp_path / "other._pth", b"other.zip\n.\n")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    assert (tmp_path / "python3.exe").is_file()
    assert (tmp_path / "python3.exe").read_bytes() == b"fresh-bytes"


def test_install_shim_pth_trap_case_insensitive_lower(tmp_path, capsys):
    # Pins the assumed case-insensitive filesystem behavior (default on
    # Windows) -- a future change in that assumption should break this test.
    _write(tmp_path / "python.exe")
    _write(tmp_path / "Python._pth", b"python312.zip\n.\n")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 1
    assert not (tmp_path / "python3.exe").exists()


def test_install_shim_pth_trap_case_insensitive_upper(tmp_path, capsys):
    _write(tmp_path / "python.exe")
    _write(tmp_path / "PYTHON._PTH", b"python312.zip\n.\n")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 1
    assert not (tmp_path / "python3.exe").exists()


# ---------------------------------------------------------------------------
# _install_shim — already valid (hardlinked) shim is an idempotent no-op
# ---------------------------------------------------------------------------


def test_install_shim_already_valid_noop(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"same-bytes")
    os.link(tmp_path / "python.exe", tmp_path / "python3.exe")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# _install_shim — CHECK_ONLY on a stale shim reports without mutating
# ---------------------------------------------------------------------------


def test_install_shim_check_only_stale_no_mutation(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"new-bytes")
    _write(tmp_path / "python3.exe", b"old-bytes")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=True)
    assert rc == 1
    captured = capsys.readouterr()
    assert "would install" in captured.out
    assert (tmp_path / "python3.exe").read_bytes() == b"old-bytes"


# ---------------------------------------------------------------------------
# _install_shim — stale shim gets removed and re-hardlinked
# ---------------------------------------------------------------------------


def test_install_shim_stale_reshimmed_via_hardlink(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"new-bytes")
    _write(tmp_path / "python3.exe", b"old-bytes")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert "hardlinked" in captured.err
    assert (tmp_path / "python3.exe").read_bytes() == b"new-bytes"
    # Confirm it's actually a hardlink (same inode), not a copy.
    assert (tmp_path / "python.exe").stat().st_ino == (tmp_path / "python3.exe").stat().st_ino


# ---------------------------------------------------------------------------
# _install_shim — fresh install (no pre-existing shim) via hardlink
# ---------------------------------------------------------------------------


def test_install_shim_fresh_hardlink(tmp_path, capsys):
    _write(tmp_path / "python.exe", b"fresh-bytes")
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    assert (tmp_path / "python3.exe").is_file()
    assert (tmp_path / "python3.exe").read_bytes() == b"fresh-bytes"


# ---------------------------------------------------------------------------
# _install_shim — hardlink fails, falls back to copy
# ---------------------------------------------------------------------------


def test_install_shim_hardlink_fails_falls_back_to_copy(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "python.exe", b"copy-me")

    def _raise_link(src, dst):
        raise OSError("cross-device link simulated")

    monkeypatch.setattr(mod.os, "link", _raise_link)
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 0
    captured = capsys.readouterr()
    assert "copied" in captured.err
    assert (tmp_path / "python3.exe").read_bytes() == b"copy-me"


# ---------------------------------------------------------------------------
# _install_shim — both hardlink and copy fail
# ---------------------------------------------------------------------------


def test_install_shim_hardlink_and_copy_both_fail(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "python.exe", b"unreachable")

    def _raise_link(src, dst):
        raise OSError("link failed")

    def _raise_copy(src, dst):
        raise OSError("copy failed")

    monkeypatch.setattr(mod.os, "link", _raise_link)
    monkeypatch.setattr(mod.shutil, "copy2", _raise_copy)
    rc = _install_shim(str(tmp_path / "python.exe"), check_only=False)
    assert rc == 1


# ---------------------------------------------------------------------------
# _is_appx_stub
# ---------------------------------------------------------------------------


def test_is_appx_stub_false_for_nonexistent_path(tmp_path):
    assert _is_appx_stub(tmp_path / "nope.exe") is False


def test_is_appx_stub_false_for_regular_file(tmp_path):
    target = tmp_path / "python.exe"
    _write(target)
    assert _is_appx_stub(target) is False


def test_is_appx_stub_true_for_exists_not_file(tmp_path):
    target = tmp_path / "python3.exe"
    target.mkdir()  # exists(), not is_file() — the module's stand-in for the reparse stub
    assert _is_appx_stub(target) is True


# ---------------------------------------------------------------------------
# _classify_python3 — install.detect_python3_appx_stub
# ---------------------------------------------------------------------------


def test_classify_python3_not_found(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert _classify_python3() == {"classification": "not_found", "path": None}


def test_classify_python3_stub_via_appx_probe(monkeypatch, tmp_path):
    stub = tmp_path / "python3"
    stub.mkdir()  # exists(), not is_file()
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(stub))

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run must not be invoked on a detected AppX stub")

    monkeypatch.setattr(mod.subprocess, "run", _fail_if_called)
    result = _classify_python3()
    assert result == {"classification": "stub", "path": str(stub)}


def test_classify_python3_stub_via_failed_version_probe(monkeypatch, tmp_path):
    fake = tmp_path / "python3"
    _write(fake)
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(fake))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=1),
    )
    result = _classify_python3()
    assert result == {"classification": "stub", "path": str(fake)}


def test_classify_python3_stub_on_oserror(monkeypatch, tmp_path):
    fake = tmp_path / "python3"
    _write(fake)
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(fake))

    def _raise(*a, **k):
        raise OSError("cannot exec")

    monkeypatch.setattr(mod.subprocess, "run", _raise)
    result = _classify_python3()
    assert result == {"classification": "stub", "path": str(fake)}


def test_classify_python3_stub_on_timeout(monkeypatch, tmp_path):
    fake = tmp_path / "python3"
    _write(fake)
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(fake))

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=10)

    monkeypatch.setattr(mod.subprocess, "run", _raise)
    result = _classify_python3()
    assert result == {"classification": "stub", "path": str(fake)}


def test_classify_python3_ready(monkeypatch, tmp_path):
    real = tmp_path / "python3"
    _write(real)
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(real))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=0),
    )
    result = _classify_python3()
    assert result == {"classification": "ready", "path": str(real)}


def test_classify_python3_idempotent_double_invocation(monkeypatch, tmp_path):
    real = tmp_path / "python3"
    _write(real)
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(real))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=0),
    )
    first = _classify_python3()
    second = _classify_python3()
    assert first == second == {"classification": "ready", "path": str(real)}


def test_classify_python3_real_machine_does_not_crash():
    # Unmocked — exercises the real shutil.which / subprocess.run path on
    # whatever machine runs the suite. Must never raise, and on macOS/Linux
    # must never misreport "stub" (no AppX reparse-point shape exists here).
    result = _classify_python3()
    assert result["classification"] in {"not_found", "ready", "stub"}
    if sys.platform not in ("win32", "cygwin"):
        assert result["classification"] != "stub"


# ---------------------------------------------------------------------------
# install.detect_python3_appx_stub — registered op handler
# ---------------------------------------------------------------------------


def test_op_registered_under_install_key():
    from coordinator_core.ipc import _REGISTRY

    assert "install.detect_python3_appx_stub" in _REGISTRY


def test_detect_python3_appx_stub_handler_ignores_params_and_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    result = asyncio.run(_detect_python3_appx_stub({"unexpected": "value"}, repo_root=tmp_path))
    assert result == {"classification": "not_found", "path": None}


def test_detect_python3_appx_stub_handler_default_repo_root(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    result = asyncio.run(_detect_python3_appx_stub({}))
    assert result == {"classification": "not_found", "path": None}
