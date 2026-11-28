"""An irreversible payload with no verdict is denied on both halves; a recoverable one is allowed with an advisory."""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm.door.tests.test_cold_leg_kill_telemetry import (  # noqa: F401 -- fixtures
    _HANG,
    _Harness,
    door,
)
from coordinator_core.warm.tests.door_test_support import WINDOWS_ONLY, make_stub_engine_root, pipe_name_for

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_IRREVERSIBLE = "git push --force origin main"
_RECOVERABLE = "echo hi"


@pytest.fixture
def guard_timeout(door, tmp_path, monkeypatch):
    harness = _Harness(door, tmp_path, _HANG, monkeypatch)
    yield harness
    harness.close()


def _decision(stdout: bytes) -> dict:
    return json.loads(stdout.decode("utf-8", "replace"))


def test_irreversible_with_a_cold_guard_timeout_is_denied(guard_timeout):
    proc = guard_timeout.hook(_IRREVERSIBLE)
    assert proc.returncode == 0, proc.stderr
    hook = _decision(proc.stdout)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert _IRREVERSIBLE in hook["permissionDecisionReason"]
    assert "within the hook deadline" in hook["permissionDecisionReason"]


def test_recoverable_with_a_cold_guard_timeout_is_allowed_with_an_advisory(guard_timeout):
    proc = guard_timeout.hook(_RECOVERABLE)
    assert proc.returncode == 0, proc.stderr
    body = _decision(proc.stdout)
    assert "permissionDecision" not in json.dumps(body)
    assert "returned no verdict in time" in body["systemMessage"]


@pytest.mark.cadence
@WINDOWS_ONLY
def test_irreversible_with_a_post_delivery_failure_is_denied_and_recoverable_is_allowed(tmp_path):
    from coordinator_core.warm.door.tests.test_hook_mode_post_delivery_names_timeout import _run_hook_door
    from coordinator_core.warm.tests.test_door_read_deadline import _WedgedServer

    root = make_stub_engine_root(tmp_path)
    results = {}
    for name, command in (("irreversible", f"git -C . push origin main"), ("recoverable", _RECOVERABLE)):
        wedge = _WedgedServer(pipe_name_for(root))
        try:
            results[name] = _run_hook_door(root, "hooks.preuse_bash_dispatch", command=command)[0]
        finally:
            wedge.close()

    denied = _decision(results["irreversible"].stdout)["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "git -C . push origin main" in denied["permissionDecisionReason"]

    allowed = _decision(results["recoverable"].stdout)
    assert "permissionDecision" not in json.dumps(allowed)
    assert "command allowed" in json.dumps(allowed)
