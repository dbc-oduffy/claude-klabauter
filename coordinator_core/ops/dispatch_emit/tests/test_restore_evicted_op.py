"""Registration and resume ordering for the fenced restore operation."""
import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.restore_evicted_op import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose


def test_restore_refuses_invalid_run_id(tmp_path):
    with pytest.raises(CallerFacingValidationError):
        _handler({"run_id": "../peer"}, tmp_path)


def test_resume_restores_before_the_first_worktree_leg():
    script = _compose(restore_evicted_run=True)
    assert script.index("await engineOp('dispatch.restore_evicted'") < script.index("label: 'size'")
    assert "label: 'restore-evicted'" not in script
    assert "dispatch.restore_evicted" not in _compose()


@pytest.mark.parametrize("shape", [
    {"prompt": None, "sizing_rel": "state/sizings/x.yaml"},
    {"prompt": None, "sizing_rel": "state/sizings/x.yaml", "stop_after": STOP_AFTER_PLAN},
    {"stop_after": STOP_AFTER_PLAN},
])
def test_resume_shapes_restore_before_first_phase_and_halt_on_refusal(shape):
    script = _compose(restore_evicted_run=True, **shape)
    restore = script.index("await engineOp('dispatch.restore_evicted'")
    assert restore < script.index("phase('")
    assert "label: 'restore-evicted'" not in script
    halt = script.index(f"return {{ halted: '{HALT_REFUSAL}', kind: '{HALT_REFUSAL}', reason: 'restore-evicted: '")
    assert restore < script.index("_restore.refused") < halt < script.index("phase('")
