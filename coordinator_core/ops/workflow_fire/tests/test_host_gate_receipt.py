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
