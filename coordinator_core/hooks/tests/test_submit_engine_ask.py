"""The EM's submission of an ask is the running Workflow: `hooks.submit_engine_ask` turns a
`Workflow({name: "coordinator:engine-ask", args})` call into allow + a rewrite to the emitted
script, and the main loop's `Agent(sizing-scout)` is denied naming that call."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import run_token


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _payload(root: Path, args, **extra) -> dict:
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }
    payload.update(extra)
    return payload


class _Emit:
    """Stands in for `dispatch.emit`: records params, writes a script, mints the run token."""

    def __init__(self, fire_args=None, raises=None):
        self.calls: list = []
        self.fire_args = fire_args if fire_args is not None else {"repoRoot": "/r"}
        self.raises = raises

    def __call__(self, params: dict, root: Path) -> dict:
        self.calls.append((params, root))
        if self.raises:
            raise self.raises
        run_id = "ask-20261013T000000-abcdef"
        script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("export const meta = {}\n", encoding="utf-8")
        run_token.mint(
            root, run_id, str(script), session_id=params.get("session_id"),
            sizing_rel=params.get("sizing_path"),
        )
        return {"ok": True, "path": str(script), "run_id": run_id, "fire_args": self.fire_args}


def _hso(env: dict) -> dict:
    return env.get("hookSpecificOutput") or {}


@pytest.fixture(autouse=True)
def _local_venue(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)


def test_submit_call_is_what_every_guard_names():
    call = warp_fire.fire_command()
    assert call.startswith('Workflow({name: "coordinator:engine-ask", args: {"ask": ')
    assert 'args: {"sizing": "state/sizings/x.yaml"}' in warp_fire.fire_command("state/sizings/x.yaml")
    assert warp_fire.fire_command() in warp_fire.fire_step()
    assert warp_fire.is_submit_name("coordinator:engine-ask")
    assert warp_fire.is_submit_name("engine-ask")
    assert not warp_fire.is_submit_name("coordinator:plan-blitz")


def test_other_workflow_calls_pass_untouched(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    for tool_input in ({"scriptPath": str(root / "x.mjs")}, {"name": "coordinator:plan-blitz"}, {"script": "x"}):
        payload = _payload(root, None)
        payload["tool_input"] = tool_input
        assert mod.evaluate(payload, emit=emit) == {}
    assert emit.calls == []


def test_raw_ask_becomes_the_fired_run(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    ask = "Make X do Y.\n  Keep  the spacing — verbatim."
    out = mod.evaluate(_payload(root, {"ask": ask}), emit=emit)

    hso = _hso(out)
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"] == {
        "scriptPath": str(root / "scratch" / "warp" / "ask-20261013T000000-abcdef.workflow.mjs"),
        "args": {"repoRoot": "/r"},
    }
    assert "one wake" in hso["additionalContext"]
    params, called_root = emit.calls[0]
    assert params == {"session_id": "sess-1", "ask": ask}
    assert called_root == root
    record = json.loads((root / "scratch" / "warp" / "ask-20261013T000000-abcdef" / "submission.json").read_text())
    assert record["session_id"] == "sess-1" and record["sizing_rel"] is None


def test_bare_string_args_is_the_ask(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, "the ask"), emit=emit)
    assert emit.calls[0][0]["ask"] == "the ask"


def test_sizing_submission_routes_the_sizing(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit(fire_args={})
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=emit)
    assert emit.calls[0][0] == {"session_id": "sess-1", "ask": True, "sizing_path": "state/sizings/a.yaml"}
    assert "args" not in _hso(out)["updatedInput"]


def test_remote_venue_carries_the_cross_repo_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a"}), emit=emit)
    assert emit.calls[0][0]["cross_repo_approved"] is True


def test_local_cross_repo_approval_rides_in_args(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a"}), emit=emit)
    assert "cross_repo_approved" not in emit.calls[0][0]
    mod.evaluate(_payload(root, {"ask": "a", "cross_repo_approved": True}), emit=emit)
    assert emit.calls[1][0]["cross_repo_approved"] is True


def test_repo_arg_selects_the_repo_from_a_non_repo_cwd(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a", "repo": str(root)}, cwd=str(outside)), emit=emit)
    assert emit.calls[0][1] == root


@pytest.mark.parametrize(
    "args, cwd_inside, needle",
    [
        ({}, True, "neither an ask nor a sizing"),
        ({"ask": "a", "sizing": "s.yaml"}, True, "neither an ask nor a sizing, or both"),
        ({"ask": "   "}, True, "neither an ask nor a sizing"),
        ({"ask": "a"}, False, "in no git repo"),
    ],
)
def test_malformed_submissions_deny_with_the_shape(tmp_path, args, cwd_inside, needle):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    emit = _Emit()
    out = mod.evaluate(_payload(root, args, cwd=str(root if cwd_inside else outside)), emit=emit)
    hso = _hso(out)
    assert hso["permissionDecision"] == "deny"
    assert needle in hso["permissionDecisionReason"]
    assert "coordinator:engine-ask" in hso["permissionDecisionReason"]
    assert emit.calls == []


def test_engine_refusal_is_the_deny_reason(tmp_path):
    root = _repo(tmp_path)
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit(raises=ValueError("sizing not accepted")))
    hso = _hso(out)
    assert hso["permissionDecision"] == "deny"
    assert "sizing not accepted" in hso["permissionDecisionReason"]


def test_a_subagent_cannot_submit(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "a"}, agent_id="abc"), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny"
    assert emit.calls == []


def test_second_submission_of_a_live_sizing_is_denied(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    first = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=emit)
    assert _hso(first)["permissionDecision"] == "allow"
    second = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=emit)
    hso = _hso(second)
    assert hso["permissionDecision"] == "deny"
    assert "ask-20261013T000000-abcdef is already working state/sizings/a.yaml" in hso["permissionDecisionReason"]
    assert len(emit.calls) == 1


def test_sizing_scout_from_the_main_loop_is_denied_naming_the_submission():
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "coordinator:sizing-scout", "prompt": "size"}}
    out = mod.sizing_scout_deny(payload)
    assert _hso(out)["permissionDecision"] == "deny"
    assert warp_fire.fire_command() in _hso(out)["permissionDecisionReason"]
    assert mod.sizing_scout_deny({**payload, "agent_id": "a1"}) is None
    other = {"tool_name": "Agent", "tool_input": {"subagent_type": "coordinator:executor"}}
    assert mod.sizing_scout_deny(other) is None


def test_agent_leg_carries_the_sizing_scout_deny():
    from coordinator_core.hooks import block_unenumerated_agent_type

    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "coordinator:sizing-scout", "prompt": "x"}}
    out = block_unenumerated_agent_type.check(payload)
    assert _hso(out)["permissionDecision"] == "deny"
    assert "coordinator:engine-ask" in _hso(out)["permissionDecisionReason"]


def test_op_door_answers_the_wrapped_payload(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    emit = _Emit()
    monkeypatch.setattr(mod, "_default_emit", emit)
    out = mod._handler({"payload": _payload(root, {"ask": "a"})})
    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls


# --- warp_live: liveness ends with the harness's terminal notification --------------------


def _launch(tmpdir: Path, session: str, task: str, script: str) -> None:
    (tmpdir / f"workflow-run-{session}-{task}.json").write_text(
        json.dumps({"run_id": "wf_1", "scriptPath": script, "fired_at": int(time.time())}), encoding="utf-8"
    )


def test_live_until_the_task_notification_lands(tmp_path):
    root = _repo(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    emit = _Emit()
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=emit)
    script = str(root / "scratch" / "warp" / "ask-20261013T000000-abcdef.workflow.mjs")
    _launch(scratch, "sess-1", "w1", script)
    transcript = root / "transcript.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    def live():
        return warp_live.live_run_for_sizing(root, "state/sizings/a.yaml", tmpdir=str(scratch))

    assert live() == "ask-20261013T000000-abcdef"
    transcript.write_text(
        transcript.read_text()
        + "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n",
        encoding="utf-8",
    )
    assert live() is None


def test_an_unlaunched_emit_stops_counting_after_the_grace(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit())
    assert warp_live.live_run_for_sizing(root, "state/sizings/a.yaml", tmpdir=str(scratch))
    later = time.time() + warp_live.UNLAUNCHED_GRACE_SECONDS + 5
    monkeypatch.setattr(warp_live.time, "time", lambda: later)
    assert warp_live.live_run_for_sizing(root, "state/sizings/a.yaml", tmpdir=str(scratch)) is None


def test_a_raw_ask_run_owns_a_sizing_written_after_it_in_its_own_session(tmp_path):
    root = _repo(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    sizing = root / "state" / "sizings" / "new.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("status: sized\n", encoding="utf-8")
    rel = "state/sizings/new.yaml"
    assert warp_live.live_run_for_sizing(root, rel, session_id="sess-1", tmpdir=str(scratch))
    assert warp_live.live_run_for_sizing(root, rel, session_id="other", tmpdir=str(scratch)) is None
    assert warp_live.live_run_for_sizing(root, rel, tmpdir=str(scratch)) is None


def test_a_consumed_token_is_not_live(tmp_path):
    root = _repo(tmp_path)
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit())
    run_token.consume(root, "ask-20261013T000000-abcdef")
    assert warp_live.live_run_for_sizing(root, "state/sizings/a.yaml", tmpdir=str(tmp_path)) is None
