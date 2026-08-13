"""
coordinator_core.ops.fleet.tests.test_setup_error_stderr_channel — pins the ONLY
diagnostic channel a fleet-op setup error has.

Purpose: a setup error returns the frozen exit_code:1 envelope, which carries no
reason field, and `coordinator_core.invoke` maps process exit status off the
JSON-RPC `error` key — which a setup error does NOT set. So the spawned op process
exits 0 while the envelope says exit_code:1, and the human-readable reason exists
ONLY on that process's stderr (_common._setup_error). Any consumer wanting the
reason must read the spawned process's stderr on the rc==0 path.

These tests assert both halves of that contract across a REAL
`python -m coordinator_core.invoke` spawn — the transport coordinator-claude's
coordinator/bin/lib/cc_invoke.py uses — because an in-process call to the op
cannot observe the process-exit-status half at all.

Spec backlink: state/bug-backlog/2026-07-22-memo-send-cli-path-refuses-unregistered-sender.yaml
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _claude_klabauter_root() -> Path:
    # this file: coordinator_core/ops/fleet/tests/test_*.py
    return Path(__file__).resolve().parents[4]


def _invoke_memo_send_unresolvable_receiver(tmp_path: Path) -> subprocess.CompletedProcess:
    """Spawn the real invoke CLI on a memo.send whose receiver cannot resolve.

    The isolated settings home holds a schema-only registry with no repos.* entry,
    so _resolve_receiver_inbox returns None and memo_send takes the
    receiver-not-registered build_setup_error_result branch.
    """
    machine_local = tmp_path / "settings-home" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")

    # memo.send is common_dir-scoped: --repo must be a real git common dir, or the
    # dispatcher rejects it before the op runs and this probe misses its target.
    sender = tmp_path / "sender"
    sender.mkdir()
    subprocess.run(
        ["git", "init", str(sender)],  # popup-safe-env-suppressed
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    params = {
        "dry_run": False,
        "topic": "setup-error-channel-probe",
        "to": "no-such-receiver-em",
        "title": "Probe",
        "body": "Body.\n",
        "from_id": "probe-sender-em",
        "kind": "fyi",
        "summary": "Setup-error stderr channel probe.",
    }
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(_claude_klabauter_root()),
        "COORDINATOR_SETTINGS_HOME": str(tmp_path / "settings-home"),
        "CLAUDE_HOME": str(tmp_path / "settings-home"),
        "SYSTEMROOT": "C:\\Windows",  # Windows: required for socket/crypto init
    }
    return subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.invoke", "memo.send",
            "--bare", json.dumps(params), "--repo", str(sender),
        ],  # popup-safe-env-suppressed
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture(scope="module")
def _setup_error_spawn(tmp_path_factory) -> subprocess.CompletedProcess:
    """One spawn shared by both assertions — the subprocess costs ~1s, the two
    halves of the contract are properties of the same invocation."""
    return _invoke_memo_send_unresolvable_receiver(tmp_path_factory.mktemp("setup-err"))


def test_setup_error_reason_reaches_the_spawned_process_stderr(_setup_error_spawn) -> None:
    """The reason must be on the op process's stderr, not only in the log stream.

    `_LOG.error` alone reaches stderr only via logging's lastResort handler, which
    any consumer calling logging.basicConfig() silently diverts — hence the explicit
    write in _setup_error. Without this line the refusal is undiagnosable: the caller
    sees only `refused (exit_code=1, failed=0)`.
    """
    proc = _setup_error_spawn
    assert "fleet op setup error:" in proc.stderr, (
        "setup-error reason absent from the spawned op process's stderr — the only "
        f"diagnostic channel a frozen exit_code:1 envelope has.\nstderr: {proc.stderr!r}"
    )
    assert "no-such-receiver-em" in proc.stderr, (
        "setup-error reason reached stderr but does not name the failing receiver — "
        f"the reason text must identify which branch fired.\nstderr: {proc.stderr!r}"
    )


def test_setup_error_exits_the_process_zero_despite_envelope_exit_code_one(
    _setup_error_spawn,
) -> None:
    """Process rc is 0 on a setup error — this is WHY stderr must be read on rc==0.

    A consumer that inspects stderr only on the nonzero-exit path (the natural
    fail-closed shape) will silently drop every setup-error reason. Pinning the
    rc==0/exit_code==1 pairing here makes that asymmetry a tested contract rather
    than an incidental property, so a future change to _exit_code_for_response
    cannot quietly move the channel out from under consumers.
    """
    proc = _setup_error_spawn
    assert proc.returncode == 0, (
        f"expected process rc 0 for a setup error (JSON-RPC success carrying an "
        f"exit_code:1 envelope), got {proc.returncode}\nstderr: {proc.stderr!r}"
    )
    envelope = json.loads(proc.stdout)
    assert envelope["exit_code"] == 1
    assert envelope["failed"] == [], (
        "a setup error must report no per-item failures — a consumer printing only "
        "result['failed'] therefore has nothing to print, which is the diagnostic gap."
    )
