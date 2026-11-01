"""land_run.params_from_task_output unwraps a multi-ask launcher task-output to the per-run digest."""
import json

import pytest

from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output


def _run(run_id, status="ok"):
    if status != "ok":
        return {"run_id": run_id, "status": status, "error": "boom"}
    return {"run_id": run_id, "status": "ok",
            "result": {"next_action": {"kind": "terminal_commit", "params": {"run_id": run_id, "script_path": f"{run_id}.mjs"}}}}


def _out(tmp_path, runs):
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"summary": "x", "result": {"parent_id": "p", "runs": runs}}), encoding="utf-8")
    return str(f)


def test_single_run_envelope_yields_that_runs_params(tmp_path):
    assert params_from_task_output(_out(tmp_path, [_run("ask-a")]))["run_id"] == "ask-a"


def test_several_runs_select_by_run_id(tmp_path):
    out = _out(tmp_path, [_run("ask-a"), _run("ask-b")])
    assert params_from_task_output(out, None, "ask-b")["script_path"] == "ask-b.mjs"


def test_several_runs_without_run_id_name_the_ids_not_missing_params(tmp_path):
    out = _out(tmp_path, [_run("ask-a"), _run("ask-b")])
    with pytest.raises(ValueError) as exc:
        params_from_task_output(out)
    assert "ask-a" in str(exc.value) and "ask-b" in str(exc.value)
    assert "carries no next_action.params" not in str(exc.value)


def test_unknown_run_id_and_failed_run_are_refused(tmp_path):
    with pytest.raises(ValueError, match="no single run"):
        params_from_task_output(_out(tmp_path, [_run("ask-a")]), None, "ask-z")
    with pytest.raises(ValueError, match="status 'error'"):
        params_from_task_output(_out(tmp_path, [_run("ask-a", "error")]))


def test_bare_digest_still_unwraps(tmp_path):
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"result": _run("ask-a")["result"]}), encoding="utf-8")
    assert params_from_task_output(str(f))["run_id"] == "ask-a"
