"""pipeline_compose family arm: a mapped stage runs through the family-stage-runner with one claude fallback; no map leaves the script unchanged."""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.pipeline_compose import (
    compose_chain_parts,
    compose_chain_script,
    compose_pipeline_script,
)
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose import _structured

_FAMILIES = {("structured", "scout"): "grok", ("structured", "verify"): "agy"}


def _plain() -> str:
    manifest, inputs, schedule = _structured()
    return compose_pipeline_script(manifest, inputs, schedule, run_id="pipeline-structured-r1", agent_type_host=None)


def _mapped(families=_FAMILIES) -> str:
    manifest, inputs, schedule = _structured()
    return compose_pipeline_script(
        manifest, inputs, schedule, run_id="pipeline-structured-r1", agent_type_host=None, stage_families=families
    )


def test_none_and_empty_map_leave_every_byte_unchanged():
    plain = _plain()
    assert _mapped(None) == plain
    assert _mapped({}) == plain
    assert "_familyStage" not in plain and stage_family.RUNNER_AGENT not in plain


def test_chain_script_and_parts_thread_the_map():
    segments = [_structured()]
    script = compose_chain_script(segments, run_id="r1", agent_type_host=None, stage_families=_FAMILIES)
    parts = compose_chain_parts(segments, run_id="r1", agent_type_host=None, stage_families=_FAMILIES)
    assert parts[0].script == script
    assert compose_chain_parts(segments, run_id="r1", agent_type_host=None)[0].script == compose_chain_script(
        segments, run_id="r1", agent_type_host=None
    )


def test_mapped_stage_calls_the_helper_inside_produced_with_its_own_claude_options():
    script = _mapped()
    assert script.count("async function _familyStage(") == 1
    scout = re.search(r"produced\(s\.subject \+ ':scout', _familyStage\('grok', 'scout', .*?\{ (.*?) \}\)\)", script)
    assert scout and "agentType: 'coordinator:research-scout'" in scout.group(1) and "model: 'haiku'" in scout.group(1)
    assert "_familyStage('agy', 'research'," in script
    synth = script.split("phase('synth');")[1]
    assert "_familyStage" not in synth and "agent(" in synth


def test_helper_dispatches_the_runner_with_brief_and_result_schema():
    script = _mapped()
    assert f"agentType: '{stage_family.RUNNER_AGENT}'" in script
    assert "family: grok\\nstage: scout\\n" in script and "family: agy\\nstage: research\\n" in script
    schema = json.loads(re.search(r"const _FAMILY_RESULT_SCHEMA = (\{.*\});", script).group(1))
    assert schema["required"] == list(stage_family._RESULT_KEYS)


def test_helper_declared_only_when_the_map_is_non_empty_and_once_across_a_chain():
    segments = [_structured(), _structured()]
    script = compose_chain_script(segments, run_id="r1", agent_type_host=None, stage_families=_FAMILIES)
    assert script.count("async function _familyStage(") == 1


def test_fallback_arm_keys_match_stage_record():
    script = _mapped()
    helper = script.split("async function _familyStage(")[1].split("\n}\n")[0]
    family = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "", "verdict": "FAILED",
              "blocked_reason": "x"}
    fallback = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"}
    record = stage_family.stage_record("research", family, fallback)
    for key in (*record, "fallback_for", *stage_family._RESULT_KEYS, "blocked_reason"):
        assert key in helper
    assert ":fallback'" in helper
    assert helper.count("agent(") == 2
    assert "reported_family: 'claude'" in helper
    assert "verdict: 'FAILED', blocked_reason: 'family-stage-runner reply missing or unparseable'" in helper
    for verdict in stage_family.FALLBACK_VERDICTS:
        assert f"fam.verdict === '{verdict}'" in helper


def test_claude_bound_entry_stays_an_ordinary_agent_call():
    script = _mapped({("structured", "scout"): "claude", ("structured", "verify"): "agy"})
    assert "_familyStage('grok'" not in script
    assert "_familyStage('claude'" not in script
    assert "_familyStage('agy', 'research'," in script
