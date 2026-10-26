"""P-A replay: a verify leg marking a not_met row `essential: false` must not pass it through the composed
close gate. The row blocks, reaches the in-run remnant leg before the close, and a gap that stays open
halts at the ledger check (a partial close with the row visible) instead of committing."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ledger_recovery import REMNANT_LABEL, REMNANT_REVERIFY_LABEL
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_recovery_loop import _MET, _labels, _run, _script
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_remnant_completion import _DONE
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NON_ESSENTIAL = {
    "id": "R1", "status": "not_met", "essential": False, "remaining": ["something no chunk owns"], "evidence": "",
}


def test_an_essential_false_not_met_row_reaches_the_remnant_leg_before_the_close(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NON_ESSENTIAL],
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert labels.count(REMNANT_LABEL) == 1
    assert labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL) < labels.index("commit")
    prompt = next(c for c in out["calls"] if c["label"] == REMNANT_LABEL)["prompt"]
    assert "something no chunk owns" in prompt


def test_an_essential_false_row_still_open_after_the_remnant_leg_halts_with_the_row_visible(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NON_ESSENTIAL],
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_NON_ESSENTIAL]}},
    )
    halted = out["result"]["halted_by"]
    assert halted["halted"] == "ledger-check"
    assert [i["id"] for i in halted["items"]] == ["R1"]
    assert "commit" not in _labels(out)


def test_a_met_row_still_closes(monkeypatch):
    out = _run(_script(monkeypatch), items=[_MET])
    assert out["result"]["halted_by"] is None
    assert REMNANT_LABEL not in _labels(out)
