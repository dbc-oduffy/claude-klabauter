import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.workflow_fire import headless


@pytest.mark.parametrize("verdict", [
    {"arm": "s", "halt": None, "resume_plan": "docs/plans/actual.md", "resume_rows": []},
    {"arm": None, "halt": {"kind": "refusal", "reason": "actual refusal"}, "resume_plan": None, "resume_rows": None},
])
def test_host_gate_receipt_rejects_missing_and_foreign_binding(tmp_path, monkeypatch, verdict):
    artifacts = tmp_path / "gate"
    record = {"run_id": "own-run", "fire_id": "own-fire", "repo_root": str(tmp_path),
              "script_path": str(tmp_path / "root.mjs"), "script_sha256": "a" * 64,
              "caller_session_id": "own-caller"}
    params = {"run_id": "own-run", "require_live_token": True, "sizing_path": "sizing.yaml"}

    def run(argv, **kwargs):
        if argv[0] == "claude-fixture":
            assert argv == ["claude-fixture", "auth", "status", "--json"]
            assert kwargs["cwd"] == str(tmp_path)
            return SimpleNamespace(returncode=0, stdout=b'{"loggedIn":true}')
        assert argv[1:4] == ["-P", "-m", "coordinator_core.ops.workflow_fire.cold_operation"]
        request, response = map(Path, argv[-2:])
        assert json.loads(request.read_text(encoding="utf-8")) == {
            "op": "dispatch.ask_gate", "params": params, "repo_root": str(tmp_path),
        }
        response.write_text(json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": verdict,
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(headless.shutil, "which", lambda executable: "claude-fixture")
    monkeypatch.setattr(headless.subprocess, "run", run)
    receipt = artifacts / "consumed.json"
    with pytest.raises(ValueError, match="missing"):
        headless._read_host_gate_receipt(receipt, record, None)
    result, digest = headless._host_gate(params, record, artifacts, 1)
    evidence = headless._read_host_gate_receipt(receipt, record, digest)
    assert result == verdict and evidence["arm"] == verdict["arm"]
    assert evidence["stage"] == "gate"
    assert json.loads((artifacts / "response.json").read_text(encoding="utf-8"))["result"] == result
    assert evidence["artifacts"]["response"]["result"] == result
    assert evidence["artifacts"]["request"]["params"] == params
    assert evidence["artifacts"]["warm_route"]["route"] == "cold"
    assert evidence["loggedIn"] is True and evidence["process_exit_code"] == 0
    original = receipt.read_text(encoding="utf-8")
    for field, value in [("run_id", "other-run"), ("fire_id", "other-fire"),
                         ("script_path", "other.mjs"), ("script_sha256", "b" * 64),
                         ("caller_session_id", "other-caller"),
                         ("stage", "review"), ("operation", "dispatch.emit")]:
        foreign = {**json.loads(original), field: value}
        headless._save(receipt, foreign)
        with pytest.raises(ValueError, match="binding"):
            headless._read_host_gate_receipt(receipt, record, digest)
    headless._save(receipt, {**json.loads(original), "arm": "xs"})
    with pytest.raises(ValueError, match="content mismatch"):
        headless._read_host_gate_receipt(receipt, record, digest)
    with pytest.raises(ValueError, match="binding"):
        headless._host_gate({**params, "run_id": "other-run"}, record, artifacts, 1)
    with pytest.raises(ValueError, match="binding"):
        headless._host_gate({**params, "require_live_token": False}, record, artifacts, 1)

    receipt.write_text(original, encoding="utf-8")
    (artifacts / "response.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="response artifact content mismatch"):
        headless._read_host_gate_receipt(receipt, record, digest)


def test_native_nested_workflow_cannot_claim_host_gate(tmp_path):
    from coordinator_core.ops.workflow_fire.tests.test_headless_runtime import run_script

    child = tmp_path / "child.mjs"
    child.write_text("""export const meta = {};
if (engineOp.directRun || engineOp.hostGateOwned || engineOp.hostWorkflow) throw new Error('authority leak');
return await engineOp('gate.host_run', {run_id:'foreign', require_live_token:true});
""", encoding="utf-8")
    code, events, _ = run_script(tmp_path, "export const meta = {};\nreturn await workflow({scriptPath: "
                                 + json.dumps(str(child)) + "});")
    assert code == 1
    assert not any(event["type"] == "invoke" for event in events)
    assert "Nested workflows do not own host operations" in events[-1]["error"]


@pytest.mark.parametrize("change", ["none", "script", "manifest", "commit request", "foreign read"])
def test_commit_worker_requires_gate_issued_staged_bytes(tmp_path, monkeypatch, change):
    import hashlib
    from coordinator_core.ops.dispatch_emit import nested_run_contract, reply_sidecar
    from coordinator_core.ops.workflow_fire.tests.test_headless import record

    run_id = "ask-1"
    source = "export const meta={};\n// coordinator:ask-run-manifest v1 state/warp/ask-1/manifest.json\n" + nested_run_contract.STAGED_INLINE_SLOT + "\nawait agent('work',{}); return await agent('commit',{phase:'commit'});"
    if change == "foreign read":
        source = source.replace("await agent('work',{});", "await engineOp('workflow.staged_late_rows',{run_id:'other'});")
    base = tmp_path / "scratch/warp/ask-1.workflow.mjs"
    base.parent.mkdir(parents=True)
    base.write_bytes(source.encode())
    manifest = tmp_path / "state/warp/ask-1/manifest.json"
    marker = base.parent / "request.txt"
    marker.write_text("fixture request", encoding="utf-8")
    reply_sidecar.write_reply(tmp_path, manifest, {"rows": [], "run_dir": "state/warp/ask-1",
                              "review_declared_paths": [], "marker_path": "scratch/warp/request.txt"})
    stage = {}
    stage["manifest_handle"], stage["twin_path"] = reply_sidecar.handle_and_twin(
        tmp_path, manifest, source_receipt=stage)
    child = tmp_path / stage["twin_path"]
    assert stage["twin_sha256"] == hashlib.sha256(child.read_bytes()).hexdigest()
    if change == "script":
        child.write_bytes(child.read_bytes() + b"\n")
    path = record(tmp_path, "const gate=await engineOp('gate.host_run',{});"
                  "return await workflow({scriptPath:gate.stage_reply.twin_path});")
    row = json.loads(path.read_bytes())
    path.write_text(json.dumps({**row, "run_id": run_id}))
    monkeypatch.setattr(headless, "_host_gate", lambda *args: ({"stage_reply": stage}, "fixture"))
    calls = []
    def worker(prompt, options, **kwargs):
        calls.append(prompt)
        if prompt == "work" and change in ("manifest", "commit request"):
            target = manifest if change == "manifest" else marker
            target.write_bytes(target.read_bytes() + b"\n")
        return {}
    result = headless.execute(path, agent_runner=worker, terminal_runner=lambda *args: None)
    if change == "script":
        assert result["exit_code"] == 1 and "differs from host-issued" in result["error"]
        assert calls == []
    elif change == "foreign read":
        assert result["exit_code"] == 1 and "host-issued staged run" in result["error"]
        assert calls == []
    elif change in ("manifest", "commit request"):
        assert result["exit_code"] == 1 and "changed after fire" in result["error"]
        assert calls == ["work"]
    else:
        assert result["exit_code"] == 0, result.get("error")
        assert calls == ["work", "commit"]


@pytest.mark.parametrize("binding, accepted", [
    ({"depth": 0, "script_path": "/r/resume.mjs", "script_sha256": "a" * 64}, True),
    ({"depth": 0, "script_path": "/r/foreign.mjs", "script_sha256": "a" * 64}, False),
    ({"depth": 0, "script_path": "/r/resume.mjs", "script_sha256": "b" * 64}, False),
    ({"depth": 2, "script_path": "/r/resume.mjs", "script_sha256": "a" * 64}, False),
    ({"depth": 1, "script_path": "/r/twin.mjs", "script_sha256": "c" * 64}, True),
    ({"depth": 0, "script_path": "/r/twin.mjs", "script_sha256": "c" * 64}, True),
    (None, False),
])
def test_staged_commit_binding_accepts_issuing_root_only(binding, accepted):
    staged = {"depth": 1, "script_path": "/r/twin.mjs", "script_sha256": "c" * 64}
    root = {"script_path": "/r/resume.mjs", "script_sha256": "a" * 64}
    assert headless._is_staged_commit_binding(binding, staged, root) is accepted


@pytest.mark.parametrize("root", ["staged twin", "resume root"])
def test_resumed_depth0_commit_worker_binding(tmp_path, monkeypatch, root):
    import hashlib
    from coordinator_core.ops.dispatch_emit import nested_run_contract, reply_sidecar
    from coordinator_core.ops.workflow_fire.tests.test_headless import record

    run_id = "ask-1"
    body = ("const gate=await engineOp('gate.host_run',{});"
            "await agent('work',{}); return await agent('commit',{phase:'commit'});")
    source = ("export const meta={};\n// coordinator:ask-run-manifest v1 state/warp/ask-1/manifest.json\n"
              + nested_run_contract.STAGED_INLINE_SLOT + "\n" + body)
    base = tmp_path / "scratch/warp/ask-1.workflow.mjs"
    base.parent.mkdir(parents=True)
    base.write_bytes(source.encode())
    manifest = tmp_path / "state/warp/ask-1/manifest.json"
    (base.parent / "request.txt").write_text("fixture request", encoding="utf-8")
    reply_sidecar.write_reply(tmp_path, manifest, {"rows": [], "run_dir": "state/warp/ask-1",
                              "review_declared_paths": [], "marker_path": "scratch/warp/request.txt"})
    stage = {}
    stage["manifest_handle"], stage["twin_path"] = reply_sidecar.handle_and_twin(
        tmp_path, manifest, source_receipt=stage)
    twin = tmp_path / stage["twin_path"]
    path = record(tmp_path, body)
    row = json.loads(path.read_bytes())
    if root == "staged twin":
        row["script_path"] = str(twin)
        row["script_sha256"] = hashlib.sha256(twin.read_bytes()).hexdigest()
    else:
        resume = tmp_path / "resume-script.workflow.mjs"
        resume.write_bytes(Path(row["script_path"]).read_bytes())
        row["script_path"] = str(resume)
        assert row["script_path"] != str(twin)
        assert row["script_sha256"] != hashlib.sha256(twin.read_bytes()).hexdigest()
    path.write_text(json.dumps({**row, "run_id": run_id}))
    monkeypatch.setattr(headless, "_host_gate", lambda *args: ({"stage_reply": stage}, "fixture"))
    calls = []
    def worker(prompt, options, **kwargs):
        calls.append(prompt)
        return {}
    result = headless.execute(path, agent_runner=worker, terminal_runner=lambda *args: None)
    assert result["exit_code"] == 0, result.get("error")
    assert calls == ["work", "commit"]
