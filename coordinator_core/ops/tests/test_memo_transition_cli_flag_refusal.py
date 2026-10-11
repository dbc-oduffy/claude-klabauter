"""The no-disposition refusal reaches the CLI user in --flag spelling."""

from __future__ import annotations

from coordinator_core import archive_stamp
from coordinator_core.ops.memo_transition import _validate_action_disposition


def test_op_refusal_carries_flag_form_field():
    reply = _validate_action_disposition({}, "resolve")
    assert reply is not None
    assert "--decision" in reply["cli_error"]
    assert "--actioned-note" in reply["cli_error"]
    assert "--" not in reply["error"]


def test_cli_prints_flag_form(tmp_path, monkeypatch, capsys):
    refusal = {"exit_code": 1, "applied": False, "error": "plain", "cli_error": "use --decision"}
    assert archive_stamp._refusal_text(refusal) == "use --decision"
    assert archive_stamp._refusal_text({"error": "plain"}) == "plain"
