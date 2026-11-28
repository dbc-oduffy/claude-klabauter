"""Raw default submissions keep sizing assembly separate from the native host gate."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from coordinator_core.hooks.tests.test_submit_engine_ask_real_emit import repo
from coordinator_core.ops.dispatch_emit import ask_compose, minted_run
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.workflow_fire import headless

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_reword_schema_accepts_explicit_disk_proof():
    import jsonschema

    reply = {"exit_code": 0, "stdout": "abridged", "stderr": "",
             "exit_criterion": "observable", "sizing_present": True}
    jsonschema.validate(reply, ask_compose._REWORD_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**reply, "sizing_present": "true"}, ask_compose._REWORD_SCHEMA)


@pytest.mark.parametrize("sidecar,accepted", [
    ("halt-fix/notes.md", True), ("peer/notes.md", False),
    ("halt-fix/../peer/notes.md", False),
])
def test_review_recovery_accepts_only_its_own_sidecar(sidecar, accepted):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_halt_self_correction import (
        _OWN, _OWNED, _done, _drive, _green, _red,
    )
    from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL

    path = ".coordinator-local/workflow-sidecars/ask/r1/" + sidecar
    out = _drive(reverify=_red(_OWNED), replies={
        HALT_FIX_LABEL: [_done(_OWN, path)],
        HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}],
    })
    assert (out["halted"] is None) == accepted, out
    if not accepted:
        assert out["halted"]["self_correction"]["status"] == "scope"


@pytest.mark.parametrize("powershell", [False, True])
def test_verifier_operand_survives_native_shell_quoting(monkeypatch, powershell):
    import shlex

    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    monkeypatch.setattr(ask_compose, "_VERIFIER_POWERSHELL", powershell)
    value = json.dumps({"run_id": "run's id", "paths": ["space dir/a'b.py"]})
    expression = ask_compose._verifier_arg_js("operand")
    completed = subprocess.run(
        [node, "-e", "const operand = " + json.dumps(value) + "; console.log(" + expression + ");"],
        check=True, text=True, capture_output=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    quoted = completed.stdout.strip()
    if powershell:
        assert quoted[1:-1].replace("''", "'") == value
    else:
        assert shlex.split(quoted) == [value]


@pytest.fixture
def submitted(repo, monkeypatch):
    import psutil
    from coordinator_core.ops.workflow_fire import authority
    from coordinator_core.session import external_registry
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal(sid, "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: {"session_id": s, "pid": 7})
    monkeypatch.setattr(psutil, "Process", lambda p: type("P", (), {"cmdline": lambda self: ["node", "codex.js"]})())
    started = []
    monkeypatch.setattr(headless, "start", lambda *a, **kw: started.append((a, kw)) or {"fire_id": "offline"})
    params = {"ask": "add an offline fixture", "ask_source": "relayed",
              "caller": "codex-em", "session_id": sid, "cwd": str(repo)}
    stdout, stderr, code = _dispatch_argv(
        ["engine_ask.submit", json.dumps(params), "--repo", str(repo), "--bare"],
        str(repo.parent), allow_warm=False,
    )
    assert code == 0, (stdout, stderr)
    result = json.loads(stdout)
    assert "fire_error" not in result, result
    assert result["fire"]["fire_id"] == "offline" and len(started) == 1
    assert started[0][1]["caller_session_id"] == sid
    deferred = Path(result["script_path"]).read_text(encoding="utf-8")
    params_file = json.loads(re.search(r"^const PARAMS_FILE = (.*);$", deferred, re.M)[1])
    return repo, result, json.loads(Path(params_file).read_text())


@pytest.fixture
def emitted(submitted):
    from coordinator_core.hooks import submit_engine_ask
    repo, _, params = submitted
    actual = submit_engine_ask._default_emit(params, repo)
    start = minted_run.start_args(repo, actual["run_id"])
    assert start["fire_args"]["runToken"]
    return repo / actual["path"], start["fire_args"], repo, actual


def observe(emitted, tmp_path, replies, verdict, *, depth=0, token=True, deferred=False):
    script, args, root, actual = emitted
    if deferred:
        script, = root.glob("scratch/warp/defer-*.workflow.mjs")
        args = {}
    if not token:
        args = {**args, "runToken": None}
    runtime = Path(headless.__file__).with_name("runtime.mjs")
    program = tmp_path / "observe.mjs"
    program.write_text("""
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {executeWorkflow} = await import(pathToFileURL(input.runtime));
const calls = [], briefs = [];
const stop = new Error('offline boundary');
let release, gateEntered;
const entered = new Promise(r => gateEntered = r);
const paused = new Promise(r => release = r);
const expand = p => p.replace(/Read (.+?\\/prompts\\/[^\\n]+?\\.md) in full and follow it as the rest of this brief\\./g,
  (_, path) => fs.readFileSync(path, 'utf8'));
