"""Plan-branch ask_stage params carry no sizing-time `writes`: a reviewer read it as a stage contract."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Add a flag to the exporter and cover it with a test",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_no_plan_branch_stage_params_carry_writes():
    script = _compose()
    plan_params = re.findall(r"JSON\.stringify\(\{ run_id: _runId, plan_path:[^}]*", script)
    ready_ops = re.findall(r"readyOp: \{ op: [^{]*\{[^}]*", script)
    assert plan_params and ready_ops
    for params in [*plan_params, *ready_ops]:
        assert "writes:" not in params, params
