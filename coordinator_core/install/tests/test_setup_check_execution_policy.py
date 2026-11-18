"""setup --check: per-shell execution policy and python3 resolution (Windows only)."""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.install import setup_check
from coordinator_core.install.setup_check import check_execution_policy, check_python3


def _which(present):
    return lambda name: f"/fake/{name}" if name in present else None


def test_off_windows_both_checks_pass_without_spawning():
    def boom(_exe):
        raise AssertionError("spawned off Windows")

    items = check_execution_policy("linux", _which({"pwsh"}), boom)
    assert [i.ok for i in items] == [True]
    assert check_python3("linux", _which(set())).ok


@pytest.mark.parametrize("policy", ["Restricted", "Undefined"])
def test_blocking_policy_fails_naming_shell_and_fix(policy):
    items = check_execution_policy("win32", _which({"pwsh", "powershell"}), lambda exe: policy)
    assert [i.ok for i in items] == [False, False]
    assert "pwsh" in items[0].name and "Set-ExecutionPolicy" in items[0].detail
    assert "powershell" in items[1].name


@pytest.mark.parametrize("policy", ["RemoteSigned", "Bypass", "Unrestricted"])
def test_permitting_policy_passes(policy):
    items = check_execution_policy("win32", _which({"powershell"}), lambda exe: policy)
    assert len(items) == 1 and items[0].ok


def test_one_query_per_installed_shell_only():
    calls = []
    check_execution_policy("win32", _which({"powershell"}), lambda exe: calls.append(exe) or "RemoteSigned")
    assert calls == ["/fake/powershell"]


def test_no_shell_installed_passes():
    items = check_execution_policy("win32", _which(set()), lambda exe: "Restricted")
    assert len(items) == 1 and items[0].ok


def test_query_failure_is_a_fail_item_not_a_crash():
    def broken(_exe):
        raise OSError("exit 1")

    items = check_execution_policy("win32", _which({"pwsh"}), broken)
    assert not items[0].ok and "could not query" in items[0].detail


def test_python3_missing_fails_and_present_passes_on_windows():
    assert not check_python3("win32", _which(set())).ok
    assert check_python3("win32", _which({"python3"})).ok


def test_query_clears_the_preference_env_var(monkeypatch):
    import subprocess
    import types

    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return types.SimpleNamespace(returncode=0, stdout="RemoteSigned\n", stderr="")

    monkeypatch.setenv("PSExecutionPolicyPreference", "Bypass")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert setup_check._query_execution_policy("exe") == "RemoteSigned"
    assert not any(k.upper() == "PSEXECUTIONPOLICYPREFERENCE" for k in seen["env"])


@pytest.mark.skipif(sys.platform != "win32", reason="real shell query is Windows-only")
def test_real_query_returns_a_policy_name():
    import shutil

    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe is None:
        pytest.skip("no PowerShell on PATH")
    assert setup_check._query_execution_policy(exe).isalpha()


def _reg(values):
    return lambda hive, path, name: values.get((path.rsplit("\\", 1)[-1], name))


@pytest.mark.skipif(sys.platform != "win32", reason="winreg is Windows-only")
def test_windows_powershell_policy_precedence_from_registry():
    pol = setup_check._windows_powershell_policy
    assert pol(_reg({("PowerShell", "EnableScripts"): 0})) == "Restricted"
    assert pol(_reg({("PowerShell", "EnableScripts"): 1, ("PowerShell", "ExecutionPolicy"): "AllSigned"})) == "AllSigned"
    assert pol(_reg({("Microsoft.PowerShell", "ExecutionPolicy"): "RemoteSigned"})) == "RemoteSigned"
    assert pol(_reg({("ProductOptions", "ProductType"): "WinNT"})) == "Restricted"
    assert pol(_reg({("ProductOptions", "ProductType"): "ServerNT"})) == "RemoteSigned"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only dispatch")
def test_windows_powershell_query_does_not_spawn(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    assert setup_check._query_execution_policy(str(tmp_path / "x" / "powershell.exe")).isalpha()