const running = executeWorkflow(fs.readFileSync(input.script, 'utf8'), (p, o) => {
  calls.push({agent: o.label, prompt: expand(p)});
  if (['size','size-retry','size-reword'].includes(o.label)) {
    briefs.push({label:o.label, text:expand(p)});
    return input.replies.shift();
  }
  throw stop;
}, () => {}, input.args, {depth:input.depth, cwd: input.root, scriptPath: input.script,
  engineOp: async (op, params) => {
    calls.push({op, params});
    if (op === 'dispatch.emit' && input.deferred) return input.actual;
    if (op === 'gate.host_run') {
      gateEntered(); await paused;
      calls.push({verdict:input.verdict});
      return input.verdict;
    }
    if (op === 'gate.host_receipt') return {artifacts:{response:{result:input.verdict}}};
    throw stop;
  }}).catch(e => {if (e !== stop) throw e;});
const first = await Promise.race([entered.then(()=>'gate'), running.then(()=>'done')]);
const before = structuredClone(calls);
release(); await running;
console.log(JSON.stringify({calls, before, briefs, first}));
""", encoding="utf-8")
    data = tmp_path / "input.json"
    data.write_text(json.dumps({"runtime": str(runtime), "script": str(script), "args": args,
                                "root": str(root), "replies": replies, "verdict": verdict, "depth": depth,
                                "deferred": deferred, "actual": actual}))
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    out = subprocess.run([node, str(program), str(data)], capture_output=True, text=True,
                         timeout=10, check=True, cwd=tmp_path,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(out.stdout)


def assembled(**over):
    return {"exit_code": 0, "stdout": '{"route":"dispatch"}', "stderr": "",
            "sizing_path": "state/sizings/fixture.yaml", "writes": ["fixture.txt"],
            "route": "dispatch", "tshirt": "XS", "arm": "xs", "halt": None, **over}


HALT = {"arm": None, "halt": {"kind": "refusal", "reason": "ACTUAL-HOST-REFUSAL"},
        "resume_plan": None, "resume_rows": None}


@pytest.mark.parametrize("case", ["xs", "unknown", "retry", "values-retry", "reword", "reword-disk-proof", "disk-proof", "failed", "unproven", "spoofed-arm"])
def test_native_raw_gate_matrix(emitted, tmp_path, case):
    reply = assembled()
    replies = [reply]
    if case == "unknown":
        replies = [assembled(arm=None, route=None, tshirt=None)]
    elif case == "retry":
        replies = [{}, reply]
    elif case == "values-retry":
        replies = [{"tshirt": "XS", "intent": "offline fixture", "name": "fixture",
                    "premise_provenance": "read", "premise_evidence": "offline", "exit_criterion": "observable",
                    "writes": ["fixture.txt"], "gated": []}, reply]
    elif case == "disk-proof":
        replies = [assembled(stdout="abridged", sizing_present=True)]
    elif case in {"reword", "reword-disk-proof"}:
        replies = [assembled(exit_code=2, stdout="", stderr=ask_compose.EXIT_CRITERION_REFUSAL_HEAD
                            + " " + ask_compose.REMEDIATION_MARKER),
                   {**reply, "exit_criterion": "a reworded criterion",
                    **({"stdout": "abridged", "sizing_present": True} if case == "reword-disk-proof" else {})}]
    elif case == "failed":
        replies = [assembled(exit_code=2, stdout="", stderr="assembly failed")]
    elif case == "unproven":
        replies = [{"exit_code": 0, "stdout": "not run"}] * 2
    elif case == "spoofed-arm":
        replies = [assembled(stdout="not run")] * 2
    out = observe(emitted, tmp_path, replies, HALT)
    gates = [c for c in out["calls"] if c.get("op") == "gate.host_run"]
    success = case not in {"failed", "unproven", "spoofed-arm"}
    assert len(gates) == int(success), out
    if success:
        assert gates[0]["params"]["require_live_token"] is True
        assert gates[0]["params"]["sizing_path"] == "state/sizings/fixture.yaml"
        assert [c.get("agent") for c in out["before"] if "agent" in c] == [b["label"] for b in out["briefs"]]
        assert out["calls"][len(out["before"])]["verdict"] == HALT
        assert any("ACTUAL-HOST-REFUSAL" in c.get("prompt", "") for c in out["calls"])
    for brief in out["briefs"]:
        assert "dispatch.ask_gate" not in brief["text"]
        assert "dispatch.ask_stage" not in brief["text"]
        assert "dispatch.emit" not in brief["text"]
    assert not any(c.get("agent") in {"xs", "execute", "stage"} for c in out["calls"])


@pytest.mark.parametrize("depth,token", [(1, True), (0, False)])
def test_native_worker_ownership_preserves_legacy_closing_acts(emitted, tmp_path, depth, token):
    out = observe(emitted, tmp_path, [assembled()], HALT, depth=depth, token=token)
    assert not any(c.get("op") == "gate.host_run" for c in out["calls"])
    assert "dispatch.ask_gate" in out["briefs"][0]["text"]


@pytest.mark.parametrize("verdict", [
    HALT,
    {"arm": "xs", "halt": None, "resume_plan": None, "resume_rows": None,
     "stage_reply": {"error": "ACTUAL-STAGE-REFUSAL"}},
    {"arm": "s", "halt": None, "resume_plan": "docs/plans/actual.md", "resume_rows": []},
])
@pytest.mark.parametrize("deferred,case", [(False, "xs"), *[(True, c) for c in
                         ("xs", "unknown", "retry", "reword", "failed", "unproven")]])
def test_emitted_root_consumes_authenticated_broker_before_dependents(submitted, tmp_path, monkeypatch, verdict, deferred, case):
    import concurrent.futures
    import hashlib
    import threading
    from types import SimpleNamespace

    from coordinator_core.hooks import submit_engine_ask
    root, submission, emit_params = submitted
    run_id = submission["run_id"]
    root_script = root / "scratch" / "warp" / (run_id + ".workflow.mjs")
    if deferred:
        script = Path(submission["script_path"])
        args = {}
    else:
        actual = submit_engine_ask._default_emit(emit_params, root)
        script = root / actual["path"]
        args = minted_run.start_args(root, run_id)["fire_args"]
    artifacts = tmp_path / "native-artifacts"
    artifacts.mkdir()
    (artifacts / "start").touch()
    record_path = tmp_path / "native.json"
    record = {"run_id": run_id, "fire_id": "offline-native", "repo_root": str(root),
              "caller_session_id": "11111111-2222-3333-4444-555555555555",
              "script_path": str(script), "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
              "artifact_dir": str(artifacts), "journal_path": str(artifacts / "journal.jsonl"),
              "backend": "codex", "args": args, "timeout_s": 10}
    record_path.write_text(json.dumps(record))
    entered, release = threading.Event(), threading.Event()
    agents, requests = [], []
    briefs = []
    reply = assembled(resume_plan="docs/plans/SCOUT-DECOY.md", resume_rows=["DECOY"])
    replies = [reply]
    if case == "unknown":
        replies = [assembled(arm=None, route=None, tshirt=None)]
    elif case == "retry":
        replies = [{}, reply]
    elif case == "reword":
        replies = [assembled(exit_code=2, stdout="", stderr=ask_compose.EXIT_CRITERION_REFUSAL_HEAD
                            + " " + ask_compose.REMEDIATION_MARKER), reply]
    elif case == "failed":
        replies = [assembled(exit_code=2, stdout="", stderr="assembly failed")]
    elif case == "unproven":
        replies = [{"exit_code": 0, "stdout": "not run"}] * 2
    expected_agents = ["size"] + (["size-retry"] if case in {"retry", "unproven"} else
                                 ["size-reword"] if case == "reword" else [])
    original_run, original_which = headless.subprocess.run, headless.shutil.which
    monkeypatch.setattr("coordinator_core.warm.skew.compute_client_token", lambda *a: "offline-source-stamp")

    def run(argv, **kwargs):
        if argv[0] == "offline-claude":
            assert argv[1:] == ["auth", "status", "--json"]
            return SimpleNamespace(returncode=0, stdout=b'{"loggedIn":true}')
        if "coordinator_core.ops.workflow_fire.cold_operation" not in argv:
            return original_run(argv, **kwargs)
        request_path, response_path = map(Path, argv[-2:])
        request = json.loads(request_path.read_text())
        assert request["op"] == "dispatch.ask_gate"
        requests.append(request)
        assert request["params"]["run_id"] == run_id
        assert request["params"]["require_live_token"] is True
        assert request["repo_root"] == str(root)
        entered.set()
        assert release.wait(3), "test did not release gate"
        response_path.write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "result": verdict}))
        return SimpleNamespace(returncode=0)

    def worker(prompt, options, **kwargs):
        agents.append(options["label"])
        if options["label"] in {"size", "size-retry", "size-reword"}:
            def expand(match):
                return Path(match[1]).read_text()
            briefs.append(re.sub(r"Read (.+?/prompts/[^\n]+?\.md) in full and follow it as the rest of this brief\.", expand, prompt))
            return replies.pop(0)
        receipt_path = next((artifacts / "operations").glob("*/consumed.json"))
        digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        current = json.loads(record_path.read_text())
        binding = {**current, **current.get("emitted_root", {})}
        receipt = headless._read_host_gate_receipt(receipt_path, binding, digest)
        assert receipt["artifacts"]["response"]["result"] == verdict
        assert receipt["loggedIn"] is True
        if verdict.get("resume_plan"):
            assert verdict["resume_plan"] in prompt and "SCOUT-DECOY" not in prompt
        else:
            assert (verdict.get("halt") or verdict["stage_reply"])["reason" if verdict.get("halt") else "error"] in prompt
        raise RuntimeError("offline boundary after actual verdict consumed")

    monkeypatch.setattr(headless.shutil, "which", lambda name, *a, **kw: "offline-claude" if name == "claude" else original_which(name, *a, **kw))
    monkeypatch.setattr(headless.subprocess, "run", run)
    if case in {"failed", "unproven"}:
        result = headless.execute(record_path, agent_runner=worker)
        assert agents == expected_agents, result
        assert requests == []
        assert not list((artifacts / "operations").glob("*/consumed.json"))
        assert result["result"]["halted_by"]["halted"] == "refusal"
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(headless.execute, record_path, agent_runner=worker)
        try:
            assert entered.wait(3), future.result() if future.done() else "host gate never requested"
            assert agents == expected_agents and len(requests) == 1
            assert not list((artifacts / "operations").glob("*/consumed.json"))
        finally:
            release.set()
        result = future.result(timeout=5)
    assert len(requests) == 1
    for brief in briefs:
        assert "dispatch.ask_gate" not in brief and "dispatch.ask_stage" not in brief and "dispatch.emit" not in brief
    if deferred and not verdict.get("resume_plan"):
        assert agents == expected_agents, result
        assert (verdict.get("halt") or verdict["stage_reply"])["reason" if verdict.get("halt") else "error"] in result["error"]
    else:
        assert len(agents) == len(expected_agents) + 1, result
        assert "offline boundary after actual verdict consumed" in result["error"], result
    if deferred:
        current = json.loads(record_path.read_text())
        assert current["script_path"] == str(script)
        assert current["emitted_root"]["script_path"] == str(root_script)
        assert current["emitted_root"]["script_sha256"] == hashlib.sha256(root_script.read_bytes()).hexdigest()
        receipt_path = next((artifacts / "operations").glob("*/consumed.json"))
        receipt = json.loads(receipt_path.read_text())
        assert receipt["caller_session_id"] == record["caller_session_id"]
        from coordinator_core.ops.dispatch_emit import run_token
        secret = run_token.escrowed(root, run_id)
        assert secret
        for directory in (artifacts, root / "scratch"):
            for artifact in directory.rglob("*"):
                if artifact.is_file():
                    assert secret.encode() not in artifact.read_bytes(), artifact


def test_native_nested_wrapper_cannot_promote_its_emitted_child(emitted, tmp_path):
    out = observe(emitted, tmp_path, [assembled()], HALT, deferred=True)
    assert any(c.get("op") == "dispatch.emit" for c in out["calls"])
    assert len([c for c in out["calls"] if c.get("op") == "gate.host_run"]) == 0, {
        "operations": [c.get("op") or c.get("agent") for c in out["calls"]],
        "scout_asks_worker_gate": "dispatch.ask_gate" in out["briefs"][0]["text"],
    }


@pytest.mark.parametrize("damage", ["caller", "run", "digest", "receipt", "spent"])
def test_host_root_requires_bound_emission_and_live_escrow(emitted, monkeypatch, damage):
    script, _, root, actual = emitted
    record = {"repo_root": str(root), "run_id": actual["run_id"],
              "caller_session_id": "11111111-2222-3333-4444-555555555555"}
    bound = headless._emitted_host_root(actual, record)
    assert bound["script_path"] == str(script)
    if damage == "caller":
        record["caller_session_id"] = "foreign"
    elif damage == "run":
        record["run_id"] = "foreign"
    elif damage == "digest":
        actual = {**actual, "sha256": "f" * 64}
    elif damage == "receipt":
        receipt_path = script.with_name(script.name + ".emitted.json")
        receipt = json.loads(receipt_path.read_text())
        receipt_path.write_text(json.dumps({**receipt, "sha256": "f" * 64}))
    else:
        from coordinator_core.ops.dispatch_emit import run_token
        monkeypatch.setattr(run_token, "admits", lambda *a: False)
    with pytest.raises(ValueError, match="binding|digest"):
        headless._emitted_host_root(actual, record)


def test_emit_broker_never_serializes_live_fire_args(emitted, tmp_path, monkeypatch):
    from coordinator_core import ipc

    _, _, root, actual = emitted
    from coordinator_core.ops.dispatch_emit import run_token
    token = run_token.escrowed(root, actual["run_id"])
    actual = {**actual, "fire_args": {"runToken": token}}
    async def dispatch(request):
        assert request["method"] == "dispatch.emit"
        return {"result": actual}

    monkeypatch.setattr(ipc, "dispatch_message", dispatch)
    monkeypatch.setattr("coordinator_core.warm.skew.compute_client_token", lambda *a: "offline-stamp")
    artifacts = tmp_path / "emit-broker"
    result = headless._invoke_source("dispatch.emit", {}, str(root), artifacts, 1)
    assert result["path"] == actual["path"]
    assert "runToken" not in result["fire_args"]
    assert token not in json.dumps(result)
    assert not list(artifacts.iterdir())


def test_native_deferred_chain_uses_host_adoption_only_for_initial_root(emitted, tmp_path):
    from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

    _, _, root, actual = emitted
    wrapper, = root.glob("scratch/warp/defer-*.workflow.mjs")
    child = tmp_path / "chained.workflow.mjs"
    child.write_text("export const meta = {}; return {done: true};", encoding="utf-8")
    source = "export const meta = {}; return " + json.dumps({
        CHAIN_HANDOFF_KEY: {"path": str(child), "run_id": "chained", "chain_hop": True},
    }) + ";"
    runtime = Path(headless.__file__).with_name("runtime.mjs")
    program = tmp_path / "chain.mjs"
    program.write_text("""
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {executeWorkflow} = await import(pathToFileURL(input.runtime));
let starts = 0;
const result = await executeWorkflow(fs.readFileSync(input.wrapper, 'utf8'),
  () => {throw new Error('unexpected worker');}, () => {}, {}, {
    cwd: input.root, scriptPath: input.wrapper, hostWorkflow: true,
    engineOp: async (op) => {
      if (op === 'dispatch.emit') return input.actual;
      if (op !== 'workflow.host_start' || ++starts !== 1) throw new Error('unexpected host adoption');
      return {source: input.source, script_path: input.actual.path, args: {},
        script_sha256: createHash('sha256').update(input.source).digest('hex')};
    },
  });
console.log(JSON.stringify({starts, result}));
""", encoding="utf-8")
    data = tmp_path / "chain.json"
    data.write_text(json.dumps({"runtime": str(runtime), "wrapper": str(wrapper),
                                "root": str(root), "actual": actual, "source": source}), encoding="utf-8")
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    completed = subprocess.run([node, str(program), str(data)], capture_output=True, text=True,
                               timeout=10, check=True, cwd=tmp_path,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    observed = json.loads(completed.stdout)
    assert observed["starts"] == 1
    assert observed["result"]["done"] is True
