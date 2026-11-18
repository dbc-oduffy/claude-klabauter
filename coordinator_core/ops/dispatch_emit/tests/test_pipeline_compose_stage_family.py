"""pipeline_compose family arm: a mapped stage runs through the family-stage-runner with one claude fallback; no map leaves the script unchanged."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

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


_NODE = shutil.which("node")
_FAILED_FAMILY = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "", "verdict": "FAILED", "blocked_reason": "no stage work"}


def _run_stage(tmp_path, fallback_reply: dict) -> dict:
    """Execute the emitted `_familyStage` in node: the family attempt FAILED, the fallback answers `fallback_reply`."""
    script = _mapped()
    start = script.index("const _FAMILY_BRIEFS")
    body = script[start : script.index("\n}\n", script.index("async function _familyStage(")) + 3]
    prog = (
        "const logs = [];\nconst log = (m) => logs.push(m);\n"
        f"const _replies = [{json.dumps({'v': _FAILED_FAMILY})}, {json.dumps(fallback_reply)}]; let _n = 0;\n"
        "async function agent() { const x = _replies[_n++]; if (x.throw) throw new Error(x.throw); return x.v; }\n"
        f"{body}\n_familyStage('agy', 'research', 'p', 'lbl', {{}}).then(\n"
        "  (v) => console.log(JSON.stringify({ result: v, logs })),\n"
        "  (e) => console.log(JSON.stringify({ error: e.message, logs })));\n"
    )
    path = tmp_path / "stage.js"
    path.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        [_NODE, str(path)], capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return json.loads(out)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("reply", ["DONE: lbl\nStatus: DONE", {"structured": True}])
def test_done_fallback_reply_is_consumed_and_returned(tmp_path, reply):
    out = _run_stage(tmp_path, {"v": reply})
    assert out["result"] == reply and "error" not in out


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize(
    "reply, cause",
    [
        ({"throw": "boom"}, "dispatch threw: boom"),
        ({"v": None}, "reply missing (dispatch returned nothing)"),
        ({"v": "  \n"}, "reply was empty"),
        ({"v": "PARTIAL: lbl\ngate-blocker: codex worker performed no stage work\n"}, "replied PARTIAL, gate-blocker: codex worker performed no stage work"),
        ({"v": "BLOCKED: lbl cannot read the brief\nmore"}, "replied BLOCKED: lbl cannot read the brief"),
    ],
)
def test_non_done_fallback_reply_records_its_actual_cause(tmp_path, reply, cause):
    out = _run_stage(tmp_path, reply)
    assert out["error"].endswith(f"fallback: {cause}")
    rec = json.loads(out["logs"][-1].split("rerunning on claude ", 1)[1])
    fb = rec["attempts"][1]
    assert fb["verdict"] == "FAILED" and fb["blocked_reason"] == f"claude fallback {cause}"


def test_claude_bound_entry_stays_an_ordinary_agent_call():
    script = _mapped({("structured", "scout"): "claude", ("structured", "verify"): "agy"})
    assert "_familyStage('grok'" not in script
    assert "_familyStage('claude'" not in script
    assert "_familyStage('agy', 'research'," in script
