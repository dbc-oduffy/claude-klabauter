"""The judge arm's closing verify: a scoped test leg started beside review and a criterion judge on the
roster judge's type that runs after re-verify, each raced against the verify wall-time budget."""

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

pytestmark = pytest.mark.complete

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


def test_the_judge_arm_composes_a_test_leg_and_a_judge_and_neither_absorbed_leg():
    script = _compose()
    labels = _agent_labels(script)

    assert labels.count("verify") == 1
    assert "test:terminal" not in labels
    assert [l for l in labels if l.endswith("exit-criterion-judge")] == ["review:coordinator:exit-criterion-judge"]
    assert "phases: [" in script and "'Verify'" in script
    assert "'Scoped test run'" not in script and "'Criterion judge'" not in script
    assert "phase('Verify')" in script


def test_the_judge_call_is_hosted_on_the_roster_judges_type_and_options():
    script = _compose()
    opts = script[script.index("label: 'review:coordinator:exit-criterion-judge'") :].split(", schema:", 1)[0]

    assert f"agentType: '{_JUDGE_TYPE}'" in opts
    assert "model: 'sonnet'" in opts and "effort: 'low'" in opts
    assert "phase: 'Verify'" in opts


def test_the_test_leg_is_hosted_on_the_roster_judges_type_in_the_verify_phase():
    script = _compose()
    opts = script[script.index("label: 'verify'") :].split(", schema:", 1)[0]

    assert "phase: 'Verify'" in opts and f"agentType: '{_JUDGE_TYPE}'" in opts
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


def test_the_test_prompt_carries_the_scoped_targets_and_the_judge_prompt_the_brief():
    script = _compose()
    test_prompt = script[script.index("Run the scoped test targets:") :].split(", { label: 'verify'", 1)[0]
    judge_prompt = script[script.index("Judge whether this run met its plan") :].split(
        ", { label: 'review:coordinator:exit-criterion-judge'", 1
    )[0]

    assert test_prompt.startswith("Run the scoped test targets: [coordinator_core/ops/dispatch_emit/tests/test_wave_map.py]")
    assert "You have no Write tool: omit sidecar_path." in test_prompt
    assert "return sidecar_path -- required" not in test_prompt
    assert "VERIFY TEST LEG RESULT" in judge_prompt and "never the verdict itself" in judge_prompt


def test_the_test_leg_starts_at_launch_and_the_judge_reads_both_results_after_reverify():
    script = _compose()
    launch = script.index("let _verifyTestRun = null;")
    judge = script.index("phase('Verify')")

    assert script.index("phase('Review re-verify')") < judge
    assert launch < script.index("phase('Review prep')") < judge
    assert "if (!_halted) {\n    const _testCall = " in script
    assert "_testResult = await _verifyTestRun;" in script
    assert "JSON.stringify(_testResult)" in script and "JSON.stringify(_reviewReverify)" in script
    assert "_falsifierResult = await ((_t) => Promise.race([_judgeCall," in script


def test_the_test_binding_and_judge_stay_inside_the_halted_guard():
    script = _compose()
    guarded = script[script.index("  if (!_halted) {\n  phase('Verify');") :]

    assert "_testResult = await _verifyTestRun;" in guarded and "const _judgeCall" in guarded
    assert "_testResult = await _verifyTestRun;" not in script[: script.index("  if (!_halted) {\n  phase('Verify');")]


def test_the_test_sidecar_is_absent_on_this_arm_and_the_digest_and_bookkeeping_tolerate_it(tmp_path):
    script = _compose()
    schema = script[script.index("label: 'verify'") :].split("});", 1)[0]
    assert '"required": ["status", "tests_run", "tests_failed"]' in schema
    assert "sidecar_path ?? null" in script
    assert resolve_prep_sidecar(None, repo_root=tmp_path, session_id="s", tests_sidecar=None) is None
    assert resolve_prep_sidecar("prep.md", repo_root=tmp_path, session_id="s", tests_sidecar=None) is None


def test_the_falsifier_rides_the_judge_brief_and_no_falsifier_leg_is_composed():
    script = _compose(falsifier=_FALSIFIER)

    assert _agent_labels(script).count("review:coordinator:exit-criterion-judge") == 1
    judge_prompt = script[script.index("Judge whether this run met its plan") :].split(
        ", { label: 'review:coordinator:exit-criterion-judge'", 1
    )[0]
    assert "recorded falsifier: how=" in judge_prompt and "run it" in judge_prompt
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


def test_the_falsifier_act_aims_the_how_at_the_working_tree():
    from coordinator_core.ops.dispatch_emit.ask_compose import _FALSIFIER_ACT

    assert "from the working tree" in _FALSIFIER_ACT
    assert "cannot see this run's uncommitted change" in _FALSIFIER_ACT.replace("\n", " ")
    assert "not an internal name the change may rename" in _FALSIFIER_ACT


