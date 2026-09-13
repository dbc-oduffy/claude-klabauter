"""
coordinator_core/p4/runner.py — the one p4 spawn helper (D2).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § D2.

Every p4 spawn in this engine goes through ``run()``. It passes ``-p/-u/-c``
explicitly (never relies on ambient ``P4PORT``/``P4USER``/``P4CLIENT`` or a
``.p4config`` walk-up — D1), runs in ``-s`` script mode, pins ``stdin=DEVNULL``,
and applies its own timeout with a process-tree kill (a blackholed P4PORT
hangs past 20s even with ``-v net.maxwait=3`` — cockpit spike,
``example-cockpit-repo docs/research/spike-verdicts/2026-09-12-p4-cli-mechanics-for-the-vcs-provider.md``).

The spike showed an exclusive-lock refusal exiting 0, so the classifier —
never the process exit code — is the contract (D2). ``classify_error`` folds
every ``error:`` line plus plain-text connect errors into exactly three typed
outcomes: ``lock_held(holder)``, ``ticket_expired``, ``refused(raw)``. Every
other case (ignored, default_change_conflict, unreachable, not_installed,
limit_exceeded, unclassified output, and a runner-level timeout) folds into
``refused(raw)``, which fails closed. No caller in this plan branches on more
than these three.

Negative-spec:
  - Never trusts the p4 process exit code.
  - Never retries and never prompts (stdin is DEVNULL).
  - Never reads ``.p4config`` / ambient ``P4*`` env / runs ``p4 set`` — every
    invocation carries its own ``-p/-u/-c``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# DR-054 console-flash guard: 0 (no-op) on POSIX where CREATE_NO_WINDOW
# doesn't exist. Matches this engine's existing convention (dag.py,
# machine_resolver.py, person_resolver.py, ...).
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Runner-level timeout, seconds. The cockpit spike showed a blackholed
#: P4PORT hangs past 20s even with `-v net.maxwait=3`.
DEFAULT_TIMEOUT_S = 20.0

_TICKET_EXPIRED_MARKERS = (
    "ticket",
    "not logged in",
    "session has expired",
    "invalid or unset auth ticket",
)
_LOCK_HELD_MARKERS = ("locked by", "already opened", "exclusive")
_CONNECT_REFUSED_MARKERS = (
    "connect to server failed",
    "tcp connect",
    "couldn't connect",
    "connection refused",
    "network is unreachable",
)


@dataclass(frozen=True)
class P4Error:
    """One of D2's three typed outcomes. ``kind`` is always one of
    ``lock_held`` | ``ticket_expired`` | ``refused``."""

    kind: str
    holder: Optional[str] = None
    raw: Optional[str] = None


@dataclass(frozen=True)
class P4Result:
    ok: bool
    stdout: str
    error: Optional[P4Error] = None


def _extract_holder(body: str) -> Optional[str]:
    match = re.search(r"locked by ([^\s.]+)", body, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"already opened by ([^\s.]+)", body, re.IGNORECASE)
    return match.group(1) if match else None


def classify_error(stdout_text: str, stderr_text: str) -> Optional[P4Error]:
    """Classify p4 output into D2's three typed outcomes, or ``None`` for
    success. Never consults the process exit code (D2 — the spike showed an
    exclusive-lock refusal exiting 0).

    Two arms, in this order:
      1. ``-s`` script-mode ``error:``-prefixed lines — the normal case.
      2. A plain-text arm for connect errors, which print plain text even
         under machine-readable output modes (D2).
    """
    combined = "\n".join(t for t in (stdout_text or "", stderr_text or "") if t)

    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("error:"):
            continue
        body = stripped.split(":", 1)[1].strip()
        lower = body.lower()
        if any(marker in lower for marker in _TICKET_EXPIRED_MARKERS):
            return P4Error(kind="ticket_expired", raw=body)
        if any(marker in lower for marker in _LOCK_HELD_MARKERS):
            return P4Error(kind="lock_held", holder=_extract_holder(body), raw=body)
        return P4Error(kind="refused", raw=body)

    lower_combined = combined.lower()
    if any(marker in lower_combined for marker in _CONNECT_REFUSED_MARKERS):
        return P4Error(kind="refused", raw=combined.strip())

    return None


def _kill_process_tree(pid: int) -> None:
    """Best-effort process-tree kill on runner timeout. ``psutil`` is
    already a dependency of this engine's other subprocess-timeout paths
    (diagnostics/contained_run.py, benchmarks/ambient_sampler.py)."""
    try:
        import psutil
    except ImportError:
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass


def run(
    port: str,
    user: str,
    client: str,
    args: List[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    spec_input: Optional[str] = None,
) -> P4Result:
    """Spawn ``p4 -p <port> -u <user> -c <client> -s <args...>``.

    ``stdin`` is ``DEVNULL`` (never prompts). On a runner-level timeout the
    whole process tree is killed and the result classifies as
    ``refused(raw="timeout")`` — fails closed, never retries.

    ``spec_input`` is the ONE exception to the DEVNULL rule, and it does not
    weaken it. The p4 spec verbs (``change -i`` is the only one this plan
    uses) take their form on standard input and have no command-line flag
    carrying a description or client — ``-i`` plus piped spec text is the
    only non-interactive creation path the CLI offers. DEVNULL is about
    never blocking on an INTERACTIVE prompt; a fully-formed spec written and
    closed before the process can read it is the opposite of a prompt, since
    the pipe reaches EOF immediately and the child can never wait on a human.
    Omitted (the default) the behaviour is byte-for-byte what it was: the
    non-spec verbs keep their DEVNULL guarantee and cannot acquire one by
    accident.
    """
    cmd = ["p4", "-p", port, "-u", user, "-c", client, "-s", *args]
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL if spec_input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATIONFLAGS,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(input=spec_input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            proc.communicate(timeout=2)
        except Exception:  # noqa: BLE001 — best-effort cleanup only
            pass
        return P4Result(ok=False, stdout="", error=P4Error(kind="refused", raw="timeout"))

    error = classify_error(stdout, stderr)
    if error is not None:
        return P4Result(ok=False, stdout=stdout or "", error=error)
    return P4Result(ok=True, stdout=stdout or "")
