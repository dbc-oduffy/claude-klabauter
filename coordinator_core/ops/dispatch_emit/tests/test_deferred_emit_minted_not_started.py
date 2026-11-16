"""A deferred emit that minted a run but could not verify its script names the start op, never a bare refusal."""

from coordinator_core.ops.dispatch_emit.deferred_emit_script import _DEFERRED_JS


def test_minted_unverified_names_the_start_op():
    assert "status: 'minted_not_started'" in _DEFERRED_JS
    assert "'minted, not started: dispatch.minted_run start ' + _rid" in _DEFERRED_JS


def test_no_run_id_stays_a_plain_emit_refusal():
    assert ": { status: 'emit_refused', error: _verified.error }" in _DEFERRED_JS
    assert "_rid\n    ? { status: 'minted_not_started'" in _DEFERRED_JS
