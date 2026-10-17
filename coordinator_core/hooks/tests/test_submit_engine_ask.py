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
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
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
        self.script = str(script)
        self.token = run_token.mint(
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


def test_a_refire_echoes_the_revisions_it_carries(tmp_path):
    root = _repo(tmp_path)

    def emit(params, root_):
        out = _Emit()(params, root_)
        out["revisions_inherited"] = [
            {"id": "R4", "first_line": "cover Windows"},
            {"id": "R5", "first_line": "keep it XS"},
        ]
        return out

    ctx = _hso(mod.evaluate(_payload(root, {"ask": "a"}), emit=emit))["additionalContext"]
    assert "Carries revisions: R4 (cover Windows); R5 (keep it XS)." in ctx

    plain = _hso(mod.evaluate(_payload(root, {"ask": "b"}, tool_use_id="t2"), emit=_Emit()))["additionalContext"]
    assert "Carries revisions" not in plain


def test_a_research_sizing_takes_the_research_route(tmp_path):
    root = _repo(tmp_path)
    sizing = root / "state" / "sizings" / "x.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("research:\n  question: q\n", encoding="utf-8")
    (root / "ctx.md").write_text("c\n", encoding="utf-8")
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/x.yaml", "context": ["ctx.md"]}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    params, _ = emit.calls[0]
    assert params["from_sizing"] == "state/sizings/x.yaml" and params["context"]
    assert "ask" not in params and "sizing_path" not in params

    refused = mod.evaluate(
        _payload(root, {"sizing": "state/sizings/x.yaml", "brief": "b"}, tool_use_id="t2"), emit=_Emit()
    )
    assert _hso(refused)["permissionDecision"] == "deny"
    assert "research sizing takes no brief" in _hso(refused)["permissionDecisionReason"]


def test_an_empty_research_block_or_unreadable_sizing_keeps_the_plan_chain(tmp_path):
    root = _repo(tmp_path)
    sizing = root / "state" / "sizings" / "y.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("research: {}\n", encoding="utf-8")
    for rel in ("state/sizings/y.yaml", "state/sizings/missing.yaml"):
        emit = _Emit()
        mod.evaluate(_payload(root, {"sizing": rel}, tool_use_id=rel), emit=emit)
        params, _ = emit.calls[0]
        assert params["sizing_path"] == rel and "from_sizing" not in params


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


def test_sizing_submission_carries_its_writes(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit(fire_args={})
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml", "writes": ["tests/t.py"]}), emit=emit)
    assert emit.calls[0][0]["writes"] == ["tests/t.py"]


def _spawn(tmp_path: Path, name: str, agent_id: str) -> Path:
    """A workflow spawn's transcript under <session>/subagents/workflows/<name>."""
    path = tmp_path / "sess-1" / "subagents" / "workflows" / name / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _launch_record(tmp_path: Path, monkeypatch, script: str, wf_dir: Path, task: str) -> None:
    launches = tmp_path / "launches"
    launches.mkdir(exist_ok=True)
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: launches)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda _name="": launches)
    (launches / f"workflow-run-sess-1-{task}.json").write_text(
        json.dumps({"scriptPath": script, "transcript_dir": str(wf_dir), "fired_at": 1.0}), encoding="utf-8"
    )


def _agent_write(dispatch, root: Path, tmp_path: Path, agent_id: str, path: str = "pkg/mod.py"):
    return dispatch.op({"tool_name": "Edit", "agent_id": agent_id, "session_id": "sess-1", "cwd": str(root),
                        "transcript_path": str(tmp_path / "sess-1.jsonl"),
                        "tool_input": {"file_path": str(root / path), "old_string": "a", "new_string": "b"}})


def test_size_stage_subagent_cannot_write_product_code(tmp_path, monkeypatch):
    from coordinator_core.hooks import nudge_em_code_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_is_scratch_or_tmp", lambda _p: False)  # tmp_path is under the temp dir
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "fix detect()"}), emit=emit)
    spawn = _spawn(tmp_path, "wf_run", "a1")
    _launch_record(tmp_path, monkeypatch, emit.script, spawn.parent, "t1")

    def write(path):
        return _agent_write(dispatch, root, tmp_path, "a1", path)

    assert write("pkg/mod.py")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert write("DELIVERY.md") is None
    run_token.bind_sizing(root, "ask-20261013T000000-abcdef", "state/sizings/a.yaml")
    assert write("pkg/mod.py") is None


