"""ask_spool.withdraw: closed params, pending entry retired with a recorded reason, other states reported."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ask_spool import record
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import ask_spool_withdraw as op
from coordinator_core.ops._registry_map import OP_MODULE_MAP


def _spool(root) -> str:
    sid, _ = record.spool_ask(str(root), b'{"a":1}', session_id="s", tool_use_id="tu", reason="engine_unreachable")
    return sid


def test_withdraw_pending_records_reason_through_the_handler(tmp_path):
    sid = _spool(tmp_path)
    out = op._handler({"spool_id": sid, "reason": "hand-done"}, tmp_path)
    assert out == {"spool_id": sid, "state": "withdrawn", "withdrawn": True}
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.WITHDRAWN_SUFFIX))
    assert header["withdraw_reason"] == "hand-done"
    assert record.list_pending(str(tmp_path)) == []


def test_repo_root_param_wins_over_the_dispatcher_root(tmp_path):
    sid = _spool(tmp_path)
    out = op._handler({"spool_id": sid, "reason": "r", "repo_root": str(tmp_path)}, None)
    assert out["withdrawn"] is True


def test_absent_and_claimed_entries_are_reported_not_retired(tmp_path):
    assert op._handler({"spool_id": "0" * 16, "reason": "r"}, tmp_path)["state"] == "absent"
    sid = _spool(tmp_path)
    record.claim(str(tmp_path), sid)
    out = op._handler({"spool_id": sid, "reason": "r"}, tmp_path)
    assert (out["state"], out["withdrawn"]) == ("claimed", False)


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 16, "0" * 15, "0" * 17, "../" + "0" * 13])
def test_a_malformed_spool_id_is_refused(tmp_path, bad):
    with pytest.raises(CallerFacingValidationError):
        op._handler({"spool_id": bad, "reason": "r"}, tmp_path)


def test_blank_reason_and_missing_root_are_refused(tmp_path):
    with pytest.raises(CallerFacingValidationError):
        op._handler({"spool_id": "0" * 16, "reason": "   "}, tmp_path)
    with pytest.raises(CallerFacingValidationError):
        op._handler({"spool_id": "0" * 16, "reason": "r"}, None)


def test_param_set_is_closed():
    assert op._SCHEMA["additionalProperties"] is False
    assert set(op._SCHEMA["properties"]) == {"spool_id", "reason", "repo_root"}
    assert set(op._SCHEMA["required"]) == {"spool_id", "reason"}


def test_op_is_registered_classified_and_scoped():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert OP_MODULE_MAP[op.OP_KEY] == "coordinator_core.ops.ask_spool_withdraw"
    assert OP_CLASSIFICATION[op.OP_KEY] is OpClass.MUTATING
    assert _OP_KEY_SCOPE[op.OP_KEY] == "show_top"
