"""The launcher M+ chain never hands a dispatch.emit reply's keys (path, run_id, ok) to a second emit."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY, staged_twin_rel
from coordinator_core.ops.dispatch_emit.tests.test_launcher_child_m_plus_chains_incident import (
    _SIZING,
    _drive,
    _script,
)

_PARAMS = {"ask": True, "sizing_path": _SIZING, "session_id": "sess-1", "target_root": "/r"}


def _handoff(reply: dict) -> dict:
    return {CHAIN_HANDOFF_KEY: {**_PARAMS, **reply}, "sizing": _SIZING, "run_id": "ask-r1"}


def _reply(tmp_path, run_id="ask-r2") -> dict:
    return {"path": f"{tmp_path.as_posix()}/scratch/warp/{run_id}/c.mjs", "run_id": run_id, "ok": True}


@pytest.mark.spawns_process
def test_a_gate_emitted_reply_starts_its_run_without_a_second_emit(tmp_path):
    reply = _reply(tmp_path)
    out = _drive(_script(tmp_path), [_handoff(reply), {"route": "dispatch", "done": True}], {"error": "must not be called"})
    assert out["r"]["status"] == "ok", out["r"]
    assert [e["label"] for e in out["emits"]] == []
    assert out["calls"][1]["scriptPath"] == reply["path"]


@pytest.mark.spawns_process
def test_a_gate_emitted_reply_resumes_its_own_twin_on_stage_pending(tmp_path):
    sha = "cd" * 32
    pending = {"stage_pending": {"handle": {"sha256": sha}}}
    out = _drive(_script(tmp_path), [_handoff(_reply(tmp_path)), pending, {"done": True}], {})
    assert out["r"]["status"] == "ok"
    assert out["calls"][2]["scriptPath"] == f"{tmp_path.as_posix()}/{staged_twin_rel('ask-r2', sha)}"


@pytest.mark.spawns_process
def test_a_reply_without_a_run_still_emits_once_with_no_reply_keys(tmp_path):
    chain = {"path": f"{tmp_path.as_posix()}/scratch/warp/ask-r2/c.mjs", "run_id": "ask-r2", "fire_args": {}}
    out = _drive(_script(tmp_path), [_handoff({"ok": True, "path": ""}), {"done": True}], chain)
    assert out["r"]["status"] == "ok", out["r"]
    assert [e["label"] for e in out["emits"]] == ["chain-emit-ask-r1"]
    prompt = out["emits"][0]["prompt"]
    for key in ('"path"', '"run_id"', '"ok"'):
        assert key not in prompt
    assert '"sizing_path"' in prompt and '"launcher_child":true' in prompt


@pytest.mark.spawns_process
def test_a_gate_error_reply_fails_without_emitting(tmp_path):
    out = _drive(_script(tmp_path), [_handoff({"error": "SizingFireRefused: gone"})], {})
    assert out["r"]["status"] == "failed"
    assert "SizingFireRefused: gone" in out["r"]["error"]
    assert out["emits"] == [] and len(out["calls"]) == 1


@pytest.mark.spawns_process
def test_a_gate_reply_naming_the_childs_own_run_is_refused(tmp_path):
    out = _drive(_script(tmp_path), [_handoff(_reply(tmp_path, "ask-r1"))], {})
    assert out["r"]["status"] == "failed"
    assert "not a fresh run id" in out["r"]["error"]
    assert len(out["calls"]) == 1