def test_a_second_live_run_is_never_denied_by_the_size_stage_guard(tmp_path, monkeypatch):
    from coordinator_core.hooks import nudge_em_code_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_is_scratch_or_tmp", lambda _p: False)
    root = _repo(tmp_path)
    run_a, run_b = "ask-20261013T000000-aaaaaa", "ask-20261013T000001-bbbbbb"
    agent_a, agent_b = "a1", "b1"
    for run_id, agent_id, wf in ((run_a, agent_a, "wf_a"), (run_b, agent_b, "wf_b")):
        script = root / "scratch" / f"{run_id}.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("export const meta = {}\n", encoding="utf-8")
        run_token.mint(root, run_id, str(script), session_id="sess-1")
        spawn = _spawn(tmp_path, wf, agent_id)
        _launch_record(tmp_path, monkeypatch, str(script), spawn.parent, run_id[-6:])

    def write(agent_id):
        return _agent_write(dispatch, root, tmp_path, agent_id)

    run_token.bind_sizing(root, run_b, "state/sizings/b.yaml")
    assert write(agent_b) is None
    assert write(agent_a)["hookSpecificOutput"]["permissionDecision"] == "deny"


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
        ({}, True, "Submit exactly one of"),
        ({"ask": "a", "sizing": "s.yaml"}, True, "Submit exactly one of"),
        ({"ask": "a", "asks": [{"ask": "b"}]}, True, "Submit exactly one of"),
        ({"asks": [{"ask": "a"}, {"ask": "b"}], "brief": "x"}, True, "rides one run"),
        ({"asks": [{"ask": "a"}, {"ask": "b", "source": "peer"}]}, True, "asks[1].source must be one of"),
        ({"asks": []}, True, "non-empty list"),
        ({"asks": [{"ask": " "}]}, True, "carries no ask text"),
        ({"asks": [{"ask": "a", "writes": "x"}, {"ask": "b"}]}, True, "writes must be a list"),
        ({"ask": "   "}, True, "Submit exactly one of"),
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


def _foreign_refusal(root: Path) -> Exception:
    from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError

    target = root / "state" / "warp" / ("ask-20261016T200022-" + "x" * 150 + ".workflow.mjs")
    return ForeignEmissionError(
        f"{target} already holds a DIFFERENT emission (mtime 2026-10-16T20:00:22, 1234 bytes). "
        "Coordinate with that session, name a different output_path, or pass force=true deliberately."
    )


def test_foreign_emission_refusal_reaches_the_deny_whole(tmp_path):
    # Pins hook pass-through only (no hook-side cap exists); the length bound is test_op's.
    root = _repo(tmp_path)
    exc = _foreign_refusal(root)
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit(raises=exc))
    hso = _hso(out)
    assert hso["permissionDecision"] == "deny"
    assert str(exc) in hso["permissionDecisionReason"]
    assert hso["permissionDecisionReason"].count("pass force=true deliberately.") == 1


def test_the_op_door_returns_the_whole_refusal(tmp_path, monkeypatch):
    from coordinator_core import machine_profile

    root = _repo(tmp_path)
    exc = _foreign_refusal(root)
    monkeypatch.setattr(mod, "_default_emit", _Emit(raises=exc))
    monkeypatch.setattr(machine_profile, "apply_guard_level", lambda _name, env, **_kw: env)
    out = mod._handler({"payload": _payload(root, {"sizing": "state/sizings/a.yaml"})})
    assert str(exc) in _hso(out)["permissionDecisionReason"]


def test_the_transport_shaping_keeps_the_whole_refusal(tmp_path):
    from coordinator_core.warm import hook_http

    root = _repo(tmp_path)
    exc = _foreign_refusal(root)
    env = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit(raises=exc))
    body = hook_http._decision_to_response("PreToolUse", env)
    assert str(exc) in body["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_refusal_longer_than_any_cap_keeps_its_tail_through_every_door(tmp_path, monkeypatch):
    from coordinator_core import machine_profile
    from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError
    from coordinator_core.warm import hook_http

    tail = "TAIL-TOKEN-7f3a9c"
    root = _repo(tmp_path)
    exc = ForeignEmissionError("x" * 2500 + " " + tail)
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Emit(raises=exc))
    assert tail in _hso(out)["permissionDecisionReason"]
    assert tail in hook_http._decision_to_response("PreToolUse", out)["hookSpecificOutput"]["permissionDecisionReason"]
    monkeypatch.setattr(mod, "_default_emit", _Emit(raises=exc))
    monkeypatch.setattr(machine_profile, "apply_guard_level", lambda _name, env, **_kw: env)
    handled = mod._handler({"payload": _payload(root, {"sizing": "state/sizings/a.yaml"})})
    assert tail in _hso(handled)["permissionDecisionReason"]


def test_a_redirected_emit_leads_the_allow_context_with_the_redirect(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY

    root = _repo(tmp_path)

    class _Redirected(_Emit):
        def __call__(self, params, root):
            result = super().__call__(params, root)
            result[OUTPUT_PATH_REDIRECT_KEY] = {
                "from": "state/warp/ask-old.workflow.mjs",
                "to": "state/warp/ask-new.workflow.mjs",
                "halted_run": "ask-old",
                "prior_session": "sess-dead",
                "reason": "emitter-gone",
            }
            return result

    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=_Redirected())
    context = _hso(out)["additionalContext"]
    lead = "Halted run ask-old belonged to sess-dead (emitter-gone); emitted fresh at state/warp/ask-new.workflow.mjs. "
    assert context.index(lead) < context.index("Submitted to the engine")
    plain = mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    assert "Halted run" not in _hso(plain)["additionalContext"]


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


