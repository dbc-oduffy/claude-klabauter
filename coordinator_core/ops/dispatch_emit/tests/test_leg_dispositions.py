"""leg_register.DISPOSITIONS: one non-empty disposition per ask, emit and review REGISTER key."""

from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.leg_register import Disposition
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _RUNTIME_FLOOR

_WORKFLOWS = ("ask", "emit", "review")
#: A FLOOR leg off the ratchet's floor must keep its current (non-haiku) model until its row lands.
_FLOOR_KEEPS_MODEL = {("emit", "seam:wave-")}


def _register_keys():
    return {(d.workflow, d.label) for d in lr.REGISTER if d.workflow in _WORKFLOWS}


def test_every_register_key_has_one_disposition_with_a_reason():
    for key in _register_keys():
        assert key in lr.DISPOSITIONS, key
        disposition, reason = lr.DISPOSITIONS[key]
        assert isinstance(disposition, Disposition) and reason.strip(), key


def test_no_disposition_key_is_outside_the_register():
    assert set(lr.DISPOSITIONS) <= _register_keys()


def test_every_floor_key_is_on_the_ratchet_floor_or_keeps_its_model():
    floor = {k for k, (d, _) in lr.DISPOSITIONS.items() if d is Disposition.FLOOR}
    assert floor - _RUNTIME_FLOOR <= _FLOOR_KEEPS_MODEL


def test_arm_split_labels_hold_one_disposition_each():
    assert lr.DISPOSITIONS[("ask", "size-retry")][0] is Disposition.JUDGMENT
    assert lr.DISPOSITIONS[("ask", "gate")][0] is Disposition.FLOOR
    disposition, reason = lr.DISPOSITIONS[("ask", "verify")]
    assert disposition is Disposition.FOLD and "skipped" in reason and "pre-rule" in reason


def test_fold_reasons_name_their_condition():
    for key, (d, reason) in lr.DISPOSITIONS.items():
        if d is Disposition.FOLD:
            assert "fold condition" in reason, key


def test_engine_and_hook_labels_start_without_emission_bans():
    for wf in _WORKFLOWS:
        assert lr.engine_labels(wf) == frozenset()
        assert lr.hook_labels(wf) == frozenset()
    assert dict(lr.HOOK_STEPS) == {}


def test_engine_labels_returns_only_engine_dispositions(monkeypatch):
    patched = {**lr.DISPOSITIONS, ("ask", "footprint"): (Disposition.ENGINE, "x")}
    monkeypatch.setattr(lr, "DISPOSITIONS", patched)
    assert lr.engine_labels("ask") == {"footprint"}
    assert lr.engine_labels("emit") == frozenset()


def test_hook_labels_reads_fallback_labels(monkeypatch):
    monkeypatch.setattr(lr, "HOOK_STEPS", {("ask", "size-assemble"): ("size", "gate", "r")})
    assert lr.hook_labels("ask") == {"gate"}
    assert lr.hook_labels("emit") == frozenset()
