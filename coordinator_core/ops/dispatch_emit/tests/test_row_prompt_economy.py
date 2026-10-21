"""The executor row prompt pins its spec inline and the composed script carries the checkpoint
continuation and the absorb tail.

Plan: docs/plans/2026-10-21-2026-10-21-executor-context-waste-and-wave-revise.md (C9).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import (
    _WORKFLOW_SCRIPT_BYTE_CAP,
    _row_prompt,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.executor_economy import (
    BATCHING_CLAUSE,
    EXCERPT_HEADING,
    EXCERPT_MAX_BYTES,
    MAX_CONTINUATIONS,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.dispatch_emit.work_label import parse_work_label

def _stage(kind: str, agent_type: str, model: str, schema: str, **extra) -> dict:
    return {"kind": kind, "agents": [{"agentType": agent_type, "model": model, "effort": "low", "schema": schema, **extra}]}


REVIEW_KW = {
    "review_roster_fragment": {
        "schema": "review-roster-fragment",
        "schema_version": 5,
        "execute_review": {
            "stages": [
                _stage("prep", "coordinator:review-prep", "sonnet", "prep"),
                _stage("review-wave", "coordinator:code-reviewer", "opus", "wave", per="whole-diff"),
                _stage("integration", "coordinator:integrator", "opus", "integration"),
            ]
        },
    },
    "review_stage_schemas": {k: {"type": "object"} for k in ("prep", "wave", "integration")},
}

_PLAN = "docs/plans/p.md"
_GOLDEN = Path(__file__).resolve().parents[3] / "benchmarks" / "fixtures" / "emit_full_path_plan.expected.script.mjs"


def _row(id_: str = "C1", body: str = "Change `alpha` only.") -> WaveRow:
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="m",
        writes=[f"pkg/{id_.lower()}.py"],
        reads=[],
        depends_on=[],
        body=body,
    )


def _script(**kw) -> str:
    return compose_script([[_row()]], name="wf", description="d", plan_path=_PLAN, **REVIEW_KW, **kw)


def test_prompt_pins_the_row_instead_of_sending_the_executor_to_read_the_plan():
    prompt = _row_prompt(_row(body="Change `alpha` only."), _PLAN)
    assert "Read all of it" not in prompt
    assert EXCERPT_HEADING in prompt
    assert "Change `alpha` only." in prompt
    assert "## Problem" in prompt and "## Anti-scope" in prompt
    assert BATCHING_CLAUSE in prompt
    assert _PLAN in prompt and "id: C1" in prompt


def test_prompt_names_its_checkpoint_path_inside_the_row_sidecar_dir():
    prompt = _row_prompt(_row(), _PLAN)
    assert "CHECKPOINT: " in prompt
    assert "C1.checkpoint-1.md" in prompt
    assert "/C1/C1.checkpoint-1.md" in prompt.replace("\\", "/")


def test_composed_excerpts_cap_each_row_block(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "c1.py").write_text("def alpha():\n    pass\n\nclass Beta:\n    pass\n", encoding="utf-8")
    long_body = "word " * 1000
    script = compose_script(
        [[_row(body=long_body)]],
        name="wf",
        description="d",
        plan_path=_PLAN,
        repo_root=tmp_path,
        run_base_sha="a" * 40,
        **{k: v for k, v in REVIEW_KW.items() if k != "repo_root"},
    )
    block = script.split(EXCERPT_HEADING, 1)[1].split("\\n\\nBatch independent", 1)[0]
    assert len(EXCERPT_HEADING + block) <= EXCERPT_MAX_BYTES + 40
    assert "(truncated)" in block


def test_composed_excerpts_name_symbol_spans(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "c1.py").write_text("def alpha():\n    pass\n\nclass Beta:\n    pass\n", encoding="utf-8")
    script = compose_script(
        [[_row()]],
        name="wf",
        description="d",
        plan_path=_PLAN,
        repo_root=tmp_path,
        run_base_sha="b" * 40,
        **{k: v for k, v in REVIEW_KW.items() if k != "repo_root"},
    )
    assert "pkg/c1.py: alpha L1-3, Beta L4-5" in script
    assert "compose HEAD " + "b" * 12 + ";" in script


def test_checkpoint_branch_and_cap_are_in_the_row_helper():
    script = _script()
    assert "const _CHECKPOINT_RE" in script
    assert f"_ckpts >= {MAX_CONTINUATIONS}" in script
    assert "_contOf[id] = { n: _ckpts, path: _ck[1] };" in script
    assert "'PARTIAL: executor checkpointed '" in script


def test_a_continuable_row_labels_its_continuation_and_appends_the_clause():
    script = _script()
    assert "_contOf['C1'] = null;" in script
    assert "label: 'work:C1' + (_contOf['C1'] ? '#cont' + _contOf['C1'].n : '')" in script
    assert "Continue from " in script
    assert "_contText(_contOf['C1'], '" in script and "/C1/C1.checkpoint-')" in script
    assert "prefix + (c.n + 1) + '.md and reply `CHECKPOINT: <that path>`.'" in script


def test_continuation_label_parses_back_to_the_row_id():
    assert parse_work_label("work:C1#cont2") == "C1"


def test_no_absorb_tail_without_a_run_id():
    script = _script()
    assert "absorb:check-" not in script
    assert "_absorbPrompt" not in script


def test_absorb_tail_is_present_once_after_the_dag_settles_and_before_the_terminal_commit():
    script = _script(absorb_run_id="run-1")
    assert script.count("label: 'absorb:check-'") == 1
    tail_at = script.index("absorb:check-")
    assert tail_at > script.index("await Promise.all(Object.values(_rows));")
    assert tail_at > script.index("await Promise.all(_verifications);")
    assert tail_at > script.index("await _commitChain;")
    assert tail_at < script.index("\n// coordinator:terminal-commit-request v1 ")
    assert script.index("function _absorbPrompt(id, title)") < tail_at
    assert "if (!_halted) {" in script[script.index("function _absorbPrompt") - 40 : script.index("function _absorbPrompt")]


def test_empty_pending_revisions_dispatches_only_the_check_agent():
    script = _script(absorb_run_id="run-1")
    tail = script[script.index("absorb:check-") :]
    stop = tail.index("if (!Array.isArray(_absorbPending.rows) || !_absorbPending.rows.length) break;")
    assert stop < tail.index("absorb:plan-")
    assert tail.index("absorb:plan-") < tail.index("'work:' + a.id")


def test_absorbed_prompt_says_it_has_no_excerpts_and_keeps_the_never_edit_the_plan_sentence():
    script = _script(absorb_run_id="run-1")
    fn = script[script.index("function _absorbPrompt") :].split("\n", 1)[0]
    assert "No excerpts" in fn
    assert "never edit the plan" in fn
    assert "ABSORBROWID" not in fn and "ABSORBROWTITLE" not in fn
    assert "id + " in fn or "+ id" in fn


def test_emit_script_composes_no_tail_by_default():
    assert inspect.signature(emit.emit_script).parameters["absorb_run_id"].default is None
    assert inspect.signature(compose_script).parameters["absorb_run_id"].default is None


def test_benchmark_script_stays_inside_the_byte_cap():
    assert _GOLDEN.stat().st_size < _WORKFLOW_SCRIPT_BYTE_CAP