def test_the_judge_suffix_reports_an_unrunnable_how_as_error_and_keeps_the_verbatim_run():
    from coordinator_core.ops.dispatch_emit.ask_compose import _FALSIFIER_SUFFIX_JS

    assert "cannot run as written" in _FALSIFIER_SUFFIX_JS
    assert "falsifier verdict error" in _FALSIFIER_SUFFIX_JS and "output_anchor" in _FALSIFIER_SUFFIX_JS
    assert "fail is only for a how that ran" in _FALSIFIER_SUFFIX_JS
    assert "Judge the criterion from your own observation" in _FALSIFIER_SUFFIX_JS
    assert "run its how verbatim" in _FALSIFIER_SUFFIX_JS
    assert "rule not_met when it still shows the baseline" in _FALSIFIER_SUFFIX_JS


def test_the_register_holds_verify_and_still_holds_the_arms_that_keep_the_absorbed_legs():
    for label in ("verify", "test:terminal", "review:coordinator:exit-criterion-judge"):
        assert leg_register.lookup("emit", label) is not None, label


def test_the_retest_clause_launches_once_and_polls_to_a_verdict():
    from coordinator_core.ops.dispatch_emit.emit import VERIFY_RETEST_CLAUSE as clause

    assert clause.count("retest_targeted --from-task-output") == 1
    assert "retest_targeted --poll --from-task-output" in clause


def test_the_judge_arm_leg_does_not_carry_the_ask_only_retest():
    assert "retest_targeted" not in _compose()


def test_the_scoped_tests_call_states_a_deadline_below_the_budget_and_a_bash_timeout():
    from coordinator_core.ops.review_mint.post_exec_tail import VERIFY_WALL_BUDGET_S

    script = _compose()
    deadline = VERIFY_WALL_BUDGET_S * 4 // 5
    assert deadline < VERIFY_WALL_BUDGET_S
    assert f"return your test_result within {deadline}s" in script
    assert f"Bash tool timeout set to {deadline * 1000} ms or less" in script


def test_the_composed_script_never_carries_the_retired_orphaned_wording():
    script = _compose()
    assert "verify budget exceeded (" in script
    assert "orphaned, not stopped" not in script


def _ask_verify_script() -> str:
    from .test_ask_compose import _compose as ask_compose_script

    return ask_compose_script()


def _call_at(script: str, label: str) -> str:
    at = script.index(f"label: '{label}'")
    return script[at : script.index("schema:", at)]


def test_the_ask_scoped_tests_run_on_a_haiku_test_runner_leg_awaited_before_the_verify_agent():
    script = _ask_verify_script()
    tests_at, verify_at = script.index("label: 'verify-tests'"), script.index("label: 'verify'")
    assert script.count("label: 'verify-tests'") == 1 and script.count("label: 'verify'") == 1
    assert script.rindex("_verifyTest = await agent(", 0, tests_at) < tests_at < verify_at
    tests, verify = _call_at(script, "verify-tests"), _call_at(script, "verify")
    assert "agentType: 'coordinator:test-runner'" in tests and "model: 'haiku'" in tests
    assert "agentType: 'coordinator:executor'" in verify and "model: 'sonnet'" in verify


def test_the_verify_agent_gets_the_tests_result_inline_and_returns_no_test_of_its_own():
    script = _ask_verify_script()
    run = _run_verify(script)
    assert "if (_testWanted) {" in run and "JSON.stringify(_verifyTest ?? null)" in run
    assert "_testWanted ? ('The scoped tests already ran in a separate test-runner leg" in run
    assert "_verify.test = _verifyTest" in run and "Return one object: `items`" in run
    assert "`test` (step 1" not in script


def test_the_tests_leg_prompt_carries_the_deadline_and_the_measured_rev_instruction_the_verify_prompt_dropped():
    from coordinator_core.ops.dispatch_emit import ask_compose

    tests = ask_compose._verify_tests_call(
        head="H", anchor="A", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), run_id="r1"
    )
    verify = ask_compose._verify_call(
        head="H", anchor="A", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), docs_parts=(),
        verdict_parts=("v",), judged=False, schema={}, run_id="r1", test_result_var="_verifyTest",
    )
    assert "Deadline: return your test_result within" in tests and "dispatch.measured_rev" in tests
    assert "dispatch.measured_rev" not in verify and "Run the scoped test targets" not in verify


def test_a_non_usage_limit_throw_in_the_tests_leg_leaves_the_result_null_and_the_verify_agent_still_runs():
    run = _run_verify(_ask_verify_script())
    assert "if (_USAGE_LIMIT_RE.test(String((e && e.message) ?? e))) throw e; log('verify-tests leg threw: '" in run
    assert run.index("verify-tests leg threw") < run.index("_verify = await agent(")


def _run_verify(script: str) -> str:
    start = script.index("const _runVerify")
    return script[start : script.index("\n", start)]
