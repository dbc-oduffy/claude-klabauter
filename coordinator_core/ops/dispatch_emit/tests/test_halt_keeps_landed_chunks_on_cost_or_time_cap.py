"""A usage-limit halt in a later wave leaves every earlier wave's checkpoint commit landed and commits nothing of the halted wave."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose

from .test_ask_checkpoint_waits_for_wave_tests import _run, _verdict

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WAVES = [{"n": 0, "ids": ["C0"]}, {"n": 1, "ids": ["C1"]}]
_TARGETS = {"0": ["t0.py"], "1": ["t0.py", "t1.py"]}


def test_halt_in_wave_one_keeps_wave_zero_commit_and_commits_nothing_more():
    out = _run(_WAVES, {"0": _verdict("pass"), "1": "limit"}, _TARGETS)

    assert out["halted"]["halted"] == ask_compose.HALT_USAGE_LIMIT
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0", "verify:wave-1"]
    assert out["shas"] == ["sha-commit:wave-0"]


def test_without_a_halt_both_waves_commit():
    out = _run(_WAVES, {"0": _verdict("pass"), "1": _verdict("pass")}, _TARGETS)

    assert out["halted"] is None
    assert out["shas"] == ["sha-commit:wave-0", "sha-commit:wave-1"]
