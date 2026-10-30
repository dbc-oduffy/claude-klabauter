"""dispatch.terminal_commit reads its params from a Workflow task-output file."""
import json

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc


@pytest.mark.parametrize("wrap", [False, True])
def test_params_come_out_of_bare_or_wrapped_digest(tmp_path, wrap):
    digest = {"next_action": {"params": {"script_path": "s.js", "incomplete_chunks": []}}}
    body = {"summary": "x", "logs": [], "result": digest} if wrap else digest
    f = tmp_path / "out.json"
    f.write_text(json.dumps(body), encoding="utf-8")
    assert tc._params_from_task_output(str(f)) == digest["next_action"]["params"]


SESSION = "4d47f5c4-9b93-49c0-bbbf-a67e580e5bc0"


def _task_output(tmp_path, params):
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"result": {"next_action": {"params": params}}}), encoding="utf-8")
    return str(f)


def _receipt(root, run_id, session):
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    emission_receipt_path(script).write_text(json.dumps({"session_id": session}), encoding="utf-8")


def test_task_output_params_gain_the_runs_session_id(tmp_path):
    from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output

    _receipt(tmp_path, "run-1", SESSION)
    out = _task_output(tmp_path, {"run_id": "run-1", "script_path": "s.js"})
    assert params_from_task_output(out, tmp_path)["session_id"] == SESSION
    assert "session_id" not in params_from_task_output(out)


def test_a_session_id_the_params_carry_is_kept_and_a_run_without_receipt_gets_none(tmp_path):
    from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output

    _receipt(tmp_path, "run-1", SESSION)
    kept = _task_output(tmp_path, {"run_id": "run-1", "session_id": "mine"})
    assert params_from_task_output(kept, tmp_path)["session_id"] == "mine"
    bare = _task_output(tmp_path, {"run_id": "run-2"})
    assert "session_id" not in params_from_task_output(bare, tmp_path)


def test_a_file_without_next_action_params_is_refused(tmp_path):
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"result": {"other": 1}}), encoding="utf-8")
    with pytest.raises(ValueError, match="next_action.params"):
        tc._params_from_task_output(str(f))


def test_handler_merges_file_params_and_surfaces_refusal(tmp_path):
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"result": {"next_action": {"params": {
        "script_path": "missing.js", "incomplete_chunks": []}}}}), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    reply = tc._terminal_commit({"task_output_path": str(f)}, tmp_path / ".git", {}, {})
    assert "missing.js" in reply["error"], "file params must reach the script read"
    bad = tc._terminal_commit({"task_output_path": str(tmp_path / "nope.json")}, tmp_path / ".git", {}, {})
    assert "cannot read params.task_output_path" in bad["error"]
