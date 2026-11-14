"""Composer: a research.close next_action runs in-run and returns only when the close did not commit."""

from __future__ import annotations

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)

KW = {"run_id": "r1", "agent_type_host": None}
CLOSE = {"op": "research.close", "params": {"run_id": "r1", "note": "it's"}}


def _chain(tmp_path, action):
    return compose_chain_script(
        [_segment(tmp_path, "pre", [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)]),
         _segment(tmp_path, "main", [_stage("work")])],
        next_action=action, **KW,
    )


def test_scoped_close_runs_an_op_runner_call_gated_on_delivered(tmp_path):
    script = _chain(tmp_path, CLOSE)
    assert script.count("label: 'research-close'") == 1
    assert f"agentType: '{OP_RUNNER_AGENT_TYPE}'" in script and "model: '" in script
    assert "'coordinator-invoke ' + action.op" in script
    assert 'const NEXT_ACTION = {"op": "research.close", "params": {"note": "it\'s", "run_id": "r1"}};' in script
    assert "const closed = delivered ? await closeInRun(effective) : null;" in script
    assert script.index("closeInRun(effective)") < script.index("next_action: effective")


def test_next_action_returns_only_when_the_close_did_not_commit(tmp_path):
    script = _chain(tmp_path, CLOSE)
    assert "...(delivered && closed?.committed !== true ? { next_action: effective } : {})" in script
    assert script.count("next_action:") == 1


def test_unscoped_close_runs_in_run_and_returns_next_action_on_refusal(tmp_path):
    seg = _segment(tmp_path, "main", [_stage("work")])
    script = compose_chain_script([seg], next_action=CLOSE, **KW)
    assert "const closed = await closeInRun(NEXT_ACTION);" in script
    assert "...(closed?.committed === true ? {} : { next_action: NEXT_ACTION })" in script


def _run_close_in_run(reply_text):
    """Execute the composed closeInRun JS under node with a stubbed agent returning `reply_text`."""
    import json
    import shutil
    import subprocess

    from coordinator_core.ops.dispatch_emit.pipeline_compose import _close_agent_options, _close_in_run_js

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node not installed")
    js = (
        f"const agent = async () => ({{ exit_code: 0, reply: {json.dumps(reply_text)} }});\n"
        + "\n".join(_close_in_run_js(_close_agent_options(None)))
        + "\nconsole.log(JSON.stringify(await closeInRun({op:'research.close', params:{}})));"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_closed_committed_follows_the_wrapped_close_reply():
    import json

    inner = {"committed": True, "sha": "604c181bca"}
    for text in (
        json.dumps(inner),
        json.dumps({"ok": True, "result": inner}),
        json.dumps({"result": json.dumps(inner)}),
        "note: done\n" + json.dumps(inner) + "\n",
    ):
        assert _run_close_in_run(text)["committed"] is True, text
    assert _run_close_in_run("garbled " + '"committed": true' + " no braces")["committed"] is True


def test_closed_committed_stays_false_when_the_close_did_not_commit():
    import json

    assert _run_close_in_run(json.dumps({"committed": False}))["committed"] is False
    assert _run_close_in_run(json.dumps({"ok": True, "result": {"committed": False}}))["committed"] is False
    assert _run_close_in_run("not json at all")["committed"] is False


def test_other_next_action_ops_keep_the_plain_tail(tmp_path):
    script = _chain(tmp_path, {"op": "other.op", "params": {"a": 1}})
    assert "closeInRun" not in script and "research-close" not in script
    assert "...(delivered ? { next_action:" in script
