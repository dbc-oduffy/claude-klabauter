import json

import pytest

from coordinator_core.ops.workflow_fire import headless


def test_host_gate_receipt_rejects_missing_and_foreign_binding(tmp_path, monkeypatch):
    artifacts = tmp_path / "gate"
    record = {"run_id": "own-run", "fire_id": "own-fire", "repo_root": str(tmp_path)}
    params = {"run_id": "own-run", "require_live_token": True, "sizing_path": "sizing.yaml"}

    def invoke(op, actual, repo, out, timeout):
        assert (op, actual, repo) == ("dispatch.ask_gate", params, str(tmp_path))
        return {"jsonrpc": "2.0", "id": 1, "result": {"arm": "s", "halt": None}}

    monkeypatch.setattr(headless, "_warm_reply", invoke)
    receipt = artifacts / "consumed.json"
    with pytest.raises(ValueError, match="missing"):
        headless._read_host_gate_receipt(receipt, record, None)
    result, digest = headless._host_gate(params, record, artifacts, 1)
    evidence = headless._read_host_gate_receipt(receipt, record, digest)
    assert result["arm"] == evidence["arm"] == "s"
    assert evidence["stage"] == "gate"
    assert json.loads((artifacts / "response.json").read_text(encoding="utf-8"))["result"] == result
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
