import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.workflow_fire import headless


def test_host_gate_receipt_rejects_missing_and_foreign_binding(tmp_path, monkeypatch):
    artifacts = tmp_path / "gate"
    record = {"run_id": "own-run", "fire_id": "own-fire", "repo_root": str(tmp_path)}
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
            "jsonrpc": "2.0", "id": 1, "result": {"arm": "s", "halt": None},
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(headless.shutil, "which", lambda executable: "claude-fixture")
    monkeypatch.setattr(headless.subprocess, "run", run)
    receipt = artifacts / "consumed.json"
    with pytest.raises(ValueError, match="missing"):
        headless._read_host_gate_receipt(receipt, record, None)
    result, digest = headless._host_gate(params, record, artifacts, 1)
    evidence = headless._read_host_gate_receipt(receipt, record, digest)
    assert result["arm"] == evidence["arm"] == "s"
    assert evidence["stage"] == "gate"
    assert json.loads((artifacts / "response.json").read_text(encoding="utf-8"))["result"] == result
    assert evidence["artifacts"]["response"]["result"] == result
    assert evidence["artifacts"]["request"]["params"] == params
    assert evidence["artifacts"]["warm_route"]["route"] == "cold"
    assert evidence["loggedIn"] is True and evidence["process_exit_code"] == 0
    original = receipt.read_text(encoding="utf-8")
    for field, value in [("run_id", "other-run"), ("stage", "review")]:
        foreign = {**json.loads(original), field: value}
        headless._save(receipt, foreign)
        with pytest.raises(ValueError, match="binding"):
            headless._read_host_gate_receipt(receipt, record, digest)
    headless._save(receipt, {**json.loads(original), "arm": "xs"})
    with pytest.raises(ValueError, match="content mismatch"):
        headless._read_host_gate_receipt(receipt, record, digest)
    with pytest.raises(ValueError, match="binding"):
        headless._host_gate({**params, "run_id": "other-run"}, record, artifacts, 1)

    receipt.write_text(original, encoding="utf-8")
    (artifacts / "response.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="response artifact content mismatch"):
        headless._read_host_gate_receipt(receipt, record, digest)