def _tokens(root: Path) -> list:
    return [r["run_id"] for r in warp_live._token_records(root)]


def _result_line(tool_use_id: str) -> str:
    return json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]}}
    ) + "\n"


def test_a_resubmit_after_a_refused_call_emits_anew_and_leaves_one_token(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    sizing = {"sizing": "state/sizings/a.yaml"}
    assert _hso(mod.evaluate(_payload(root, sizing, tool_use_id="tu1"), emit=emit))["permissionDecision"] == "allow"
    (root / "transcript.jsonl").write_text(_result_line("tu1"), encoding="utf-8")
    second = mod.evaluate(_payload(root, sizing, tool_use_id="tu2"), emit=emit)
    assert _hso(second)["permissionDecision"] == "allow"
    assert len(emit.calls) == 2
    assert _tokens(root) == ["ask-20261013T000000-abcdef"]


def test_a_resubmit_while_the_first_call_has_no_result_is_denied(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    sizing = {"sizing": "state/sizings/a.yaml"}
    mod.evaluate(_payload(root, sizing, tool_use_id="tu1"), emit=emit)
    (root / "transcript.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    second = mod.evaluate(_payload(root, sizing, tool_use_id="tu2"), emit=emit)
    assert _hso(second)["permissionDecision"] == "deny"
    assert len(emit.calls) == 1
    assert _tokens(root) == ["ask-20261013T000000-abcdef"]


def test_a_run_with_a_launch_record_is_never_unlaunched_even_after_a_result(tmp_path):
    root = _repo(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    emit = _Emit()
    sizing = {"sizing": "state/sizings/a.yaml"}
    mod.evaluate(_payload(root, sizing, tool_use_id="tu1"), emit=emit)
    script = str(root / "scratch" / "warp" / "ask-20261013T000000-abcdef.workflow.mjs")
    _launch(scratch, "sess-1", "w1", script)
    (root / "transcript.jsonl").write_text(_result_line("tu1"), encoding="utf-8")
    assert warp_live.unlaunched_run_for_sizing(root, "state/sizings/a.yaml", "sess-1", tmpdir=str(scratch)) is None


def _scout_payload(root: Path, subagent_type="coordinator:sizing-scout", **extra) -> dict:
    tool_input = {"prompt": "size"}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    payload = {
        "tool_name": "Agent",
        "tool_input": tool_input,
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }
    payload.update(extra)
    return payload


def test_sizing_scout_is_denied_with_no_live_run_naming_the_ask_submission(tmp_path):
    root = _repo(tmp_path)
    out = mod.sizing_scout_deny(_scout_payload(root))
    assert _hso(out)["permissionDecision"] == "deny"
    reason = _hso(out)["permissionDecisionReason"]
    assert warp_fire.SUBMIT_WORKFLOW_NAME in reason
    assert "ask:" in reason
    bare = _scout_payload(root)
    del bare["session_id"]
    assert _hso(mod.sizing_scout_deny(bare))["permissionDecision"] == "deny"


def test_sizing_scout_with_a_live_run_names_the_run(tmp_path):
    root = _repo(tmp_path)
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    out = mod.sizing_scout_deny(_scout_payload(root))
    assert _hso(out)["permissionDecision"] == "deny"
    assert "ask-20261013T000000-abcdef" in _hso(out)["permissionDecisionReason"]
    assert warp_fire.fire_command() not in _hso(out)["permissionDecisionReason"]
    padded = _scout_payload(root, " coordinator:sizing-scout ")
    assert _hso(mod.sizing_scout_deny(padded))["permissionDecision"] == "deny"
    other = mod.sizing_scout_deny(_scout_payload(root, session_id="other"))
    assert "ask-20261013T000000-abcdef" not in _hso(other)["permissionDecisionReason"]


def test_a_scout_payload_with_an_agent_id_is_not_denied_without_a_run(tmp_path):
    assert mod.sizing_scout_deny(_scout_payload(_repo(tmp_path), agent_id="a1")) is None


def test_a_scout_payload_with_an_agent_id_is_not_denied_with_a_run_live(tmp_path):
    root = _repo(tmp_path)
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    assert mod.sizing_scout_deny(_scout_payload(root, agent_id="a1")) is None


def test_a_non_scout_agent_payload_reads_no_launch_records(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())

    def _boom(*a, **k):
        raise AssertionError("launch-record dir listed")

    monkeypatch.setattr(warp_live, "_launch_dirs", _boom)
    assert mod.sizing_scout_deny(_scout_payload(root, "coordinator:executor")) is None
    assert mod.sizing_scout_deny(_scout_payload(root, None)) is None


def test_agent_leg_carries_the_sizing_scout_deny(tmp_path):
    from coordinator_core.hooks import block_unenumerated_agent_type

    root = _repo(tmp_path)
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    out = block_unenumerated_agent_type.check(_scout_payload(root))
    assert _hso(out)["permissionDecision"] == "deny"
    assert "ask-20261013T000000-abcdef" in _hso(out)["permissionDecisionReason"]


def test_engine_emitted_no_run_consumes_the_token(tmp_path):
    root = _repo(tmp_path)

    def emit(params, repo_root):
        run_token.mint(repo_root, "ask-x", str(repo_root / "s.mjs"), session_id=params["session_id"],
                       sizing_rel=None)
        return {"ok": False, "run_id": "ask-x", "findings": "nothing to emit"}

    out = mod.evaluate(_payload(root, {"ask": "a"}), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny"
    assert "The engine emitted no run" in _hso(out)["permissionDecisionReason"]
    assert warp_live.live_run_for_session(root, "sess-1", tmpdir=str(tmp_path)) is None
    record = run_token._record_path(root, "ask-x")
    assert record is None or not record.exists()


def test_non_repo_cwd_refusal_shows_the_repo_argument_shape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    out = mod.evaluate(_payload(_repo(tmp_path), {"ask": "a"}, cwd=str(outside)), emit=_Emit())
    assert 'repo: "<abs path>"' in _hso(out)["permissionDecisionReason"]


def test_a_repo_argument_that_is_not_a_repo_is_refused_naming_it(tmp_path):
    root = _repo(tmp_path)
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "a", "repo": str(bad)}), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny"
    assert str(bad) in _hso(out)["permissionDecisionReason"]
    assert emit.calls == []


def test_relayed_source_forwards_ask_source(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a", "source": "relayed"}), emit=emit)
    assert emit.calls[0][0]["ask_source"] == "relayed"
    mod.evaluate(_payload(root, {"ask": "a", "source": "pm"}), emit=emit)
    mod.evaluate(_payload(root, {"ask": "a"}), emit=emit)
    assert "ask_source" not in emit.calls[1][0]
    assert "ask_source" not in emit.calls[2][0]


def test_a_relayed_ask_is_admitted_with_no_pm_turn_check_of_its_own(tmp_path):
    emit = _Emit()
    out = mod.evaluate(_payload(_repo(tmp_path), {"ask": "a", "source": "relayed"}), emit=emit)
    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls[0][0]["ask_source"] == "relayed"


def test_an_engine_refusal_still_denies_with_its_own_text(tmp_path):
    out = mod.evaluate(
        _payload(_repo(tmp_path), {"ask": "a"}), emit=_Emit(raises=ValueError("boom")),
    )
    assert _hso(out)["permissionDecision"] == "deny"
    assert "boom" in _hso(out)["permissionDecisionReason"]


def test_a_relayed_sender_reaches_the_fanout_and_must_be_a_string(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "ready")])
    args = {"asks": [{"ask": "x", "sender": "coordinator-content-repo-dc"}, {"ask": "y"}], "source": "relayed"}
    out = mod.evaluate(_payload(root, args), emit=_Emit(), fanout=fan)
    assert _hso(out)["permissionDecision"] == "allow"
    assert fan.calls[0][0]["asks"][0]["sender"] == "coordinator-content-repo-dc"
    bad = {"asks": [{"ask": "x", "sender": 3}, {"ask": "y"}]}
    out = mod.evaluate(_payload(root, bad), emit=_Emit(), fanout=fan)
    assert "asks[0].sender must be a string" in _hso(out)["permissionDecisionReason"]


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"ask": "a", "source": "peer"}, "'peer'"),
        ({"sizing": "state/sizings/a.yaml", "source": "relayed"}, "sizing submission carries none"),
    ],
)
def test_a_bad_source_is_refused(tmp_path, args, needle):
    emit = _Emit()
    out = mod.evaluate(_payload(_repo(tmp_path), args), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny"
    assert needle in _hso(out)["permissionDecisionReason"]
    assert emit.calls == []


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


def test_a_raw_ask_run_owns_no_sizing_but_is_live_for_its_session(tmp_path):
    root = _repo(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    mod.evaluate(_payload(root, {"ask": "a"}), emit=_Emit())
    sizing = root / "state" / "sizings" / "new.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("status: sized\n", encoding="utf-8")
    rel = "state/sizings/new.yaml"
    assert warp_live.live_run_for_sizing(root, rel, session_id="sess-1", tmpdir=str(scratch)) is None
    assert warp_live.live_run_for_session(root, "sess-1", tmpdir=str(scratch))
    assert warp_live.live_run_for_session(root, "other", tmpdir=str(scratch)) is None


def test_a_redeemed_token_is_not_live(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml"}), emit=emit)
    assert run_token.redeem(root, "ask-20261013T000000-abcdef", emit.token, emit.script)
    assert warp_live.live_run_for_sizing(root, "state/sizings/a.yaml", tmpdir=str(tmp_path)) is None


def test_an_em_brief_beside_the_ask_reaches_the_emit_params(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    for name in ("c.md", "d.md"):
        (root / name).write_text("x\n", encoding="utf-8")
    args = {"ask": "a", "brief": "prefer X", "brief_file": "b.md", "context": "c.md"}
    mod.evaluate(_payload(root, args), emit=emit)
    params = emit.calls[0][0]
    assert (params["ask_brief"], params["ask_brief_file"], params["ask_context"]) == ("prefer X", "b.md", ["c.md"])
    mod.evaluate(_payload(root, {"sizing": "state/sizings/a.yaml", "context": ["c.md", "d.md"]}), emit=_Emit())


def test_prose_context_rides_the_brief_not_ask_context(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    note = "Sequence B after A.\nA lands first, then B."
    out = mod.evaluate(_payload(root, {"ask": "a", "context": note}), emit=emit)
    assert _hso(out)["permissionDecision"] == "allow"
    params = emit.calls[0][0]
    assert params["ask_brief"] == note
    assert "ask_context" not in params

    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a", "brief": "prefer X", "context": "do it in order"}), emit=emit)
    assert emit.calls[0][0]["ask_brief"] == "prefer X\n\ndo it in order"


def test_a_path_context_that_exists_reaches_ask_context_unchanged(tmp_path):
    root = _repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text("x\n", encoding="utf-8")
    absolute = tmp_path / "abs.md"
    absolute.write_text("x\n", encoding="utf-8")
    emit = _Emit()
    ctx = ["docs/note.md", str(absolute)]
    mod.evaluate(_payload(root, {"ask": "a", "context": ctx}), emit=emit)
    params = emit.calls[0][0]
    assert params["ask_context"] == ctx
    assert "ask_brief" not in params


@pytest.mark.parametrize("missing", ["docs/missing.md", "docs\\missing.md"])
def test_a_missing_path_shaped_context_is_refused_naming_it(tmp_path, missing):
    root = _repo(tmp_path)
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "a", "context": missing}), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny"
    assert missing in _hso(out)["permissionDecisionReason"]
    assert emit.calls == []


def test_a_mixed_context_list_splits_paths_from_prose(tmp_path):
    root = _repo(tmp_path)
    (root / "c.md").write_text("x\n", encoding="utf-8")
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a", "context": ["c.md", "run it v2.1 first"]}), emit=emit)
    params = emit.calls[0][0]
    assert params["ask_context"] == ["c.md"]
    assert params["ask_brief"] == "run it v2.1 first"


def test_a_submission_without_a_brief_adds_no_brief_params(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"ask": "a", "brief": "  ", "context": []}), emit=emit)
    assert not any(k.startswith("ask_") for k in emit.calls[0][0])


def test_a_malformed_brief_is_refused_not_dropped(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    for bad in ({"brief": 3}, {"context": [1]}):
        out = mod.evaluate(_payload(root, {"ask": "a", **bad}), emit=emit)
        assert _hso(out)["permissionDecision"] == "deny"
    assert emit.calls == []


def test_a_brief_the_engine_refuses_is_named_in_the_deny(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit(raises=ValueError("--brief-file nope.md: no such file"))
    out = mod.evaluate(_payload(root, {"ask": "a", "brief_file": "nope.md"}), emit=emit)
    assert "nope.md: no such file" in _hso(out)["permissionDecisionReason"]


def test_the_deny_shape_names_the_brief_field():
    assert "brief:" in mod._ARGS_SHAPE and "brief_file" in mod._ARGS_SHAPE
    assert "asks:" in mod._ARGS_SHAPE


PARENT = "ask-parent-20261015T000000-aaaaaa"


class _Fanout:
    """Stands in for `dispatch.ask_fanout`: writes the slice sizings and the parent record."""

    def __init__(self, runs, raises=None):
        self.runs = runs  # [(run_id, label, after, status)]
        self.raises = raises
        self.calls: list = []

    def __call__(self, params: dict, root: Path) -> dict:
        self.calls.append((params, root))
        if self.raises:
            raise self.raises
        records = []
        for run_id, label, after, status in self.runs:
            rel = f"state/sizings/{run_id}.yaml"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(
                f"em_analysis:\n  ask_parent: {PARENT} {run_id}\n", encoding="utf-8"
            )
            records.append(
                {"run_id": run_id, "label": label, "after": list(after), "slice_sizing": rel, "status": status}
            )
        parent = root / contract.parent_record_rel(PARENT)
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(
            json.dumps({"schema": "ask-parent", "version": 1, "parent_id": PARENT, "runs": records}),
            encoding="utf-8",
        )
        return {"parent_id": PARENT, "runs": [{"run_id": r[0], "label": r[1]} for r in self.runs]}


def _status(root: Path, run_id: str, status: str) -> None:
    path = root / contract.status_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "status": status}), encoding="utf-8")


