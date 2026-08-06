"""test_check_forwarder_drift_trampoline.py — reachability coverage for
check-forwarder-drift.py, the CLI trampoline over
coordinator_core.plugin_health.forwarder_drift.

coordinator_core/plugin_health/tests/test_forwarder_drift.py already covers
the underlying check_forwarder_drift() logic exhaustively (clean match,
stale-derived, orphaned-installed, per-location independence, skip paths).
What that suite does NOT prove is that anything actually CALLS the op through
a real caller — a detector nothing invokes is exactly the 2026-07-23 incident
shape this probe exists to close (see forwarder_drift.py's module docstring).
This file exercises the trampoline as a subprocess, the same way
/workday-start's Step 1.10 Addon Health invokes it, proving the wiring
itself — not re-testing the op's diff logic.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-claude-klabauter-pickup-assemble-heads-up.md
"""

from __future__ import annotations

import os
import subprocess
import sys

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "check-forwarder-drift.py")


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, _TARGET],
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_trampoline_reaches_the_real_op_and_exits_zero(tmp_path):
    """CLAUDE_KLABAUTER_ROOT resolves to this actual checkout (rung 1: env var
    fast-path — see cc_invoke._resolve_claude_klabauter_root) — the trampoline must
    import and run the real coordinator_core.plugin_health.forwarder_drift
    op, not merely fail to import it, and it must never fail the calling
    ceremony regardless of what the scan finds."""
    proc = _run({"CLAUDE_KLABAUTER_ROOT": _REPO_ROOT})

    assert proc.returncode == 0, proc.stderr
    # Every emission path (ok/warn/skip) from the real op carries this tag —
    # proves the op actually ran rather than the trampoline silently no-op'ing.
    assert "forwarder-drift" in proc.stdout


def test_trampoline_never_fails_when_claude_klabauter_root_is_unresolvable(tmp_path, monkeypatch):
    """An operator/machine with no claude-klabauter checkout registered anywhere must
    get a clean, non-blocking skip from the TRAMPOLINE itself (before it can
    even import the op) — this is a distinct failure mode from the op's own
    internal skip (agent_bin unresolvable) covered by
    coordinator_core/plugin_health/tests/test_forwarder_drift.py."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        "HOME": str(fake_home),
        "CLAUDE_HOME": str(fake_home / ".claude"),
        "CLAUDE_KLABAUTER_ROOT": "",
        "REPO_CLAUDE_KLABAUTER": "",
        "MACHINE_LOCAL_REGISTRY_DIR": str(tmp_path / "no-such-registry"),
    }
    proc = _run(env)

    assert proc.returncode == 0
    assert "CLAUDE_KLABAUTER_ROOT resolution failed" in proc.stderr
