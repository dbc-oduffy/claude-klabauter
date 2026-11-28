import os
import uuid

import psutil
import pytest

from coordinator_core.ops.workflow_fire import headless_em_grant as grant
from coordinator_core.ops.workflow_fire import host_submit
from coordinator_core.session import external_registry


@pytest.fixture
def allocation(tmp_path, monkeypatch):
    settings = tmp_path / "host"
    settings.mkdir(mode=0o700)
    monkeypatch.setenv(grant.SETTINGS_HOME_KEY, str(settings))
    sid = str(uuid.uuid4())
    env = {"COORDINATOR_SESSION_ID": sid, "CODEX_THREAD_ID": str(uuid.uuid4()),
           grant.HEADLESS_EM_TOKEN_ENV: grant.write_headless_em_grant(sid, "codex")}
    work = tmp_path / "workspace"
    work.mkdir()
    pid = os.getpid()
    birth = int(psutil.Process(pid).create_time())
    assert grant.observe_codex_registration(env, env["CODEX_THREAD_ID"], pid, str(work))
    row = dict(session_id=env["CODEX_THREAD_ID"], provider="codex", role="em", pid=pid,
               start_epoch=birth, cwd=str(work))
    monkeypatch.setattr(external_registry, "lookup", lambda native_id: row)
    params = dict(ask="Aggregate duplicate Elm values", ask_source="relayed", caller="codex-em")
    def receiver():
        return host_submit.HostSubmit(authorized_params=params, workspace=work,
                                      receipt_directory=settings, launcher_session_id=sid)
    return receiver, params, dict(caller_env=env, native_pid=pid, native_birth=birth)


def test_replay_and_refusals(allocation, monkeypatch):
    receiver, params, caller = allocation
    calls = []
    result = {"run_id": "r1", "fire": {"fire_id": "f1"}}
    monkeypatch.setattr(host_submit, "_engine_ask_submit", lambda p, w: calls.append(p) or result)
    for change in (dict(ask="different"), dict(ask_source="pm"), dict(backend="claude"), dict(cwd="/")):
        assert receiver().receive({**params, **change}, **caller)["refused"] == "issued-request-mismatch"
    for change in (dict(native_pid=0), dict(native_birth=0), dict(caller_env={})):
        assert receiver().receive(params, **{**caller, **change})["refused"] == "launch-identity-mismatch"
    assert not calls
    assert receiver().receive(params, **caller) == result
    assert receiver().receive(params, **caller) == result
    assert len(calls) == 1
    assert "session_id" not in calls[0]


def test_uncertain_receiver_outcome_is_not_reissued(allocation, monkeypatch):
    receiver, params, caller = allocation
    def interrupted(*args):
        raise RuntimeError("lost reply after dispatch")
    monkeypatch.setattr(host_submit, "_engine_ask_submit", interrupted)
    with pytest.raises(RuntimeError, match="lost reply"):
        receiver().receive(params, **caller)
    assert receiver().receive(params, **caller) == {"refused": "reconcile-existing-submit"}


def test_ordinary_receiver_supplies_launcher_identity_and_codex_route(allocation, monkeypatch):
    from coordinator_core.hooks import _engine_ask_gates
    from coordinator_core.ops.workflow_fire import op

    receiver, params, caller = allocation
    minted = []
    fired = []
    def mint(**kwargs):
        from coordinator_core.ops.workflow_fire.authority import require_submission_em
        assert require_submission_em(kwargs["session_id"], kwargs["cwd"]) is None
        minted.append(kwargs)
        return {"run_id": "issued", "script_path": "script", "sha256": "fixture"}
    def fire(kwargs, workspace):
        fired.append(kwargs)
        return {"refused": "fixture-fire-refusal", "reason": "fixture"}
    monkeypatch.setattr(_engine_ask_gates, "submit_ask", mint)
    monkeypatch.setattr(op, "_workflow_fire", fire)
    result = receiver().receive(params, **caller)
    assert minted[0]["session_id"] == caller["caller_env"]["COORDINATOR_SESSION_ID"]
    assert fired[0]["caller_session_id"] == minted[0]["session_id"]
    assert (fired[0]["backend"], fired[0]["executor"]) == ("codex", "headless")
    assert result["launch_observation"]["native_session_id"] == caller["caller_env"]["CODEX_THREAD_ID"]
    assert result["fire_error"] == "fixture-fire-refusal: fixture"
    assert receiver().receive(params, **caller) == result
    assert len(minted) == len(fired) == 1
