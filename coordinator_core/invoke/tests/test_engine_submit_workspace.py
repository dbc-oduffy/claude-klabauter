import json

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv


@pytest.mark.parametrize("explicit", [False, True])
def test_submit_params_default_to_selected_workspace(tmp_path, monkeypatch, explicit):
    workspace = tmp_path / "selected"
    workspace.mkdir()
    params = {"ask": "Check the product", "session_id": "registered", "caller": "codex"}
    if explicit:
        params["cwd"] = str(tmp_path)
    params_dir = tmp_path / ".coordinator-local"
    params_dir.mkdir()
    (params_dir / "em-submit.json").write_text(json.dumps(params), encoding="utf-8")

    async def observe(msg, *, caller):
        return {"jsonrpc": "2.0", "id": 1, "result": msg["params"]}

    monkeypatch.setattr("coordinator_core.invoke.dispatch.dispatch_message", observe)
    out, err, code = _dispatch_argv(
        ["engine_ask.submit", "--params-file", ".coordinator-local/em-submit.json", "--repo", "selected"],
        str(tmp_path), allow_warm=False,
    )
    assert code == 0, err
    received = json.loads(out)
    assert received.get("result", received) == {**params, "cwd": str(tmp_path if explicit else workspace)}
