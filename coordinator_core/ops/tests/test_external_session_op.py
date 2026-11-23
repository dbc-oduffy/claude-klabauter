"""external_session.handle: dispatch per event, caller-supplied env and pid, notices on registry failure."""
from __future__ import annotations

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import external_session as op

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    log: list = []
    from coordinator_core.session import external_registry
    from coordinator_core.host_hooks import codex_lifecycle, session_context
    monkeypatch.setattr(external_registry, "unregister", lambda sid, **kw: log.append(("unregister", sid, kw)))
    monkeypatch.setattr(external_registry, "register_current", lambda payload, **kw: log.append(("register", payload, kw)))
    monkeypatch.setattr(session_context, "context", lambda payload, env, **kw: log.append(("context", payload, env, kw)) or {"ctx": 1})
    monkeypatch.setattr(codex_lifecycle, "handle", lambda payload, env, **kw: log.append(("handle", payload, env)) or {"h": 1})
    monkeypatch.setattr(op, "_observed_launch", lambda pid: {"native_main": True})
    return log


def _call(event: str, payload: dict | None = None, env: dict | None = None, pid: int = 4242) -> dict:
    return op.handle({"event": event, "payload": {"session_id": SID, "cwd": "/w", **(payload or {})},
                      "env": env if env is not None else {"K": "v", "N": 3}, "caller_pid": pid})


def test_session_end_unregisters_and_renders_nothing(calls):
    assert _call("SessionEnd") == {"output": {}, "notices": []}
    assert [c[0] for c in calls] == ["unregister"] and calls[0][1] == SID


def test_top_level_session_start_registers_with_caller_pid_and_env_then_context(calls):
    assert _call("SessionStart") == {"output": {"ctx": 1}, "notices": []}
    assert [c[0] for c in calls] == ["register", "context"]
    assert calls[0][2] == {"host_pid": 4242, "env": {"K": "v"}}
    assert calls[1][2] == {"K": "v"} and calls[1][3] == {"observed": {"native_main": True}}


def test_subagent_session_start_and_subagent_start_never_register(calls):
    _call("SessionStart", {"agent_id": "a"})
    _call("SubagentStart")
    assert [c[0] for c in calls] == ["context", "context"]


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact"])
def test_other_events_go_to_the_lifecycle_handler_with_caller_env(calls, event):
    assert _call(event) == {"output": {"h": 1}, "notices": []}
    assert calls[0][0] == "handle" and calls[0][1]["hook_event_name"] == event and calls[0][2] == {"K": "v"}


def test_registry_failure_is_a_notice_and_context_still_returns(calls, monkeypatch):
    from coordinator_core.session import external_registry
    monkeypatch.setattr(external_registry, "register_current", lambda *a, **k: (_ for _ in ()).throw(ValueError("no observed Codex ancestor")))
    out = _call("SessionStart")
    assert out["output"] == {"ctx": 1}
    assert out["notices"] == ["external session registration unavailable: ValueError: no observed Codex ancestor"]


@pytest.mark.parametrize("params", [
    {"event": "Nope", "payload": {}, "env": {}, "caller_pid": 1},
    {"event": "Stop", "payload": [], "env": {}, "caller_pid": 1},
    {"event": "Stop", "payload": {}, "env": {}, "caller_pid": 0},
    {"event": "Stop", "payload": {}, "env": {}, "caller_pid": True},
    {"event": "Stop", "payload": {}, "env": {}, "caller_pid": 1, "extra": 1},
])
def test_bad_params_are_refused(calls, params):
    with pytest.raises(CallerFacingValidationError):
        op.handle(params)
    assert calls == []


def test_op_is_registered_in_every_surface():
    import json
    from pathlib import Path
    import coordinator_core.ops as ops  # noqa: F401 -- eager registration
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP
    name = "external_session.handle"
    assert name in _REGISTRY and OP_MODULE_MAP[name] == "coordinator_core.ops.external_session"
    assert OP_CLASSIFICATION[name] is OpClass.MUTATING and _OP_KEY_SCOPE[name] == "none"
    catalog = json.loads((Path(op.__file__).parents[1] / "contract" / "op-catalog.json").read_text(encoding="utf-8"))
    assert name in {entry["name"] for entry in catalog["ops"]}


def test_real_register_current_resolves_host_from_caller_pid_and_env(tmp_path, monkeypatch):
    import os
    import psutil
    from coordinator_core.session import external_registry as reg

    class _Host:
        pid = 777

        def name(self):
            return "codex.exe"

        def create_time(self):
            return 1700000000.0

        def cmdline(self):
            return ["codex"]

    seen = {}

    class _Proc:
        def __init__(self, pid):
            seen["pid"] = pid

        def parents(self):
            return [_Host()]

    monkeypatch.setattr(psutil, "Process", _Proc)
    monkeypatch.delenv("COORDINATOR_PARENT_SESSION_ID", raising=False)
    parent = "99999999-2222-3333-4444-555555555555"
    work = tmp_path / "w"
    work.mkdir()
    record = reg.register_current(
        {"session_id": SID, "cwd": str(work)}, tmp_path / "reg",
        host_pid=4242, env={"COORDINATOR_PARENT_SESSION_ID": parent})
    assert seen["pid"] == 4242 and record["pid"] == 777
    assert record["parent_session_id"] == parent
    assert os.getpid() != 4242