def _asks(*names, **extra):
    return {"asks": [{"ask": f"ask {n}", "label": n} for n in names], **extra}


def test_asks_fire_the_first_ready_run_and_list_the_rest(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "ready"), ("r3", "c", ("r1",), "after")])
    emit = _Emit()
    out = mod.evaluate(_payload(root, _asks("a", "b", "c", confirm_split=True)), emit=emit, fanout=fan)
    hso = _hso(out)
    assert hso["permissionDecision"] == "allow"
    assert fan.calls[0][0]["confirm_split"] is True and len(fan.calls[0][0]["asks"]) == 3
    params = emit.calls[0][0]
    assert params["sizing_path"] == "state/sizings/r1.yaml" and params["reserved_run_id"] == "r1"
    assert "ask" in params and params["ask"] is True
    assert out["hookSpecificOutput"]["updatedInput"]["scriptPath"] == emit.script
    reason = json.dumps(out)
    assert PARENT in reason and "state/sizings/r2.yaml" in reason
    assert "state/sizings/r1.yaml" not in reason.split("Fire the listed")[-1]
    assert "state/sizings/r3.yaml" not in reason and "r3" in reason
    assert "Fire the listed runs now" in reason and "End the turn" not in reason


def test_a_queued_run_is_not_listed_as_fireable(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "queued")])
    out = mod.evaluate(_payload(root, _asks("a", "b")), emit=_Emit(), fanout=fan)
    assert "state/sizings/r2.yaml" not in json.dumps(out)


