"""Every ask gate leg, launcher child included, is typed with a shell-pinned, model-pinned agent type."""

from __future__ import annotations

import re

import pytest

from coordinator_core.codex_runner.workflow import _INVOKE_ONLY_AGENT_TYPES
from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, emit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_launcher_child_resume import _script
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import _BLITZ_FN

pytestmark = pytest.mark.complete

_GATE_CALL = re.compile(r"label: 'gate', phase: '[^']*', agentType: '([^']*)', model: '([^']*)'")


@pytest.mark.parametrize("launcher_child", [True, False])
@pytest.mark.parametrize("raw", [True, False])
def test_every_gate_leg_emits_the_pinned_gate_type(monkeypatch: pytest.MonkeyPatch, launcher_child, raw) -> None:
    script = _script(monkeypatch, launcher_child=launcher_child, raw=raw)
    assert script.count("label: 'gate'") >= 1
    calls = _GATE_CALL.findall(script)
    assert len(calls) == script.count("label: 'gate'")
    for agent_type, model in calls:
        assert agent_type == ask_compose._GATE_AGENT_TYPE
        assert model == emit._AGENT_MODELS[ask_compose._GATE_AGENT_TYPE]


def test_the_gate_type_is_the_model_pinned_op_runner_never_test_runner_or_unpinned() -> None:
    gate = ask_compose._GATE_AGENT_TYPE
    assert gate == "coordinator:queue-grind-op-runner"
    assert gate != emit._TEST_AGENT_TYPE
    assert gate != "general-purpose"
    assert gate != emit._TEST_AGENT_TYPE
    assert gate in emit._AGENT_MODELS
    assert gate not in _INVOKE_ONLY_AGENT_TYPES
    assert ask_compose._MECHANICAL_MODEL == emit._AGENT_MODELS[gate]


@pytest.mark.parametrize("arm", ["xs", "s", "m_plus"])
def test_the_emitted_gate_prompt_carries_exactly_one_invoke(monkeypatch: pytest.MonkeyPatch, arm) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = ask_compose_script_presized()
    ends = [m.start() for m in re.finditer(re.escape("label: 'gate'"), script)]
    assert ends
    for end in ends:
        prompt = script[script.rindex("agent(", 0, end) : end]
        assert prompt.count(ask_compose._INVOKE) == 1
        assert ask_compose.OP_ASK_GATE in prompt
        assert ask_compose.OP_ASK_STAGE not in prompt and "dispatch.emit" not in prompt
    declared = {f.name for f in ask_gate._PARAMS}
    assert {"stage_act", "gated", "chain_emit", "target_root", "stop_after"} <= declared
    assert "stage_act: true, gated: _gated" in script


def test_the_rulings_alias_stays_off_the_gate_type() -> None:
    assert ask_compose._RULINGS_AGENT_TYPE == ask_compose._MECHANICAL_AGENT_TYPE != ask_compose._GATE_AGENT_TYPE


def ask_compose_script_presized() -> str:
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", **REVIEW_KW,
    )


def _closing(repo, params: dict, out: dict) -> dict:
    (repo / ".git").mkdir(exist_ok=True)
    _put(repo, tshirt="XS", route="dispatch")
    return ask_gate._close_gate_leg({"sizing_path": REL, **params}, out, repo / ".git")


def test_the_gate_reply_carries_the_sizings_tshirt_and_route_read_in_process(repo) -> None:
    out = _closing(repo, {}, {"arm": "xs", "halt": None})
    assert out["tshirt"] == "XS" and out["route"] == "dispatch"
    assert "stage_reply" not in out and "chain_emit" not in out


