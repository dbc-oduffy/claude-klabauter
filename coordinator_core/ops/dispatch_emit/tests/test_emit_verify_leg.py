"""The judge arm's scoped test run and criterion judge are one `verify` agent on the judge's type."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core.bash_guards.guard_heavy_command_admission import _VERIFIER_TYPES
from coordinator_core.ops.dispatch_emit import leg_register
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.review_mint.wave_bookkeeping import resolve_prep_sidecar

from .conftest import REVIEW_KW
from .test_emit_wake_digest import (
    _STAGE_SCHEMAS_WITH_JUDGE,
    _V5_FRAGMENT,
    _row,
)

_TESTABLE = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
_PROSE = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
_JUDGE_TYPE = "coordinator:exit-criterion-judge"
_FALSIFIER = {"how": "run it", "baseline_output": "fails", "expected_when_true": "passes"}


def _judge_fragment(agent_type: str = _JUDGE_TYPE) -> dict:
    stages = list(_V5_FRAGMENT["execute_review"]["stages"]) + [
        {"kind": "judge", "agents": [{"agentType": agent_type, "model": "sonnet", "effort": "low", "schema": "judge"}]}
    ]
    return {**_V5_FRAGMENT, "execute_review": {"stages": stages}}


def _compose(waves=_TESTABLE, *, fragment=None, **kwargs) -> str:
    return compose_script(
        waves, name="wf", description="d", plan_path="docs/plans/p.md",
        review_roster_fragment=fragment or _judge_fragment(), review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
        **kwargs,
    )


def _agent_labels(script: str) -> list[str]:
    return re.findall(r"label: '([^']*)'", script)


def test_the_judge_arm_composes_one_verify_agent_and_neither_absorbed_leg():
    script = _compose()
    labels = _agent_labels(script)

    assert labels.count("verify") == 1
    assert not [l for l in labels if l == "test:terminal" or l.endswith("exit-criterion-judge")]
    assert "phases: [" in script and "'Verify'" in script
    assert "'Scoped test run'" not in script and "'Criterion judge'" not in script
    assert "phase('Verify')" in script


def test_the_verify_call_is_hosted_on_the_roster_judges_type_and_options():
    script = _compose()
    opts = script[script.index("label: 'verify'") :].split(", schema:", 1)[0]

    assert f"agentType: '{_JUDGE_TYPE}'" in opts
    assert "model: 'sonnet'" in opts and "effort: 'low'" in opts


def test_the_verify_type_is_admitted_by_the_heavy_command_guard():
    assert _JUDGE_TYPE in _VERIFIER_TYPES


def _tools_of(definition: str) -> list[str]:
    front = definition.split("---", 2)[1]
    match = re.search(r"^tools:\s*\[(.*)\]\s*$", front, re.MULTILINE)
    assert match, "agent definition carries no tools list"
    return [t.strip().strip("\"'") for t in match.group(1).split(",") if t.strip()]


def test_the_tools_reader_reports_a_definition_holding_edit(tmp_path):
    held = _tools_of('---\nname: j\ntools: ["Read", "Edit"]\n---\n')
    assert "Edit" in held


def test_the_roster_judges_type_holds_no_edit_or_write():
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    definition = Path(root or "") / "agents" / f"{_JUDGE_TYPE.split(':', 1)[1]}.md"
    if not root or not definition.is_file():
        pytest.skip("the coordinator plugin's judge definition is not installed on this box")
    tools = _tools_of(definition.read_text(encoding="utf-8"))
    assert "Bash" in tools
    assert not {"Edit", "Write", "MultiEdit", "NotebookEdit"} & set(tools)


def test_the_prompt_carries_the_scoped_targets_then_the_judge_brief():
    script = _compose()
    prompt = script[script.index("Verify in two steps") :].split(", { label: 'verify'", 1)[0]

    targets = prompt.index("Run the scoped test targets: [coordinator_core/ops/dispatch_emit/tests/test_wave_map.py]")
    step_two = prompt.index("Step 2 -- criterion verdict")
    brief = prompt.index("Judge whether this run met its plan")
    assert prompt.index("Step 1 -- scoped tests") < targets < step_two < brief
    assert "never the verdict itself" in prompt
    assert "return sidecar_path -- required" not in prompt


def test_the_return_nests_test_and_judge_and_binds_both_result_variables():
    script = _compose()

    assert '"required": ["judge"]' in script
    assert "_testResult = _verifyReply.test;" in script
    assert "_falsifierResult = _verifyReply.judge;" in script
    assert "test: r.test ?? null" in script
    assert "return { test: null, judge: { status: 'indeterminate'" in script


def test_the_test_sidecar_is_absent_on_this_arm_and_the_digest_and_bookkeeping_tolerate_it(tmp_path):
    script = _compose()
    schema = script[script.index("label: 'verify'") :]
    test_schema = schema[schema.index('"test": {') :]
    assert '"required": ["status", "tests_run", "tests_failed"]' in test_schema.split("}}, \"required\": [\"judge\"]")[0]
    assert "sidecar_path ?? null" in script
    assert resolve_prep_sidecar(None, repo_root=tmp_path, session_id="s", tests_sidecar=None) is None
    assert resolve_prep_sidecar("prep.md", repo_root=tmp_path, session_id="s", tests_sidecar=None) is None


def test_the_falsifier_rides_the_judge_brief_inside_the_verify_prompt():
    script = _compose(falsifier=_FALSIFIER)

    assert _agent_labels(script).count("verify") == 1
    prompt = script[script.index("Verify in two steps") :].split(", { label: 'verify'", 1)[0]
    assert "recorded falsifier: how=" in prompt and "run it" in prompt
    assert "test:terminal-falsifier" not in script


def test_a_roster_with_no_judge_keeps_the_separate_test_and_falsifier_legs():
    script = compose_script(
        _TESTABLE, name="wf", description="d", plan_path="docs/plans/p.md", falsifier=_FALSIFIER, **REVIEW_KW,
    )
    labels = _agent_labels(script)

    assert "verify" not in labels
    assert "test:terminal" in labels and "test:terminal-falsifier" in labels
    assert "phase('Scoped test run')" in script


def test_a_roster_with_no_judge_and_no_falsifier_keeps_the_lone_test_leg():
    script = compose_script(_TESTABLE, name="wf", description="d", plan_path="docs/plans/p.md", **REVIEW_KW)
    labels = _agent_labels(script)

    assert "verify" not in labels and "test:terminal" in labels


def test_a_judge_arm_with_no_test_target_runs_the_judge_alone():
    script = _compose(_PROSE)
    labels = _agent_labels(script)

    assert "verify" not in labels and "test:terminal" not in labels
    assert [l for l in labels if l.endswith("exit-criterion-judge")] == ["review:coordinator:exit-criterion-judge"]
    assert "'Criterion judge'" in script


def test_a_review_only_run_composes_no_verify_leg():
    script = compose_script(
        _TESTABLE, name="wf", description="d", plan_path="docs/plans/p.md", run_base_sha="a" * 40,
        review_only=True, review_terminal_sha="b" * 40, **REVIEW_KW,
    )

    assert "verify" not in _agent_labels(script)


def test_the_register_holds_verify_and_still_holds_the_arms_that_keep_the_absorbed_legs():
    for label in ("verify", "test:terminal", "review:coordinator:exit-criterion-judge"):
        assert leg_register.lookup("emit", label) is not None, label

