"""An XS one-row single-wave ask spends no checkpoint commit leg; the emitted loop's labels pin the agent budget."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_wave_barrier import _loop, _script

# Measured on run ask-20261023T034045-c3265e before the last-wave checkpoint was dropped:
# size, work:X1, commit:wave-0, verify, commit = 5 agents, ~170k subagent tokens.
CHECKPOINT_GUARD = "if (w !== _waves[_waves.length - 1]) await _commitWave(w, _waveIds);"


def test_checkpoint_is_the_sole_commit_wave_call_and_skips_the_last_wave():
    loop = _loop(_script())
    assert loop.count("_commitWave(") == 1
    assert CHECKPOINT_GUARD in loop
