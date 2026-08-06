"""Tests for coordinator_core.ops.verify_ps51_clean.

Port of: verify-ps51-clean.sh (example-doctrine-repo b5a4192c, 2026-07-20), DOE-PORT BIG_PORT
wave A. PS 5.1 engine invocation (`parse_with_ps51`) is monkeypatched in every
test that needs a specific parse outcome — this suite runs identically on any
platform, with or without a real `powershell.exe` on PATH.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from coordinator_core.ops import verify_ps51_clean as vps


CLEAN_PS1 = """# Clean PowerShell 5.1 script
param(
    [string]$Path = "C:\\Temp"
)
function Get-Status {
    param([string]$Name)
    Write-Host "Checking: $Name"
}
Get-Status -Name $Path
"""

WARN_PS1 = (
    '# Script with || inside a string\n'
    '$cmdHint = "Try: apt-get install foo || apt-get install bar"\n'
    'Write-Host $cmdHint\n'
)

PS7_FLOORED_PS1 = (
    "#requires -Version 7.0\n"
    "param([string]$Value = $null)\n"
    '$result = $Value ?? "default-value"\n'
)

PS7_NO_MINOR_PS1 = (
    "#requires -Version 7\n"
    "param([string]$Value = $null)\n"
    '$result = $Value ?? "default"\n'
)

PS6_FLOORED_PS1 = (
    "#requires -Version 6.0\n"
    "param([string]$Value = $null)\n"
    '$result = $Value ?? "default"\n'
)

BOM_FLOORED_PS1 = "﻿#requires -Version 7.0\n$x = $a ?? $b\n"


def _write(tmp_path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def _run_main(argv, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = vps.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Usage guard
# ---------------------------------------------------------------------------

def test_no_args_usage_error():
    rc, out, err = _run_main([])
    assert rc == 2
    assert "Usage:" in err


# ---------------------------------------------------------------------------
# SKIP path — no powershell.exe resolvable
# ---------------------------------------------------------------------------

def test_skip_when_engine_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: None)
    ps1 = _write(tmp_path, "clean.ps1", CLEAN_PS1)
    rc, out, err = _run_main([ps1])
    assert rc == 0
    assert out.startswith("SKIP:")


# ---------------------------------------------------------------------------
# find_ps51_exe / collect_ps1_files (pure logic, no engine needed)
# ---------------------------------------------------------------------------

def test_collect_ps1_files_single_file(tmp_path):
    ps1 = _write(tmp_path, "a.ps1", CLEAN_PS1)
    files, warnings = vps.collect_ps1_files([ps1])
    assert files == [ps1]
    assert warnings == []


def test_collect_ps1_files_missing_path_warns(tmp_path):
    missing = str(tmp_path / "does-not-exist.ps1")
    files, warnings = vps.collect_ps1_files([missing])
    assert files == []
    assert len(warnings) == 1
    assert "WARN: path not found" in warnings[0]


def test_collect_ps1_files_directory_recurses_and_filters(tmp_path):
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "good.ps1").write_text(CLEAN_PS1, encoding="utf-8")
    (d / "ignored.sh").write_text("not ps1", encoding="utf-8")
    (d / "ignored.txt").write_text("not ps1", encoding="utf-8")
    node_modules = d / "node_modules"
    node_modules.mkdir()
    (node_modules / "vendored.ps1").write_text(CLEAN_PS1, encoding="utf-8")

    files, warnings = vps.collect_ps1_files([str(d)])
    assert len(files) == 1
    assert files[0].endswith("good.ps1")
    assert warnings == []


# ---------------------------------------------------------------------------
# #requires -Version N floor detection
# ---------------------------------------------------------------------------

def test_detect_ps7_floor_version_with_minor():
    is_floored, version = vps.detect_ps7_floor(PS7_FLOORED_PS1)
    assert is_floored is True
    assert version == "7.0"


def test_detect_ps7_floor_version_no_minor():
    is_floored, version = vps.detect_ps7_floor(PS7_NO_MINOR_PS1)
    assert is_floored is True
    assert version == "7"


def test_detect_ps7_floor_major_below_7_not_floored():
    is_floored, version = vps.detect_ps7_floor(PS6_FLOORED_PS1)
    assert is_floored is False
    assert version is None


def test_detect_ps7_floor_bom_does_not_mask_requires():
    # Regression test: PORTER-BRIEF-ADDENDUM rule 6 — the fixture's edge (a
    # leading BOM immediately before #requires) is exercised, not glossed over.
    is_floored, version = vps.detect_ps7_floor(BOM_FLOORED_PS1)
    assert is_floored is True
    assert version == "7.0"


def test_detect_ps7_floor_no_requires_line():
    is_floored, version = vps.detect_ps7_floor(CLEAN_PS1)
    assert is_floored is False
    assert version is None


# ---------------------------------------------------------------------------
# Static pwsh7-only syntax scan
# ---------------------------------------------------------------------------

def test_static_scan_flags_double_pipe_in_string():
    warnings = vps.static_scan_warnings(WARN_PS1)
    assert any("&&/||" in w for w in warnings)


def test_static_scan_flags_null_coalescing():
    warnings = vps.static_scan_warnings('$result = $Value ?? "default"\n')
    assert any(w.startswith("?? @line") for w in warnings)


def test_static_scan_null_coalescing_assign_not_double_counted():
    warnings = vps.static_scan_warnings("$x ??= 1\n")
    tokens_on_line_1 = [w for w in warnings if "@line 1" in w and w.startswith("??")]
    assert len(tokens_on_line_1) == 1
    assert tokens_on_line_1[0].startswith("??=")


def test_static_scan_clean_script_no_warnings():
    assert vps.static_scan_warnings(CLEAN_PS1) == []


# ---------------------------------------------------------------------------
# End-to-end classification via main(), with parse_with_ps51 monkeypatched
# ---------------------------------------------------------------------------

def test_main_clean_file_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (0, ""))
    ps1 = _write(tmp_path, "clean.ps1", CLEAN_PS1)
    rc, out, err = _run_main([ps1])
    assert rc == 0
    assert "OK " in out
    assert "1/1 parse-clean (incl. 0 with advisory warnings), 0 failed, 0 expected-ps7" in out


def test_main_parse_error_without_ps7_floor_is_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(
        vps, "parse_with_ps51", lambda exe, path: (1, "Missing closing '}' @line 5")
    )
    ps1 = _write(tmp_path, "bad.ps1", "function Broken {\n")
    rc, out, err = _run_main([ps1])
    assert rc == 1
    assert "FAIL " in out
    assert "0/1 parse-clean" in out
    assert "1 failed" in out


def test_main_parse_error_with_ps7_floor_is_expected_ps7(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (1, "Unexpected token '??'"))
    ps1 = _write(tmp_path, "ps7.ps1", PS7_FLOORED_PS1)
    rc, out, err = _run_main([ps1])
    assert rc == 0
    assert "EXPECTED-PS7 " in out
    assert "1 expected-ps7 (PS7-floored)" in out


def test_main_parse_clean_with_advisory_warn_still_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (0, ""))
    ps1 = _write(tmp_path, "warn.ps1", WARN_PS1)
    rc, out, err = _run_main([ps1])
    assert rc == 0
    assert "WARN " in out
    # WARN files do NOT also print a separate "OK <path>" line (mutually exclusive).
    assert f"OK {ps1}" not in out
    assert "1/1 parse-clean (incl. 1 with advisory warnings), 0 failed" in out


def test_main_directory_with_multiple_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (0, ""))
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "a.ps1").write_text(CLEAN_PS1, encoding="utf-8")
    (d / "b.ps1").write_text(WARN_PS1, encoding="utf-8")
    rc, out, err = _run_main([str(d)])
    assert rc == 0
    assert "2/2 parse-clean" in out


def test_main_missing_path_warns_but_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (0, ""))
    ps1 = _write(tmp_path, "clean.ps1", CLEAN_PS1)
    missing = str(tmp_path / "nonexistent.ps1")
    rc, out, err = _run_main([ps1, missing])
    assert rc == 0
    assert "WARN: path not found" in err


def test_main_empty_directory_no_ps1_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    d = tmp_path / "empty"
    d.mkdir()
    (d / "ignored.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    rc, out, err = _run_main([str(d)])
    assert rc == 0
    assert "No .ps1 files found" in out
    assert "0/0 parse-clean" in out


def test_main_version_6_floor_below_7_is_fail_not_expected(tmp_path, monkeypatch):
    monkeypatch.setattr(vps, "find_ps51_exe", lambda: "/fake/powershell.exe")
    monkeypatch.setattr(vps, "parse_with_ps51", lambda exe, path: (1, "Unexpected token '??'"))
    ps1 = _write(tmp_path, "ps6.ps1", PS6_FLOORED_PS1)
    rc, out, err = _run_main([ps1])
    assert rc == 1
    assert "FAIL " in out
    assert "EXPECTED-PS7" not in out


# ---------------------------------------------------------------------------
# parse_with_ps51 — engine-invocation edge cases (timeout / OSError / nonzero rc)
# ---------------------------------------------------------------------------

def test_parse_with_ps51_nonzero_exit_is_one_parse_error(monkeypatch, tmp_path):
    class _FakeResult:
        returncode = 1
        stdout = ""

    def _fake_run(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(vps.subprocess, "run", _fake_run)
    ps1 = _write(tmp_path, "x.ps1", CLEAN_PS1)
    count, msg = vps.parse_with_ps51("/fake/powershell.exe", ps1)
    assert count == 1
    assert "invocation failed" in msg


def test_parse_with_ps51_timeout_is_one_parse_error(monkeypatch, tmp_path):
    import subprocess as real_subprocess

    def _fake_run(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd="powershell.exe", timeout=30)

    monkeypatch.setattr(vps.subprocess, "run", _fake_run)
    ps1 = _write(tmp_path, "x.ps1", CLEAN_PS1)
    count, msg = vps.parse_with_ps51("/fake/powershell.exe", ps1)
    assert count == 1
    assert "invocation failed" in msg


def test_parse_with_ps51_passes_timeout_and_stdin_devnull(monkeypatch, tmp_path):
    captured = {}

    class _FakeResult:
        returncode = 0
        stdout = "PARSE_ERRORS=0\n"

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeResult()

    monkeypatch.setattr(vps.subprocess, "run", _fake_run)
    ps1 = _write(tmp_path, "x.ps1", CLEAN_PS1)
    vps.parse_with_ps51("/fake/powershell.exe", ps1)
    assert captured.get("timeout") == vps._PS_PARSE_TIMEOUT_SECS
    assert captured.get("stdin") is vps.subprocess.DEVNULL


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
