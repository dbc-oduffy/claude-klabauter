"""The M+ chained child is a launcher child: its script holds no workflow() stage reader, and the chain leg resumes its staged twin."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring import env  # noqa: F401
from coordinator_core.ops.dispatch_emit.nested_run_contract import (
    LAUNCHER_CHILD_EMIT_PARAM,
    STAGE_PENDING_KEY,
    staged_twin_rel,
)
from coordinator_core.ops.dispatch_emit.reply_sidecar import READ_STAGE_SIDECAR_JS
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_READER_CALL = "workflow({ scriptPath: reader })"
_REAL_COMPOSE = ask_compose.compose_ask_script


def _compose(**kw) -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text="stub", **REVIEW_KW, **kw,
    )


def test_chain_params_carry_launcher_child_true():
    assert f"{LAUNCHER_CHILD_EMIT_PARAM}: true" in ask_compose._CHAIN_EMIT_PARAMS_JS


def test_chained_child_script_holds_no_workflow_reader():
    assert _READER_CALL in READ_STAGE_SIDECAR_JS
    child = _compose(launcher_child=True, in_run_commit=True)
    assert _READER_CALL not in child
    assert "workflow(" not in child


def test_every_child_render_variant_is_reader_free_and_the_top_level_keeps_its_reader():
    for sizing_rel, prompt in ((None, "add a thing"), ("state/sizings/s.yaml", None)):
        for in_run_commit in (False, True):
            for accept_pending in (False, True):
                kw = dict(launcher_child=True, in_run_commit=in_run_commit, accept_pending=accept_pending)
                child = ask_compose.compose_ask_script(
                    repo_root="REPO", prompt=prompt, sizing_rel=sizing_rel, run_id="run-1", session_id=None,
                    wrap_stage=lambda text: ("", []), plan_blitz_text="stub", **REVIEW_KW, **kw,
                )
                assert "workflow(" not in child, kw
    assert _READER_CALL in _compose()


def test_default_emit_renders_a_child_script_reader_free_and_a_standalone_one_with_its_reader(env):  # noqa: F811
    for params, reader in (({"launcher_child": True}, False), ({}, True)):
        op._dispatch_emit({"ask": "add a widget", **params}, repo_root=env.root)
        kw = env.calls["compose"]
        assert kw["launcher_child"] is bool(params)
        rendered = _REAL_COMPOSE(**kw)
        assert (_READER_CALL in rendered) is reader
        if not reader:
            assert "workflow(" not in rendered


def test_child_script_sources_pass_launcher_child_to_the_emit(tmp_path):
    from coordinator_core.ops.dispatch_emit import fan_launcher as fl

    child = fl.LaunchChild("ask-r1", "state/sizings/s.yaml", {"ask": True, "sizing_path": "state/sizings/s.yaml"})
    launcher = fl.write_launcher(tmp_path, "ask-parent", [child], "sess-1", [{"path": "scratch/warp/ask-r1.workflow.mjs", "run_id": "ask-r1", "fire_args": {}}]).read_text(encoding="utf-8")
    assert f"{LAUNCHER_CHILD_EMIT_PARAM}: true" in launcher
    assert f"{LAUNCHER_CHILD_EMIT_PARAM}: true" in ask_compose._CHAIN_EMIT_PARAMS_JS


def test_chain_leg_starts_the_staged_twin_on_stage_pending():
    parent = _compose()
    chain = ask_compose._chain_stage_loop_js()
    assert chain in parent
    assert "workflow({ scriptPath: _chainPath })" in parent
    assert f"_chained.{STAGE_PENDING_KEY}" in chain
    prefix, rest = staged_twin_rel("\x00r", "\x00s").split("\x00r")
    middle, suffix = rest.split("\x00s")
    for part in (prefix, middle, suffix):
        assert part in chain
    assert chain.count("await workflow(") == 1
    assert "_chainRunId" in chain
