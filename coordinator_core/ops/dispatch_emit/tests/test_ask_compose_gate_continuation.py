import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import GateVerdict


@pytest.mark.parametrize("schema", [ask_compose._GATE_SCHEMA, ask_compose._GATE_STAGED_SCHEMA])
@pytest.mark.parametrize("missing", ["resume_plan", "resume_rows"])
def test_gate_schema_refuses_lossy_continuation_reply(schema, missing):
    reply = {"arm": "m_plus", "halt": None, "resume_plan": "docs/plans/retained.md",
             "resume_rows": ["G", "D", "N", "V", "G1", "W2", "W3"]}
    jsonschema.validate(reply, schema)
    del reply[missing]
    with pytest.raises(jsonschema.ValidationError, match=missing):
        jsonschema.validate(reply, schema)


@pytest.mark.parametrize("rows", [None, [], ["G", "W2"]])
def test_gate_verdict_keeps_nullable_continuation_keys(rows):
    plan = "docs/plans/retained.md" if rows is not None else None
    reply = GateVerdict(arm="m_plus", halt=None, resume_plan=plan, resume_rows=rows).to_json()
    assert reply["resume_plan"] == plan
    assert reply["resume_rows"] == rows
    jsonschema.validate(reply, ask_compose._GATE_SCHEMA)


def test_gate_ruling_requires_continuation_keys_only_when_relaying_gate():
    schema = ask_compose._ruling_schema({
        "type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}},
    })
    reply = {"verdict": "pm-only", "pmOnly": True, "ruling": "Needs approval"}
    jsonschema.validate(reply, schema)
    reply.update(verdict="ruled", pmOnly=False, arm="m_plus", halt=None)
    with pytest.raises(jsonschema.ValidationError, match="resume_plan"):
        jsonschema.validate(reply, schema)
    reply.update(resume_plan=None, resume_rows=None)
    jsonschema.validate(reply, schema)


@pytest.mark.spawns_process
def test_stage_recovery_preserves_explicit_empty_resume_rows():
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = """
      const _runId = 'r', _sizingRel = 's.yml', _planRel = 'docs/plans/retained.md';
      const _crossRepoApproved = false, _SESSION_ID = 'owner';
      let _gate = {resume_rows: []};
    """ + ask_compose._STAGE_RECOVER_PARAMS_JS + """
      const empty = _stageRecoverParams();
      _gate = {resume_rows: null};
      console.log(JSON.stringify([empty, _stageRecoverParams()]));
    """
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    empty, unspecific = json.loads(result.stdout)
    assert empty["plan_path"] == unspecific["plan_path"] == "docs/plans/retained.md"
    assert empty["only_rows"] == []
    assert "only_rows" not in unspecific