def test_a_one_element_asks_array_is_an_ask(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([])
    emit = _Emit()
    mod.evaluate(_payload(root, _asks("a")), emit=emit, fanout=fan)
    assert fan.calls == []
    assert emit.calls[0][0] == {"session_id": "sess-1", "ask": "ask a"}


def _sources(fan):
    return [a.get("source") for a in fan.calls[0][0]["asks"]]


def test_call_level_relayed_source_reaches_the_fanout_on_every_item(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "ready")])
    out = mod.evaluate(_payload(root, _asks("a", "b", source="relayed")), emit=_Emit(), fanout=fan)
    assert _hso(out)["permissionDecision"] == "allow"
    assert _sources(fan) == ["relayed", "relayed"]


def test_per_item_source_wins_over_the_call_level_default(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "ready")])
    args = {"asks": [{"ask": "a", "source": "relayed"}, {"ask": "b"}]}
    assert _hso(mod.evaluate(_payload(root, args), emit=_Emit(), fanout=fan))["permissionDecision"] == "allow"
    assert _sources(fan) == ["relayed", None]
    fan2 = _Fanout([("r1", "a", (), "ready"), ("r2", "b", (), "ready")])
    args = {"asks": [{"ask": "a", "source": "pm"}, {"ask": "b"}], "source": "relayed"}
    mod.evaluate(_payload(_repo(tmp_path / "two"), args), emit=_Emit(), fanout=fan2)
    assert _sources(fan2) == ["pm", "relayed"]


