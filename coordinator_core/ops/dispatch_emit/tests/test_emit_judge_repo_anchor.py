"""The criterion judge's prompt carries the repo-root anchor row prompts carry."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _BRIEF_PRECEDENCE_CLAUSE, compose_script, derive_plan_context
from coordinator_core.ops.dispatch_emit.tests.test_emit_judge_engine_rows import _PLAN_CWD, _fragment
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS


def _script(tmp_path, repo_root):
    plan = tmp_path / "plan.md"
    plan.write_text(_PLAN_CWD, encoding="utf-8")
    row = WaveRow(
        id="T2", title="t", surface="x", writes=["lib/widget.py"], reads=[], depends_on=[],
        agent_type=None, agent_model=None,
    )
    return compose_script(
        [[row]], name="wf", description="d", review_roster_fragment=_fragment(),
        review_stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path=str(plan), plan_text=_PLAN_CWD, repo_root=repo_root,
        plan_context=derive_plan_context(
            _PLAN_CWD, fallback_title="p", repo_root=None if repo_root is None else repo_root.as_posix()
        ),
    )


def _judge_call(script):
    at = script.index("coordinator:criterion-judge")
    return script[script.rfind("agent(", 0, at):at]


def test_judge_prompt_anchors_to_the_runtime_root(tmp_path):
    call = _judge_call(_script(tmp_path, tmp_path))
    assert "Repo root: ' + _repoRoot + '" in call
    assert call.index("Repo root: ") > call.index(_BRIEF_PRECEDENCE_CLAUSE[:40])


def test_judge_prompt_without_a_repo_root_declares_no_anchor(tmp_path):
    assert "Repo root: " not in _judge_call(_script(tmp_path, None))
