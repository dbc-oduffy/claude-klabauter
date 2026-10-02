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
