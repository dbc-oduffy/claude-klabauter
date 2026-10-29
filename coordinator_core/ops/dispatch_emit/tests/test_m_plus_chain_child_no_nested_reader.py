"""The M+ chained child is a launcher child: its script holds no workflow() stage reader, and the chain leg resumes its staged twin."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.nested_run_contract import (
    LAUNCHER_CHILD_EMIT_PARAM,
    STAGE_PENDING_KEY,
    staged_twin_rel,
)
from coordinator_core.ops.dispatch_emit.reply_sidecar import READ_STAGE_SIDECAR_JS
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_READER_CALL = "workflow({ scriptPath: reader })"


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