def test_a_one_item_relayed_asks_array_collapses_to_a_relayed_ask(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"asks": [{"ask": "a", "source": "relayed"}]}), emit=emit, fanout=_Fanout([]))
    assert emit.calls[0][0]["ask_source"] == "relayed"
    mod.evaluate(_payload(root, {"asks": [{"ask": "a"}], "source": "relayed"}), emit=emit, fanout=_Fanout([]))
    assert emit.calls[1][0]["ask_source"] == "relayed"


def test_a_fanout_refusal_is_the_deny_text_verbatim(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([], raises=ValueError("Partition refused; unclaimed rows: R2."))
    emit = _Emit()
    out = mod.evaluate(_payload(root, _asks("a", "b")), emit=emit, fanout=fan)
    assert _hso(out)["permissionDecision"] == "deny"
    assert _hso(out)["permissionDecisionReason"] == "[coordinator] Partition refused; unclaimed rows: R2."
    assert emit.calls == []


def test_a_returned_fanout_refusal_is_named_not_reported_as_no_ready_run(tmp_path):
    root = _repo(tmp_path)

    def refusing(params, root):
        return {"exit_code": 1, "applied": False, "error": "no recorded PM turn"}

    emit = _Emit()
    out = mod.evaluate(_payload(root, _asks("a", "b")), emit=emit, fanout=refusing)
    reason = _hso(out)["permissionDecisionReason"]
    assert _hso(out)["permissionDecision"] == "deny"
    assert "no recorded PM turn" in reason and "no ready run" not in reason
    assert emit.calls == []


def test_the_reported_three_ask_relayed_shape_fires_one_and_orders_the_overlap(tmp_path):
    root = _repo(tmp_path)
    args = {
        "source": "relayed",
        "asks": [
            {"ask": "revise from peer", "label": "revise-from-peer",
             "writes": ["requirements_ledger.py", "ask_compose.py"]},
            {"ask": "revise pinned plan warn", "label": "revise-pinned-plan-warn",
             "writes": ["requirements_ledger.py"]},
            {"ask": "venv creation guard", "label": "venv-creation-guard", "writes": []},
        ],
    }
    fan = _Fanout([
        ("r1", "revise-from-peer", (), "ready"),
        ("r2", "revise-pinned-plan-warn", ("r1",), "after"),
        ("r3", "venv-creation-guard", (), "ready"),
    ])
    emit = _Emit()
    out = mod.evaluate(_payload(root, args), emit=emit, fanout=fan)
    assert _hso(out)["permissionDecision"] == "allow"
    assert _sources(fan) == ["relayed"] * 3
    assert [a["writes"] for a in fan.calls[0][0]["asks"]][1] == ["requirements_ledger.py"]
    assert emit.calls[0][0]["reserved_run_id"] == "r1"
    reason = json.dumps(out)
    assert "state/sizings/r3.yaml" in reason and "state/sizings/r2.yaml" not in reason
    assert "Held runs (r2)" in reason


def test_the_reported_shape_partitions_into_two_firable_runs_and_one_ordered_after():
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    slices = [
        contract.AskSlice("revise-from-peer", "s1.yaml", (), ("requirements_ledger.py", "ask_compose.py"), None),
        contract.AskSlice("revise-pinned-plan-warn", "s2.yaml", (), ("requirements_ledger.py",), None),
        contract.AskSlice("venv-creation-guard", "s3.yaml", (), (), None),
    ]
    assert contract.after_edges(slices) == {0: (), 1: (0,), 2: ()}


def _fanned(tmp_path, runs):
    root = _repo(tmp_path)
    mod.evaluate(_payload(root, _asks(*[r[1] for r in runs])), emit=_Emit(), fanout=_Fanout(runs))
    return root


def _submit_slice(root, run_id, **extra):
    emit = _Emit()
    out = mod.evaluate(
        _payload(root, {"sizing": f"state/sizings/{run_id}.yaml"}, tool_use_id=f"t-{run_id}", **extra),
        emit=emit,
    )
    return out, emit


def test_a_slice_with_a_landed_predecessor_binds_its_reserved_id(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "landed")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls[0][0]["reserved_run_id"] == "r2"


def test_a_slice_is_denied_naming_an_unlanded_predecessor(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "running")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "deny"
    assert "r1" in _hso(out)["permissionDecisionReason"] and "not landed" in _hso(out)["permissionDecisionReason"]
    assert emit.calls == []


def test_a_slice_behind_a_halted_predecessor_is_told_to_resubmit_fresh(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "halted")
    out, _ = _submit_slice(root, "r2")
    reason = _hso(out)["permissionDecisionReason"]
    assert 'fresh {ask, supersedes: "r2"}' in reason and "r1" in reason


def _halt_record(root, run_id, plan_rel, plan_status):
    plan = root / plan_rel
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\nstatus: {plan_status}\n---\n# p\n", encoding="utf-8")
    record = root / "state" / "warp" / run_id / "halted.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"plan": plan_rel}), encoding="utf-8")


def test_a_halted_predecessor_whose_plan_is_implemented_releases_the_slice(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "halted")
    _halt_record(root, "r1", "docs/plans/p.md", "implemented")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls[0][0]["reserved_run_id"] == "r2"


def test_a_halted_predecessor_whose_plan_is_not_implemented_still_holds_the_slice(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "halted")
    _halt_record(root, "r1", "docs/plans/p.md", "reviewed")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "deny" and emit.calls == []


def test_a_superseded_slice_is_denied_naming_its_superseding_run(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "landed")
    marker = root / contract.superseded_rel("r2")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"superseded_by": "r9"}), encoding="utf-8")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "deny"
    assert "r2 is superseded by r9" in _hso(out)["permissionDecisionReason"] and emit.calls == []


