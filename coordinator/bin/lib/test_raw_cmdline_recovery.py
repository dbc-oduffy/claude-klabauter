"""test_raw_cmdline_recovery.py — unit tests for the shared
`recover_windows_argv` helper.

See coordinator/bin/lib/raw_cmdline_recovery.py module docstring for the
caret-eating defect this helper recovers from. `_mod._host_is_nt` (a named
platform seam, not the process-global `os.name`) is patched to exercise the
Windows-only recovery path from a non-Windows test host — the actual
cmd.exe caret behaviour cannot be exercised here (no Windows host in this
environment); these tests hold the recovery function's own parse/fallback
contract given a synthesized `%CMDCMDLINE%` capture file.

Negative-spec: does NOT monkeypatch `os.name` — flipping that process-global
makes every `pathlib.Path(...)` constructed afterwards in the same process
(including this module's own `Path(raw_file)` read) pick `WindowsPath`,
which then fails to find a real POSIX temp path. `_host_is_nt` isolates the
Windows-only branch from that global, so no `Path`-pinning workaround is
needed here either. See `_host_is_nt`'s own docstring in
`raw_cmdline_recovery.py`.
"""
from __future__ import annotations

import os
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


def _write_capture(tmp_path, text, monkeypatch):
    capture = tmp_path / "capture.tmp"
    capture.write_text(text, encoding="utf-8")
    monkeypatch.setenv(RAW_CMDLINE_FILE_ENV, str(capture))
    return capture


def _patch_windows(monkeypatch):
    monkeypatch.setattr(_mod, "_host_is_nt", lambda: True)


def test_non_windows_returns_argv_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(_mod, "_host_is_nt", lambda: False)
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
    mangled = ["--sha-range", "abc123..def456", "--", "a.txt"]
    recovered = recover_windows_argv(mangled, _LAUNCHER)
    assert recovered == ["--sha-range", "abc123^..def456", "--", "a.txt"]
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


def test_classify_shape_e_outer_quoted_unquoted_exe_is_sound():
    # Single quote after /c; caret SURVIVES. Must not be a false refusal.
    raw = 'cmd.exe /c "scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == (
        "SOUND", '"scoped-git-commit.cmd --r e9^..e9"',
    )


def test_classify_shape_h_slash_s_slash_c_outer_quoted_is_sound():
    raw = '/s /c "scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == (
        "SOUND", '"scoped-git-commit.cmd --r e9^..e9"',
    )


def test_classify_shape_f_slash_d_slash_s_no_cmd_c_substring_is_sound():
    raw = 'cmd.exe /d /s /c ""scoped-git-commit.cmd" --r e9^..e9"'
    assert "cmd.exe /c " not in raw
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '""scoped-git-commit.cmd" --r e9^..e9"'


def test_classify_list_form_not_outer_quoted_is_unsound():
    raw = "cmd.exe /c scoped-git-commit.cmd --r e9..e9"
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "UNSOUND"
    assert remainder == "scoped-git-commit.cmd --r e9..e9"


def test_classify_no_switch_token_is_unknown():
    raw = 'set "_X=scoped-git-commit.cmd --r e9^..e9"'
    assert _classify_raw_cmdline_transport(raw) == ("UNKNOWN", None)


def test_classify_quoted_comspec_path_then_slash_c_is_sound():
    raw = r'"C:\Windows\System32\cmd.exe" /c "scoped-git-commit.cmd --r e9^..e9"'
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '"scoped-git-commit.cmd --r e9^..e9"'


def test_classify_does_not_infer_from_caret_presence():
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


def test_classify_comspec_with_escaped_quote_then_slash_c_is_sound():
    raw = r'"quoted\path\he said \"hi\"\cmd.exe" /c "scoped-git-commit.cmd --r e9^..e9"'
    status, remainder = _classify_raw_cmdline_transport(raw)
    assert status == "SOUND"
    assert remainder == '"scoped-git-commit.cmd --r e9^..e9"'


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
    _patch_windows(monkeypatch)
    monkeypatch.delenv(RAW_CMDLINE_FILE_ENV, raising=False)
    assert recover_windows_argv(["-m", "hi"], _LAUNCHER) == ["-m", "hi"]
