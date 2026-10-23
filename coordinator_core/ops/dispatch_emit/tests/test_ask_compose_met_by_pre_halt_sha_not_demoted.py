"""A met blocking row whose met_by names a sha stays met on a ledger resume; the commit leg verifies the sha.
Only a met_by naming no sha is demoted. The composed script runs under node with every agent leg stubbed."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_landed_ledger import (
    _RESUME,
    _labels,
    _run,
    _script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    readers_cfg,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_N1 = {
    "run_id": "ask-prior", "plan": _RESUME["plan"], "blocking": ["R1"], "halt_sha": "7ed10ac381" + "0" * 30,
    "unlanded": [], "revise_notes": ["R1 is ruled met by the landed work at 07ec9214b0 and 9d08bfbd0b"],
    "remaining": {"R1": {"remaining": ["update the sizing"], "evidence": "no sha here"}},
}


def _met(met_by):
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def test_the_n1_shape_stays_met_and_reaches_the_commit_leg(monkeypatch):
    out = _run(_script(monkeypatch), verify=_met(["07ec9214b0", "9d08bfbd0b"]), resume=_N1)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


@pytest.mark.parametrize("met_by", ["none", "", [], ["none"]])
def test_a_met_by_naming_no_sha_is_still_demoted(monkeypatch, met_by):
    out = _run(_script(monkeypatch), verify=_met(met_by), resume=_N1)
    item = out["result"]["halted_by"]["items"][0]
    assert item["status"] == "not_met"
    assert item["remaining"] == ["met_by: the commit sha that carries the credited work"]
    assert "commit" not in _labels(out)


@pytest.mark.parametrize("met_by", [["abcdef1234"], ["x.md", "abcdef1234"]])
def test_a_met_by_sha_matching_no_known_sha_is_demoted(monkeypatch, met_by):
    out = _run(_script(monkeypatch), verify=_met(met_by), resume=_N1)
    item = out["result"]["halted_by"]["items"][0]
    assert item["status"] == "not_met"
    assert "commit" not in _labels(out)


def test_the_composed_text_states_the_pre_halt_rule(monkeypatch):
    script = _script(monkeypatch)
    assert "met on work changed since the halt commit" in script
    assert "committed at or before it when met_by names that commit sha" in script
    assert "work committed at or before the halt meets a row when the verdict met_by names that commit" in script
    assert "met only on work changed since" not in script
    assert "re-ruling the row lets work committed before" not in script
