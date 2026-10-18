"""Structural pin: the child's warm reach does not load the server-side or
payload-miss closures when it resolves a caller context and a client token.

Checked in a fresh subprocess by modules loaded, never by timing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[3]

_PROBE = """
import json, sys
from coordinator_core.warm import caller_context, skew
caller_context.resolve_caller_context(
    {"plugin_root": "p", "cwd": "c", "session_id": "s", "agent_id": "a", "pid": "1"}
)
try:
    skew.compute_client_token(sys.argv[1])
except skew.UnstampedEngineRootError:
    pass
deferred = [
    "coordinator_core.lifecycle",
    "coordinator_core.engine_version",
    "coordinator_core.subagent_sandbox.provision_report",
    "coordinator_core.session.scope",
    "coordinator_core.session.claim_index",
    "hashlib",
]
print(json.dumps({m: (m in sys.modules) for m in deferred}))
"""


def _loaded(tmp_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
def test_caller_context_and_client_token_leave_deferred_closures_unloaded(tmp_path):
    loaded = _loaded(tmp_path)
    assert not any(loaded.values()), loaded


def test_skew_server_attributes_still_resolve_lazily():
    from coordinator_core import engine_version, lifecycle
    from coordinator_core.warm import skew

    assert skew.engine_version is engine_version
    assert skew.lifecycle is lifecycle


def test_payload_miss_still_resolves_plugin_root_through_provision_report(monkeypatch):
    from coordinator_core.subagent_sandbox import provision_report
    from coordinator_core.warm import caller_context

    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: "ambient-root")
    ctx = caller_context.resolve_caller_context({"session_id": "s"})
    assert ctx.plugin_root == "ambient-root"


_CLIENT_PROBE = """
import json, sys
from coordinator_core.warm import client
client._spawn_once = lambda *a, **k: None
try:
    client.try_warm_dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "memo.list_outbox", "params": {}}
    )
except Exception:
    pass
deferred = [
    "coordinator_core.subagent_sandbox.provision_report",
    "coordinator_core.session.scope",
    "coordinator_core.session.claim_index",
]
print(json.dumps({m: (m in sys.modules) for m in deferred}))
"""


@pytest.mark.spawns_process
def test_real_client_entry_leaves_provision_report_unloaded(tmp_path):
    import os

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", _CLIENT_PROBE],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not any(loaded.values()), loaded


def test_plugin_root_leaf_agrees_with_provision_report():
    from coordinator_core.subagent_sandbox import plugin_root, provision_report

    assert plugin_root.resolve_plugin_root() == provision_report.resolve_plugin_root()
