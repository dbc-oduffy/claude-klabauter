"""Fail-closed dual-host PowerShell execution-policy verdict.

<!-- Spec backlink: pln-second-managed-launcher-class-aea900,
     Problem §3, chunk C2. -->

This module computes a verdict for whether `.ps1` launchers may run on THIS
machine. It is pure verdict computation — it probes and reports. It does
**not** emit launchers, roll back anything, or mutate the machine in any
way: no `Set-ExecutionPolicy`, no `Unblock-File`. Per the C7 ruling
(`wont_do`, pm_approved), remediation is explicitly out of scope for this
module; a caller decides what to do with a RED verdict (see C3/C4).

Four properties, each defending a NAMED false-green this module exists to
close. A partial gate that drops any one of these reports green when it
should not — that is the exact unsoundness this module is for.

(a) DUAL-HOST, INDEPENDENT VERDICTS. pwsh 7 and Windows PowerShell 5.1
    (``powershell.exe``) are probed and verdicted separately. One is never
    inferred from the other, and effective policy is never compared across
    hosts and collapsed to a single probe. Measured on the dev box, same
    user, same moment, with ``PSExecutionPolicyPreference`` cleared in both
    children::

        scope         | pwsh 7.6.4    | Windows PowerShell 5.1.26100.8875
        CurrentUser   | Undefined     | RemoteSigned
        LocalMachine  | RemoteSigned  | Undefined

    The two hosts disagree on BOTH scopes. Effective policy matching is
    this box's coincidence — sourced from a different scope on each host,
    not a property of the hosts agreeing.

(b) CLEAR THE INHERITED PREFERENCE. ``PSExecutionPolicyPreference`` is a
    process-scope environment variable that propagates policy to child
    processes. It is removed from each probe child's environment before
    probing. Without this, the probe measures the INSTALLER's policy, not
    the operator's — a measured false green on exactly the automated paths
    (agent harness, CI) least likely to be human-checked; the agent
    harness shell carries ``Bypass``.

(c) INDETERMINATE IS RED. Host absent, probe timed out, non-zero exit,
    unparseable output — every one of these resolves RED. There is no path
    to a green verdict that is not a positive, parsed, affirmative result.
    Every probe carries an explicit timeout; a hung host is RED, not a
    hang.

(d) NO REMEDIATION. This module reports and skips. It must not call
    ``Set-ExecutionPolicy`` and must not call ``Unblock-File`` — the
    unblock step lives in C3's emission path in ``substrate.py``, behind
    ``_refuse_machine_mutation``, and is not this module's concern.

Doctrine this instantiates (cited, not re-derived):
``docs/wiki/claude-klabauter-install-doctor-system.md``, "Dogfood learnings
(2026-07-04 live validation)" section: "a doctor that self-spawns the
thing it checks proves it CAN start, not that it IS connected."
Properties (a) and (b) are that doctrine's execution-policy instance — a
probe run inside the installer's own environment certifies the
installer's launch conditions, not the operator's policy.

AC10 — post-install mutability residual (documented here, not designed
around): execution policy is mutable AFTER install. Nothing in this
module, or anywhere in the install chain, closes that permanently — an
operator (or another process) can change either host's policy the moment
after this gate reports GREEN. This verdict is a point-in-time read, not a
durable guarantee.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()

# Process-scope env var that propagates execution policy to child
# processes. MUST be cleared from every probe child's environment — see
# module docstring, property (b).
_INHERITED_POLICY_ENV = "PSExecutionPolicyPreference"

# Policy values under which a `.ps1` launcher runs without a signature.
# Anything else (Restricted, AllSigned, Default, Undefined-with-no-signed-
# fallback, or a value we don't recognize) is NOT a green.
_PERMISSIVE_POLICIES = frozenset({"RemoteSigned", "Unrestricted", "Bypass"})

# Probe timeout: a hung host is RED, not a hang. See property (c).
_PROBE_TIMEOUT_SECONDS = 15

HOST_PWSH = "pwsh"
HOST_WINDOWS_POWERSHELL = "powershell"

# The two hosts this gate probes. Order is stable for callers that display it.
ALL_HOSTS = (HOST_PWSH, HOST_WINDOWS_POWERSHELL)

_HOST_EXECUTABLE = {
    HOST_PWSH: "pwsh",
    HOST_WINDOWS_POWERSHELL: "powershell.exe",
}

_HOST_DISPLAY_NAME = {
    HOST_PWSH: "PowerShell 7 (pwsh)",
    HOST_WINDOWS_POWERSHELL: "Windows PowerShell 5.1 (powershell.exe)",
}

# `Get-ExecutionPolicy` reports the resolved effective policy for the
# current process across the scope precedence chain (MachinePolicy,
# UserPolicy, Process, CurrentUser, LocalMachine). That IS what governs
# whether a `.ps1` runs, which is what this gate needs — not a per-scope
# breakdown. `PSExecutionPolicyPreference` is stripped from the child's
# environment first (property (b)) so the value reflects the host's own
# stores, not an inherited process-scope override.
_PROBE_COMMAND = "Get-ExecutionPolicy"


@dataclass(frozen=True)
class HostVerdict:
    """One host's independent probe result. Never inferred from another host's.

    `not_applicable` is distinct from a RED `green=False`: it marks a host
    that structurally cannot exist on this platform (Windows PowerShell
    5.1 on a non-Windows host — see `_probe_host`'s FileNotFoundError
    branch), not one that was probed and found wanting or absent-when-it-
    should-be-present. A NOT-APPLICABLE host is excluded from the overall
    AND in `evaluate_policy_gate` (it is neither evidence for nor against
    THIS host's own .ps1-execution soundness) but still reported in
    `host_verdicts` and folded into `reason` so the operator sees which
    question could not be asked here, not just a bare RED.
    """

    host: str
    green: bool
    effective_policy: Optional[str]
    reason: str
    not_applicable: bool = False


@dataclass(frozen=True)
class PolicyGateVerdict:
    """The gate's overall verdict. This is C3's contract — keep it explicit."""

    green: bool
    host_verdicts: List[HostVerdict] = field(default_factory=list)
    reason: str = ""

    def verdict_for(self, host: str) -> Optional[HostVerdict]:
        for hv in self.host_verdicts:
            if hv.host == host:
                return hv
        return None


ProbeFn = Callable[[str], subprocess.CompletedProcess]


def _default_probe(executable: str) -> subprocess.CompletedProcess:
    """Run `Get-ExecutionPolicy` on `executable` with the inherited preference cleared.

    This is the real subprocess seam. Tests inject a fake via the
    `probe` parameter on `evaluate_policy_gate` / `_probe_host` rather
    than monkeypatching this function, so this stays a thin, honest
    wrapper with no fallback logic to accidentally paper over a RED.
    """
    env = dict(os.environ)
    env.pop(_INHERITED_POLICY_ENV, None)
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", _PROBE_COMMAND],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        env=env,
        **_NO_CONSOLE,
    )


def _probe_host(host: str, probe: ProbeFn) -> HostVerdict:
    """Probe one host in isolation. Every failure mode here resolves RED (property (c))."""
    executable = _HOST_EXECUTABLE[host]
    display = _HOST_DISPLAY_NAME[host]

    try:
        proc = probe(executable)
    except FileNotFoundError:
        if host == HOST_WINDOWS_POWERSHELL and sys.platform != "win32":
            # Windows PowerShell 5.1 is a Windows-only host by construction
            # — it will never be on PATH on macOS/Linux, which is a fact
            # about this platform, not a probe failure. Recording it RED
            # (property (c)'s ordinary reading) would roll back a `.ps1`
            # leg that this box's `pwsh` CAN independently verify, purely
            # because this box cannot also answer a Windows-only question.
            # See module docstring, "what the gate is FOR": emitting for a
            # future Windows operator does not require a Windows host here.
            return HostVerdict(
                host=host, green=False, effective_policy=None,
                reason=(
                    f"{display}: not applicable on this platform "
                    f"({sys.platform}) — Windows-only host, not probed here"
                ),
                not_applicable=True,
            )
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: host not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: probe timed out after {_PROBE_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: probe failed to launch ({exc})",
        )
    except UnicodeDecodeError as exc:
        # Review: coordinator:code-reviewer (C2, P2) — subprocess.run(text=True)
        # decodes stdout/stderr with the locale encoding under errors='strict';
        # non-UTF-8 probe output (e.g. a garbled PowerShell banner on a
        # non-en-US locale) raises UnicodeDecodeError, which subclasses
        # ValueError and so was escaping the OSError/TimeoutExpired/
        # FileNotFoundError catches above and propagating out of the gate
        # instead of resolving RED (property (c): INDETERMINATE IS RED).
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: probe output could not be decoded ({exc})",
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: probe exited {proc.returncode}{detail}",
        )

    raw = (proc.stdout or "").strip()
    # Get-ExecutionPolicy prints exactly one token; tolerate trailing CRLF
    # noise but do not tolerate multi-line or otherwise unparseable output —
    # that is INDETERMINATE, which is RED (property (c)).
    lines = [ln.strip() for ln in raw.replace("\r", "").splitlines() if ln.strip()]
    if len(lines) != 1:
        return HostVerdict(
            host=host, green=False, effective_policy=None,
            reason=f"{display}: probe output unparseable ({raw!r})",
        )

    policy = lines[0]
    if policy not in _PERMISSIVE_POLICIES:
        return HostVerdict(
            host=host, green=False, effective_policy=policy,
            reason=f"{display}: effective policy {policy!r} does not permit unsigned scripts",
        )

    return HostVerdict(
        host=host, green=True, effective_policy=policy,
        reason=f"{display}: effective policy {policy!r} permits execution",
    )


def evaluate_policy_gate(
    hosts: Sequence[str] = ALL_HOSTS, probe: ProbeFn = _default_probe
) -> PolicyGateVerdict:
    """Compute the fail-closed dual-host verdict.

    `hosts` and `probe` exist for test injection (a fake probe seam, a
    reduced host set); production callers use the defaults. Each host in
    `hosts` is probed independently (property (a)) — this function never
    infers one host's verdict from another's, and never short-circuits on
    the first RED, so the returned `host_verdicts` always covers every
    requested host.

    Overall verdict is green iff EVERY APPLICABLE probed host is green — a
    host marked `not_applicable` (Windows PowerShell probed from a non-
    Windows platform; see `_probe_host`) contributes its reason to
    `reason` for visibility but is excluded from the AND, so it can never
    by itself pull an otherwise-green host's `.ps1` verification down to
    RED. Any single RED *applicable* host still makes the overall verdict
    RED; the `reason` names which host(s) failed and why, for C4 to render
    to the operator.
    """
    host_verdicts = [_probe_host(host, probe) for host in hosts]

    red = [hv for hv in host_verdicts if not hv.green and not hv.not_applicable]
    if red:
        reason = "; ".join(hv.reason for hv in red)
        return PolicyGateVerdict(green=False, host_verdicts=host_verdicts, reason=reason)

    reason = "; ".join(hv.reason for hv in host_verdicts)
    return PolicyGateVerdict(green=True, host_verdicts=host_verdicts, reason=reason)