def test_supersedes_on_a_held_slice_writes_the_marker_with_the_emitted_run_id(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r1", "halted")
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "redo b", "supersedes": "r2"}), emit=emit)
    assert _hso(out)["permissionDecision"] == "allow"
    assert contract.read_superseded_by(root, "r2") == "ask-20261013T000000-abcdef"
    assert "Marked r2 superseded by ask-20261013T000000-abcdef." in _hso(out)["additionalContext"]


def test_supersedes_on_a_sizing_submission_marks_too(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    out = mod.evaluate(_payload(root, {"sizing": "state/sizings/new.yaml", "supersedes": "r2"}), emit=_Emit())
    assert _hso(out)["permissionDecision"] == "allow"
    assert contract.read_superseded_by(root, "r2") == "ask-20261013T000000-abcdef"


@pytest.mark.parametrize("held_status", ["landed", "running"])
def test_supersedes_naming_a_landed_or_running_run_is_denied_before_emit(tmp_path, held_status):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    _status(root, "r2", held_status)
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "redo b", "supersedes": "r2"}), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny" and emit.calls == []
    assert contract.read_superseded_by(root, "r2") is None


def test_supersedes_over_an_existing_mark_is_denied_before_emit(tmp_path):
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", ("r1",), "after")])
    marker = root / contract.superseded_rel("r2")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"superseded_by": "r9"}), encoding="utf-8")
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"ask": "redo b", "supersedes": "r2"}), emit=emit)
    assert _hso(out)["permissionDecision"] == "deny" and emit.calls == []
    assert "r9" in _hso(out)["permissionDecisionReason"]


