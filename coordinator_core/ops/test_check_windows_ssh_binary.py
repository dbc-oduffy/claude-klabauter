"""Characterization tests for coordinator_core.ops.check_windows_ssh_binary.

Mocks OS/git/filesystem probing throughout — none of these tests depend on
the host actually being Windows or having a real ssh binary/git remote
configured.

Port of: check-windows-ssh-binary.sh (example-doctrine-repo 290997c7, 2026-07-22)

Spec backlink: DR-079 tool 8 assignment; source memo
cross-repo/inbox/2026-07-21-claude-central-em-dr079-doe-dispositions-and-install-health-defect.md
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import check_windows_ssh_binary as mod
from coordinator_core.ops.check_windows_ssh_binary import (
    _classify_and_warn,
    _first_token,
    _has_ssh_remote,
    _resolve_ssh_binary,
    main,
)


def _fake_run_git(mapping):
    """Build a _run_git stand-in keyed on the args tuple."""

    def _run(args):
        return mapping.get(tuple(args))

    return _run


# ---------------------------------------------------------------------------
# main() OS gate
# ---------------------------------------------------------------------------


def test_main_noop_off_windows(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    # If the OS gate didn't short-circuit, this would blow up — no git
    # remote/ssh mocking is installed.
    assert main([]) == 0


def test_main_noop_no_ssh_remote(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_has_ssh_remote", lambda: False)
    assert main([]) == 0


def test_main_never_exits_nonzero_even_on_ambiguous_warn(monkeypatch, capsys):
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_has_ssh_remote", lambda: True)
    monkeypatch.setattr(mod, "_resolve_ssh_binary", lambda: ("", "PATH"))
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no ssh binary resolvable" in captured.err


def test_main_survives_unexpected_exception_from_resolve(monkeypatch, capsys):
    # Review: code-reviewer (Finding 2) — pins the "unexpected internal
    # exception still returns 0" contract that Finding 1's guard now
    # enforces; UnicodeDecodeError is the concrete real-world trigger
    # (non-UTF-8 Windows codepage decoding subprocess output) but any
    # exception must be absorbed here, so a generic RuntimeError suffices
    # to characterize the guard.
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_has_ssh_remote", lambda: True)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_resolve_ssh_binary", _raise)
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "internal error" in captured.err


def test_main_idempotent(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_has_ssh_remote", lambda: True)
    monkeypatch.setattr(
        mod, "_resolve_ssh_binary", lambda: ("C:/Windows/System32/OpenSSH/ssh.exe", "core.sshCommand")
    )
    assert main([]) == 0
    assert main([]) == 0
    assert main([]) == 0


# ---------------------------------------------------------------------------
# _has_ssh_remote
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", True),
        ("ssh://git@github.com/owner/repo.git", True),
        ("https://github.com/owner/repo.git", False),
        ("", False),
    ],
)
def test_has_ssh_remote(monkeypatch, url, expected):
    monkeypatch.setattr(mod, "_run_git", lambda args: url or None)
    assert _has_ssh_remote() is expected


def test_has_ssh_remote_git_failure_treated_as_no_remote(monkeypatch):
    monkeypatch.setattr(mod, "_run_git", lambda args: None)
    assert _has_ssh_remote() is False


# ---------------------------------------------------------------------------
# _first_token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("C:/Windows/System32/OpenSSH/ssh.exe", "C:/Windows/System32/OpenSSH/ssh.exe"),
        ('"C:/Windows/System32/OpenSSH/ssh.exe" -F /dev/null', '"C:/Windows/System32/OpenSSH/ssh.exe"'),
        ("", ""),
        ("   ", ""),
    ],
)
def test_first_token(value, expected):
    assert _first_token(value) == expected


# ---------------------------------------------------------------------------
# _resolve_ssh_binary — precedence order
# ---------------------------------------------------------------------------


def test_resolve_prefers_git_ssh_command_env(monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "C:/Windows/System32/OpenSSH/ssh.exe -F /dev/null")
    monkeypatch.setattr(mod, "_run_git", lambda args: "C:/should/not/be/used/ssh.exe")
    monkeypatch.setenv("GIT_SSH", "C:/also/not/used/ssh.exe")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/also/not/used/ssh")
    resolved, source = _resolve_ssh_binary()
    assert resolved == "C:/Windows/System32/OpenSSH/ssh.exe"
    assert source == "GIT_SSH_COMMAND"


def test_resolve_falls_back_to_core_ssh_command(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(mod, "_run_git", _fake_run_git({("config", "--get", "core.sshCommand"): "C:/pinned/ssh.exe"}))
    monkeypatch.setenv("GIT_SSH", "C:/not/used/ssh.exe")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/not/used/ssh")
    resolved, source = _resolve_ssh_binary()
    assert resolved == "C:/pinned/ssh.exe"
    assert source == "core.sshCommand"


def test_resolve_falls_back_to_git_ssh_env(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(mod, "_run_git", lambda args: None)
    monkeypatch.setenv("GIT_SSH", "C:/legacy/ssh.exe")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/not/used/ssh")
    resolved, source = _resolve_ssh_binary()
    assert resolved == "C:/legacy/ssh.exe"
    assert source == "GIT_SSH"


def test_resolve_falls_back_to_path(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr(mod, "_run_git", lambda args: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/ssh")
    resolved, source = _resolve_ssh_binary()
    assert resolved == "/usr/bin/ssh"
    assert source == "PATH"


def test_resolve_nothing_available(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr(mod, "_run_git", lambda args: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    resolved, source = _resolve_ssh_binary()
    assert resolved == ""
    assert source == "PATH"


# ---------------------------------------------------------------------------
# _classify_and_warn — PASS (Win32-OpenSSH)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resolved",
    [
        "C:/Windows/System32/OpenSSH/ssh.exe",
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "c:/windows/system32/openssh/ssh.exe",
        "D:/Windows/System32/OpenSSH/ssh.exe",
    ],
)
def test_classify_pass_win32_openssh_silent(resolved, capsys):
    _classify_and_warn(resolved, "core.sshCommand")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_classify_pass_single_char_drive_pattern(capsys):
    # /?/windows/system32/openssh/ssh.exe pattern — single-char segment.
    _classify_and_warn("/c/windows/system32/openssh/ssh.exe", "PATH")
    captured = capsys.readouterr()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# _classify_and_warn — unresolvable
# ---------------------------------------------------------------------------


def test_classify_unresolvable_warns(capsys):
    _classify_and_warn("", "PATH")
    captured = capsys.readouterr()
    assert "no ssh binary resolvable" in captured.err
    assert "core.sshCommand" in captured.err


# ---------------------------------------------------------------------------
# _classify_and_warn — recognized MSYS shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resolved",
    [
        "C:/Program Files/Git/usr/bin/ssh.exe",
        "C:/Program Files/Git/usr/bin/ssh",
        r"C:\Users\me\AppData\Local\GitHubDesktop\app\resources\app\git\usr\bin\ssh.exe",
        "C:/Program Files (x86)/Microsoft Visual Studio/2019/Team Explorer/Git/usr/bin/ssh.exe",
        # Review: code-reviewer (Finding 2) — the no-space "teamexplorer"
        # pattern is distinct from "team explorer" (above) and had no
        # dedicated realistic-path test.
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\TeamExplorer\Git\usr\bin\ssh.exe",
        "C:/tools/cwrsync/bin/ssh.exe",
        r"C:\Users\me\AppData\Local\GitHubDesktop\app\resources\app\git\mingit\usr\bin\ssh.exe",
    ],
)
def test_classify_msys_shapes_warn(resolved, capsys):
    _classify_and_warn(resolved, "GIT_SSH_COMMAND")
    captured = capsys.readouterr()
    assert "MSYS ssh.exe" in captured.err
    assert resolved in captured.err
    assert "GIT_SSH_COMMAND" in captured.err


# ---------------------------------------------------------------------------
# _classify_and_warn — resolved path missing on disk
# ---------------------------------------------------------------------------


def test_classify_missing_on_disk_warns(monkeypatch, capsys):
    monkeypatch.setattr(mod.os.path, "exists", lambda path: False)
    _classify_and_warn("C:/some/custom/ssh.exe", "core.sshCommand")
    captured = capsys.readouterr()
    assert "does not exist on disk" in captured.err


# ---------------------------------------------------------------------------
# _classify_and_warn — ambiguous (exists, unrecognized shape)
# ---------------------------------------------------------------------------


def test_classify_ambiguous_existing_binary_warns(monkeypatch, capsys):
    monkeypatch.setattr(mod.os.path, "exists", lambda path: True)
    _classify_and_warn("C:/custom/vendor/ssh.exe", "PATH")
    captured = capsys.readouterr()
    assert "neither confirmed Win32-OpenSSH nor a" in captured.err
    assert "ambiguous" in captured.err


# ---------------------------------------------------------------------------
# Idempotency of classification itself
# ---------------------------------------------------------------------------


def test_classify_idempotent_same_output_each_call(capsys):
    _classify_and_warn("C:/Program Files/Git/usr/bin/ssh.exe", "PATH")
    first = capsys.readouterr().err
    _classify_and_warn("C:/Program Files/Git/usr/bin/ssh.exe", "PATH")
    second = capsys.readouterr().err
    assert first == second
    assert "MSYS ssh.exe" in first


# ---------------------------------------------------------------------------
# _run_git — subprocess plumbing
# ---------------------------------------------------------------------------


def test_run_git_returns_none_on_missing_binary(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(mod.subprocess, "run", _raise)
    assert mod._run_git(["remote", "get-url", "origin"]) is None


def test_run_git_returns_none_on_nonzero_exit(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
    assert mod._run_git(["config", "--get", "core.sshCommand"]) is None


def test_run_git_strips_and_returns_stdout(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "  git@github.com:owner/repo.git\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
    assert mod._run_git(["remote", "get-url", "origin"]) == "git@github.com:owner/repo.git"
