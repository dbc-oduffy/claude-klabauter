"""{sizing, context} submissions: the hook forwards context, and each brief refusal names the accepted shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_brief import EmBrief, EmBriefRefused, load_em_brief


def test_hook_forwards_context_beside_a_sizing(tmp_path):
    (tmp_path / ".git").mkdir()
    seen: list[dict] = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    payload = {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {
            "name": mod.SUBMIT_WORKFLOW_NAME,
            "args": {"sizing": "state/sizings/x.yaml", "context": "docs/ctx.md"},
        },
    }
    env = mod.evaluate(payload, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert seen[0]["sizing_path"] == "state/sizings/x.yaml"
    assert seen[0]["ask_context"] == ["docs/ctx.md"]


def test_outside_context_refusal_names_the_accepted_shapes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    peer = tmp_path / "peer.md"
    peer.write_text("x", encoding="utf-8")
    with pytest.raises(EmBriefRefused, match=r"outside the repo root; accepted: a file inside the repo or in this session's scratchpad"):
        load_em_brief(repo, brief=None, brief_file=None, context=[str(peer)], session_id="s1")


def test_outside_brief_file_refusal_names_the_accepted_shape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    peer = tmp_path / "peer.md"
    peer.write_text("x", encoding="utf-8")
    with pytest.raises(EmBriefRefused, match=r"outside the repo root; accepted: a file inside the repo$"):
        load_em_brief(repo, brief=None, brief_file=str(peer), context=[])


def test_hook_surfaces_the_engine_refusal_text_in_the_deny_reason(tmp_path):
    (tmp_path / ".git").mkdir()

    def emit(params: dict, root: Path) -> dict:
        raise EmBriefRefused("--context /x: outside the repo root; accepted: a file inside the repo or in this session's scratchpad")

    payload = {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": "state/sizings/x.yaml", "context": "/x"}},
    }
    env = mod.evaluate(payload, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "accepted: a file inside the repo or in this session's scratchpad" in env["hookSpecificOutput"]["permissionDecisionReason"]


def test_in_repo_context_alone_is_admitted(tmp_path):
    (tmp_path / "ctx.md").write_text("x", encoding="utf-8")
    b = load_em_brief(tmp_path, brief=None, brief_file=None, context=["ctx.md"])
    assert b == EmBrief(text="", source="inline", context=("ctx.md",))


def test_roadmap_arm_refusal_names_the_workflow_args(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_ROADMAP)
    monkeypatch.setattr(ask_compose, "parse_execute_review", lambda *_: None)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "")
    monkeypatch.setattr(ask_compose, "_xl_exit_open", lambda *_: False)
    with pytest.raises(ask_compose.AskComposeRefused) as exc:
        ask_compose.compose_ask_script(
            repo_root=".",
            prompt=None,
            sizing_rel="state/sizings/x.yaml",
            run_id="r1",
            session_id="s1",
            review_roster_fragment={},
            review_stage_schemas={},
            wrap_stage=lambda text, **kw: (f"async function {ask_compose._PLAN_BLITZ_FN}(", []),
            em_brief=EmBrief(text="", source="inline", context=("ctx.md",)),
        )
    assert "args.context" in str(exc.value)
    assert "args.brief" in str(exc.value)
