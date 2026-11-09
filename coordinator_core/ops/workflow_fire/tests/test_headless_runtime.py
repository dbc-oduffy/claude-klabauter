"""Real Node runtime/broker tests; no vendor harness or model spend."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
RUNTIME = Path(__file__).parents[1] / "runtime.mjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for Workflow execution")


def run_script(tmp_path, source, answer=lambda request: request["prompt"], args=None):
    script = tmp_path / "workflow.mjs"
    script.write_text(source, encoding="utf-8")
    command = [NODE, str(RUNTIME), str(script)]
    if args is not None:
        argfile = tmp_path / "args.json"
        argfile.write_text(json.dumps(args), encoding="utf-8")
        command.append(str(argfile))
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    events = []
    try:
        for line in child.stdout:
            event = json.loads(line)
            events.append(event)
            if event["type"] in ("agent", "invoke"):
                child.stdin.write(json.dumps({"id": event["id"], "result": answer(event)}) + "\n")
                child.stdin.flush()
        child.wait(timeout=10)
        return child.returncode, events, child.stderr.read()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


def test_real_broker_preserves_schema_labels_and_bound_args(tmp_path):
    code, events, stderr = run_script(tmp_path, """
/* metadata containing export const meta */
export const meta = { name: 'probe' };
const args = {value: 'bound'};
await phase('Build');
const answer = await agent(args.value, {
  label: 'builder', model: 'sonnet', agentType: 'coordinator:executor',
  schema: {type: 'object', properties: {ok: {type: 'boolean'}}}
});
console.log('diagnostic');
return {answer, untouched: 'export const meta'};
""", answer=lambda request: {"ok": True}, args={"value": "outer"})
    assert code == 0, events
    request = next(event for event in events if event["type"] == "agent")
    assert request["prompt"] == "bound"
    assert request["options"] == {
        "label": "builder", "phase": "Build", "model": "sonnet", "agentType": "coordinator:executor",
        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    }
    assert events[-1] == {"type": "result", "result": {"answer": {"ok": True}, "untouched": "export const meta"}}
    assert "diagnostic" in stderr


def test_pipeline_has_no_cross_item_stage_barrier(tmp_path):
    # Record scheduling: both first agents are dispatched before a reply, then
    # each item reaches its next stage without waiting for the sibling stage.
    code, events, _ = run_script(tmp_path, """export const meta = {name: 'pipeline'};
return await pipeline([1, 2],
  n => agent(String(n), {label: 'first-' + n}),
  n => agent(n + '-next', {label: 'second-' + n}));
""")
    assert code == 0, events
    prompts = [event["prompt"] for event in events if event["type"] == "agent"]
    assert prompts[:2] == ["1", "2"]
    assert events[-1]["result"] == ["1-next", "2-next"]


def test_parallel_accepts_thunks_and_promises_and_global_args(tmp_path):
    code, events, _ = run_script(tmp_path, """export const meta = {name: 'parallel'};
return await parallel([() => agent(args.value), agent('second')]);
""", args={"value": "injected"})
    assert code == 0
    assert events[-1]["result"] == ["injected", "second"]


@pytest.mark.parametrize("source, expected", [
    ("return 'export const meta = {}';", "must begin with export const meta"),
    ("export const meta = {};\nreturn brokenName;", "ReferenceError"),
    ("export const meta = {};\nawait agent(42);", "prompt must be a string"),
    ("/* broken", "Unterminated"),
])
def test_runtime_errors_are_terminal_failures(tmp_path, source, expected):
    code, events, _ = run_script(tmp_path, source)
    assert code == 1
    assert events[-1]["type"] == "error"
    assert expected in events[-1]["error"]


def test_broker_error_propagates_without_success(tmp_path):
    script = tmp_path / "fail.mjs"
    script.write_text("export const meta = {}; return await agent('fail');", encoding="utf-8")
    child = subprocess.Popen([NODE, str(RUNTIME), str(script)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    events = []
    try:
        for line in child.stdout:
            event = json.loads(line)
            events.append(event)
            if event["type"] == "agent":
                child.stdin.write(json.dumps({"id": event["id"], "error": "backend refused"}) + "\n")
                child.stdin.flush()
        assert child.wait(timeout=10) == 1
        assert events[-1]["type"] == "error"
        assert "backend refused" in events[-1]["error"]
        assert not any(event["type"] == "result" for event in events)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


def test_nested_workflow_shares_broker_and_returns_digest(tmp_path):
    nested = tmp_path / 'nested.mjs'
    nested.write_text("""export const meta = {name: 'nested'};
