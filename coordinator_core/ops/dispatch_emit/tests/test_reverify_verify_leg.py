"""Reverify's tests re-run and criterion judge are one `Verify` agent on the judge's type."""

from __future__ import annotations

import json
import re

from coordinator_core.bash_guards.guard_heavy_command_admission import _VERIFIER_TYPES
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_JUDGE_TYPE = "coordinator:exit-criterion-judge"
_CLAIMS = [{"claim": "adds the frobnicator", "anchor": "no frob.py in diff"}]


def _compose(*, rerun_tests=True, with_judge=True, host_degraded=False) -> str:
    fragment = _v5_fragment()
    if with_judge:
        fragment["execute_review"]["stages"].append(
            {"kind": "judge", "agents": [{"agentType": _JUDGE_TYPE, "model": "sonnet",
                                          "effort": "low", "schema": "judge-result"}]}
        )
    schemas = {**_STAGE_SCHEMAS, "judge-result": {"type": "object", "properties": {"status": {"type": "string"}},
                                                  "required": ["status"]}}
    return rd.compose_reverify_script(
        fragment=fragment, stage_schemas=schemas, plan_path="p.md", run_record_rel="r.md",
        plan_id="pln-1", run_base_sha="a" * 40, head_sha="b" * 40, claims=_CLAIMS,
        rerun_tests=rerun_tests, host_degraded=host_degraded,
    )


def test_the_merged_arm_composes_one_verify_agent_and_no_tests_rerun_agent():
    script = _compose()

    assert "Tests re-run" not in script and "Criterion judge" not in script
    assert script.count("agent(") == 2  # delivery verifier + verify
    assert "phase('Verify')" in script
    assert script.count(f"agentType: '{_JUDGE_TYPE}'") == 1


def test_the_verify_prompt_carries_the_test_step_then_the_judge_brief():
    script = _compose()

    step_one = script.index("Step 1 -- scoped tests")
    step_two = script.index("Step 2 -- criterion verdict")
    assert step_one < script.index("python3") < step_two
    assert "never the verdict itself" in script


def test_the_tests_brief_runs_retest_targeted_isolated_export_and_reports_both_results():
    script = _compose()
    step = script[script.index("Step 1 -- scoped tests"):script.index("Step 2 -- criterion verdict")]

    assert "--retest-targeted" in step and "--isolated-export" in step and "--poll" in step
    assert "`export` and `live`" in step
    assert "Never build an export yourself" in step


def test_the_test_runner_sidecar_clause_does_not_reach_the_merged_prompt():
    script = _compose()

    assert "Write your record and return sidecar_path" not in script
    assert "omit sidecar_path" in script


def test_the_verify_type_is_admitted_by_the_heavy_command_guard():
    emitted = set(re.findall(r"agentType: '([^']*)'", _compose()))

    assert _JUDGE_TYPE in emitted
    assert _JUDGE_TYPE in _VERIFIER_TYPES


def test_the_verify_schema_requires_judge_and_leaves_sidecar_path_optional():
    script = _compose()
    start = script.index('schema: {"additionalProperties": false, "properties": {"judge"')
    decoder = json.JSONDecoder()
    schema, _ = decoder.raw_decode(script[start + len("schema: "):])

    assert schema["required"] == ["judge"]
    assert set(schema["properties"]) == {"test", "judge"}
    assert "sidecar_path" not in schema["properties"]["test"]["required"]


def test_the_returned_fields_keep_their_shape():
    script = _compose()

    assert "const _tests = _verify.test ?? null;" in script
    assert "const _judge = _verify.judge ?? null;" in script
    assert "criterion: _judge ?" in script and "tests: _tests ?" in script


def test_without_a_judge_the_standalone_tests_leg_composes_as_before():
    script = _compose(with_judge=False)

    assert "Tests re-run" in script and "Write your record and return sidecar_path" in script
    assert "Verify" not in script


def test_without_a_tests_rerun_the_judge_composes_as_before():
    script = _compose(rerun_tests=False)

    assert "Criterion judge" in script and "Verify" not in script and "_tests" not in script


def test_a_degraded_host_keeps_the_two_legs():
    script = _compose(host_degraded=True)

    assert "Tests re-run" in script and "Criterion judge" in script and "Verify" not in script
