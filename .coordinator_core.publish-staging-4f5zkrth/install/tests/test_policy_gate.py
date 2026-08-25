"""
coordinator_core.install.tests.test_policy_gate

Behavioural tests for the fail-closed dual-host PowerShell execution-policy
gate (coordinator_core.install.policy_gate).

Spec backlink: pln-second-managed-launcher-class-aea900,
chunk C2 (AC3, AC4, AC6).

All probes are faked — no real PowerShell/pwsh is invoked, and no test
depends on this box's actual execution policy.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.install import policy_gate
from coordinator_core.install.policy_gate import (
    HOST_PWSH,
    HOST_WINDOWS_POWERSHELL,
    HostVerdict,
    PolicyGateVerdict,
    _INHERITED_POLICY_ENV,
    evaluate_policy_gate,
)


def _completed(stdout="", stderr="", returncode=0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


def _scripted_probe(results: dict):
    """Build a fake probe keyed by executable name (see _HOST_EXECUTABLE)."""

    def probe(executable: str) -> subprocess.CompletedProcess:
        outcome = results[executable]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return probe


# ---------------------------------------------------------------------------
# AC3 — dual-host, independent verdicts: a green on one host never implies
# the other.
# ---------------------------------------------------------------------------


def test_hosts_are_probed_independently_one_green_one_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="Restricted\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    ps_v = verdict.verdict_for(HOST_WINDOWS_POWERSHELL)
    assert pwsh_v.green is True
    assert ps_v.green is False
    # Both verdicts present and independently derived — pwsh's green did
    # not leak into, or get inferred for, Windows PowerShell.
    assert pwsh_v.effective_policy == "RemoteSigned"
    assert ps_v.effective_policy == "Restricted"


def test_both_hosts_green_yields_overall_green() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="Unrestricted\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is True
    assert all(hv.green for hv in verdict.host_verdicts)
    assert {hv.host for hv in verdict.host_verdicts} == {HOST_PWSH, HOST_WINDOWS_POWERSHELL}


def test_reason_names_which_host_failed() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="Restricted\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert "Windows PowerShell" in verdict.reason
    assert "Restricted" in verdict.reason


# ---------------------------------------------------------------------------
# AC4 — the inherited PSExecutionPolicyPreference cannot produce a green
# verdict. This is the regression test for the measurement that killed the
# original single-probe design (property (b)).
# ---------------------------------------------------------------------------


def test_default_probe_strips_inherited_preference_from_child_env(monkeypatch) -> None:
    """The regression test for property (b): the real probe seam must not
    forward PSExecutionPolicyPreference to the child, even when it is set
    (e.g. Bypass) in this process's own environment — the agent harness
    shell is exactly this case.
    """
    import coordinator_core.install.policy_gate as policy_gate_module

    monkeypatch.setenv(_INHERITED_POLICY_ENV, "Bypass")

    captured_envs = []

    def fake_run(argv, capture_output, text, timeout, env, **kwargs):
        captured_envs.append(env)
        return _completed(stdout="Restricted\n")

    monkeypatch.setattr(policy_gate_module.subprocess, "run", fake_run)

    policy_gate_module._default_probe("pwsh")

    assert len(captured_envs) == 1
    assert _INHERITED_POLICY_ENV not in captured_envs[0]


def test_inherited_bypass_in_this_process_env_does_not_leak_through_default_probe(monkeypatch) -> None:
    """End-to-end AC4 regression via the real (default) probe: with Bypass
    inherited in the parent's environment, the default probe must still
    ask the child host directly rather than short-circuiting on the
    inherited value, and the child's env must not carry it.
    """
    import coordinator_core.install.policy_gate as policy_gate_module

    monkeypatch.setenv(_INHERITED_POLICY_ENV, "Bypass")

    def fake_run(argv, capture_output, text, timeout, env, **kwargs):
        assert _INHERITED_POLICY_ENV not in env
        # Host's real store reports Restricted — must NOT be masked by the
        # inherited Bypass that was cleared.
        return _completed(stdout="Restricted\n")

    monkeypatch.setattr(policy_gate_module.subprocess, "run", fake_run)

    verdict = evaluate_policy_gate()

    assert verdict.green is False
    for hv in verdict.host_verdicts:
        assert hv.effective_policy == "Restricted"
        assert hv.green is False


# ---------------------------------------------------------------------------
# AC6 — INDETERMINATE resolves to RED: absent host, timeout, non-zero exit,
# unparseable output, each as a separate case.
# ---------------------------------------------------------------------------


def test_absent_host_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": FileNotFoundError("no such file"),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert pwsh_v.effective_policy is None
    assert "not found" in pwsh_v.reason


def test_probe_timeout_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": subprocess.TimeoutExpired(cmd="pwsh", timeout=15),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert "timed out" in pwsh_v.reason


def test_nonzero_exit_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="", stderr="boom", returncode=1),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert pwsh_v.effective_policy is None
    assert "exited 1" in pwsh_v.reason


def test_unparseable_output_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\nExtraGarbageLine\n"),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert pwsh_v.effective_policy is None
    assert "unparseable" in pwsh_v.reason


def test_empty_output_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout=""),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert verdict.verdict_for(HOST_PWSH).green is False


def test_os_error_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": OSError("permission denied"),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert "failed to launch" in pwsh_v.reason


def test_undecodable_probe_output_resolves_red() -> None:
    """Regression for C2/P2: subprocess.run(text=True) raises UnicodeDecodeError
    on non-UTF-8 probe output (plausible on a non-en-US locale). That
    subclasses ValueError, not OSError/TimeoutExpired/FileNotFoundError, so
    it must be caught explicitly or it escapes the gate entirely instead of
    resolving RED (property (c): INDETERMINATE IS RED).
    """
    undecodable = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
    probe = _scripted_probe(
        {
            "pwsh": undecodable,
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert pwsh_v.effective_policy is None
    assert "decoded" in pwsh_v.reason


def test_unrecognized_policy_value_resolves_red() -> None:
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="AllSigned\n"),
            "powershell.exe": _completed(stdout="RemoteSigned\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.green is False
    assert pwsh_v.effective_policy == "AllSigned"


# ---------------------------------------------------------------------------
# General shape / no-remediation sanity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Windows PowerShell 5.1 absent on a non-Windows platform is NOT-APPLICABLE,
# not RED — it must never by itself sink an otherwise-green pwsh verdict.
# ---------------------------------------------------------------------------


def test_windows_powershell_absent_on_non_windows_is_not_applicable_not_red(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": FileNotFoundError("no such file"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is True
    ps_v = verdict.verdict_for(HOST_WINDOWS_POWERSHELL)
    assert ps_v.not_applicable is True
    assert ps_v.green is False
    assert "not applicable" in ps_v.reason
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.not_applicable is False
    assert pwsh_v.green is True


def test_windows_powershell_absent_on_windows_platform_still_resolves_red(monkeypatch) -> None:
    """The N/A carve-out is platform-gated — on an actual Windows host,
    `powershell.exe` missing is a genuine, RED-worthy gap (property (c)
    unchanged there), not a structural non-question."""
    monkeypatch.setattr("sys.platform", "win32")
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": FileNotFoundError("no such file"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    ps_v = verdict.verdict_for(HOST_WINDOWS_POWERSHELL)
    assert ps_v.not_applicable is False
    assert ps_v.green is False
    assert "not found on PATH" in ps_v.reason


def test_pwsh_absent_is_still_red_even_when_windows_powershell_not_applicable(monkeypatch) -> None:
    """N/A on one host never masks a RED on the other — dual-host
    independence (property (a)) holds across the N/A carve-out too."""
    monkeypatch.setattr("sys.platform", "darwin")
    probe = _scripted_probe(
        {
            "pwsh": FileNotFoundError("no such file"),
            "powershell.exe": FileNotFoundError("no such file"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert "not found on PATH" in verdict.reason
    assert verdict.verdict_for(HOST_PWSH).not_applicable is False


# ---------------------------------------------------------------------------
# AC7/AC8 — an unreadable probe (no parsed policy token) must be
# distinguishable from a parsed-but-restrictive policy, and neither case may
# become green. Spec: docs/plans/2026-08-20-newline-bearing-argv-fails-loud.md
# chunk C6.
# ---------------------------------------------------------------------------


def test_unreadable_probe_is_classified_unreadable_not_restrictive() -> None:
    """A probe that exits non-zero without emitting a parsed policy token
    (the CouldNotAutoloadMatchingModule case on this box) must be flagged
    `unreadable=True` — distinct from a host that answered with a policy
    the gate does not permit."""
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(
                stdout="",
                stderr=(
                    "Get-ExecutionPolicy: The term 'Get-ExecutionPolicy' was found in the "
                    "module 'Microsoft.PowerShell.Security', but the module could not be "
                    "loaded."
                ),
                returncode=1,
            ),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert verdict.unreadable is True
    ps_v = verdict.verdict_for(HOST_WINDOWS_POWERSHELL)
    assert ps_v.green is False
    assert ps_v.unreadable is True
    assert ps_v.effective_policy is None
    # The other host parsed a permissive policy and must not be flagged
    # unreadable — the distinction is per-host, not gate-wide.
    pwsh_v = verdict.verdict_for(HOST_PWSH)
    assert pwsh_v.unreadable is False


def test_restrictive_policy_is_classified_restrictive_not_unreadable() -> None:
    """A probe that DID produce a single parsed policy token — just not a
    permissive one — must be flagged `unreadable=False`: this box was
    asked and answered, unlike the unreadable case above."""
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="Restricted\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert verdict.unreadable is False
    ps_v = verdict.verdict_for(HOST_WINDOWS_POWERSHELL)
    assert ps_v.green is False
    assert ps_v.unreadable is False
    assert ps_v.effective_policy == "Restricted"


def test_restrictive_policy_remains_non_green_after_classification() -> None:
    """AC8 — the classification in the previous test must not be usable to
    turn a genuinely restrictive policy green. Both RED reasons stay RED;
    this is the anti-scope guard the plan calls out explicitly."""
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="Restricted\n"),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert verdict.verdict_for(HOST_WINDOWS_POWERSHELL).green is False


def test_unreadable_probe_also_remains_non_green() -> None:
    """AC8's other half — an unreadable probe stays RED too; legibility of
    the distinction is not a path to a green verdict for either case."""
    probe = _scripted_probe(
        {
            "pwsh": _completed(stdout="RemoteSigned\n"),
            "powershell.exe": _completed(stdout="", stderr="module autoload failed", returncode=1),
        }
    )

    verdict = evaluate_policy_gate(probe=probe)

    assert verdict.green is False
    assert verdict.verdict_for(HOST_WINDOWS_POWERSHELL).green is False


def test_module_calls_get_execution_policy_only_no_mutation_command() -> None:
    """(d) NO REMEDIATION — the probe command itself never mutates policy.

    This asserts the mechanism, not just the docstring's promise: the
    actual command string sent to each host is read-only.
    """
    import coordinator_core.install.policy_gate as policy_gate_module

    assert policy_gate_module._PROBE_COMMAND == "Get-ExecutionPolicy"
    assert "Set-ExecutionPolicy" not in policy_gate_module._PROBE_COMMAND
    assert "Unblock-File" not in policy_gate_module._PROBE_COMMAND


# ---------------------------------------------------------------------------
# Probe environment (2026-08-20) — the gate read RED fleet-wide on a box whose
# policy was readable and permissive, because an agent-spawned Python process
# carries no PSModulePath and `env=` replaces the child environment wholesale,
# so `Get-ExecutionPolicy`'s owning module could not autoload.
# ---------------------------------------------------------------------------


def test_probe_env_restores_psmodulepath_when_absent(monkeypatch):
    monkeypatch.setattr(
        policy_gate.os, "environ", {"SystemRoot": r"C:\Windows"}, raising=False
    )
    env = policy_gate._probe_env()
    assert env["PSModulePath"] == r"C:\Windows\system32\WindowsPowerShell\v1.0\Modules"


def test_probe_env_never_overrides_an_existing_psmodulepath(monkeypatch):
    monkeypatch.setattr(
        policy_gate.os,
        "environ",
        {"SystemRoot": r"C:\Windows", "PSModulePath": r"D:\operator\modules"},
        raising=False,
    )
    assert policy_gate._probe_env()["PSModulePath"] == r"D:\operator\modules"


def test_probe_env_still_clears_the_inherited_policy_variable(monkeypatch):
    monkeypatch.setattr(
        policy_gate.os,
        "environ",
        {"SystemRoot": r"C:\Windows", "PSExecutionPolicyPreference": "Bypass"},
        raising=False,
    )
    assert "PSExecutionPolicyPreference" not in policy_gate._probe_env()


def test_probe_env_omits_psmodulepath_when_systemroot_is_unknown(monkeypatch):
    monkeypatch.setattr(policy_gate.os, "environ", {}, raising=False)
    assert "PSModulePath" not in policy_gate._probe_env()
