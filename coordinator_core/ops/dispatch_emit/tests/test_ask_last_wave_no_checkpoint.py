"""The ask execute loop checkpoints every wave but the last; the terminal commit lands the last wave."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_wave_barrier import _loop, _script

GUARD = "if (w !== _waves[_waves.length - 1]) await _commitWave(w, _waveIds);"


def test_checkpoint_call_is_guarded_by_not_last_wave():
    loop = _loop(_script())
    assert loop.count("_commitWave(") == 1
    assert GUARD in loop


def test_commit_leg_label_is_emitted_once_per_wave_template():
    script = _script()
    assert script.count("label: 'commit:wave-' + n") == 1
