"""A ledger-check resume with no unlanded rows and a revise note judges the work at HEAD: a met verdict
stands only when met_by names a sha the row's own halt record names; otherwise the row stays not_met.
The whole composed script runs under node with every agent leg stubbed by label."""

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

_SHA = "9d5f6cb5f6"
_HALT = "a" * 40
_REVISED = {
    "run_id": "ask-prior", "plan": _RESUME["plan"], "blocking": ["R1"], "halt_sha": _HALT,
    "unlanded": [], "revise_notes": ["R1 is ruled met by the work in " + _SHA],
    "remaining": {"R1": {"remaining": ["update the sizing"], "evidence": "committed in " + _SHA}},
}


def _met(met_by):
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def test_a_met_verdict_naming_the_halt_recorded_commit_reaches_the_commit_leg(monkeypatch):
    out = _run(_script(monkeypatch), verify=_met(_SHA), resume=_REVISED)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


@pytest.mark.parametrize("met_by", ["none", ""])
def test_a_met_verdict_naming_no_sha_halts_not_met(monkeypatch, met_by):
    out = _run(_script(monkeypatch), verify=_met(met_by), resume=_REVISED)
    halted = out["result"]["halted_by"]
    assert halted["halted"] == "ledger-check"
    assert halted["items"][0]["status"] == "not_met"
    assert halted["items"][0]["remaining"] == ["met_by: the commit sha that carries the credited work"]
    assert "commit" not in _labels(out)


def test_a_met_verdict_naming_a_sha_only_the_revise_note_names_reaches_the_commit_leg(monkeypatch):
    noted = dict(_REVISED, remaining={"R1": {"remaining": ["update the sizing"], "evidence": "no sha here"}},
                 revise_notes=["R1 met by the work committed in 134841934b and cd96cb412a"])
    out = _run(_script(monkeypatch), verify=_met(["134841934b", "cd96cb412a"]), resume=noted)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


def test_a_met_verdict_naming_a_sha_no_note_or_record_names_is_left_for_the_commit_leg(monkeypatch):
    noted = dict(_REVISED, remaining={"R1": {"remaining": ["x"], "evidence": "none"}},
                 revise_notes=["R1 met by 134841934b"])
    out = _run(_script(monkeypatch), verify=_met("fedcba9876"), resume=noted)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


def test_the_judge_prompt_credits_work_before_the_halt_only_when_a_note_re_rules(monkeypatch):
    out = _run(_script(monkeypatch), verify=_met(_SHA), resume=_REVISED)
    verify = next(c for c in out["calls"] if c["label"] == "verify")["prompt"]
    assert "including commits before the halt commit" in verify
    assert "met_by must name the commit sha" in verify
    plain = dict(_REVISED, revise_notes=[])
    verify = next(c for c in _run(_script(monkeypatch), resume=plain)["calls"] if c["label"] == "verify")["prompt"]
    assert "met on work changed since the halt commit" in verify
    assert "committed at or before it when met_by names that commit sha" in verify
    assert "including commits before the halt commit" not in verify


@pytest.mark.parametrize(
    "note,met_by",
    [
        ("Re-rule R1 as met by commit 2fd9d96c64 (15 parity tests pass)", ["2fd9d96c64"]),
        ("R1 met by d46d2a1b88, 18/18 tests", ["d46d2a1b88"]),
        ("met by 2fd9d96c64e1a0b", "2fd9d96c64"),
        ("met by 2fd9d96c64", "2fd9d96c64e1a0b"),
        ("met by 12345678", "12345678"),
        ("met by 2fd9d96c64", None),
    ],
)
def test_the_composed_verdict_agrees_with_credited_rows(monkeypatch, note, met_by):
    from coordinator_core.ops.dispatch_emit.pre_halt_credit import credited_rows

    resume = dict(_REVISED, revise_notes=[note], remaining={"R1": {"remaining": ["x"], "evidence": "none"}})
    verdict = _met(met_by)
    credited = "R1" in credited_rows(resume, verdict)
    out = _run(_script(monkeypatch), verify=verdict, resume=resume)
    halted = out["result"]["halted_by"]
    assert (halted is None) is credited, halted
    assert ("commit" in _labels(out)) is credited


def test_the_pre_halt_credit_const_is_declared_before_every_read(monkeypatch):
    script = _script(monkeypatch)
    decl = script.index("const _preHaltCredit =")
    reads = [i for i in range(len(script)) if script.startswith("_preHaltCredit", i) and i != decl + len("const ")]
    assert reads, "composed script no longer reads _preHaltCredit"
    assert all(decl < i for i in reads), (decl, reads)


def test_the_halt_next_action_names_the_pre_halt_credit_path(monkeypatch):
    out = _run(_script(monkeypatch), verify=_met("none"), resume=_REVISED)
    assert "work committed at or before the halt meets a row when the verdict met_by names that commit" in out["result"]["halted_by"]["next_action"]["detail"]
