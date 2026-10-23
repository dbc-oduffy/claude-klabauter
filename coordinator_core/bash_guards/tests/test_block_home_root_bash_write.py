"""Behavioral tests for the home-root Bash/PowerShell guard, driven through ``check()``."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import block_home_root_bash_write as guard
from coordinator_core.write_guards._home_root import DENY_REASON
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: h))
    machine_profile.reset_cache()
    yield h
    machine_profile.reset_cache()


def _payload(cmd, tool="Bash", cwd=None):
    payload = {"tool_name": tool, "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def _denied(result):
    if result is None:
        return False
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == DENY_REASON
    return True


@pytest.mark.parametrize(
    "cmd",
    [
        "echo x > ~/fo.txt",
        "echo x >> ~/fo.txt",
        "echo x > $HOME/fo.txt",
        "echo x > ${HOME}/fo.txt",
        'echo x > "$HOME/fo.txt"',
        "echo x | tee ~/fo.txt",
        "bash -c 'echo x > ~/fo.txt'",
    ],
)
def test_bash_home_creation_is_denied(home, tmp_path, cmd):
    assert _denied(_enveloped(guard.check)(_payload(cmd, cwd=tmp_path)))


def test_bash_absolute_home_literal_is_denied(home, tmp_path):
    cmd = f"echo x > {(home / 'fo.txt').as_posix()}"
    assert _denied(_enveloped(guard.check)(_payload(cmd, cwd=tmp_path)))


def test_relative_redirect_with_cwd_home_is_denied(home):
    assert _denied(_enveloped(guard.check)(_payload("echo x > fo.txt", cwd=home)))


def test_relative_redirect_with_cwd_elsewhere_is_allowed(home, tmp_path):
    assert _enveloped(guard.check)(_payload("echo x > fo.txt", cwd=tmp_path)) is None


@pytest.mark.parametrize(
    "cmd",
    [
        r"Set-Content -Path $env:USERPROFILE\fo.txt -Value x",
        r"Out-File ~\fo.txt",
        "Out-File ~/fo.txt",
        "New-Item ~/fo.txt",
        "'x' | Out-File -FilePath $HOME/fo.txt",
        "echo x > ~/fo.txt",
    ],
)
def test_powershell_home_creation_is_denied(home, tmp_path, cmd):
    assert _denied(_enveloped(guard.check)(_payload(cmd, tool="PowerShell", cwd=tmp_path)))


def test_tool_owned_dotfile_is_allowed(home, tmp_path):
    assert _enveloped(guard.check)(_payload("echo x >> ~/.bashrc", cwd=tmp_path)) is None


def test_existing_home_file_is_allowed(home, tmp_path):
    (home / "keep.txt").write_text("x", encoding="utf-8")
    assert _enveloped(guard.check)(_payload("echo x >> ~/keep.txt", cwd=tmp_path)) is None


def test_home_subdir_target_is_allowed(home, tmp_path):
    assert _enveloped(guard.check)(_payload("echo x > ~/X/repo/scratch/x", cwd=tmp_path)) is None
    assert (
        _enveloped(guard.check)(
            _payload(r"Out-File ~\X\repo\scratch\x", tool="PowerShell", cwd=tmp_path)
        )
        is None
    )


def test_unrelated_command_is_allowed(home, tmp_path):
    assert _enveloped(guard.check)(_payload("git status", cwd=tmp_path)) is None
    assert _enveloped(guard.check)(_payload("echo x > out.txt", cwd=tmp_path)) is None


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive-mount spelling is only decoded on Windows")
def test_msys_spelled_home_is_denied(home, tmp_path):
    drive, rest = os.path.splitdrive(str(home / "fo.txt"))
    msys = "/" + drive[0].lower() + rest.replace("\\", "/")
    assert _denied(_enveloped(guard.check)(_payload(f"echo x > {msys}", cwd=tmp_path)))


@pytest.mark.parametrize("level,decision", [("warn", "allow"), ("off", None)])
def test_guard_level_downgrades_the_deny(home, tmp_path, monkeypatch, level, decision):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
    machine_profile.reset_cache()
    out = _enveloped(guard.check)(_payload("echo x > ~/fo.txt", cwd=tmp_path))
    if decision is None:
        assert out is None
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == decision


def test_malformed_payload_allows(home):
    assert _enveloped(guard.check)({}) is None
    assert _enveloped(guard.check)({"tool_name": "Bash", "tool_input": "nope"}) is None
    assert _enveloped(guard.check)({"tool_name": "Write", "tool_input": {"command": "echo x > ~/f"}}) is None


def test_prefilter_skips_commands_that_cannot_name_home(home, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("tokenizer reached")

    monkeypatch.setattr(guard, "resolve_command_positions", boom)
    assert _enveloped(guard.check)(_payload("echo hello > out.txt", cwd=tmp_path)) is None