@pytest.mark.parametrize(
    "args",
    [
        {"asks": [{"ask": "a"}, {"ask": "b"}], "supersedes": "r2"},
        {"asks": [{"ask": "a"}], "supersedes": "r2"},
        {"ask": "x", "supersedes": 7},
        {"ask": "x", "supersedes": " "},
    ],
)
def test_supersedes_is_refused_on_asks_and_on_a_non_string(tmp_path, args):
    root = _repo(tmp_path)
    emit = _Emit()
    out = mod.evaluate(_payload(root, args), emit=emit, fanout=_Fanout([]))
    assert _hso(out)["permissionDecision"] == "deny" and emit.calls == []
    assert "supersedes" in _hso(out)["permissionDecisionReason"]


def test_a_queued_slice_is_denied_while_the_parent_is_at_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "concurrent_run_budget", lambda physical_cores=None: 1)
    root = _fanned(tmp_path, [("r1", "a", (), "ready"), ("r2", "b", (), "queued")])
    _status(root, "r1", "running")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "deny"
    assert "r1" in _hso(out)["permissionDecisionReason"] and emit.calls == []
    _status(root, "r1", "landed")
    out, emit = _submit_slice(root, "r2")
    assert _hso(out)["permissionDecision"] == "allow" and emit.calls[0][0]["reserved_run_id"] == "r2"


def test_a_sizing_without_a_parent_marker_binds_nothing(tmp_path):
    root = _repo(tmp_path)
    emit = _Emit()
    mod.evaluate(_payload(root, {"sizing": "state/sizings/plain.yaml"}), emit=emit)
    assert "reserved_run_id" not in emit.calls[0][0]


def _op_root(tmp_path):
    root = _repo(tmp_path)
    parent = root / contract.parent_record_rel(PARENT)
    parent.parent.mkdir(parents=True)
    parent.write_text('{"schema": "ask-parent", "version": 1, "parent_id": "p"}\n', encoding="utf-8")
    return root, parent


def test_reserved_id_bind_copies_the_parent_and_marks_running(tmp_path):
    from coordinator_core.ops.dispatch_emit import op

    root, parent = _op_root(tmp_path)
    doc = {"em_analysis": {"ask_parent": f"{PARENT} r1"}}
    assert op._reserved_run(root, "r1", doc, "state/sizings/r1.yaml") == ("r1", PARENT)
    op._bind_reserved_run(root, "r1", PARENT)
    copy = root / contract.run_parent_copy_rel("r1")
    assert copy.read_text(encoding="utf-8") == parent.read_text(encoding="utf-8")
    assert json.loads((root / contract.status_rel("r1")).read_text(encoding="utf-8"))["status"] == "running"


def test_a_second_bind_of_one_reserved_id_is_refused(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import op

    root, _ = _op_root(tmp_path)
    doc = {"em_analysis": {"ask_parent": f"{PARENT} r1"}}
    op._bind_reserved_run(root, "r1", PARENT)
    # A running status with no live run is settled halted and rebinds; only a live run refuses.
    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda *_a, **_k: "r1")
    with pytest.raises(ValueError, match="already working"):
        op._reserved_run(root, "r1", doc, "state/sizings/r1.yaml")
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(root, "r1", PARENT)


def test_a_reserved_id_the_sizing_does_not_reserve_is_refused(tmp_path):
    from coordinator_core.ops.dispatch_emit import op

    root, _ = _op_root(tmp_path)
    doc = {"em_analysis": {"ask_parent": f"{PARENT} r1"}}
    with pytest.raises(ValueError, match="not the run"):
        op._reserved_run(root, "r9", doc, "state/sizings/r1.yaml")
    with pytest.raises(ValueError, match="slice sizing_path"):
        op._reserved_run(root, "r1", None, None)



def test_a_one_item_relayed_ask_with_sender_keeps_the_fanout_arm_and_forwards_it(tmp_path):
    root = _repo(tmp_path)
    fan = _Fanout([("r1", "a", (), "ready")])
    args = {"asks": [{"ask": "a", "source": "relayed", "sender": "coordinator-content-repo-dc", "relayed_text": "the words"}]}
    out = mod.evaluate(_payload(root, args), emit=_Emit(), fanout=fan)
    assert _hso(out)["permissionDecision"] == "allow"
    item = fan.calls[0][0]["asks"][0]
    assert item["sender"] == "coordinator-content-repo-dc" and item["relayed_text"] == "the words"
