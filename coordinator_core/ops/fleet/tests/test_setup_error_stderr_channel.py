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
`python -m coordinator_core.invoke` spawn — the transport DoE's
coordinator/bin/lib/cc_invoke.py uses — because an in-process call to the op
cannot observe the process-exit-status half at all.

Op under test: `memo.check_addressee`. It was `memo.send` until 2026-08-25,
when this module was found red because `memo.send` is no longer in the op
registry at all (`ipc.py` says so in as many words) -- the spawn came back
`Method not found`, so the module had stopped exercising its own subject.
`memo.check_addressee` is live, `common_dir`-scoped, and reaches the same
`_common._setup_error` channel, so the contract is pinned against an op that
exists. The receiver-naming assertion did not survive the move: it tested
`memo.send`'s unresolvable-receiver branch specifically, and that branch went
with the op. What replaces it asserts the same underlying property -- the
reason must identify what fired -- against a branch that is still reachable.

Spec backlink: state/bug-backlog/2026-07-22-memo-send-cli-path-refuses-unregistered-sender.yaml
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _claude_klabauter_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _invoke_setup_error_op(tmp_path: Path) -> subprocess.CompletedProcess:
    claude_home = tmp_path / "settings-home"
    machine_local = claude_home / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")

    caller = tmp_path / "caller"
    caller.mkdir()
    subprocess.run(
        ["git", "init", str(caller)],
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    params = {"to": "no-such-receiver-em", "dry_run": False}
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(_claude_klabauter_root()),
        "COORDINATOR_SETTINGS_HOME": str(claude_home),
        "CLAUDE_HOME": str(claude_home),
        "SYSTEMROOT": "C:\\Windows",
    }
    return subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.invoke", "memo.check_addressee",
            "--bare", json.dumps(params), "--repo", str(caller),
            "--allow-unstamped-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture(scope="module")
def _setup_error_spawn(tmp_path_factory) -> subprocess.CompletedProcess:
    return _invoke_setup_error_op(tmp_path_factory.mktemp("setup-err"))


def test_setup_error_reason_reaches_the_spawned_process_stderr(_setup_error_spawn) -> None:
    proc = _setup_error_spawn
    assert "fleet op setup error:" in proc.stderr, (
        "setup-error reason absent from the spawned op process's stderr — the only "
        f"diagnostic channel a frozen exit_code:1 envelope has.\nstderr: {proc.stderr!r}"
    )
    assert "memo.check_addressee" in proc.stderr, (
        "setup-error reason reached stderr but does not name the op it came from — "
        f"the reason text must identify which branch fired.\nstderr: {proc.stderr!r}"
    )


def test_setup_error_exits_the_process_zero_despite_envelope_exit_code_one(
    _setup_error_spawn,
) -> None:
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
