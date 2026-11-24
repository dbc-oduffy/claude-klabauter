"""run.hold: the holder sets and clears; a non-holder clear is answered with the held reply unless continued."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import run_hold_op as op
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.dispatch_emit import run_hold
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261123T175614-5d606f"


def _set(root: Path, session: str = "S", **extra) -> dict:
    params = {"run_id": RUN, "session_id": session, "pm_turn_ref": "PM turn 9",
              "release_condition": "tool gap closes", **extra}
    return op._handler(params, root)


def test_holder_sets_then_clears(tmp_path: Path) -> None:
    out = _set(tmp_path)
    assert out["state"] == "held"
    hold = run_hold.read_hold(record_dir(tmp_path, RUN))
    assert (hold.holder_session, hold.pm_turn_ref, hold.release_condition) == ("S", "PM turn 9", "tool gap closes")
    assert op._handler({"run_id": RUN, "session_id": "S", "clear": True}, tmp_path)["state"] == "cleared"
    assert run_hold.read_hold(record_dir(tmp_path, RUN)) is None


def test_non_holder_clear_returns_held_reply_and_keeps_the_hold(tmp_path: Path) -> None:
    _set(tmp_path)
    out = op._handler({"run_id": RUN, "session_id": "T", "clear": True}, tmp_path)
    assert out["state"] == "held_by_other"
    assert "S" in out["reply"] and "tool gap closes" in out["reply"] and "continue_held" in out["reply"]
    assert run_hold.read_hold(record_dir(tmp_path, RUN)) is not None


def test_non_holder_clear_with_continue_held_clears(tmp_path: Path) -> None:
    _set(tmp_path)
    out = op._handler({"run_id": RUN, "session_id": "T", "clear": True, "continue_held": RUN}, tmp_path)
    assert out["state"] == "cleared"
    assert run_hold.read_hold(record_dir(tmp_path, RUN)) is None


def test_non_holder_set_does_not_overwrite(tmp_path: Path) -> None:
    _set(tmp_path)
    assert _set(tmp_path, session="T")["state"] == "held_by_other"
    assert run_hold.read_hold(record_dir(tmp_path, RUN)).holder_session == "S"


def test_non_holder_set_with_continue_held_takes_over_the_hold(tmp_path: Path) -> None:
    _set(tmp_path)
    out = _set(tmp_path, session="T", continue_held=RUN, release_condition="new condition")
    assert out["state"] == "held" and out["holder_session"] == "T"
    hold = run_hold.read_hold(record_dir(tmp_path, RUN))
    assert (hold.holder_session, hold.release_condition) == ("T", "new condition")


def test_clear_with_no_hold_is_absent(tmp_path: Path) -> None:
    assert op._handler({"run_id": RUN, "session_id": "S", "clear": True}, tmp_path)["state"] == "absent"


@pytest.mark.parametrize("params", [
    {"run_id": RUN, "session_id": ""},
    {"run_id": RUN},
    {"run_id": RUN, "session_id": "S", "release_condition": "r"},
    {"run_id": RUN, "session_id": "S", "pm_turn_ref": "t"},
    {"run_id": "../x", "session_id": "S", "pm_turn_ref": "t", "release_condition": "r"},
])
def test_missing_session_or_fields_are_typed_errors(tmp_path: Path, params: dict) -> None:
    with pytest.raises(CallerFacingValidationError):
        op._handler(params, tmp_path)


def test_no_repo_root_is_refused() -> None:
    with pytest.raises(CallerFacingValidationError):
        op._handler({"run_id": RUN, "session_id": "S", "clear": True}, None)


def test_op_is_in_the_registry_map() -> None:
    assert OP_MODULE_MAP["run.hold"] == "coordinator_core.ops.run_hold_op"