phase('Nested'); log('running', args.value); await sleep(1);
return {next_action: {op: 'dispatch.land_run'}, answer: await agent(args.value)};
""", encoding='utf-8')
    code, events, _ = run_script(tmp_path, "export const meta = {}; return await workflow(" +
                                 json.dumps({'scriptPath': str(nested), 'args': {'value': 'nested'}}) + ");")
    assert code == 0, events
    assert events[-1]['result'] == {'next_action': {'op': 'dispatch.land_run'}, 'answer': 'nested'}
    assert any(event.get('event') == 'workflow-start' for event in events)
    assert any(event.get('event') == 'log' and event['message'] == 'running nested' for event in events)


def test_recursive_workflow_fails_at_depth_bound(tmp_path):
    # run_script writes workflow.mjs; point recursion there to use exactly one
    # script and prove the guard rather than a missing-file failure.
    source = "export const meta = {}; return await workflow(" + json.dumps({'scriptPath': str(tmp_path / 'workflow.mjs')}) + ");"
    code, events, _ = run_script(tmp_path, source)
    assert code == 1
    assert 'nesting exceeds 32' in events[-1]['error']


def test_broker_eof_before_delayed_agent_is_a_terminal_error(tmp_path):
    script = tmp_path / 'eof.mjs'
    script.write_text("export const meta = {}; await sleep(20); return await agent('late');", encoding='utf-8')
    child = subprocess.run([NODE, str(RUNTIME), str(script)], input='', capture_output=True,
                           text=True, timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert child.returncode == 1
    events = [json.loads(line) for line in child.stdout.splitlines()]
    assert events[-1]['type'] == 'error'
    assert 'closed stdin' in events[-1]['error']


def test_engine_op_uses_same_broker_response_protocol(tmp_path):
    params = {'ask': 'literal $() "quotes"', 'repo': 'C:\\dev\\project'}
    source = "export const meta = {}; return await engineOp('dispatch.emit', " + json.dumps(params) + ");"
    code, events, _ = run_script(tmp_path, source, answer=lambda request: {'run_id': 'native', 'path': 'run.mjs'})
    assert code == 0, events
    requests = [event for event in events if event['type'] == 'invoke']
    assert len(requests) == 1
    assert requests[0]['op'] == 'dispatch.emit'
    assert requests[0]['params'] == params
    assert events[-1]['result'] == {'run_id': 'native', 'path': 'run.mjs'}
    assert not any(event['type'] == 'agent' for event in events)


def test_engine_op_callback_is_shared_with_nested_workflow(tmp_path):
    nested = tmp_path / 'nested-native.mjs'
    nested.write_text("export const meta={}; return await engineOp('probe.read', args);", encoding='utf-8')
    source = "export const meta={}; return await workflow(" + json.dumps({
        'scriptPath': str(nested), 'args': {'value': 42}}) + ");"
    code, events, _ = run_script(tmp_path, source, answer=lambda request: request['params'])
    assert code == 0, events
    assert events[-1]['result'] == {'value': 42}
    assert len([event for event in events if event['type'] == 'invoke']) == 1


def test_execute_workflow_uses_supplied_engine_op_callback(tmp_path):
    module = tmp_path / 'callback.mjs'
    module.write_text("import {executeWorkflow} from " + json.dumps(RUNTIME.as_uri()) + ";\n" + """
const result = await executeWorkflow(
  "export const meta={}; return await engineOp('probe.read', {value: 7});",
  () => {throw new Error('agent must not be used')}, () => {}, {},
  {engineOp: async (op, params) => ({op, ...params})});
console.log(JSON.stringify(result));
""", encoding='utf-8')
    result = subprocess.run([NODE, str(module)], capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'op': 'probe.read', 'value': 7}


@pytest.mark.parametrize('call', ["engineOp('', {})", "engineOp('probe.read', [])"])
def test_engine_op_bad_request_is_refused_before_broker(tmp_path, call):
    code, events, _ = run_script(tmp_path, 'export const meta={}; return await ' + call + ';')
    assert code == 1
    assert not any(event['type'] == 'invoke' for event in events)