def test_the_stage_act_stages_an_xs_arm_in_process_and_only_then(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.dispatch_emit import ask_stage

    calls: list = []
    monkeypatch.setattr(ask_stage, "_handler", lambda p, r=None: calls.append(p) or {"manifest_handle": {"path": "m"}})
    act = {"stage_act": True, "cross_repo_approved": True, "session_id": "s1", "gated": [{"id": "G"}]}
    out = _closing(repo, {"run_id": "r1", "writes": ["a.py"], **act}, {"arm": "xs", "halt": None})
    assert out["stage_reply"] == {"manifest_handle": {"path": "m"}}
    assert calls == [{
        "cross_repo_approved": True, "session_id": "s1", "run_id": "r1", "writes": ["a.py"],
        "sizing_path": REL, "gated": [{"id": "G"}],
    }]
    calls.clear()
    out = _closing(repo, {"run_id": "r1", **act}, {"arm": "xs", "halt": None, "resume_plan": "p.md", "resume_rows": ["A2"]})
    assert calls == [{
        "cross_repo_approved": True, "session_id": "s1", "run_id": "r1", "plan_path": "p.md",
        "commit_sizing_path": REL, "only_rows": ["A2"],
    }]
    calls.clear()
    for refused in (
        {"arm": "xs", "halt": {"kind": "refusal", "reason": "x"}},
        {"arm": "s", "halt": None},
        {"arm": "m_plus", "halt": None},
        {"arm": "s", "halt": None, "resume_plan": "p.md", "manifest_handle": {"path": "h"}},
    ):
        out = _closing(repo, {"run_id": "r1", **act}, refused)
        assert not calls and out.get("stage_reply") in (None, {"manifest_handle": {"path": "h"}})
    out = _closing(repo, {"run_id": "r1", **act}, {"arm": "s", "halt": None, "manifest": {"error": "boom"}})
    assert out["stage_reply"] == {"error": "boom"} and not calls
    out = _closing(repo, {"run_id": "r1", "cross_repo_approved": True}, {"arm": "xs", "halt": None})
    assert not calls and "stage_reply" not in out


def test_the_chain_emit_runs_in_process_on_an_m_plus_or_roadmap_arm_with_a_null_halt(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.dispatch_emit import op

    calls: list = []
    monkeypatch.setattr(op, "_dispatch_emit", lambda p, r=None: calls.append(p) or {"path": "x.mjs", "run_id": "c1", "extra": 1})
    chain = {"chain_emit": True, "target_root": "/r", "run_id": "r1", "session_id": "s1", "stop_after": "plan"}
    for arm in ("m_plus", "roadmap"):
        out = _closing(repo, chain, {"arm": arm, "halt": None})
        assert out["chain_emit"] == {"path": "x.mjs", "run_id": "c1"}
    assert [(c["ask"], c["sizing_path"], c["target_root"], c["chain_parent"], c["session_id"], c["stop_after"])
            for c in calls] == [(True, REL, "/r", "r1", "s1", "plan")] * 2
    assert all(LAUNCHER_CHILD_EMIT_PARAM in c and "cross_repo_approved" not in c for c in calls)
    calls.clear()
    for refused in ({"arm": "xs", "halt": None}, {"arm": "s", "halt": None}, {"arm": "m_plus", "halt": {"kind": "room"}}):
        assert "chain_emit" not in _closing(repo, chain, refused)
    assert "chain_emit" not in _closing(repo, {}, {"arm": "m_plus", "halt": None})
    assert not calls
    monkeypatch.setattr(op, "_dispatch_emit", lambda p, r=None: {"error": "bad"})
    assert _closing(repo, chain, {"arm": "m_plus", "halt": None})["chain_emit"] == {"error": "bad"}

    def boom(p, r=None):
        raise RuntimeError("down")

    monkeypatch.setattr(op, "_dispatch_emit", boom)
    assert _closing(repo, chain, {"arm": "m_plus", "halt": None})["chain_emit"] == {"error": "RuntimeError: down"}


@pytest.mark.parametrize("launcher_child", [True, False])
def test_a_merge_shaped_ask_routes_no_gate_leg_to_the_test_runner(
    monkeypatch: pytest.MonkeyPatch, launcher_child
) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    # The gate type is one module constant, not a per-repo route: the composition takes no repo-dependent type input.
    script = compose_ask_script(
        repo_root="REPO", prompt="merge branch mini into machine-a: git consolidation, no code rows",
        sizing_rel=None, run_id="run-1", session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="scratch/warp/x.mjs", in_run_commit=True,
        launcher_child=launcher_child, **REVIEW_KW,
    )
    calls = _GATE_CALL.findall(script)
    assert calls and len(calls) == script.count("label: 'gate'")
    for agent_type, model in calls:
        assert agent_type != emit._TEST_AGENT_TYPE
        assert agent_type == ask_compose._GATE_AGENT_TYPE
        assert agent_type not in _INVOKE_ONLY_AGENT_TYPES
        assert model == emit._AGENT_MODELS[agent_type]
