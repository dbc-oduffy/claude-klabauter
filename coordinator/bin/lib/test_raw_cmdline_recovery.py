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

import pytest  # noqa: E402

import raw_cmdline_recovery as _mod  # noqa: E402
from raw_cmdline_recovery import (  # noqa: E402
    RAW_CMDLINE_FILE_ENV,
    UnsoundRawCmdlineTransport,
    _classify_raw_cmdline_transport,
    recover_windows_argv,
    spawn_shape_prefix,
)

_LAUNCHER = "scoped-git-commit.cmd"

#: The REAL host's `os.name`, read at import before any test monkeypatches it.
#: `_patch_windows` needs to know which host it is faking Windows *from*; asking
#: `os.name` inside the fixture answers "nt" unconditionally, because the fixture
#: itself has just set it.
_REAL_OS_NAME = os.name


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

    NEGATIVE SPEC — the pin is conditional on the REAL host, and must stay so.
    Applying it unconditionally breaks every test in this file on a Windows
    host: Python 3.13 refuses to instantiate a ``PosixPath`` there
    (``UnsupportedOperation: cannot instantiate 'PosixPath' on your system``),
    so the pin that rescues a POSIX host is fatal on an ``nt`` one. This file
    was authored on a POSIX host and silently went inert when the suite began
    running on Windows — the same shape as the defect this module's own
    plan corrects, one layer down. Gate on ``_REAL_OS_NAME``, never on
    ``os.name`` (already faked by the line above).
    """
    monkeypatch.setattr(os, "name", "nt")
    if _REAL_OS_NAME != "nt":
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


# --- C1: outer-quote-pair transport classifier -----------------------------
#
# Real captured shapes, per the plan's Measured substrate (Row 4, corrected)
# and the staff-eng review that falsified the original doubled-quote rule
# (state/subagent-share/7a45b9ab-.../coordinatorstaff-eng-99cb98f5.md,
# seven exactly-constructed raw command lines against a live cmd.exe) —
# pasted from that measurement rather than hand-typed.


def test_classify_shape_e_outer_quoted_unquoted_exe_is_sound():
    # cmd.exe /c "<exe> --r e9^..e9" — outer-quoted, exe path unquoted.
    # Single quote after /c; caret SURVIVES. Must not be a false refusal.
    raw = 'cmd.exe /c "scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == (
        "SOUND", '"scoped-git-commit.cmd --r e9^..e9"',
    )


def test_classify_shape_h_slash_s_slash_c_outer_quoted_is_sound():
    # /s /c "<exe> --r e9^..e9" — same as shape E, under /s.
    raw = '/s /c "scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == (
        "SOUND", '"scoped-git-commit.cmd --r e9^..e9"',
    )


def test_classify_shape_f_slash_d_slash_s_no_cmd_c_substring_is_sound():
    # cmd.exe /d /s /c ""<exe>" --r e9^..e9" — preserves the caret and
    # contains no `cmd.exe /c ` substring at all (measured). A lexical
    # anchor on that literal substring would miss this shape entirely.
    raw = 'cmd.exe /d /s /c ""scoped-git-commit.cmd" --r e9^..e9"'
    assert "cmd.exe /c " not in raw
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '""scoped-git-commit.cmd" --r e9^..e9"'


def test_classify_list_form_not_outer_quoted_is_unsound():
    # subprocess.run([...]) list-form / git-bash-MSYS: cmd.exe /c is
    # reached without outer-quoting the remainder at all — the shape
    # named UNSOUND by C0's designed-red case.
    raw = "cmd.exe /c scoped-git-commit.cmd --r e9..e9"
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "UNSOUND"
    assert remainder == "scoped-git-commit.cmd --r e9..e9"


def test_classify_no_switch_token_is_unknown():
    raw = 'set "_X=scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == ("UNKNOWN", None)


def test_classify_quoted_comspec_path_then_slash_c_is_sound():
    # Quoted comspec token ahead of the switch (8.3-shortened or
    # space-bearing COMSPEC) must be skipped, not mistaken for the
    # remainder.
    raw = r'"C:\Windows\System32\cmd.exe" /c "scoped-git-commit.cmd --r e9^..e9"'
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '"scoped-git-commit.cmd --r e9^..e9"'


def test_classify_does_not_infer_from_caret_presence():
    # A legitimate argument with no metacharacter at all, delivered
    # through the same non-outer-quoted transport, is still UNSOUND.
    raw = "cmd.exe /c scoped-git-commit.cmd --message plain-no-metachar"
    status, _ = _classify_raw_cmdline_transport(raw)
    assert status == "UNSOUND"


def test_windows_sound_outer_quoted_unquoted_exe_recovers_caret(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(
        tmp_path,
        'cmd.exe /c "scoped-git-commit.cmd --sha-range abc123^..def456"',
        monkeypatch,
    )
    mangled = ["--sha-range", "abc123..def456"]
    recovered = recover_windows_argv(mangled, _LAUNCHER)
    assert recovered == ["--sha-range", "abc123^..def456"]


def test_windows_unsound_list_form_raises(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(
        tmp_path,
        "cmd.exe /c scoped-git-commit.cmd --sha-range abc123..def456",
        monkeypatch,
    )
    mangled = ["--sha-range", "abc123..def456"]
    with pytest.raises(UnsoundRawCmdlineTransport):
        recover_windows_argv(mangled, _LAUNCHER)


def test_windows_unknown_no_switch_token_raises(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    _write_capture(
        tmp_path,
        'set "_X=scoped-git-commit.cmd --sha-range abc123^..def456"',
        monkeypatch,
    )
    mangled = ["--sha-range", "abc123..def456"]
    with pytest.raises(UnsoundRawCmdlineTransport):
        recover_windows_argv(mangled, _LAUNCHER)


# Review: coordinator:code-reviewer (9245562b, P2) -- escaped-quote comspec
# token: a bare closing-quote scan would stop at the escaped `\"` inside the
# comspec path, resuming mid-string at an offset unrelated to the real
# switch token. Constructed shape, no known real-world COMSPEC producing it
# (per the reviewer's own finding), but the scan must not misbehave on it.
def test_classify_comspec_with_escaped_quote_then_slash_c_is_sound():
    raw = r'"quoted\path\he said \"hi\"\cmd.exe" /c "scoped-git-commit.cmd --r e9^..e9"'
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '"scoped-git-commit.cmd --r e9^..e9"'


# --- spawn_shape_prefix: leading transport tokens only, never the payload --


def test_spawn_shape_prefix_omits_remainder_payload():
    raw = 'cmd.exe /c "scoped-git-commit.cmd --note super-secret-value"'
    prefix = spawn_shape_prefix(raw)
    assert prefix == "cmd.exe /c"
    assert "super-secret-value" not in prefix
    assert "scoped-git-commit.cmd" not in prefix


def test_spawn_shape_prefix_unknown_shape_is_capped():
    raw = 'set "_X=scoped-git-commit.cmd --note super-secret-value-that-is-long"'
    prefix = spawn_shape_prefix(raw)
    assert len(prefix) <= 40
    assert "super-secret-value-that-is-long" not in prefix


def test_windows_missing_env_var_never_raises_even_when_shape_would_be_unsound(monkeypatch):
    # Negative spec: the missing-env-var fail-safe branch is the escape
    # hatch C2/C2b's remediation message points callers at. It must
    # never become a refusal, regardless of what shape a hypothetical
    # capture would have classified as.
    _patch_windows(monkeypatch)
    monkeypatch.delenv(RAW_CMDLINE_FILE_ENV, raising=False)
    assert recover_windows_argv(["-m", "hi"], _LAUNCHER) == ["-m", "hi"]
