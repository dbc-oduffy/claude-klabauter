"""A re-verify red listed in the record's foreign_red is foreign: no fix leg, no review_blocked halt."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL

from .test_ask_compose_halt_self_correction import _OWNED, _OWN, _drive, _done, _green, _labels, _red

_PEER = "coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py::test_burn_down_known_preexisting_amplification_sites"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_foreign_red_only_runs_no_fix_leg_and_no_halt():
    rec = _red(_PEER, foreign_red={_PEER: ["coordinator_core/benchmarks/submit_ask_hook_to_reply_legs.py"]})
    out = _drive(reverify=rec, replies={})
    assert _labels(out) == []
    assert out["halted"] is None
    assert out["foreign"] == [_PEER]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_owned_red_beside_a_foreign_red_still_gets_its_fix_leg():
    rec = _red(_OWNED, _PEER, foreign_red={_PEER: ["x.py"]})
    out = _drive(
        reverify=rec,
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )
    assert HALT_FIX_LABEL in _labels(out)
    assert out["foreign"] == [_PEER] or out["foreign"] == []
    fix = next(c for c in out["calls"] if c["label"] == HALT_FIX_LABEL)
    assert _OWNED in fix["prompt"] and _PEER not in fix["prompt"]
