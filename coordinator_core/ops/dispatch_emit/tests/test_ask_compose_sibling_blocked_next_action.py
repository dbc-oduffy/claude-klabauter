"""The landed-commit branch emits refire_after_sibling when the close carries blocked_rows, else close_job."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _landed_branch(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    tail = script[script.index("_commit = await") :]
    return tail[: tail.index("else { if (!_halted)")]


def test_blocked_rows_select_refire_after_sibling_with_non_null_op(monkeypatch):
    branch = _landed_branch(monkeypatch)
    assert "_commit.close?.siblings?.blocked_rows ?? []" in branch
    assert "if (_blockedRows.length > 0)" in branch
    sibling = branch[branch.index("kind: 'refire_after_sibling'") :].split("else {", 1)[0]
    assert f"op: {ask_compose._lit(SUBMIT_WORKFLOW_NAME)}" in sibling
    assert "op: null" not in sibling
    assert "refire: _blockedRows[0].refire" in sibling
    assert "blocked_on: _blockedRows.map(" in sibling
    assert "sizing: _sizingRel" in sibling
    assert "fire: _commit.close?.siblings?.released ?? []" in sibling


def test_blocked_rows_branch_precedes_unchanged_close_job_record(monkeypatch):
    branch = _landed_branch(monkeypatch)
    assert branch.index("refire_after_sibling") < branch.index("_nextAction = { kind: 'close_job', op: null,")
    close_job = branch[branch.index("_nextAction = { kind: 'close_job', op: null,") :]
    assert "params: { run_id: _runId, close: _commit.close ?? null, fire: _commit.close?.siblings?.released ?? [] } }" in close_job
