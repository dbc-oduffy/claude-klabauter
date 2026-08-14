"""test_raw_cmdline_recovery.py — unit tests for the shared
`recover_windows_argv` helper.

See coordinator/bin/lib/raw_cmdline_recovery.py module docstring for the
caret-eating defect this helper recovers from. `os.name` is patched to
exercise the Windows-only recovery path from a non-Windows test host — the
actual cmd.exe caret behaviour cannot be exercised here (no Windows host in
this environment); these tests hold the recovery function's own parse/
fallback contract given a synthesized `%CMDCMDLINE%` capture file.
"""
from __future__ import annotations

import os
import pathlib
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import raw_cmdline_recovery as _mod  # noqa: E402
from raw_cmdline_recovery import RAW_CMDLINE_FILE_ENV, recover_windows_argv  # noqa: E402

_LAUNCHER = "scoped-git-commit.cmd"


def _write_capture(tmp_path, text, monkeypatch):
    capture = tmp_path / "capture.tmp"
    capture.write_text(text, encoding="utf-8")
    monkeypatch.setenv(RAW_CMDLINE_FILE_ENV, str(capture))
    return capture


def _patch_windows(monkeypatch):
    """Fake ``os.name == "nt"`` to exercise the Windows-only branch from a
    POSIX test host. ``pathlib.Path`` itself picks ``WindowsPath`` vs.
    ``PosixPath`` by re-checking ``os.name`` on every construction (not just
    at import time), so patching ``os.name`` alone makes the module's own
    ``Path(raw_file)`` calls try to interpret a real POSIX temp path as a
    Windows one and fail to find it — an artifact of faking the platform
    from the wrong host, not a real behaviour. Pinning the module's ``Path``
    to ``pathlib.PosixPath`` here keeps the read working against this test
    host's real filesystem while still exercising the ``os.name``-gated
    recovery logic; real Windows never needs this pin (there, ``Path``
    already resolves to ``WindowsPath`` correctly on its own).
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(_mod, "Path", pathlib.PosixPath)


def test_non_windows_returns_argv_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    _write_capture(tmp_path, 'cmd /c "scoped-git-commit.cmd -m "hi^!" -- a.txt"', monkeypatch)
    assert recover_windows_argv(["-m", "hi", "--", "a.txt"], _LAUNCHER) == [
        "-m", "hi", "--", "a.txt",
    ]


def test_windows_missing_env_var_returns_argv_unchanged(monkeypatch):
    _patch_windows(monkeypatch)
    monkeypatch.delenv(RAW_CMDLINE_FILE_ENV, raising=False)
    assert recover_windows_argv(["-m", "hi"], _LAUNCHER) == ["-m", "hi"]


def test_windows_recovers_caret_from_raw_capture(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    capture = _write_capture(
        tmp_path,
        'cmd /c "scoped-git-commit.cmd --sha-range abc123^..def456 -- a.txt"',
        monkeypatch,
    )
    # The mangled argv cmd.exe would have actually delivered to sys.argv:
    # the caret is gone (cmd.exe's own %* population strips it).
    mangled = ["--sha-range", "abc123..def456", "--", "a.txt"]
    recovered = recover_windows_argv(mangled, _LAUNCHER)
    assert recovered == ["--sha-range", "abc123^..def456", "--", "a.txt"]
    # Best-effort cleanup: the capture file is consumed and removed.
    assert not capture.exists()


def test_windows_token_count_mismatch_falls_back_to_argv(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(
        tmp_path,
        'cmd /c "scoped-git-commit.cmd -m one two three"',
        monkeypatch,
    )
    mangled = ["-m", "one"]
    assert recover_windows_argv(mangled, _LAUNCHER) == mangled


def test_windows_launcher_name_absent_falls_back_to_argv(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(tmp_path, 'cmd /c "some-other-launcher.cmd -m hi"', monkeypatch)
    mangled = ["-m", "hi"]
    assert recover_windows_argv(mangled, _LAUNCHER) == mangled


def test_windows_empty_capture_falls_back_to_argv(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(tmp_path, "", monkeypatch)
    mangled = ["-m", "hi"]
    assert recover_windows_argv(mangled, _LAUNCHER) == mangled


def test_windows_unreadable_capture_falls_back_to_argv(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    monkeypatch.setenv(RAW_CMDLINE_FILE_ENV, str(tmp_path / "does-not-exist.tmp"))
    mangled = ["-m", "hi"]
    assert recover_windows_argv(mangled, _LAUNCHER) == mangled
